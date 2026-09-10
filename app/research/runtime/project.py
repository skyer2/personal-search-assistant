"""Read-only projection from canonical ResearchState to the process LoopState."""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan, LoopState, TaskIntent


def sync_execution_projection(loop: LoopState, gstate: dict[str, Any]) -> LoopState:
    """Project graph-owned facts for legacy process-local services.

    This function never reconstructs task state and never writes workflow
    authority fields. LoopState remains an execution view, not a control plane.
    """
    brief = gstate.get("brief")
    intent = gstate.get("intent")
    if isinstance(brief, dict) and brief:
        deliverable = dict(brief.get("deliverable") or {})
        requested_format = str(deliverable.get("format") or "text")
        loop.intent = TaskIntent(
            raw_query=str(brief.get("raw_query") or brief.get("objective") or gstate.get("task_query") or ""),
            summary=str(brief.get("objective") or gstate.get("task_query") or ""),
            needs_network=True,
            deliverable="pdf" if requested_format == "pdf" else "md" if requested_format in {"markdown", "md"} else "text",
        )
    elif isinstance(intent, dict) and intent:
        loop.intent = TaskIntent.from_dict(intent)
    plan = gstate.get("plan")
    if isinstance(plan, dict) and plan:
        loop.plan = ExecutionPlan.from_dict(plan)

    budget = gstate.get("budget")
    if isinstance(budget, dict):
        metadata_budget = dict(loop.metadata.get("run_budget") or {})
        for key in ("max_parallel_workers", "max_replan_count"):
            if budget.get(key) is not None:
                metadata_budget[key] = max(0, int(budget[key]))
        loop.metadata["run_budget"] = metadata_budget

    supervisor = gstate.get("supervisor")
    if isinstance(supervisor, dict):
        loop.supervisor_iterations = max(0, int(supervisor.get("iteration") or 0))

    final = gstate.get("final_content")
    if isinstance(final, str):
        loop.final_content = final

    abort = gstate.get("abort_reason")
    if abort:
        loop.abort_reason = str(abort)

    metadata: dict[str, Any] = {
        "workflow_authority": "research_state",
        "partial_findings": [
            dict(item) for item in (gstate.get("findings") or []) if isinstance(item, dict)
        ][:24],
        "search_mode": str(gstate.get("search_mode") or ""),
        "termination": dict(gstate.get("termination") or {}),
    }
    for key in (
        "progress_assessment",
        "evidence_assessment",
        "execution_health",
        "delivery_readiness",
        "quality_assessment",
        "control_decision",
    ):
        value = gstate.get(key)
        if isinstance(value, dict):
            metadata[key] = dict(value)
    loop.metadata.update(metadata)
    return loop


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
