"""Typed contracts for the canonical research control plane."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class WorkflowPhase(StrEnum):
    BOOTSTRAP = "bootstrap"
    DIRECT = "direct"
    UNDERSTAND = "understand"
    CLARIFY = "clarify"
    PLAN = "plan"
    PLAN_VALIDATED = "plan_validated"
    DISPATCH = "dispatch"
    EXECUTE = "execute"
    ASSESS = "assess"
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
    REPLAN = "replan"
    SYNTHESIZE = "synthesize"
    REPAIR_SYNTHESIS = "repair_synthesis"
    DELIVER_PARTIAL = "deliver_partial"
    FINALIZE_SUCCESS = "finalize_success"
    FINALIZE_FAILURE = "finalize_failure"
    CANCEL = "cancel"


class ReplanBudget(TypedDict):
    attempted: int
    applied: int
    max_attempts: int


class ControlDecision(TypedDict):
    decision_id: str
    action: str
    mode: str
    reason_codes: list[str]
    task_ids: list[str]
    state_version: int
    plan_version: int
    assessment_refs: list[str]
    policy_version: str


def new_replan_budget(max_attempts: int) -> ReplanBudget:
    return ReplanBudget(attempted=0, applied=0, max_attempts=max(0, int(max_attempts)))


def replan_budget_from_state(state: dict[str, Any]) -> ReplanBudget:
    raw = state.get("replan_budget")
    value = dict(raw) if isinstance(raw, dict) else {}
    maximum = int(value.get("max_attempts") or 0)
    if maximum <= 0:
        budget = state.get("budget")
        maximum = int(budget.get("max_replan_count") or 0) if isinstance(budget, dict) else 0
    return ReplanBudget(
        attempted=max(0, int(value.get("attempted") or 0)),
        applied=max(0, int(value.get("applied") or 0)),
        max_attempts=max(0, maximum),
    )


def replan_budget_exhausted(budget: ReplanBudget) -> bool:
    return budget["attempted"] >= budget["max_attempts"]


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
    "BudgetStatus",
    "ControlAction",
    "ControlDecision",
    "LifecycleStatus",
    "OutcomeStatus",
    "ReplanBudget",
    "WorkflowPhase",
    "budget_status",
    "new_replan_budget",
    "replan_budget_exhausted",
    "replan_budget_from_state",
]
