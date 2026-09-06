"""Allowed workflow transitions and termination invariants."""

from __future__ import annotations

from typing import Any, Mapping

from app.research.domain.contracts import (
    OutcomeStatus,
    TerminationReason,
    WorkflowPhase,
    merge_termination,
)


class InvalidTransition(RuntimeError):
    pass


_ALLOWED: dict[WorkflowPhase, frozenset[WorkflowPhase]] = {
    WorkflowPhase.BOOTSTRAP: frozenset(
        {WorkflowPhase.DIRECT, WorkflowPhase.UNDERSTAND, WorkflowPhase.ABORT}
    ),
    WorkflowPhase.DIRECT: frozenset({WorkflowPhase.FINALIZE}),
    WorkflowPhase.UNDERSTAND: frozenset(
        {WorkflowPhase.CLARIFY, WorkflowPhase.PLAN, WorkflowPhase.ABORT}
    ),
    WorkflowPhase.CLARIFY: frozenset({WorkflowPhase.PLAN, WorkflowPhase.ABORT}),
    WorkflowPhase.PLAN: frozenset(
        {WorkflowPhase.PLAN_VALIDATED, WorkflowPhase.REPLAN, WorkflowPhase.ABORT}
    ),
    WorkflowPhase.PLAN_VALIDATED: frozenset(
        {WorkflowPhase.DISPATCH, WorkflowPhase.REPLAN, WorkflowPhase.ABORT}
    ),
    WorkflowPhase.DISPATCH: frozenset(
        {WorkflowPhase.EXECUTE, WorkflowPhase.PROGRESS, WorkflowPhase.ABORT}
    ),
    WorkflowPhase.EXECUTE: frozenset({WorkflowPhase.PROGRESS, WorkflowPhase.ABORT}),
    WorkflowPhase.PROGRESS: frozenset(
        {
            WorkflowPhase.DISPATCH,
            WorkflowPhase.REPLAN,
            WorkflowPhase.PREPARE_SYNTHESIS,
            WorkflowPhase.QUALITY,
            WorkflowPhase.ABORT,
        }
    ),
    WorkflowPhase.REPLAN: frozenset(
        {
            WorkflowPhase.PLAN_VALIDATED,
            WorkflowPhase.DISPATCH,
            WorkflowPhase.PREPARE_SYNTHESIS,
            WorkflowPhase.QUALITY,
        }
    ),
    WorkflowPhase.PREPARE_SYNTHESIS: frozenset(
        {WorkflowPhase.SYNTHESIS, WorkflowPhase.ABORT}
    ),
    WorkflowPhase.SYNTHESIS: frozenset(
        {WorkflowPhase.QUALITY, WorkflowPhase.REPAIR_SYNTHESIS, WorkflowPhase.ABORT}
    ),
    WorkflowPhase.REPAIR_SYNTHESIS: frozenset({WorkflowPhase.SYNTHESIS}),
    WorkflowPhase.QUALITY: frozenset(
        {
            WorkflowPhase.FINALIZE,
            WorkflowPhase.REPAIR_SYNTHESIS,
            WorkflowPhase.REPLAN,
        }
    ),
    WorkflowPhase.FINALIZE: frozenset({WorkflowPhase.TERMINATED}),
    WorkflowPhase.ABORT: frozenset({WorkflowPhase.TERMINATED}),
    WorkflowPhase.TERMINATED: frozenset(),
}


def transition_allowed(current: WorkflowPhase | str, target: WorkflowPhase | str) -> bool:
    try:
        current_phase = WorkflowPhase(current)
        target_phase = WorkflowPhase(target)
    except ValueError:
        return False
    return target_phase in _ALLOWED.get(current_phase, frozenset())


def assert_transition(current: WorkflowPhase | str, target: WorkflowPhase | str) -> None:
    if not transition_allowed(current, target):
        raise InvalidTransition(f"invalid workflow transition: {current} -> {target}")


def transition_update(
    state: Mapping[str, Any] | WorkflowPhase | str,
    target: WorkflowPhase,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a node update after enforcing the workflow transition."""
    if isinstance(state, WorkflowPhase):
        current: WorkflowPhase | str = state
    elif isinstance(state, str):
        current = state
    else:
        current = str(state.get("phase") or "")
    if not current:
        current = WorkflowPhase.BOOTSTRAP
    assert_transition(current, target)
    update = dict(payload or {})
    update["phase"] = target.value
    return update


def termination_reason_for_outcome(outcome: OutcomeStatus | str, raw_reason: str = "") -> TerminationReason:
    aliases = {
        "token_limit": TerminationReason.BUDGET_TOKENS,
        "research_token_cap": TerminationReason.RESEARCH_TOKEN_CAP,
        "llm_call_limit": TerminationReason.BUDGET_LLM_CALLS,
        "tool_call_limit": TerminationReason.BUDGET_TOOL_CALLS,
        "run_deadline": TerminationReason.DEADLINE_EXCEEDED,
        "synthesis_reserve": TerminationReason.SYNTHESIS_TIME_RESERVE,
        "control_plane_no_progress": TerminationReason.CONTROL_NO_PROGRESS,
    }
    try:
        normalized = OutcomeStatus(outcome)
    except ValueError:
        normalized = OutcomeStatus.PARTIAL
    if raw_reason:
        if raw_reason in aliases:
            return aliases[raw_reason]
        try:
            return TerminationReason(raw_reason)
        except ValueError:
            pass
    return {
        OutcomeStatus.SUCCESS: TerminationReason.COMPLETED,
        OutcomeStatus.PARTIAL: TerminationReason.PARTIAL_DELIVERED,
        OutcomeStatus.ABORTED: TerminationReason.ABORTED,
        OutcomeStatus.INTERRUPTED: TerminationReason.INTERRUPTED,
        OutcomeStatus.RUNNING: TerminationReason.INCOMPLETE,
    }[normalized]


def terminal_update(
    state: Mapping[str, Any],
    *,
    outcome: OutcomeStatus,
    reason: str,
    stage: str,
    detected_stage: str,
    origin_stage: str = "",
    cause_event_id: str = "",
    research_completed: bool = False,
    synthesis_attempted: bool = False,
    quality_attempted: bool = False,
) -> dict[str, Any]:
    current = str(state.get("phase") or WorkflowPhase.BOOTSTRAP)
    if current in {
        WorkflowPhase.FINALIZE.value,
        WorkflowPhase.ABORT.value,
        WorkflowPhase.TERMINATED.value,
    }:
        assert_transition(current, WorkflowPhase.TERMINATED)
    else:
        terminal_phase = (
            WorkflowPhase.ABORT
            if outcome in {OutcomeStatus.ABORTED, OutcomeStatus.INTERRUPTED}
            else WorkflowPhase.FINALIZE
        )
        assert_transition(current, terminal_phase)
    termination = {
        "outcome": outcome.value,
        "reason": termination_reason_for_outcome(outcome, reason).value,
        "stage": stage,
        "detected_stage": detected_stage,
        "origin_stage": origin_stage or stage,
        "cause_event_id": cause_event_id,
        "research_completed": research_completed,
        "synthesis_attempted": synthesis_attempted,
        "quality_attempted": quality_attempted,
    }
    existing = state.get("termination")
    return {
        "phase": WorkflowPhase.TERMINATED.value,
        "termination": merge_termination(existing, termination),
    }


__all__ = [
    "InvalidTransition",
    "assert_transition",
    "termination_reason_for_outcome",
    "terminal_update",
    "transition_update",
    "transition_allowed",
]
