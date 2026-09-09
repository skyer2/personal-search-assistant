"""Typed contracts for the canonical research control plane."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class WorkflowPhase(StrEnum):
    BOOTSTRAP = "bootstrap"
    DIRECT = "direct"
    COMPILE_SPEC = "compile_spec"
    SPEC_GATE = "spec_gate"
    CLARIFY = "clarify"
    PLAN = "plan"
    PLAN_VALIDATED = "plan_validated"
    DISPATCH = "dispatch"
    EXECUTE = "execute"
    INGEST_SEMANTICS = "ingest_semantics"
    ASSESS = "assess"
    GAP_FILL = "gap_fill"
    EXPAND_PLAN = "expand_plan"
    REPLAN = "replan"
    SYNTHESIS = "synthesis"
    REPAIR_SYNTHESIS = "repair_synthesis"
    QUALITY = "quality"
    FINALIZE = "done"
    TERMINATED = "terminated"


class LifecycleStatus(StrEnum):
    RUNNING = "running"
    TERMINATED = "terminated"


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BudgetStatus(StrEnum):
    AVAILABLE = "available"
    LOW = "low"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


class ControlAction(StrEnum):
    DISPATCH = "dispatch"
    WAIT = "wait"
    RETRY = "retry"
    GAP_FILL = "gap_fill"
    EXPAND_PLAN = "expand_plan"
    REPLAN = "replan"
    SYNTHESIZE = "synthesize"
    REPAIR_SYNTHESIS = "repair_synthesis"
    DELIVER_PARTIAL = "deliver_partial"
    FINALIZE_SUCCESS = "finalize_success"
    FINALIZE_FAILURE = "finalize_failure"
    CANCEL = "cancel"


class StopReason(StrEnum):
    NONE = "none"
    BUDGET = "budget"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MARGINAL_GAIN_LOW = "marginal_gain_low"
    SUPERSEDED = "superseded"
    POLICY = "policy"


class ActionBudget(TypedDict):
    retry: int
    gap_fill: int
    expand_plan: int
    replan: int
    max_retry: int
    max_gap_fill: int
    max_expand_plan: int
    max_replan: int


class ControlDecision(TypedDict):
    decision_id: str
    action: str
    mode: str
    reason_codes: list[str]
    task_ids: list[str]
    gap_ids: list[str]
    candidate_set_id: str
    strategy_fingerprint: str
    state_version: int
    plan_version: int
    assessment_refs: list[str]
    policy_version: str


def new_action_budget(
    *,
    max_retry: int = 2,
    max_gap_fill: int = 4,
    max_expand_plan: int = 2,
    max_replan: int = 2,
) -> ActionBudget:
    return ActionBudget(
        retry=0,
        gap_fill=0,
        expand_plan=0,
        replan=0,
        max_retry=max(0, int(max_retry)),
        max_gap_fill=max(0, int(max_gap_fill)),
        max_expand_plan=max(0, int(max_expand_plan)),
        max_replan=max(0, int(max_replan)),
    )


def action_budget_from_state(state: dict[str, Any]) -> ActionBudget:
    raw = state.get("action_budget")
    value = dict(raw) if isinstance(raw, dict) else {}
    budget = state.get("budget")
    fallback = budget if isinstance(budget, dict) else {}
    result: dict[str, int] = dict(
        new_action_budget(
            max_retry=int(fallback.get("max_task_attempts") or 2),
            max_gap_fill=int(fallback.get("max_gap_fill_attempts") or 4),
            max_expand_plan=int(fallback.get("max_expand_plan_attempts") or 2),
            max_replan=int(fallback.get("max_replan_count") or 2),
        )
    )
    for key in result:
        if key in value:
            result[key] = max(0, int(value[key] or 0))
    return result  # type: ignore[return-value]


def budget_status(state: dict[str, Any]) -> BudgetStatus:
    budget = state.get("budget")
    if not isinstance(budget, dict):
        return BudgetStatus.UNKNOWN
    if bool(budget.get("exhausted")):
        return BudgetStatus.EXHAUSTED
    if bool(budget.get("low")):
        return BudgetStatus.LOW
    if budget.get("tool_calls") is None and budget.get("deadline_remaining_sec") is None:
        return BudgetStatus.UNKNOWN
    return BudgetStatus.AVAILABLE


__all__ = [
    "ActionBudget",
    "BudgetStatus",
    "ControlAction",
    "ControlDecision",
    "LifecycleStatus",
    "OutcomeStatus",
    "StopReason",
    "action_budget_from_state",
    "new_action_budget",
    "WorkflowPhase",
    "budget_status",
]
