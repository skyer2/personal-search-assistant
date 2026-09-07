"""Offline progress grader for the canonical sufficient/gap/unknown vocabulary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep


@dataclass
class OfflineProgress:
    verdict: str
    reason: str
    coverage_gaps: list[str] = field(default_factory=list)
    missing_dimensions: list[str] = field(default_factory=list)
    unresolved_conflicts: list[str] = field(default_factory=list)
    low_confidence_claims: list[str] = field(default_factory=list)
    stale_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def assess_offline_progress(
    plan: ExecutionPlan,
    *,
    task_status: dict[str, str],
    worker_results: list[dict[str, Any]],
    aborted: bool = False,
) -> OfflineProgress:
    research_ids = [
        step.task_id
        for step in plan.steps
        if step.step_type in {"research", "network_search", "file_read"}
    ]
    if not research_ids:
        return OfflineProgress("unknown", "no_research_task")
    if aborted:
        return OfflineProgress("unknown", "aborted")

    missing = [
        task_id
        for task_id in research_ids
        if task_status.get(task_id, "pending") in {"pending", "running"}
    ]
    assessment = OfflineProgress(
        "gap" if missing else "sufficient",
        "research_incomplete" if missing else "coverage_ok",
        missing_dimensions=[f"task:{task_id}" for task_id in missing],
    )
    for row in worker_results:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        task_id = str(row.get("task_id") or "")
        for gap in payload.get("gaps") or []:
            assessment.coverage_gaps.append(str(gap))
        for dimension in payload.get("missing_dimensions") or []:
            assessment.missing_dimensions.append(str(dimension))
        for conflict in payload.get("conflicts") or []:
            assessment.unresolved_conflicts.append(str(conflict))
        try:
            confidence = float(payload.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        if confidence < 0.5:
            assessment.low_confidence_claims.append(task_id)
    if (
        assessment.coverage_gaps
        or assessment.missing_dimensions
        or assessment.unresolved_conflicts
        or assessment.low_confidence_claims
    ):
        assessment.verdict = "gap"
        assessment.reason = "semantic_gap"
    return assessment


def grade_progress_case(case: dict[str, Any]) -> dict[str, Any]:
    assessment = assess_offline_progress(
        plan_from_case(case),
        task_status=dict(case.get("task_status") or {}),
        worker_results=list(case.get("worker_results") or []),
        aborted=bool(case.get("aborted")),
    )
    expect = dict(case.get("expected") or {})
    issues: list[str] = []
    if expect.get("verdict") and assessment.verdict != expect["verdict"]:
        issues.append(f"verdict:{assessment.verdict}!={expect['verdict']}")
    for field_name in expect.get("must_have") or []:
        if not getattr(assessment, field_name, None):
            issues.append(f"missing_signal:{field_name}")
    for field_name in expect.get("must_not_have") or []:
        if getattr(assessment, field_name, None):
            issues.append(f"unexpected_signal:{field_name}")
    return {
        "ok": not issues,
        "issues": issues,
        "verdict": assessment.verdict,
        "assessment": assessment.to_dict(),
    }
