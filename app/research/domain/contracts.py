"""Typed contracts for the canonical research control plane."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class WorkflowPhase(StrEnum):
    BOOTSTRAP = "bootstrap"
    BRIEF = "brief"
    SUPERVISOR = "supervisor"
    DIRECT = "direct"
    EXECUTE = "execute"
    INGEST_FINDINGS = "ingest_findings"
    COVERAGE_JUDGE = "coverage_judge"
    SYNTHESIS = "synthesis"
    QUALITY = "quality"
    FINALIZE = "done"
    TERMINATED = "terminated"


class LifecycleStatus(StrEnum):
    RUNNING = "running"
    TERMINATED = "terminated"


class RuntimeStatus(StrEnum):
    FINISHED = "finished"
    CANCELLED = "cancelled"
    CRASHED = "crashed"


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


class StopReason(StrEnum):
    NONE = "none"
    BUDGET = "budget"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MARGINAL_GAIN_LOW = "marginal_gain_low"
    SUPERSEDED = "superseded"
    POLICY = "policy"


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
    "LifecycleStatus",
    "OutcomeStatus",
    "RuntimeStatus",
    "StopReason",
    "WorkflowPhase",
    "budget_status",
]
