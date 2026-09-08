"""Research progress derived from active plan obligations and business gaps."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict

from app.agent.harness.state import ExecutionPlan
from app.research.assessment.evidence import EvidenceStatus, assess_evidence
from app.research.domain.gaps import active_research_steps, step_gap_ids
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    normalize_tasks,
)


class SemanticProgress(StrEnum):
    SUFFICIENT = "sufficient"
    GAP = "gap"
    UNKNOWN = "unknown"


class ProgressAssessment(TypedDict):
    status: str
    gap_ids: list[str]
    coverage_gaps: list[str]
    missing_dimensions: list[str]
    resolved_gap_ids: list[str]
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


def _gap_label(step: Any) -> str:
    metadata = getattr(step, "metadata", None) or {}
    dimensions = [str(item) for item in metadata.get("coverage_keys") or [] if str(item).strip()]
    if dimensions:
        return dimensions[0]
    return str(getattr(step, "objective", "") or getattr(step, "description", "") or "")[:120]


def _active_gap_rows(
    plan: ExecutionPlan,
    tasks: dict[str, Any],
) -> tuple[dict[str, list[str]], dict[str, str], list[str]]:
    owners: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    required: list[str] = []
    for index, step in active_research_steps(plan):
        task_id = step.resolved_task_id(index)
        metadata = dict(step.metadata or {})
        if metadata.get("optional"):
            continue
        required.append(task_id)
        for gap_id in step_gap_ids(step):
            owners.setdefault(gap_id, []).append(task_id)
            labels.setdefault(gap_id, _gap_label(step))
    return owners, labels, required


def assess_progress(state: dict[str, Any]) -> ProgressAssessment:
    plan = _plan_from_state(state)
    tasks = normalize_tasks(state.get("tasks"))
    evidence = assess_evidence(state)
    plan_version = int(state.get("plan_version") or getattr(plan, "plan_version", 1) or 1)
    if str(state.get("abort_reason") or ""):
        return ProgressAssessment(
            status=SemanticProgress.UNKNOWN.value,
            gap_ids=[],
            coverage_gaps=[],
            missing_dimensions=[],
            resolved_gap_ids=[],
            unresolved_conflicts=list(evidence["unresolved_conflicts"]),
            low_confidence_claims=[],
            stale_evidence=list(evidence["stale_sources"]),
            unmet_success_criteria=[],
            reason_codes=["aborted"],
            plan_version=plan_version,
        )

    if plan is None:
        return ProgressAssessment(
            status=SemanticProgress.UNKNOWN.value,
            gap_ids=[],
            coverage_gaps=[],
            missing_dimensions=[],
            resolved_gap_ids=[],
            unresolved_conflicts=list(evidence["unresolved_conflicts"]),
            low_confidence_claims=[],
            stale_evidence=list(evidence["stale_sources"]),
            unmet_success_criteria=[],
            reason_codes=["no_active_plan"],
            plan_version=plan_version,
        )

    owners, labels, required = _active_gap_rows(plan, tasks)
    complete_tasks = 0
    partial_tasks = 0
    failed_without_result = 0
    pending_or_running = 0
    incomplete_result = 0
    for task_id in required:
        task = tasks.get(task_id)
        if task is None:
            pending_or_running += 1
            continue
        if task["execution_status"] == TaskExecutionStatus.SUCCEEDED.value:
            if task["result_status"] == ResultStatus.COMPLETE.value:
                complete_tasks += 1
            else:
                incomplete_result += 1
        elif task["execution_status"] == TaskExecutionStatus.FAILED.value:
            if task["result_status"] == ResultStatus.PARTIAL.value:
                partial_tasks += 1
            else:
                failed_without_result += 1
        elif task["execution_status"] in {TaskExecutionStatus.PENDING.value, TaskExecutionStatus.RUNNING.value}:
            pending_or_running += 1

    unresolved_gap_ids: list[str] = []
    resolved_gap_ids: list[str] = []
    for gap_id, task_ids in owners.items():
        owner_states = [tasks.get(task_id) for task_id in task_ids]
        resolved = bool(owner_states) and all(
            task_state is not None
            and task_state["execution_status"] == TaskExecutionStatus.SUCCEEDED.value
            and task_state["result_status"] == ResultStatus.COMPLETE.value
            for task_state in owner_states
        )
        if resolved:
            resolved_gap_ids.append(gap_id)
        else:
            unresolved_gap_ids.append(gap_id)

    all_terminal = pending_or_running == 0
    all_complete = all_terminal and complete_tasks == len(required)
    partial_with_sufficient_evidence = (
        all_terminal
        and partial_tasks > 0
        and evidence["status"] == EvidenceStatus.SUFFICIENT.value
    )
    reasons: list[str] = []
    if not required:
        status = SemanticProgress.UNKNOWN.value
        reasons.append("no_required_research")
    elif all_complete:
        status = SemanticProgress.SUFFICIENT.value
        reasons.append("required_research_complete")
    elif partial_with_sufficient_evidence:
        status = SemanticProgress.SUFFICIENT.value
        reasons.extend(["required_research_partial", "evidence_sufficient"])
        resolved_gap_ids = list(dict.fromkeys([*resolved_gap_ids, *unresolved_gap_ids]))
        unresolved_gap_ids = []
    else:
        status = SemanticProgress.GAP.value
        reasons.extend(
            [
                "required_research_incomplete"
                if complete_tasks or partial_tasks
                else "required_research_not_completed"
            ]
        )
    if pending_or_running:
        reasons.append("required_research_pending")
    if failed_without_result:
        reasons.append("required_research_failed")
    if partial_tasks:
        reasons.append("required_research_partial")
    if incomplete_result:
        reasons.append("required_research_result_incomplete")

    coverage_gaps = [labels.get(gap_id, gap_id) for gap_id in unresolved_gap_ids]
    return ProgressAssessment(
        status=status,
        gap_ids=unresolved_gap_ids,
        coverage_gaps=coverage_gaps,
        missing_dimensions=list(coverage_gaps),
        resolved_gap_ids=list(dict.fromkeys(resolved_gap_ids)),
        unresolved_conflicts=list(evidence["unresolved_conflicts"]),
        low_confidence_claims=[],
        stale_evidence=list(evidence["stale_sources"]),
        unmet_success_criteria=[],
        reason_codes=list(dict.fromkeys(reasons)),
        plan_version=plan_version,
    )


__all__ = ["ProgressAssessment", "SemanticProgress", "assess_progress"]
