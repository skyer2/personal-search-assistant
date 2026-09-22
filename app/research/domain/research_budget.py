"""Separate semantic research budget from execution recovery budget.

A worker timeout, provider error or llm-call cap is an execution failure. It
must consume an execution retry, never a semantic research wave; otherwise one
API hiccup silently destroys the agent's remaining research opportunities.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.research.domain.failure import FailureClass

# Failure classes that mean "the infrastructure failed", not "the evidence is
# insufficient". These are recovered with retries.
EXECUTION_FAILURE_CLASSES = frozenset(
    {
        FailureClass.TRANSIENT.value,
        FailureClass.RECOVERABLE.value,
    }
)

# Terminal reasons that are execution-level even when the classifier sees them
# as budget events scoped to a single worker.
EXECUTION_FAILURE_CODES = frozenset(
    {
        "worker_timeout",
        "timeout",
        "step_timeout",
        "synthesis_timeout",
        "provider_empty_content",
        "provider_unavailable",
        "provider_rate_limit",
        "rate_limit",
        "network_error",
        "invalid_json",
        "parser_failure",
        "worker_llm_call_cap",
        "worker_token_cap",
        "finalization_mode_budget_limit",
    }
)

SEMANTIC_FAILURE_CODES = frozenset(
    {
        "coverage_gap",
        "evidence_conflict",
        "unsupported_claim",
        "no_usable_evidence",
        "no_accepted_findings",
        "search_empty",
        "search_miss",
    }
)


@dataclass(frozen=True)
class SemanticBudget:
    """Budget for deciding *what* to research next."""

    research_waves: int = 2
    replan_count: int = 1
    gap_expansions: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionBudget:
    """Budget for recovering *how* a task runs."""

    retries_per_task: int = 2
    provider_retries: int = 2
    timeout_retries: int = 1
    tool_retries: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_execution_failure(failure: Any) -> bool:
    """True when a task failure is infrastructure, not insufficient evidence."""
    row = failure if isinstance(failure, dict) else {}
    code = str(row.get("code") or "").strip().lower()
    if code in SEMANTIC_FAILURE_CODES:
        return False
    if code in EXECUTION_FAILURE_CODES:
        return True
    failure_class = str(row.get("failure_class") or "").strip().lower()
    if failure_class == FailureClass.SEMANTIC.value:
        return False
    if failure_class in EXECUTION_FAILURE_CLASSES:
        return bool(row.get("retryable", True))
    return False


def execution_failed_task_ids(state: dict[str, Any]) -> tuple[str, ...]:
    tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    return tuple(
        task_id
        for task_id, task in tasks.items()
        if isinstance(task, dict)
        and str(task.get("execution_status") or "") in {"failed", "stopped"}
        and is_execution_failure(task.get("failure"))
    )


def worker_failures_by_type(state: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        if str(task.get("execution_status") or "") not in {"failed", "stopped"}:
            continue
        failure = task.get("failure") if isinstance(task.get("failure"), dict) else {}
        code = str(failure.get("code") or task.get("stop_reason") or "unknown").lower()
        counts[code] = counts.get(code, 0) + 1
    return counts


def is_execution_recovery_pass(
    state: dict[str, Any],
    *,
    max_attempts: int = 3,
) -> bool:
    """Whether the next supervisor pass exists only to retry a failed worker.

    Such a pass must not increment the semantic iteration counter.
    """
    tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    retryable = [
        task
        for task in tasks.values()
        if isinstance(task, dict)
        and str(task.get("execution_status") or "") == "failed"
        and bool((task.get("failure") or {}).get("retryable"))
        and is_execution_failure(task.get("failure"))
        and int(task.get("attempt") or 0) < max_attempts
    ]
    return bool(retryable)


__all__ = [
    "EXECUTION_FAILURE_CLASSES",
    "EXECUTION_FAILURE_CODES",
    "SEMANTIC_FAILURE_CODES",
    "ExecutionBudget",
    "SemanticBudget",
    "execution_failed_task_ids",
    "is_execution_failure",
    "is_execution_recovery_pass",
    "worker_failures_by_type",
]
