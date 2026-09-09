"""Canonical task execution state and readiness contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict

from app.research.domain.contracts import StopReason


class TaskExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"


class ResultStatus(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    COMPLETE = "complete"


class TaskReadiness(StrEnum):
    RUNNABLE = "runnable"
    WAITING_DEPENDENCY = "waiting_dependency"
    WAITING_RESOURCE = "waiting_resource"
    BLOCKED_DEPENDENCY_FAILED = "blocked_dependency_failed"


class TaskExecutionState(TypedDict):
    task_id: str
    execution_status: str
    result_status: str
    attempt: int
    started_at: str
    ended_at: str
    failure: dict[str, Any]
    stop_reason: str
    skip_reason: str
    evidence_refs: list[str]
    artifact_refs: list[str]


def new_task_state(task_id: str) -> TaskExecutionState:
    return TaskExecutionState(
        task_id=task_id,
        execution_status=TaskExecutionStatus.PENDING.value,
        result_status=ResultStatus.NONE.value,
        attempt=0,
        started_at="",
        ended_at="",
        failure={},
        stop_reason=StopReason.NONE.value,
        skip_reason="",
        evidence_refs=[],
        artifact_refs=[],
    )


def normalize_task_state(raw: Any, task_id: str) -> TaskExecutionState:
    value = dict(raw) if isinstance(raw, dict) else {}
    execution_status = str(value.get("execution_status") or TaskExecutionStatus.PENDING.value)
    if execution_status == "cancelled":
        execution_status = TaskExecutionStatus.STOPPED.value
    if execution_status not in {item.value for item in TaskExecutionStatus}:
        raise ValueError(f"invalid task execution status: {execution_status}")
    result_status = str(value.get("result_status") or ResultStatus.NONE.value)
    if result_status not in {item.value for item in ResultStatus}:
        raise ValueError(f"invalid task result status: {result_status}")
    failure = value.get("failure")
    return TaskExecutionState(
        task_id=str(value.get("task_id") or task_id),
        execution_status=execution_status,
        result_status=result_status,
        attempt=max(0, int(value.get("attempt") or 0)),
        started_at=str(value.get("started_at") or ""),
        ended_at=str(value.get("ended_at") or ""),
        failure=dict(failure) if isinstance(failure, dict) else {},
        stop_reason=str(value.get("stop_reason") or (StopReason.CANCELLED.value if execution_status == TaskExecutionStatus.STOPPED.value else StopReason.NONE.value)),
        skip_reason=str(value.get("skip_reason") or ""),
        evidence_refs=[str(item) for item in value.get("evidence_refs") or []],
        artifact_refs=[str(item) for item in value.get("artifact_refs") or []],
    )


def supersede_task(tasks: Any, task_id: str, *, replacement_task_id: str) -> dict[str, TaskExecutionState]:
    """Preserve historical execution facts while removing an active obligation."""
    normalized = normalize_tasks(tasks)
    current = normalized.get(task_id)
    if current is None:
        raise ValueError(f"cannot supersede unknown task: {task_id}")
    current.update(
        execution_status=TaskExecutionStatus.SUPERSEDED.value,
        skip_reason=f"superseded_by:{replacement_task_id}",
    )
    normalized[task_id] = current
    return normalized


def normalize_tasks(raw: Any) -> dict[str, TaskExecutionState]:
    if not isinstance(raw, dict):
        return {}
    return {str(task_id): normalize_task_state(value, str(task_id)) for task_id, value in raw.items()}


def initialize_tasks(plan: Any) -> dict[str, TaskExecutionState]:
    tasks: dict[str, TaskExecutionState] = {}
    for index, step in enumerate(plan.steps):
        task_id = step.resolved_task_id(index)
        tasks[task_id] = new_task_state(task_id)
    return tasks


def task_execution_projection(tasks: Any) -> dict[str, str]:
    return {
        task_id: state["execution_status"]
        for task_id, state in normalize_tasks(tasks).items()
    }


def transition_task(
    tasks: Any,
    task_id: str,
    *,
    execution_status: TaskExecutionStatus,
    result_status: ResultStatus | None = None,
    attempt: int | None = None,
    failure: dict[str, Any] | None = None,
    stop_reason: str = "",
    skip_reason: str = "",
    evidence_refs: list[str] | None = None,
    artifact_refs: list[str] | None = None,
    timestamp: str = "",
) -> dict[str, TaskExecutionState]:
    normalized = normalize_tasks(tasks)
    current = dict(normalized.get(task_id) or new_task_state(task_id))
    previous_status = TaskExecutionStatus(current["execution_status"])
    if previous_status not in {
        TaskExecutionStatus.PENDING,
        TaskExecutionStatus.RUNNING,
        TaskExecutionStatus.FAILED,
    }:
        raise ValueError(f"illegal transition from {previous_status.value} to {execution_status.value}")
    if previous_status == TaskExecutionStatus.FAILED and execution_status != TaskExecutionStatus.RUNNING:
        raise ValueError("failed tasks may only be retried by starting a new execution")
    if previous_status == TaskExecutionStatus.STOPPED and execution_status != TaskExecutionStatus.STOPPED:
        raise ValueError("stopped tasks cannot automatically become failed")
    if previous_status == TaskExecutionStatus.PENDING and execution_status not in {
        TaskExecutionStatus.RUNNING,
        TaskExecutionStatus.SKIPPED,
        TaskExecutionStatus.STOPPED,
    }:
        raise ValueError("pending tasks must start, skip, or cancel before terminal execution")
    if previous_status == TaskExecutionStatus.RUNNING and execution_status not in {
        TaskExecutionStatus.SUCCEEDED,
        TaskExecutionStatus.FAILED,
        TaskExecutionStatus.STOPPED,
    }:
        raise ValueError("running tasks must finish, fail, or stop")
    resolved_result = result_status or (
        ResultStatus.COMPLETE
        if execution_status == TaskExecutionStatus.SUCCEEDED
        else ResultStatus.PARTIAL
        if failure or evidence_refs or artifact_refs or execution_status == TaskExecutionStatus.STOPPED
        else ResultStatus.NONE
    )
    current.update(
        execution_status=execution_status.value,
        result_status=resolved_result.value,
        attempt=(int(current.get("attempt") or 0) + 1) if attempt is None else int(attempt),
        failure=failure or ({} if execution_status != TaskExecutionStatus.FAILED else current.get("failure") or {}),
        stop_reason=str(stop_reason or skip_reason or current.get("stop_reason") or StopReason.NONE.value),
        skip_reason=skip_reason or current.get("skip_reason") or "",
        evidence_refs=list(dict.fromkeys([*(current.get("evidence_refs") or []), *(evidence_refs or [])])),
        artifact_refs=list(dict.fromkeys([*(current.get("artifact_refs") or []), *(artifact_refs or [])])),
    )
    if execution_status == TaskExecutionStatus.RUNNING:
        current["started_at"] = current.get("started_at") or timestamp
        current["ended_at"] = ""
    elif execution_status != TaskExecutionStatus.PENDING:
        current["ended_at"] = current.get("ended_at") or timestamp
    normalized[task_id] = current  # type: ignore[assignment]
    return normalized


def task_readiness(
    step: Any,
    tasks: Any,
    *,
    resource_available: bool = True,
) -> TaskReadiness:
    normalized = normalize_tasks(tasks)
    task_id = str(getattr(step, "task_id") or "")
    state = normalized.get(task_id)
    if state is None:
        return TaskReadiness.WAITING_DEPENDENCY
    if state["execution_status"] != TaskExecutionStatus.PENDING.value:
        return TaskReadiness.WAITING_RESOURCE
    for dependency in list(getattr(step, "depends_on", None) or []):
        dep = normalized.get(str(dependency))
        if dep is None or dep["execution_status"] in {
            TaskExecutionStatus.PENDING.value,
            TaskExecutionStatus.RUNNING.value,
        }:
            return TaskReadiness.WAITING_DEPENDENCY
        if dep["execution_status"] == TaskExecutionStatus.FAILED.value and dep["result_status"] != ResultStatus.PARTIAL.value:
            return TaskReadiness.BLOCKED_DEPENDENCY_FAILED
    if not resource_available:
        return TaskReadiness.WAITING_RESOURCE
    return TaskReadiness.RUNNABLE


def retry_task(tasks: Any, task_id: str) -> dict[str, TaskExecutionState]:
    """Return a retryable failed task to the canonical pending state."""
    normalized = normalize_tasks(tasks)
    current = normalized.get(task_id)
    if current is None:
        raise ValueError(f"cannot retry unknown task: {task_id}")
    if current["execution_status"] != TaskExecutionStatus.FAILED.value:
        raise ValueError(f"cannot retry task in {current['execution_status']}")
    if not bool(current.get("failure", {}).get("retryable")):
        raise ValueError(f"task is not retryable: {task_id}")
    current.update(
        execution_status=TaskExecutionStatus.PENDING.value,
        ended_at="",
        skip_reason="",
    )
    normalized[task_id] = current  # type: ignore[assignment]
    return normalized


__all__ = [
    "ResultStatus",
    "TaskExecutionState",
    "TaskExecutionStatus",
    "TaskReadiness",
    "initialize_tasks",
    "new_task_state",
    "normalize_tasks",
    "normalize_task_state",
    "retry_task",
    "supersede_task",
    "task_execution_projection",
    "task_readiness",
    "transition_task",
]
