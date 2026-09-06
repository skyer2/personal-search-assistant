"""Graph → legacy execution scratch projection.

ResearchState 是唯一 workflow truth。LoopState 只是 legacy execution scratch：
领域函数仍可读取它，但 plan/task runtime 状态不得写回或反向成为 resume 来源。
"""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan, LoopState, TaskIntent


def apply_graph_to_loop(loop: LoopState, gstate: dict[str, Any]) -> LoopState:
    """Legacy alias kept for old tests and the non-graph fallback."""
    return sync_legacy_execution_scratch(loop, gstate)


def sync_legacy_execution_scratch(loop: LoopState, gstate: dict[str, Any]) -> LoopState:
    """Project graph-owned inputs to read-only legacy execution scratch."""
    intent = gstate.get("intent")
    if isinstance(intent, dict) and intent:
        loop.intent = TaskIntent.from_dict(intent)
    plan = gstate.get("plan")
    if isinstance(plan, dict) and plan:
        loop.plan = ExecutionPlan.from_dict(plan)
    if "replan_count" in gstate:
        loop.replan_count = int(gstate.get("replan_count") or 0)
    if isinstance(loop.metadata, dict):
        if "replan_attempts" in gstate:
            loop.metadata["replan_attempts"] = int(gstate.get("replan_attempts") or 0)
        if "replan_applied_count" in gstate:
            loop.metadata["replan_applied_count"] = int(
                gstate.get("replan_applied_count") or 0
            )
        if "control_no_progress" in gstate:
            loop.metadata["control_no_progress"] = bool(
                gstate.get("control_no_progress")
            )
        if "replan_exhausted" in gstate:
            loop.metadata["replan_exhausted"] = bool(gstate.get("replan_exhausted"))
    budget = gstate.get("budget")
    if isinstance(budget, dict) and budget:
        run_budget = dict(loop.metadata.get("run_budget") or {})
        for key in ("max_parallel_workers", "max_replan_count"):
            if budget.get(key) is None:
                continue
            value = max(0, int(budget[key]))
            existing = run_budget.get(key)
            if existing is not None:
                value = min(value, int(existing))
            run_budget[key] = value
        loop.metadata["run_budget"] = run_budget
    final = gstate.get("final_content")
    if isinstance(final, str) and final:
        loop.final_content = final
    abort = gstate.get("abort_reason")
    if abort:
        loop.abort_reason = str(abort)
    brief = gstate.get("brief")
    if isinstance(brief, dict) and brief:
        loop.research_brief_obj = brief
        if loop.intent is not None and (loop.intent.brief is None or loop.intent.brief.is_empty()):
            from app.agent.harness.research_brief import ResearchBrief

            loop.intent.brief = ResearchBrief.from_dict(brief)
    loop.metadata["workflow_authority"] = "research_state"
    findings = gstate.get("findings")
    if isinstance(findings, list):
        loop.metadata["partial_findings"] = [
            dict(item) for item in findings if isinstance(item, dict)
        ][:24]
    if gstate.get("progress"):
        loop.metadata["graph_progress"] = gstate.get("progress")
    if gstate.get("search_mode"):
        loop.metadata["search_mode"] = gstate.get("search_mode")
    return loop


def brief_from_intent(intent: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(intent, dict):
        return {}
    brief = intent.get("brief")
    return dict(brief) if isinstance(brief, dict) else {}


def findings_from_worker_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw_payload = row.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    findings: list[dict[str, Any]] = []
    raw = payload.get("findings") if isinstance(payload, dict) else None
    if isinstance(raw, list):
        for item in raw[:12]:
            if isinstance(item, dict):
                findings.append(dict(item))
            elif item:
                findings.append(
                    {
                        "task_id": str(row.get("task_id") or ""),
                        "summary": str(item)[:400],
                    }
                )
    if not findings and row.get("ok") and (payload.get("summary") or row.get("summary")):
        findings.append(
            {
                "task_id": str(row.get("task_id") or ""),
                "summary": str(payload.get("summary") or row.get("summary") or "")[:400],
                "facts": list(payload.get("facts") or [])[:8],
                "sources": list(payload.get("sources") or [])[:8],
                "evidence_ids": list(payload.get("evidence_ids") or [])[:8],
            }
        )
    from app.research.runtime.findings import normalize_findings

    normalized, _rejected = normalize_findings(
        findings,
        task_id=str(row.get("task_id") or ""),
        subject_id=str(payload.get("subject_id") or row.get("subject_id") or "general"),
        dimension=str(payload.get("dimension") or row.get("dimension") or "general"),
    )
    return normalized
