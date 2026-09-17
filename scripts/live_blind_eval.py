"""Run a fresh 20-query x 3-run live evaluation against the real .env providers.

This suite is intentionally separate from the historical ten-query calibration
set.  It records one row per run and computes strict Completion Contract
metrics; partial answers are never counted as passes.
"""

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
    "截至2026年9月，AI Agent领域最值得关注的三个技术趋势是什么？请给出依据。",
    "比较LangGraph、CrewAI和Microsoft AutoGen的定位、适用场景与主要限制。",
    "MCP与传统function calling有什么区别？企业平台应如何组合？",
    "A2A协议解决什么问题？它与MCP在多Agent系统中如何分工？",
    "Claude Code、Cursor和Codex分别适合哪些开发任务？请比较工作流与成本。",
    "企业在Kubernetes上部署Agent Runtime时，最需要关注哪些可靠性问题？",
    "Agent长期记忆应如何设计？请比较向量检索、结构化事实和事件记忆。",
    "2025年至2026年Agent评测有哪些代表性基准？它们分别测量什么？",
    "如何为企业Agent系统设计工具权限、审计和提示注入防护？",
    "RAG、Agentic Search和传统搜索分别适合什么问题？请给出选型建议。",
    "比较OpenAI、Anthropic和Google的主流模型API在Agent开发中的取舍。",
    "2026年哪些开源Agent框架最适合学习？请按工程成熟度和社区说明。",
    "多模态Agent在文档理解和电脑操作方面的主要进展与瓶颈是什么？",
    "企业级Agent如何实现错误恢复、幂等执行和长任务断点续跑？",
    "比较浏览器操作Agent和API工具Agent的可靠性、安全性与适用场景。",
    "查找Cursor/Anysphere公开可确认的融资或估值信息，并说明不确定之处。",
    "估算2026年企业级AI Agent市场规模，比较不同机构预测口径及差异原因。",
    "如果后端工程师转Agent Infra，六个月内最值得投入的学习方向是什么？",
    "找出三项近期Agent可靠性研究，并分别说明对生产系统的启发。",
    "对于公开证据不足的AI创业公司未来排名问题，应该如何避免过度推断？",
]


def _audit(result: Any, session_id: str, duration_sec: float) -> dict[str, Any]:
    from app.observability import get_recorder
    from app.observability.journal import build_span_tree, summarize_trace

    metadata = dict(result.metadata or {})
    run_id = str(metadata.get("run_id") or session_id)
    events = [
        event.to_dict()
        for event in get_recorder().journal.events_for_run(session_id, run_id)
    ]
    trace = summarize_trace(events)
    tree = build_span_tree(events)
    quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
    completion = quality.get("completion_contract") or metadata.get("completion_contract") or {}
    if not isinstance(completion, dict):
        completion = {}
    integrity = trace.get("trace_integrity") or {}
    termination = metadata.get("termination") if isinstance(metadata.get("termination"), dict) else {}
    answerability = metadata.get("answerability") if isinstance(metadata.get("answerability"), dict) else {}
    synthesis_metrics = metadata.get("synthesis_attempt_metrics") if isinstance(metadata.get("synthesis_attempt_metrics"), list) else []
    stage_latency = metadata.get("latency") if isinstance(metadata.get("latency"), dict) else {}
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    workers = trace.get("workers") or []
    citation_metrics = quality.get("citation_metrics") if isinstance(quality.get("citation_metrics"), dict) else {}
    answer_complete = bool(metadata.get("answer_complete"))
    quality_pass = str(quality.get("verdict") or "") == "pass"
    content_present = bool(str(result.content or "").strip())
    passed = (
        str(result.status) == "success"
        and answer_complete
        and quality_pass
        and completion.get("passed") is True
        and completion.get("citation_valid") is True
        and integrity.get("passed") is True
        and tree.get("root_count") == 1
        and tree.get("orphan_count") == 0
        and tree.get("cycle_count") == 0
        and content_present
    )
    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": str(result.status),
        "duration_sec": round(duration_sec, 2),
        "stage_latency": stage_latency,
        "usage": usage,
        "worker_durations_ms": [
            int(row.get("duration_ms") or 0)
            for row in workers
            if row.get("duration_ms") is not None
        ],
        "answer_chars": len(str(result.content or "")),
        "findings": int(citation_metrics.get("finding_count") or len(metadata.get("findings") or [])),
        "evidence": int(citation_metrics.get("evidence_count") or 0),
        "quality_verdict": str(quality.get("verdict") or ""),
        "answer_complete": answer_complete,
        "completion_passed": completion.get("passed") is True,
        "completion_failure_reason": completion.get("failure_reason"),
        "completion_unresolved_blocking": completion.get("unresolved_blocking") or [],
        "citation_valid": completion.get("citation_valid") is True,
        "evidence_valid": completion.get("evidence_valid") is True,
        "answerability_reason": answerability.get("reason"),
        "answerability_questions": answerability.get("question_status") or [],
        "termination": termination,
        "quality_issues": quality.get("issues") or [],
        "synthesis_fail_reason": metadata.get("synthesis_fail_reason"),
        "fallback_used": metadata.get("fallback_used"),
        "synthesis_attempt_metrics": synthesis_metrics,
        "synthesis_degraded": bool(metadata.get("synthesis_degraded")),
        "synthesis_attempts": int(metadata.get("synthesis_attempts") or 0),
        "trace_integrity": integrity,
        "root_count": tree.get("root_count"),
        "orphan_count": tree.get("orphan_count"),
        "cycle_count": tree.get("cycle_count"),
        "passed": passed,
    }


