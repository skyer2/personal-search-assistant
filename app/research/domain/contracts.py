"""Typed workflow contracts shared by the graph, scheduler, and executors."""

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
    PROGRESS = "progress_eval"
    REPLAN = "replan"
    PREPARE_SYNTHESIS = "prepare_synthesis"
    SYNTHESIS = "synthesized"
    REPAIR_SYNTHESIS = "repair_synthesis"
    QUALITY = "quality"
    FINALIZE = "done"
    ABORT = "abort"
    TERMINATED = "terminated"


class OutcomeStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "completed"
    PARTIAL = "partial"
    ABORTED = "aborted"
    INTERRUPTED = "interrupted"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ProgressDecision(StrEnum):
    DISPATCH = "dispatch"
    REPLAN = "replan"
    PREPARE_SYNTHESIS = "prepare_synthesis"
    QUALITY = "quality_gate"
    ABORT = "abort"


class QualityDecision(StrEnum):
    FINALIZE = "finalize"
    REPAIR_SYNTHESIS = "repair_synthesis"
    REPLAN = "replan"


class ReplanState(StrEnum):
    AVAILABLE = "available"
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXHAUSTED = "exhausted"


class TerminationReason(StrEnum):
    COMPLETED = "completed"
    PARTIAL_DELIVERED = "partial_delivered"
    CONTROL_NO_PROGRESS = "control_plane_no_progress"
    NO_TRUSTED_EVIDENCE = "no_trusted_evidence"
    BUDGET_TOKENS = "budget_tokens"
    RESEARCH_TOKEN_CAP = "research_token_cap"
    BUDGET_LLM_CALLS = "budget_llm_calls"
    BUDGET_TOOL_CALLS = "budget_tool_calls"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SYNTHESIS_TIME_RESERVE = "synthesis_time_reserve"
    REPLAN_EXHAUSTED = "replan_exhausted"
    PROVIDER_POLICY = "provider_policy"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    QUALITY_FAILED = "quality_failed"
    EMPTY_PLAN = "empty_plan"
    ABORTED = "aborted"
    INTERRUPTED = "interrupted"
    INCOMPLETE = "incomplete"


class TaskRuntimeState(TypedDict):
    task_id: str
    status: str
    attempt: int
    skip_reason: str
    failure_reason: str


class Termination(TypedDict):
    outcome: str
    reason: str
    stage: str
    detected_stage: str
    origin_stage: str
    cause_event_id: str
    research_completed: bool
    synthesis_attempted: bool
    quality_attempted: bool


def new_task_state(task_id: str, *, status: TaskStatus = TaskStatus.PENDING) -> TaskRuntimeState:
    return TaskRuntimeState(
        task_id=task_id,
        status=status.value,
        attempt=0,
        skip_reason="",
        failure_reason="",
    )


def initialize_tasks(plan: Any) -> dict[str, TaskRuntimeState]:
    tasks: dict[str, TaskRuntimeState] = {}
    for index, step in enumerate(plan.steps):
        task_id = step.resolved_task_id(index)
        tasks[task_id] = new_task_state(task_id)
    return tasks


def normalize_tasks(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for task_id, value in raw.items():
        if not isinstance(value, dict):
            continue
        normalized[str(task_id)] = {
            "task_id": str(value.get("task_id") or task_id),
            "status": str(value.get("status") or TaskStatus.PENDING.value),
            "attempt": int(value.get("attempt") or 0),
            "skip_reason": str(value.get("skip_reason") or ""),
            "failure_reason": str(value.get("failure_reason") or ""),
        }
    return normalized


def update_task(
    tasks: Any,
    task_id: str,
    *,
    status: TaskStatus,
    attempt: int | None = None,
    skip_reason: str = "",
    failure_reason: str = "",
) -> dict[str, Any]:
    updated = normalize_tasks(tasks)
    current = updated.get(task_id) or dict(new_task_state(task_id))
    current["task_id"] = task_id
    current["status"] = status.value
    current["attempt"] = int(current.get("attempt") or 0) if attempt is None else int(attempt)
    current["skip_reason"] = skip_reason or str(current.get("skip_reason") or "")
    current["failure_reason"] = failure_reason or str(current.get("failure_reason") or "")
    updated[task_id] = current
    return updated


def task_status_projection(tasks: Any) -> dict[str, str]:
    normalized = normalize_tasks(tasks)
    return {task_id: str(value["status"]) for task_id, value in normalized.items()}


def tasks_from_status(status: dict[str, str]) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for task_id, raw_status in status.items():
        tasks.update(
            update_task(
                tasks,
                str(task_id),
                status=TaskStatus(raw_status),
            )
        )
    return tasks


def merge_termination(existing: Any, update: Any) -> dict[str, Any]:
    """Keep the first terminal cause and only append later lifecycle facts."""
    old = dict(existing) if isinstance(existing, dict) else {}
    new = dict(update) if isinstance(update, dict) else {}
    if not old:
        return new
    merged = dict(old)
    for key in ("detected_stage", "quality_attempted", "synthesis_attempted"):
        if new.get(key) is not None and merged.get(key) in (None, ""):
            merged[key] = new[key]
    return merged


def merge_task_state(
    tasks: Any,
    task_id: str,
    status: TaskStatus,
    *,
    failure_reason: str = "",
    skip_reason: str = "",
) -> dict[str, Any]:
    return update_task(
        tasks,
        task_id,
        status=status,
        skip_reason=skip_reason,
        failure_reason=failure_reason,
    )


def outcome_from_status(raw: Any) -> OutcomeStatus:
    try:
        return OutcomeStatus(str(raw))
    except ValueError:
        return OutcomeStatus.RUNNING


__all__ = [
    "OutcomeStatus",
    "ProgressDecision",
    "QualityDecision",
    "ReplanState",
    "TaskRuntimeState",
    "TaskStatus",
    "Termination",
    "TerminationReason",
    "WorkflowPhase",
    "initialize_tasks",
    "merge_task_state",
    "merge_termination",
    "new_task_state",
    "normalize_tasks",
    "outcome_from_status",
    "task_status_projection",
    "tasks_from_status",
    "update_task",
]
