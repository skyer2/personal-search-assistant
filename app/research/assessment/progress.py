"""Research progress derived only from canonical plan and task facts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict

from app.agent.harness.state import ExecutionPlan
from app.research.assessment.evidence import EvidenceStatus, assess_evidence
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    normalize_tasks,
)
from app.research.runtime.scheduler import required_research_ids


class SemanticProgress(StrEnum):
    SUFFICIENT = "sufficient"
    GAP = "gap"
    UNKNOWN = "unknown"


class ProgressAssessment(TypedDict):
    status: str
    coverage_gaps: list[str]
    missing_dimensions: list[str]
    unresolved_conflicts: list[str]
    low_confidence_claims: list[str]
    stale_evidence: list[str]
    unmet_success_criteria: list[str]
    reason_codes: list[str]
    plan_version: int


def _plan_from_state(state: dict[str, Any]) -> ExecutionPlan | None:
    raw = state.get("plan")
    if not isinstance(raw, dict) or not raw:
        return None
    return ExecutionPlan.from_dict(raw)


def _coverage_keys(step: Any) -> list[str]:
    metadata = getattr(step, "metadata", None) or {}
    candidates = [
        *(metadata.get("coverage_keys") or []),
        *(metadata.get("covers_dimensions") or []),
        *(metadata.get("covers_dimension_ids") or []),
    ]
    values = [str(item) for item in candidates if str(item).strip()]
    if values:
        return values[:3]
    objective = str(getattr(step, "objective", "") or getattr(step, "description", "") or "")
    return [objective[:120]] if objective.strip() else []


def assess_progress(state: dict[str, Any]) -> ProgressAssessment:
    plan = _plan_from_state(state)
    required = required_research_ids(plan) if plan is not None else []
    tasks = normalize_tasks(state.get("tasks"))
    evidence = assess_evidence(state)
    plan_version = int(state.get("plan_version") or getattr(plan, "plan_version", 1) or 1)
    if str(state.get("abort_reason") or ""):
        return ProgressAssessment(
            status=SemanticProgress.UNKNOWN.value,
            coverage_gaps=[],
            missing_dimensions=[],
            unresolved_conflicts=list(evidence["unresolved_conflicts"]),
            low_confidence_claims=[],
            stale_evidence=list(evidence["stale_sources"]),
            unmet_success_criteria=[],
            reason_codes=["aborted"],
            plan_version=plan_version,
        )

    complete: list[str] = []
    partial: list[str] = []
    failed_without_result: list[str] = []
    incomplete_result: list[str] = []
    pending_or_running: list[str] = []
    step_by_task: dict[str, Any] = {}
    if plan is not None:
        for index, step in enumerate(plan.steps):
            step_by_task[str(step.resolved_task_id(index))] = step

    for task_id in required:
        task = tasks.get(task_id)
        if task is None:
            pending_or_running.append(task_id)
            continue
        execution_status = task["execution_status"]
        result_status = task["result_status"]
        if execution_status == TaskExecutionStatus.SUCCEEDED.value:
            if result_status == ResultStatus.COMPLETE.value:
                complete.append(task_id)
            else:
                incomplete_result.append(task_id)
        elif execution_status == TaskExecutionStatus.FAILED.value:
            if result_status == ResultStatus.PARTIAL.value:
                partial.append(task_id)
            else:
                failed_without_result.append(task_id)
        elif execution_status in {TaskExecutionStatus.PENDING.value, TaskExecutionStatus.RUNNING.value}:
            pending_or_running.append(task_id)

    gap_task_ids = list(
        dict.fromkeys(
            [
                *pending_or_running,
                *failed_without_result,
                *partial,
                *incomplete_result,
            ]
        )
    )
    coverage_gaps = [f"required_task:{task_id}" for task_id in gap_task_ids]
    missing_dimensions: list[str] = []
    for task_id in gap_task_ids:
        for dimension in _coverage_keys(step_by_task.get(task_id)):
            if dimension not in missing_dimensions:
                missing_dimensions.append(dimension)

    reasons: list[str] = []
    if not required:
        status = SemanticProgress.UNKNOWN.value
        reasons.append("no_required_research")
    else:
        all_terminal = not pending_or_running
        all_complete = all_terminal and len(complete) == len(required)
        partial_with_sufficient_evidence = (
            all_terminal
            and bool(partial)
            and evidence["status"] == EvidenceStatus.SUFFICIENT.value
        )
        if all_complete:
            status = SemanticProgress.SUFFICIENT.value
            reasons.append("required_research_complete")
        elif partial_with_sufficient_evidence:
            status = SemanticProgress.SUFFICIENT.value
            reasons.extend(["required_research_partial", "evidence_sufficient"])
        elif complete or partial:
            status = SemanticProgress.GAP.value
            reasons.extend(["required_research_incomplete"])
        else:
            status = SemanticProgress.GAP.value
            reasons.extend(["required_research_not_completed"])
        if pending_or_running:
            reasons.append("required_research_pending")
        if failed_without_result:
            reasons.append("required_research_failed")
        if partial:
            reasons.append("required_research_partial")
        if incomplete_result:
            reasons.append("required_research_result_incomplete")

    return ProgressAssessment(
        status=status,
        coverage_gaps=coverage_gaps,
        missing_dimensions=missing_dimensions,
        unresolved_conflicts=list(evidence["unresolved_conflicts"]),
        low_confidence_claims=[],
        stale_evidence=list(evidence["stale_sources"]),
        unmet_success_criteria=[],
        reason_codes=list(dict.fromkeys(reasons)),
        plan_version=plan_version,
    )


__all__ = ["ProgressAssessment", "SemanticProgress", "assess_progress"]
