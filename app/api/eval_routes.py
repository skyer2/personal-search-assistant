"""
Eval API：供前端 Eval 面板读取基线与最新报告，并触发 dry-run。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "tests" / "eval" / "results"
BASELINE_PATH = EVAL_DIR / "baseline.json"

router = APIRouter(prefix="/api/eval", tags=["eval"])


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_report() -> Path | None:
    reports = sorted(EVAL_DIR.glob("eval_report_*.json"), reverse=True)
    return reports[0] if reports else None


@router.get("/metrics-link")
def eval_metrics_link() -> dict[str, Any]:
    """Eval 面板：在线 metrics 入口。"""
    return {
        "summary_url": "/api/metrics/summary",
        "prometheus_url": "/api/metrics/prometheus",
    }


@router.get("/baseline")
def get_baseline() -> dict[str, Any]:
    return _read_json(BASELINE_PATH)


@router.get("/latest")
def get_latest_report() -> dict[str, Any]:
    latest = _latest_report()
    if latest is None:
        comparison = EVAL_DIR / "eval_comparison_latest.md"
        if BASELINE_PATH.exists():
            payload = _read_json(BASELINE_PATH)
            payload["source"] = "baseline_only"
            payload["note"] = "No eval_report_*.json yet; showing baseline."
            return payload
        raise HTTPException(status_code=404, detail="No eval report found")
    data = _read_json(latest)
    data["report_file"] = latest.name
    return data


@router.get("/reports")
def list_reports() -> dict[str, Any]:
    reports = [
        {
            "name": path.name,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            "size": path.stat().st_size,
        }
        for path in sorted(EVAL_DIR.glob("eval_report_*.json"), reverse=True)
    ]
    return {"reports": reports, "total": len(reports)}


@router.post("/run")
async def run_eval(
    dry_run: bool = Query(default=True),
    report_md: bool = Query(default=True),
) -> dict[str, Any]:
    from tests.eval.run_eval import run_dry_eval, run_live_eval, save_report, write_comparison_markdown
    from tests.eval.metrics import build_report, compare_with_baseline
    from tests.eval.runners.component import load_jsonl

    if dry_run:
        results = run_dry_eval()
    else:
        tasks = load_jsonl(ROOT / "tests" / "eval" / "datasets" / "harness_scenarios_v1.jsonl")
        results = await run_live_eval(tasks)

    report = build_report(results)
    report_dict = report.to_dict()
    report_dict["mode"] = "dry-run" if dry_run else "live"
    report_dict["generated_at"] = datetime.now().isoformat()

    baseline = None
    if BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        comparison = compare_with_baseline(report_dict, baseline)
        report_dict["baseline_comparison"] = comparison

    out = save_report(report_dict, EVAL_DIR)
    md_path = None
    if report_md and baseline:
        md_path = write_comparison_markdown(
            report_dict,
            report_dict.get("baseline_comparison"),
            EVAL_DIR,
        )

    return {
        "status": "ok",
        "report_file": out.name,
        "comparison_md": md_path.name if md_path else None,
        "summary": {
            "total": report_dict.get("total"),
            "passed": report_dict.get("passed"),
            "task_success_rate": report_dict.get("task_success_rate"),
        },
    }
