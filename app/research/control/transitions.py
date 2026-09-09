"""Allowed mechanical phase transitions."""

from __future__ import annotations

from typing import Any, Mapping

from app.research.domain.contracts import WorkflowPhase


class InvalidTransition(RuntimeError):
    pass


_ALLOWED: dict[WorkflowPhase, frozenset[WorkflowPhase]] = {
    WorkflowPhase.BOOTSTRAP: frozenset({WorkflowPhase.BRIEF, WorkflowPhase.DIRECT}),
    WorkflowPhase.BRIEF: frozenset({WorkflowPhase.SUPERVISOR, WorkflowPhase.EXECUTE}),
    WorkflowPhase.SUPERVISOR: frozenset(
        {
            WorkflowPhase.EXECUTE,
            WorkflowPhase.COVERAGE_JUDGE,
            WorkflowPhase.SYNTHESIS,
            WorkflowPhase.FINALIZE,
        }
    ),
    WorkflowPhase.EXECUTE: frozenset({WorkflowPhase.INGEST_FINDINGS}),
    WorkflowPhase.INGEST_FINDINGS: frozenset({WorkflowPhase.COVERAGE_JUDGE}),
    WorkflowPhase.COVERAGE_JUDGE: frozenset({WorkflowPhase.SUPERVISOR, WorkflowPhase.SYNTHESIS}),
    WorkflowPhase.SYNTHESIS: frozenset({WorkflowPhase.QUALITY}),
    WorkflowPhase.QUALITY: frozenset({WorkflowPhase.SYNTHESIS, WorkflowPhase.FINALIZE}),
    WorkflowPhase.FINALIZE: frozenset({WorkflowPhase.TERMINATED}),
    WorkflowPhase.TERMINATED: frozenset(),
}

def transition_allowed(current: WorkflowPhase | str, target: WorkflowPhase | str) -> bool:
    try:
        return WorkflowPhase(target) in _ALLOWED.get(WorkflowPhase(current), frozenset())
    except ValueError:
        return False


def assert_transition(current: WorkflowPhase | str, target: WorkflowPhase | str) -> None:
    if not transition_allowed(current, target):
        raise InvalidTransition(f"invalid workflow transition: {current} -> {target}")


def transition_update(
    state: Mapping[str, Any] | WorkflowPhase | str,
    target: WorkflowPhase,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current: WorkflowPhase | str
    if isinstance(state, WorkflowPhase):
        current = state
    elif isinstance(state, str):
        current = state
    else:
        current = str(state.get("phase") or WorkflowPhase.BOOTSTRAP.value)
    assert_transition(current, target)
    update = dict(payload or {})
    update["phase"] = target.value
    return update


__all__ = ["InvalidTransition", "assert_transition", "transition_allowed", "transition_update"]
