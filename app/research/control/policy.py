"""Pure control-plane routing policy for the research graph."""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan
from app.research.domain.contracts import (
    OutcomeStatus,
    ProgressDecision,
    QualityDecision,
    task_status_projection,
)
from app.research.runtime.scheduler import ready_research_steps


def plan_from_state(state: dict[str, Any]) -> ExecutionPlan | None:
    raw = state.get("plan")
    if not isinstance(raw, dict) or not raw:
        return None
    return ExecutionPlan.from_dict(raw)


def workflow_task_status(state: dict[str, Any]) -> dict[str, str]:
    return task_status_projection(state.get("tasks"))


def max_replan_attempts(state: dict[str, Any]) -> int:
    return int((state.get("budget") or {}).get("max_replan_count") or 3)


def wave_parallel(state: dict[str, Any]) -> int:
    try:
        value = int((state.get("budget") or {}).get("max_parallel_workers") or 3)
    except (TypeError, ValueError):
        value = 3
    return max(1, value)


def has_pending_synthesis(plan: ExecutionPlan, status: dict[str, str]) -> bool:
    return any(
        step.step_type in {"generate_markdown", "summarize", "convert_pdf"}
        and status.get(step.resolved_task_id(index), "pending") in {"pending", "running"}
        for index, step in enumerate(plan.steps)
    )


def lifecycle_is_terminal(state: dict[str, Any]) -> bool:
    return bool(state.get("termination")) or str(state.get("phase") or "") == "terminated"


def decide_dispatch(state: dict[str, Any]) -> str:
    if str(state.get("status") or "") == OutcomeStatus.ABORTED.value or state.get("abort_reason"):
        return "abort"
    if str(state.get("status") or "") in {
        OutcomeStatus.RUNNING.value,
    }:
        pass
    if str(state.get("status") or "") in {
        "synthesized",
        OutcomeStatus.PARTIAL.value,
        OutcomeStatus.SUCCESS.value,
    }:
        return "quality_gate"
    if lifecycle_is_terminal(state):
        return "finalize"
    plan = plan_from_state(state)
    if plan is None:
        return "finalize"
    return "dispatch"


def decide_progress(state: dict[str, Any]) -> ProgressDecision:
    from app.research.planning.candidate import candidate_artifact_status
    from app.research.runtime.synthesis_admission import evaluate_synthesis_admission

    if str(state.get("status") or "") == OutcomeStatus.ABORTED.value or state.get("abort_reason"):
        return ProgressDecision.ABORT
    if str(state.get("status") or "") in {
        "synthesized",
        OutcomeStatus.PARTIAL.value,
        OutcomeStatus.SUCCESS.value,
    }:
        return ProgressDecision.QUALITY
    assessment = dict(state.get("progress_assessment") or {})
    verdict = str(assessment.get("verdict") or "enough")
    plan = plan_from_state(state)
    status = workflow_task_status(state)
    status.update(candidate_artifact_status(state.get("candidate_set")))
    replan_attempts = int(state.get("replan_attempts") or 0)
    exhausted = bool(state.get("replan_exhausted"))
    control_no_progress = bool(state.get("control_no_progress"))
    force_synth = str(assessment.get("reason") or "") == "force_synthesis_budget"
    if verdict == "abort":
        return ProgressDecision.ABORT
    if force_synth or exhausted or verdict == "enough":
        admission = (
            evaluate_synthesis_admission(state, plan, status, forced=force_synth)
            if plan is not None
            else None
        )
        if plan is not None and has_pending_synthesis(plan, status):
            if admission is not None and admission.allowed:
                return ProgressDecision.PREPARE_SYNTHESIS
            if ready_research_steps(plan, status, include_optional=False):
                return ProgressDecision.DISPATCH
            if (
                not exhausted
                and not control_no_progress
                and verdict == "gap"
                and replan_attempts < max_replan_attempts(state)
            ):
                return ProgressDecision.REPLAN
        return ProgressDecision.QUALITY
    if (
        verdict == "run"
        and plan is not None
        and ready_research_steps(plan, status, include_optional=False)
    ):
        return ProgressDecision.DISPATCH
    can_replan = (
        verdict == "gap"
        and not exhausted
        and not control_no_progress
        and not force_synth
        and replan_attempts < max_replan_attempts(state)
    )
    if can_replan:
        return ProgressDecision.REPLAN
    if plan is not None and has_pending_synthesis(plan, status):
        admission = evaluate_synthesis_admission(state, plan, status)
        if admission.allowed:
            return ProgressDecision.PREPARE_SYNTHESIS
    return ProgressDecision.QUALITY


def decide_after_quality(state: dict[str, Any]) -> QualityDecision:
    if state.get("quality_passed", True):
        return QualityDecision.FINALIZE
    if state.get("replan_exhausted") or state.get("control_no_progress"):
        return QualityDecision.FINALIZE
    action = str(state.get("quality_repair_action") or "partial")
    attempts = int(state.get("quality_attempts") or 0)
    if action == "repair" and attempts <= 1:
        return QualityDecision.REPAIR_SYNTHESIS
    if action == "replan":
        if int(state.get("replan_attempts") or 0) >= max_replan_attempts(state):
            return QualityDecision.FINALIZE
        if int(state.get("replan_count") or 0) < max_replan_attempts(state):
            return QualityDecision.REPLAN
    return QualityDecision.FINALIZE


def decide_replan(state: dict[str, Any]) -> bool:
    """Single admission point for every proposed replan."""
    if bool(state.get("replan_exhausted")) or bool(state.get("control_no_progress")):
        return False
    if int(state.get("replan_attempts") or 0) >= max_replan_attempts(state):
        return False
    if plan_from_state(state) is None:
        return False
    assessment = dict(state.get("progress_assessment") or {})
    return (
        str(assessment.get("verdict") or "") == "gap"
        or str(state.get("quality_repair_action") or "") == "replan"
    )


__all__ = [
    "decide_after_quality",
    "decide_dispatch",
    "decide_replan",
    "decide_progress",
    "has_pending_synthesis",
    "lifecycle_is_terminal",
    "max_replan_attempts",
    "plan_from_state",
    "wave_parallel",
    "workflow_task_status",
]
