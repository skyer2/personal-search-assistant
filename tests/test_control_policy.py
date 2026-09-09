"""RuntimePolicy is mechanical only and has no semantic research actions."""

from __future__ import annotations

from app.research.control.runtime_policy import SEMANTIC_ACTIONS, decide_control
from app.research.domain.failure import classify_failure
from app.research.domain.task_state import TaskExecutionStatus, transition_task
from app.research.runtime.state import empty_research_state


def _state() -> dict:
    return empty_research_state(
        run_id="runtime-policy",
        session_id="runtime-policy",
        task_query="Compare three research products",
    )


def test_runtime_policy_has_no_semantic_actions() -> None:
    assert SEMANTIC_ACTIONS == frozenset()


def test_supervisor_research_request_dispatches() -> None:
    state = _state()
    state["supervisor_action"] = {
        "action": "CONDUCT_RESEARCH",
        "research_tasks": [{"task_id": "supervisor_task_1"}],
    }
    decision = decide_control(state)
    assert decision.action == "dispatch"
    assert decision.task_ids == ("supervisor_task_1",)


def test_transient_failure_retries_only_retryable_task() -> None:
    state = _state()
    running_tasks = transition_task(
        state["tasks"],
        "supervisor_task_1",
        execution_status=TaskExecutionStatus.RUNNING,
    )
    state["tasks"] = transition_task(
        running_tasks,
        "supervisor_task_1",
        execution_status=TaskExecutionStatus.FAILED,
        attempt=1,
        failure=dict(classify_failure("timeout")),
    )
    decision = decide_control(state)
    assert decision.action == "retry"
    assert decision.task_ids == ("supervisor_task_1",)


def test_running_worker_waits() -> None:
    state = _state()
    state["tasks"] = transition_task(
        state["tasks"],
        "supervisor_task_1",
        execution_status=TaskExecutionStatus.RUNNING,
    )
    decision = decide_control(state)
    assert decision.action == "wait"


def test_supervisor_complete_with_evidence_synthesizes() -> None:
    state = _state()
    state["supervisor_action"] = {"action": "COMPLETE"}
    state["evidence_records"] = [{"evidence_id": "e0"}]
    assert decide_control(state).action == "synthesize"


def test_supervisor_complete_without_evidence_fails() -> None:
    state = _state()
    state["supervisor_action"] = {"action": "COMPLETE"}
    assert decide_control(state).action == "finalize_failure"


def test_budget_stop_with_evidence_delivers_partial() -> None:
    state = _state()
    state["budget"]["exhausted"] = True
    state["evidence_records"] = [{"evidence_id": "e0"}]
    decision = decide_control(state)
    assert decision.action == "deliver_partial"


def test_supervisor_iteration_limit_converges() -> None:
    state = _state()
    state["supervisor"] = {"iteration": 3}
    state["supervisor_action"] = {
        "action": "CONDUCT_RESEARCH",
        "research_tasks": [{"task_id": "supervisor_task_1"}],
    }
    assert decide_control(state).action == "finalize_failure"

    state["evidence_records"] = [{"evidence_id": "e0"}]
    assert decide_control(state).action == "deliver_partial"