def _metrics(rows: list[dict[str, Any]], repeat: int) -> dict[str, Any]:
    durations = [float(row.get("duration_sec") or 0) for row in rows]
    pass_rows = [row for row in rows if row.get("passed")]
    by_case: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_case.setdefault(int(row["case"]), []).append(row)
    all_pass = sum(
        len(case_rows) == repeat and all(item.get("passed") for item in case_rows)
        for case_rows in by_case.values()
    )
    case_count = len(by_case)
    return {
        "queries": case_count,
        "runs": len(rows),
        "repeat": repeat,
        "success": sum(row.get("status") == "success" for row in rows),
        "partial": sum(row.get("status") == "partial" for row in rows),
        "failed": sum(row.get("status") == "failed" for row in rows),
        "complete_success": len(pass_rows),
        "pass_at_1": round(len(pass_rows) / max(1, len(rows)), 4),
        "pass_hat_3": round(all_pass / max(1, case_count), 4),
        "citation_valid_rate": round(sum(bool(row.get("citation_valid")) for row in rows) / max(1, len(rows)), 4),
        "evidence_valid_rate": round(sum(bool(row.get("evidence_valid")) for row in rows) / max(1, len(rows)), 4),
        "trace_integrity_rate": round(sum(bool((row.get("trace_integrity") or {}).get("passed")) for row in rows) / max(1, len(rows)), 4),
        "p50_latency_sec": round(median(durations), 2) if durations else 0,
        "p95_latency_sec": round(sorted(durations)[max(0, math.ceil(len(durations) * 0.95) - 1)], 2) if durations else 0,
    }


async def run_eval(
    output: Path,
    *,
    mode: str,
    repeat: int,
    clean: bool,
    start_case: int,
    end_case: int,
    max_research_tasks: int,
    run_timeout_sec: float,
) -> int:
    from app.agent.main_agent import harness

    if clean and output.exists():
        shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    harness.harness_config.max_replan_count = 1
    harness.harness_config.planner_max_research_tasks = max(1, int(max_research_tasks))
    rows: list[dict[str, Any]] = []
    if not clean and (output / "report.json").exists():
        try:
            existing = json.loads((output / "report.json").read_text(encoding="utf-8"))
            rows = list(existing.get("runs") or [])
        except (OSError, ValueError):
            rows = []
    completed_keys = {
        (int(row.get("case") or 0), int(row.get("attempt") or 0))
        for row in rows
    }
    first_case = max(1, int(start_case))
    last_case = min(len(QUERIES), int(end_case))
    for case, query in enumerate(QUERIES, 1):
        if case < first_case or case > last_case:
            continue
        for attempt in range(1, repeat + 1):
            if (case, attempt) in completed_keys:
                continue
            session_id = f"blind20_{case:02d}_{attempt}_{uuid.uuid4().hex[:8]}"
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    harness.run(query, session_id, mode=mode),
                    timeout=max(1.0, float(run_timeout_sec)),
                )
                row = _audit(result, session_id, time.perf_counter() - started)
                row.update({"case": case, "attempt": attempt})
                (output / f"answer_{case:02d}_{attempt}.md").write_text(str(result.content or ""), encoding="utf-8")
            except Exception as exc:
                row = {
                    "case": case,
                    "attempt": attempt,
                    "session_id": session_id,
                    "status": "failed",
                    "duration_sec": round(time.perf_counter() - started, 2),
                    "error": f"{type(exc).__name__}: {exc}",
                    "failure_type": "live_run_timeout" if isinstance(exc, asyncio.TimeoutError) else "live_run_exception",
                    "answer_chars": 0,
                    "findings": 0,
                    "evidence": 0,
                    "quality_verdict": "",
                    "answer_complete": False,
                    "completion_passed": False,
                    "completion_failure_reason": "live_run_timeout",
                    "completion_unresolved_blocking": [],
                    "citation_valid": False,
                    "evidence_valid": False,
                    "answerability_reason": "",
                    "answerability_questions": [],
                    "termination": {},
                    "quality_issues": [],
                    "synthesis_fail_reason": "",
                    "fallback_used": "",
                    "synthesis_attempt_metrics": [],
                    "synthesis_degraded": False,
                    "synthesis_attempts": 0,
                    "trace_integrity": {"passed": False, "issues": ["run_did_not_complete"]},
                    "root_count": 0,
                    "orphan_count": 0,
                    "cycle_count": 0,
                    "passed": False,
                }
            rows.append(row)
            report = {"suite": "blind-20x3", "mode": mode, "queries": QUERIES, "case_range": [first_case, last_case], "runs": rows, "metrics": _metrics(rows, repeat)}
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": case, "attempt": attempt, "status": row.get("status"), "duration_sec": row.get("duration_sec"), "passed": row.get("passed")}, ensure_ascii=False), flush=True)
    report = {"suite": "blind-20x3", "mode": mode, "queries": QUERIES, "case_range": [first_case, last_case], "runs": rows, "metrics": _metrics(rows, repeat)}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False), flush=True)
    return 0 if report["metrics"]["complete_success"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("agent", "deep_debug"), default="agent")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "blind20x3")
    parser.add_argument("--start-case", type=int, default=1)
    parser.add_argument("--end-case", type=int, default=len(QUERIES))
    parser.add_argument("--max-research-tasks", type=int, default=3)
    parser.add_argument("--run-timeout-sec", type=float, default=900.0)
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    return asyncio.run(
        run_eval(
            args.output,
            mode=args.mode,
            repeat=max(1, args.repeat),
            clean=not args.no_clean,
            start_case=args.start_case,
            end_case=args.end_case,
            max_research_tasks=args.max_research_tasks,
            run_timeout_sec=args.run_timeout_sec,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
