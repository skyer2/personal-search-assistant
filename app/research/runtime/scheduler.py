"""Mechanical research DAG scheduler. It makes no semantic workflow decisions."""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.domain.gaps import active_research_steps, stamp_initial_gap_metadata
from app.research.domain.task_state import (
    TaskExecutionStatus,
    TaskReadiness,
    initialize_tasks,
    normalize_tasks,
    task_readiness,
)

RESEARCH_STEP_TYPES = frozenset({"research", "network_search", "file_read"})


def research_only_plan(plan: ExecutionPlan) -> ExecutionPlan:
    plan.steps = [step for step in plan.steps if step.step_type in RESEARCH_STEP_TYPES]
    return plan


def annotate_plan_tasks(plan: ExecutionPlan, intent: Any | None = None) -> ExecutionPlan:
    from app.research.planning.priority import stamp_semantic_priority

    stamp_semantic_priority(plan, intent=intent)
    for index, step in enumerate(plan.steps):
        if not step.task_id:
            step.task_id = f"t{index}:{step.step_type}"
        if step.step_type in RESEARCH_STEP_TYPES and not step.depends_on:
            step.depends_on = []
    if plan.plan_version < 1:
        plan.plan_version = 1
    stamp_initial_gap_metadata(plan)
    return plan


def task_status_map(plan: ExecutionPlan) -> dict[str, str]:
    return {task_id: task["execution_status"] for task_id, task in initialize_tasks(plan).items()}


def required_research_ids(plan: ExecutionPlan) -> list[str]:
    active = [(index, step) for index, step in active_research_steps(plan)]
    required = [
        step.resolved_task_id(index)
        for index, step in active
        if not (isinstance(step.metadata, dict) and step.metadata.get("optional"))
    ]
    if required:
        return required
    return [step.resolved_task_id(index) for index, step in active]


def readiness_map(plan: ExecutionPlan, tasks: Any) -> dict[str, str]:
    return {
        step.resolved_task_id(index): task_readiness(step, tasks).value
        for index, step in enumerate(plan.steps)
    }


def select_dispatch_wave(
    plan: ExecutionPlan,
    tasks: Any,
    *,
    task_ids: list[str] | None = None,
    max_parallel: int = 3,
) -> list[tuple[int, PlanStep]]:
    selected = set(task_ids or required_research_ids(plan))
    wave: list[tuple[int, PlanStep]] = []
    for index, step in enumerate(plan.steps):
        if len(wave) >= max(1, int(max_parallel or 1)):
            break
        task_id = step.resolved_task_id(index)
        if task_id not in selected:
            continue
        if task_readiness(step, tasks) != TaskReadiness.RUNNABLE:
            continue
        wave.append((index, step))
    return wave


def dispatch_sends(
    plan: ExecutionPlan,
    tasks: Any,
    *,
    task_ids: list[str] | None = None,
    max_parallel: int = 3,
) -> list[dict[str, Any]]:
    return [
        {
            "task_id": step.resolved_task_id(index),
            "step_index": index,
            "step_type": step.step_type,
            "description": step.description,
            "subagent": step.subagent or "",
        }
        for index, step in select_dispatch_wave(
            plan,
            tasks,
            task_ids=task_ids,
            max_parallel=max_parallel,
        )
    ]


def running_task_ids(tasks: Any) -> list[str]:
    return [
        task_id
        for task_id, task in normalize_tasks(tasks).items()
        if task["execution_status"] == TaskExecutionStatus.RUNNING.value
    ]


__all__ = [
    "RESEARCH_STEP_TYPES",
    "annotate_plan_tasks",
    "dispatch_sends",
    "readiness_map",
    "research_only_plan",
    "required_research_ids",
    "running_task_ids",
    "select_dispatch_wave",
    "task_status_map",
]
