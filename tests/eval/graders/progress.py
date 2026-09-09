"""Offline adapter that grades the canonical progress assessment."""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.assessment.progress import assess_progress
from app.research.spec.compiler import compile_research_spec


def plan_from_case(case: dict[str, Any]) -> ExecutionPlan:
    raw = dict(case.get("plan") or {})
    steps = [
        PlanStep(
            step_type=str(item.get("step_type") or "research"),
            description=str(item.get("description") or item.get("objective") or ""),
            task_id=str(item.get("task_id") or ""),
            depends_on=list(item.get("depends_on") or []),
            allowed_tools=list(item.get("allowed_tools") or []),
            objective=str(item.get("objective") or ""),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in raw.get("steps") or []
    ]
    return ExecutionPlan(
        steps=steps,
        summary=str(raw.get("summary") or case.get("query") or ""),
        planning_mode=str(raw.get("planning_mode") or "dynamic"),
        research_brief=str(raw.get("research_brief") or case.get("query") or ""),
        plan_version=int(raw.get("plan_version") or 1),
    )


def _canonical_state(
    plan: ExecutionPlan,
    *,
    task_status: dict[str, str],
    worker_results: list[dict[str, Any]],
    aborted: bool,
) -> dict[str, Any]:
    results_by_task = {
        str(row.get("task_id") or ""): row
        for row in worker_results
        if isinstance(row, dict)
    }
    tasks: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    stale_sources: list[str] = []
    evidence_refs: list[str] = []

    for task_id, raw_status in task_status.items():
        status = str(raw_status or "pending")
        payload = results_by_task.get(task_id, {}).get("payload")
        payload = dict(payload) if isinstance(payload, dict) else {}
        try:
            confidence = float(payload.get("confidence", 1.0) or 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        semantic_gap = bool(
            payload.get("gaps")
            or payload.get("missing_dimensions")
            or payload.get("conflicts")
            or confidence < 0.5
        )
        tasks[task_id] = {
            "task_id": task_id,
            "execution_status": "succeeded" if status == "done" else status,
            "result_status": "partial" if semantic_gap else "complete" if status == "done" else "none",
            "attempt": 1 if status == "done" else 0,
            "evidence_refs": [],
        }
        conflicts.extend(str(item) for item in payload.get("conflicts") or [])
        stale_sources.extend(str(item) for item in payload.get("stale_sources") or [])
        evidence_refs.extend(str(item) for item in payload.get("sources") or [])

    return {
        "plan": plan.to_dict(),
        "tasks": tasks,
        "worker_results": worker_results,
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "evidence_assessment": {
            "unresolved_conflicts": list(dict.fromkeys(conflicts)),
            "stale_sources": list(dict.fromkeys(stale_sources)),
            "independent_source_count": len(set(evidence_refs)),
        },
        "abort_reason": "offline_aborted" if aborted else "",
    }


def assess_offline_progress(
    plan: ExecutionPlan,
    *,
    task_status: dict[str, str],
    worker_results: list[dict[str, Any]],
    aborted: bool = False,
) -> dict[str, Any]:
    return dict(
        assess_progress(
            _canonical_state(
                plan,
                task_status=task_status,
                worker_results=worker_results,
                aborted=aborted,
            )
        )
    )


def grade_progress_case(case: dict[str, Any]) -> dict[str, Any]:
    coverage = dict(case.get("coverage_state") or {})
    if coverage:
        assessment = dict(
            assess_progress(
                {
                    "research_spec": compile_research_spec(str(case.get("query") or "")).to_dict(),
                    "coverage_state": coverage,
                    "semantic_gaps": dict(case.get("semantic_gaps") or {}),
                }
            )
        )
    else:
        assessment = assess_offline_progress(
            plan_from_case(case),
            task_status=dict(case.get("task_status") or {}),
            worker_results=list(case.get("worker_results") or []),
            aborted=bool(case.get("aborted")),
        )
    expect = dict(case.get("expected") or {})
    issues: list[str] = []
    if expect.get("status") and assessment["status"] != expect["status"]:
        issues.append(f"status:{assessment['status']}!={expect['status']}")
    for field_name in expect.get("must_have") or []:
        if not assessment.get(field_name):
            issues.append(f"missing_signal:{field_name}")
    for field_name in expect.get("must_not_have") or []:
        if assessment.get(field_name):
            issues.append(f"unexpected_signal:{field_name}")
    return {
        "ok": not issues,
        "issues": issues,
        "status": assessment["status"],
        "assessment": assessment,
    }
