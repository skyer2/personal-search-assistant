"""Allowed workflow transitions and termination invariants."""

from __future__ import annotations

from app.research.domain.contracts import OutcomeStatus, TerminationReason, WorkflowPhase


class InvalidTransition(RuntimeError):
    pass


_ALLOWED: dict[WorkflowPhase, frozenset[WorkflowPhase]] = {
    WorkflowPhase.BOOTSTRAP: frozenset({WorkflowPhase.UNDERSTAND, WorkflowPhase.ABORT}),
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
        {WorkflowPhase.DISPATCH, WorkflowPhase.PREPARE_SYNTHESIS, WorkflowPhase.QUALITY}
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


def termination_reason_for_outcome(outcome: OutcomeStatus | str, raw_reason: str = "") -> TerminationReason:
    try:
        normalized = OutcomeStatus(outcome)
    except ValueError:
        normalized = OutcomeStatus.PARTIAL
    if raw_reason:
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


__all__ = [
    "InvalidTransition",
    "assert_transition",
    "termination_reason_for_outcome",
    "transition_allowed",
]
