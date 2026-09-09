"""Execution-only health assessment; it never decides research completion."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict

from app.agent.harness.state import ExecutionPlan
from app.research.domain.task_state import ResultStatus, TaskExecutionStatus, normalize_tasks


class ExecutionHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALLED = "stalled"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ExecutionHealth(TypedDict):
    status: str
    active_tasks: list[str]
    succeeded_tasks: int
    failed_tasks: int
    stopped_tasks: int
    retryable_tasks: list[str]
    semantic_stall: int
    failures: list[dict[str, Any]]


def assess_execution_health(state: dict[str, Any]) -> ExecutionHealth:
    tasks = normalize_tasks(state.get("tasks"))
    raw_plan = state.get("plan")
    plan = ExecutionPlan.from_dict(raw_plan) if isinstance(raw_plan, dict) and raw_plan else None
    if plan is None:
        active_ids = list(tasks)
    else:
        active_ids = [
            step.resolved_task_id(index)
            for index, step in enumerate(plan.steps)
            if step.step_type in {"research", "network_search", "file_read"}
            and not (isinstance(step.metadata, dict) and step.metadata.get("optional"))
        ]
    active_tasks = [tasks[task_id] for task_id in active_ids if task_id in tasks]
    active = [task["task_id"] for task in active_tasks if task["execution_status"] == TaskExecutionStatus.RUNNING.value]
    succeeded = sum(task["execution_status"] == TaskExecutionStatus.SUCCEEDED.value for task in active_tasks)
    failed = [task for task in active_tasks if task["execution_status"] == TaskExecutionStatus.FAILED.value]
    stopped = sum(task["execution_status"] == TaskExecutionStatus.STOPPED.value for task in active_tasks)
    retryable = [task["task_id"] for task in failed if bool(task.get("failure", {}).get("retryable"))]
    failures = [dict(task["failure"]) for task in failed if task.get("failure")]
    semantic_stall = int(state.get("semantic_stall") or 0)
    if semantic_stall >= 2:
        status = ExecutionHealthStatus.STALLED
    elif stopped and not succeeded and not any(
        task["result_status"] == ResultStatus.PARTIAL.value for task in active_tasks
    ):
        status = ExecutionHealthStatus.STOPPED
    elif failed and not succeeded and not any(task["result_status"] == ResultStatus.PARTIAL.value for task in failed):
        status = ExecutionHealthStatus.FAILED
    elif failed or retryable or stopped:
        status = ExecutionHealthStatus.DEGRADED
    elif active_tasks:
        status = ExecutionHealthStatus.HEALTHY
    else:
        status = ExecutionHealthStatus.UNKNOWN
    return ExecutionHealth(
        status=status.value,
        active_tasks=active,
        succeeded_tasks=succeeded,
        failed_tasks=len(failed),
        stopped_tasks=stopped,
        retryable_tasks=retryable,
        semantic_stall=semantic_stall,
        failures=failures,
    )


__all__ = ["ExecutionHealth", "ExecutionHealthStatus", "assess_execution_health"]
