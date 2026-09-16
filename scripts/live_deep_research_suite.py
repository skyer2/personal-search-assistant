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

QUERIES = [
    "截至 2026 年 9 月，AI Agent 领域最值得关注的 5 个技术热点是什么？请区分短期热点和结构性趋势，并说明判断依据。",
    "2026 年有哪些 AI 初创公司最值得工程师加入？请选 5 家，比较融资、商业化、技术壁垒、团队和主要风险，并说明为什么。",
    "MCP、A2A 和传统 function calling 分别适合什么场景？如果我要做企业级多 Agent 平台，应该如何组合使用？请给出架构建议和依据。",
    "现在主流 AI Coding Agent 中，Claude Code、Cursor、Codex 和 Devin 各自适合什么类型的开发任务？请比较能力边界、工作流、成本和适用团队，不要只列功能。",
    "查清楚 2026 年 Cursor/Anysphere 当前最新的融资、估值、ARR 和公司状态。对于不同媒体中互相冲突的数据，请明确指出哪些能确认、哪些不能确认，以及你最终采用哪个口径。",
    "2025–2026 年 Long-Horizon Agent Reliability 方向有哪些重要研究进展？重点关注长期任务、错误恢复、memory、context management 和 agent evaluation，并总结未来最值得研究的 3 个问题。",
    "估算 2026 年全球企业级 AI Agent 市场规模，并比较 Gartner、IDC、MarketsandMarkets 等机构的口径。不要简单平均数字，要解释为什么预测差异这么大。",
    "2026 年哪一家 AI Agent 创业公司最可能在未来 3 年成为下一家 OpenAI？请基于公开证据分析，如果证据不足，不要强行给出确定结论。",
    "找出 2026 年收入增长最快的 3 家 AI 应用公司，并进一步分析它们增长主要来自个人用户、企业客户还是 API/开发者业务。要求每个结论都能追溯到具体来源。",
    "如果一个有 8 年后端/Kubernetes 经验的工程师想在 2026 年转 Agent Infra 岗位，应该重点准备哪些技术方向？请结合当前招聘需求、开源项目、Agent Runtime/Memory/MCP/Evaluation 的发展趋势，给出 6 个月学习路线，并说明哪些方向最值得投入。",
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


async def run_suite(output: Path, mode: str, *, start: int = 1, clean: bool = True) -> int:
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
    for index, query in enumerate(QUERIES, 1):
        if index < max(1, start):
            continue
        session_id = f"blind10_{index:02d}_{uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        try:
            result = await harness.run(query, session_id, mode=mode)
            row = audit(result, session_id, time.perf_counter() - started)
            (output / f"answer_{index:02d}.md").write_text(str(result.content or ""), encoding="utf-8")
        except Exception as exc:
            row = {"session_id": session_id, "status": "failed", "duration_sec": round(time.perf_counter() - started, 2), "error": f"{type(exc).__name__}: {exc}", "passed": False}
        rows.append(row)
        report = {"suite": "requested-10", "mode": mode, "queries": QUERIES, "runs": rows}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": index, "status": row.get("status"), "duration_sec": row.get("duration_sec"), "passed": row.get("passed")}, ensure_ascii=False), flush=True)
    durations = [float(row.get("duration_sec") or 0) for row in rows]
    statuses = [str(row.get("status") or "failed") for row in rows]
    report = {
        "suite": "requested-10", "mode": mode, "queries": QUERIES, "runs": rows,
        "metrics": {
            "total": len(rows), "success": statuses.count("success"), "partial": statuses.count("partial"), "failed": statuses.count("failed"),
            "pass_at_1": round(sum(bool(row.get("passed")) for row in rows) / max(1, len(rows)), 4),
            "p50_latency_sec": median(durations) if durations else 0,
            "p95_latency_sec": sorted(durations)[max(0, math.ceil(len(durations) * .95) - 1)] if durations else 0,
        },
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False), flush=True)
    return 0 if report["metrics"]["success"] > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("agent", "deep_debug"), default="deep_debug")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "live_deep_research_suite")
    parser.add_argument("--start", type=int, default=1, help="1-based case to start or resume")
    parser.add_argument("--no-clean", action="store_true", help="preserve prior report and append")
    args = parser.parse_args()
    return asyncio.run(run_suite(args.output, args.mode, start=args.start, clean=not args.no_clean))


if __name__ == "__main__":
    raise SystemExit(main())
