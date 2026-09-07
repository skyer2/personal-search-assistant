"""Execution health assessment."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict

from app.research.domain.task_state import ResultStatus, TaskExecutionStatus, normalize_tasks


class ExecutionHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALLED = "stalled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ExecutionHealth(TypedDict):
    status: str
    active_tasks: list[str]
    succeeded_tasks: int
    failed_tasks: int
    retryable_tasks: list[str]
    stalled_cycles: int
    failures: list[dict[str, Any]]


def assess_execution_health(state: dict[str, Any]) -> ExecutionHealth:
    tasks = normalize_tasks(state.get("tasks"))
    active = [task_id for task_id, task in tasks.items() if task["execution_status"] == TaskExecutionStatus.RUNNING.value]
    succeeded = sum(task["execution_status"] == TaskExecutionStatus.SUCCEEDED.value for task in tasks.values())
    failed_tasks = [task for task in tasks.values() if task["execution_status"] == TaskExecutionStatus.FAILED.value]
    partial_failed_tasks = [
        task for task in failed_tasks if task["result_status"] == ResultStatus.PARTIAL.value
    ]
    retryable = [task["task_id"] for task in failed_tasks if bool(task.get("failure", {}).get("retryable"))]
    failures = [dict(task["failure"]) for task in failed_tasks if task.get("failure")]
    stalled_cycles = int(state.get("stalled_cycles") or 0)
    if stalled_cycles >= 2:
        status = ExecutionHealthStatus.STALLED
    elif failed_tasks and not succeeded and not partial_failed_tasks:
        status = ExecutionHealthStatus.FAILED
    elif failed_tasks or retryable:
        status = ExecutionHealthStatus.DEGRADED
    elif tasks:
        status = ExecutionHealthStatus.HEALTHY
    else:
        status = ExecutionHealthStatus.UNKNOWN
    return ExecutionHealth(
        status=status.value,
        active_tasks=active,
        succeeded_tasks=succeeded,
        failed_tasks=len(failed_tasks),
        retryable_tasks=retryable,
        stalled_cycles=stalled_cycles,
        failures=failures,
    )


__all__ = ["ExecutionHealth", "ExecutionHealthStatus", "assess_execution_health"]
