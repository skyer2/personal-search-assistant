"""Allowed mechanical phase transitions."""

from __future__ import annotations

from typing import Any, Mapping

from app.research.domain.contracts import WorkflowPhase


class InvalidTransition(RuntimeError):
    pass


_ALLOWED: dict[WorkflowPhase, frozenset[WorkflowPhase]] = {
    WorkflowPhase.BOOTSTRAP: frozenset({WorkflowPhase.DIRECT, WorkflowPhase.COMPILE_SPEC}),
    WorkflowPhase.DIRECT: frozenset({WorkflowPhase.FINALIZE}),
    WorkflowPhase.COMPILE_SPEC: frozenset({WorkflowPhase.SPEC_GATE}),
    WorkflowPhase.SPEC_GATE: frozenset({WorkflowPhase.CLARIFY, WorkflowPhase.PLAN}),
    WorkflowPhase.CLARIFY: frozenset({WorkflowPhase.COMPILE_SPEC}),
    WorkflowPhase.PLAN: frozenset({WorkflowPhase.PLAN_VALIDATED}),
    WorkflowPhase.PLAN_VALIDATED: frozenset({WorkflowPhase.DISPATCH}),
    WorkflowPhase.DISPATCH: frozenset(
        {
            WorkflowPhase.EXECUTE,
            WorkflowPhase.INGEST_SEMANTICS,
            WorkflowPhase.GAP_FILL,
            WorkflowPhase.EXPAND_PLAN,
            WorkflowPhase.REPLAN,
            WorkflowPhase.SYNTHESIS,
            WorkflowPhase.FINALIZE,
        }
    ),
    WorkflowPhase.EXECUTE: frozenset({WorkflowPhase.INGEST_SEMANTICS}),
    WorkflowPhase.INGEST_SEMANTICS: frozenset({WorkflowPhase.ASSESS}),
    WorkflowPhase.ASSESS: frozenset(
        {
            WorkflowPhase.DISPATCH,
            WorkflowPhase.GAP_FILL,
            WorkflowPhase.EXPAND_PLAN,
            WorkflowPhase.REPLAN,
            WorkflowPhase.SYNTHESIS,
            WorkflowPhase.FINALIZE,
            WorkflowPhase.TERMINATED,
        }
    ),
    WorkflowPhase.GAP_FILL: frozenset({WorkflowPhase.PLAN_VALIDATED}),
    WorkflowPhase.EXPAND_PLAN: frozenset({WorkflowPhase.PLAN_VALIDATED}),
    WorkflowPhase.REPLAN: frozenset({WorkflowPhase.PLAN_VALIDATED}),
    WorkflowPhase.SYNTHESIS: frozenset({WorkflowPhase.QUALITY, WorkflowPhase.REPAIR_SYNTHESIS}),
    WorkflowPhase.REPAIR_SYNTHESIS: frozenset({WorkflowPhase.SYNTHESIS}),
    WorkflowPhase.QUALITY: frozenset(
        {
            WorkflowPhase.FINALIZE,
            WorkflowPhase.REPAIR_SYNTHESIS,
            WorkflowPhase.REPLAN,
            WorkflowPhase.GAP_FILL,
        }
    ),
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
