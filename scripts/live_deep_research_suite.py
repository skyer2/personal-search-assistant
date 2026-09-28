"""Run the ten requested live queries against the real .env providers."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = [
    {"eval_id": "E01", "query": "你认为有哪些AI初创有意思的公司？"},
    {"eval_id": "E02", "query": "推荐3家做AI开发工具的独立初创，说明适合什么人。"},
    {"eval_id": "E03", "query": "推荐3家值得关注的中国AI应用初创，别只按融资额选。"},
    {"eval_id": "E04", "query": "比较 Cursor 和 Claude Code 的团队适用场景与局限。", "frozen_entities": ["Cursor", "Claude Code"]},
    {"eval_id": "E05", "query": "DeepSeek-R1 何时首次发布？请给出日期口径和直接来源。", "frozen_entity": "DeepSeek-R1"},
    {"eval_id": "E06", "query": "截至运行当天，LangChain 最近稳定版是什么？请使用官方发布记录。", "frozen_entity": "LangChain"},
    {"eval_id": "E07", "query": "为什么AI写作产品的试用率可能很高，但付费留存可能较低？请区分一般机制与已有实证，不要编造数据。"},
    {"eval_id": "E08", "query": "你认为未来1—2年AI测试工具会怎样发展？"},
    {"eval_id": "E09", "query": "两篇报道给出 Anysphere 不同估值，请核对指标、日期和融资轮次并解释差异；无法解释的部分请披露。", "frozen_entity": "Anysphere"},
    {"eval_id": "E10", "query": "推荐3个适合个人开发者的AI工具，并说明哪些信息未核实。"},
]


def audit(result: Any, session_id: str, duration: float) -> dict[str, Any]:
    from app.observability import get_recorder
    from app.observability.journal import build_span_tree, summarize_trace

    meta = dict(result.metadata or {})
    run_id = str(meta.get("run_id") or session_id)
    events = [event.to_dict() for event in get_recorder().journal.events_for_run(session_id, run_id)]
    trace = summarize_trace(events)
    tree = build_span_tree(events)
    quality = dict(meta.get("quality") or {})
    completion = dict(quality.get("completion_contract") or meta.get("completion_contract") or {})
    integrity = trace.get("trace_integrity") or {}
    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": str(result.status),
        "mode": str(meta.get("search_mode") or ""),
        "duration_sec": round(duration, 2),
        "answer_chars": len(str(result.content or "")),
        "findings": int((quality.get("citation_metrics") or {}).get("finding_count") or len(meta.get("findings") or [])),
        "evidence": int((quality.get("citation_metrics") or {}).get("evidence_count") or 0),
        "quality_verdict": str(quality.get("verdict") or ""),
        "answer_complete": bool(meta.get("answer_complete")),
        "synthesis_degraded": bool(meta.get("synthesis_degraded")),
        "synthesis_attempts": int(meta.get("synthesis_attempts") or 0),
        "worker_diagnostics": list(meta.get("worker_diagnostics") or []),
        "completion": completion,
        "trace_integrity": integrity,
        "root_count": sum(root.get("name") == "research.run" for root in tree.get("roots") or []),
        "orphan_count": tree.get("orphan_count"),
        "cycle_count": tree.get("cycle_count"),
        "passed": (
            str(result.status) == "success"
            and bool(meta.get("answer_complete"))
            and str(quality.get("verdict") or "") == "pass"
            and integrity.get("passed") is True
            and tree.get("orphan_count") == 0
            and tree.get("cycle_count") == 0
            and bool(str(result.content or "").strip())
        ),
    }


async def run_suite(output: Path, mode: str, *, start: int = 1, end: int | None = None, repetitions: int = 3, clean: bool = True) -> int:
    from app.agent.main_agent import harness

    if clean and output.exists():
        shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    harness.harness_config.max_replan_count = 1
    rows: list[dict[str, Any]] = []
    existing = output / "report.json"
    if not clean and existing.exists():
        try:
            rows = list((json.loads(existing.read_text(encoding="utf-8")) or {}).get("runs") or [])
        except (OSError, ValueError):
            rows = []
    completed_ids = {str(row.get("session_id")) for row in rows}
    scheduled = [(case, repetition) for case in CASES for repetition in range(1, repetitions + 1)]
    for index, (case, repetition) in enumerate(scheduled, 1):
        if index < max(1, start):
            continue
        if end is not None and index > end:
            break
        query = str(case["query"])
        eval_id = str(case["eval_id"])
        session_id = f"refactor_v7_{eval_id.lower()}_r{repetition}_{uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        try:
            result = await harness.run(query, session_id, mode=mode)
            row = audit(result, session_id, time.perf_counter() - started)
            row.update({"eval_id": eval_id, "repetition": repetition})
            (output / f"answer_{eval_id}_r{repetition}.md").write_text(str(result.content or ""), encoding="utf-8")
        except Exception as exc:
            row = {"session_id": session_id, "status": "failed", "duration_sec": round(time.perf_counter() - started, 2), "error": f"{type(exc).__name__}: {exc}", "passed": False}
        row.update({"eval_id": eval_id, "repetition": repetition})
        rows.append(row)
        report = {"suite": "answer-contract-v2-10x3", "mode": mode, "cases": CASES, "runs": rows}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": index, "status": row.get("status"), "duration_sec": row.get("duration_sec"), "passed": row.get("passed")}, ensure_ascii=False), flush=True)
    durations = [float(row.get("duration_sec") or 0) for row in rows]
    statuses = [str(row.get("status") or "failed") for row in rows]
    report = {
        "suite": "answer-contract-v2-10x3", "mode": mode, "cases": CASES, "runs": rows,
        "metrics": {
            "total": len(rows), "success": statuses.count("success"), "partial": statuses.count("partial"), "failed": statuses.count("failed"),
            "pass_at_1": round(sum(bool(row.get("passed")) for row in rows) / max(1, len(rows)), 4),
            "p50_latency_sec": median(durations) if durations else 0,
            "p95_latency_sec": sorted(durations)[max(0, math.ceil(len(durations) * .95) - 1)] if durations else 0,
        },
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False), flush=True)
    return 0 if report["metrics"]["success"] >= 27 and report["metrics"]["p95_latency_sec"] <= 240 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("agent", "deep_debug"), default="deep_debug")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "live_deep_research_suite")
    parser.add_argument("--start", type=int, default=1, help="1-based case to start or resume")
    parser.add_argument("--end", type=int, default=None, help="inclusive scheduled run to stop after")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--no-clean", action="store_true", help="preserve prior report and append")
    args = parser.parse_args()
    return asyncio.run(run_suite(args.output, args.mode, start=args.start, end=args.end, repetitions=args.repetitions, clean=not args.no_clean))


if __name__ == "__main__":
    raise SystemExit(main())
