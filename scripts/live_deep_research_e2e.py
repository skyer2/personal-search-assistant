"""Run and audit the golden Deep Research query against the real providers.

The local .env is loaded by app.agent.llm. No credentials or prompt bodies are
written to the report; only the final answer and auditable run metrics remain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QUERY = "2026年9月 agent最新的热点是什么？\n你觉得agent未来1-2年的发展方向是什么？"


def _audit(result: Any, session_id: str, duration_sec: float) -> dict[str, Any]:
    from app.observability import get_recorder
    from app.observability.journal import build_span_tree, summarize_trace

    meta = dict(result.metadata or {})
    run_id = str(meta.get("run_id") or session_id)
    events = [
        event.to_dict()
        for event in get_recorder().journal.events_for_run(session_id, run_id)
    ]
    trace = summarize_trace(events)
    tree = build_span_tree(events)
    coverage_events = [
        event for event in events if event.get("type") == "coverage.assessed"
    ]
    coverage_sufficient = any(
        bool((event.get("attributes") or {}).get("sufficient"))
        for event in coverage_events
    )
    workers = trace.get("workers") or []
    accepted_findings = sum(
        int(worker.get("accepted_finding_count") or 0) for worker in workers
    )
    admitted_evidence = sum(
        int(worker.get("admitted_evidence_count") or 0) for worker in workers
    )
    evidence_events = sum(
        event.get("type") == "evidence.registered" for event in events
    )
    synthesis_events = [
        event for event in events if str(event.get("type") or "").startswith("synthesis.")
    ]
    denied_zero_budget = [
        event for event in events
        if event.get("type") == "budget.denied"
        and (event.get("attributes") or {}).get("limit") == 0
        and (event.get("attributes") or {}).get("used") == 0
    ]
    quality = dict(meta.get("quality") or {})
    citation_metrics = quality.get("citation_metrics") if isinstance(quality.get("citation_metrics"), dict) else {}
    accepted_findings = max(accepted_findings, int(citation_metrics.get("finding_count") or 0))
    admitted_evidence = max(admitted_evidence, int(citation_metrics.get("evidence_count") or 0))
    attempts = int(meta.get("synthesis_attempts") or 0)
    successful_attempt = int(meta.get("successful_attempt") or (1 if attempts == 1 else 0))
    degraded = bool(meta.get("synthesis_degraded"))
    answer_complete = bool(meta.get("answer_complete"))
    answerability = dict(meta.get("answerability") or {})
    root_count = sum(
        root.get("name") == "research.run" for root in tree.get("roots") or []
    )
    checks = {
        "findings": accepted_findings > 0,
        "evidence": admitted_evidence > 0 or evidence_events > 0,
        "coverage_sufficient": coverage_sufficient,
        "quality_pass": quality.get("verdict") == "pass",
        "trace_pass": (trace.get("trace_integrity") or {}).get("passed") is True,
        "one_root": root_count == 1,
        "no_orphan_or_cycle": tree.get("orphan_count") == 0 and tree.get("cycle_count") == 0,
        "no_artificial_zero_budget": not denied_zero_budget,
        "answer_present": bool(str(result.content or "").strip()),
        "answerability": bool(answerability.get("answerable")) if answerability else True,
        "answer_complete": answer_complete or not answerability,
        "retry_marked_degraded": attempts <= 1 or degraded,
    }
    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": result.status,
        "duration_sec": round(duration_sec, 2),
        "accepted_findings": accepted_findings,
        "admitted_evidence": admitted_evidence,
        "evidence_events": evidence_events,
        "coverage_sufficient": coverage_sufficient,
        "quality": quality,
        "trace_integrity": trace.get("trace_integrity") or {},
        "root_count": root_count,
        "orphan_count": tree.get("orphan_count"),
        "cycle_count": tree.get("cycle_count"),
        "synthesis_attempts": attempts,
        "successful_attempt": successful_attempt,
        "synthesis_degraded": degraded,
        "answerability": answerability,
        "answer_complete": answer_complete,
        "synthesis_retry_count": int(meta.get("synthesis_retry_count") or 0),
        "first_attempt_reason": str(meta.get("first_attempt_reason") or ""),
        "synthesis_events": [
            {
                "type": event.get("type"),
                "duration_ms": event.get("duration_ms"),
                "attributes": {
                    key: value for key, value in (event.get("attributes") or {}).items()
                    if key in {
                        "attempt", "evidence_pack_tokens", "pack_tokens_estimated",
                        "prompt_chars", "digest_chars", "duration_ms", "ttft_ms",
                        "actual_input_tokens", "actual_output_tokens", "finish_reason",
                        "error", "model", "provider",
                    }
                },
            }
            for event in synthesis_events
        ],
        "answer_chars": len(str(result.content or "")),
        "checks": checks,
        "passed": result.status == "success" and all(checks.values()),
    }


async def _run(count: int, mode: str, output: Path, max_replans: int | None = None) -> int:
    from app.agent.main_agent import harness

    output.mkdir(parents=True, exist_ok=True)
    if max_replans is not None:
        harness.harness_config.max_replan_count = max(0, int(max_replans))
    rows: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        session_id = f"golden_deep_{index}_{uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        result = await harness.run(QUERY, session_id, mode=mode)
        row = _audit(result, session_id, time.perf_counter() - started)
        rows.append(row)
        (output / f"answer_{index}.md").write_text(str(result.content or ""), encoding="utf-8")
        (output / "report.json").write_text(
            json.dumps({"query": QUERY, "mode": mode, "runs": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"run": index, **{key: row[key] for key in (
            "status", "duration_sec", "accepted_findings", "admitted_evidence",
            "coverage_sufficient", "synthesis_attempts", "synthesis_degraded", "passed"
        )}}, ensure_ascii=False), flush=True)
    primary = sum(row["passed"] and row["successful_attempt"] == 1 for row in rows)
    all_passed = all(row["passed"] for row in rows)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["acceptance"] = {
        "all_runs_passed": all_passed,
        "primary_synthesis_successes": primary,
        "required_primary_successes": 2 if count >= 3 else count,
        "passed": all_passed and primary >= (2 if count >= 3 else count),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["acceptance"], ensure_ascii=False), flush=True)
    return 0 if report["acceptance"]["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--mode", default="deep_debug", choices=("agent", "deep_debug"))
    parser.add_argument("--max-replans", type=int, default=None,
                        help="Optional test-only cap on supervisor repair waves")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "live_deep_research_e2e")
    args = parser.parse_args()
    return asyncio.run(_run(max(1, args.runs), args.mode, args.output, args.max_replans))


if __name__ == "__main__":
    raise SystemExit(main())
