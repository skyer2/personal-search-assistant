from __future__ import annotations

import pytest

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.domain.failure import classify_failure
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    TaskReadiness,
    initialize_tasks,
    new_task_state,
    retry_task,
    task_execution_projection,
    task_readiness,
    transition_task,
)


def test_failed_task_with_partial_result_is_legal():
    tasks = {"t0": new_task_state("t0")}
    tasks = transition_task(
        tasks,
        "t0",
        execution_status=TaskExecutionStatus.RUNNING,
        attempt=1,
    )
    tasks = transition_task(
        tasks,
        "t0",
        execution_status=TaskExecutionStatus.FAILED,
        result_status=ResultStatus.PARTIAL,
        failure=dict(classify_failure("timeout")),
        evidence_refs=["e1"],
    )

    assert tasks["t0"]["execution_status"] == "failed"
    assert tasks["t0"]["result_status"] == "partial"
    assert tasks["t0"]["evidence_refs"] == ["e1"]


def test_pending_cannot_become_terminal_success():
    with pytest.raises(ValueError):
        transition_task({"t0": new_task_state("t0")}, "t0", execution_status=TaskExecutionStatus.SUCCEEDED)


def test_failed_task_can_only_be_retried_by_running():
    tasks = transition_task(
        {"t0": new_task_state("t0")},
        "t0",
        execution_status=TaskExecutionStatus.RUNNING,
        attempt=1,
    )
    tasks = transition_task(tasks, "t0", execution_status=TaskExecutionStatus.FAILED, attempt=1)
    with pytest.raises(ValueError):
        transition_task(tasks, "t0", execution_status=TaskExecutionStatus.SUCCEEDED)


def test_retry_returns_retryable_failed_task_to_pending():
    failure = dict(classify_failure("timeout"))
    tasks = transition_task(
        {"t0": new_task_state("t0")},
        "t0",
        execution_status=TaskExecutionStatus.RUNNING,
        attempt=1,
    )
    tasks = transition_task(tasks, "t0", execution_status=TaskExecutionStatus.FAILED, attempt=1, failure=failure)
    retried = retry_task(tasks, "t0")

    assert retried["t0"]["execution_status"] == "pending"
    assert retried["t0"]["result_status"] == "partial"
    assert retried["t0"]["attempt"] == 1


def test_readiness_is_derived_and_not_persisted():
    step = PlanStep(step_type="research", task_id="t0", description="collect")
    tasks = initialize_tasks(ExecutionPlan(steps=[step], summary="test"))
    step = PlanStep(step_type="research", task_id="t0", description="collect")
    assert task_readiness(step, tasks) == TaskReadiness.RUNNABLE

    running = transition_task(tasks, "t0", execution_status=TaskExecutionStatus.RUNNING)
    assert task_readiness(step, running) == TaskReadiness.WAITING_RESOURCE
    assert task_execution_projection(running) == {"t0": "running"}
