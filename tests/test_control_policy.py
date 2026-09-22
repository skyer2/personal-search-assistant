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


def _answerable(state: dict) -> dict:
    """Evidence bound to an answerable research question."""
    state["evidence_records"] = [{"evidence_id": "e0"}]
    state["answerability"] = {
        "answerable": True,
        "question_status": [
            {
                "question_id": "q1",
                "ask_id": "A1",
                "answerable": True,
                "supporting_evidence": ["e0"],
            }
        ],
    }
    return state


def test_budget_stop_without_answerable_ask_fails() -> None:
    state = _state()
    state["budget"]["exhausted"] = True
    # Bare evidence is not delivery-worthy; nothing answers a user ask yet.
    state["evidence_records"] = [{"evidence_id": "e0"}]
    assert decide_control(state).action == "finalize_failure"


def test_budget_stop_with_answerable_ask_delivers_partial() -> None:
    state = _answerable(_state())
    state["budget"]["exhausted"] = True
    decision = decide_control(state)
    assert decision.action == "deliver_partial"
    assert "answerable_user_ask" in decision.reason_codes


def test_supervisor_iteration_limit_converges() -> None:
    state = _state()
    state["budget"]["max_replan_count"] = 3
    state["supervisor"] = {"iteration": 4}
    state["supervisor_action"] = {
        "action": "CONDUCT_RESEARCH",
        "research_tasks": [{"task_id": "supervisor_task_1"}],
    }
    assert decide_control(state).action == "finalize_failure"

    _answerable(state)
    assert decide_control(state).action == "deliver_partial"


def test_first_allowed_repair_dispatches_at_iteration_limit() -> None:
    state = _state()
    state["budget"]["max_replan_count"] = 1
    state["supervisor"] = {"iteration": 1}
    state["supervisor_action"] = {
        "action": "CONDUCT_RESEARCH",
        "research_tasks": [{"task_id": "repair_1"}],
    }

    decision = decide_control(state)

    assert decision.action == "dispatch"
    assert decision.task_ids == ("repair_1",)


def test_stop_budget_partial_action_is_not_synthesis_complete() -> None:
    state = _answerable(_state())
    state["supervisor_action"] = {"action": "STOP_BUDGET_PARTIAL"}
    decision = decide_control(state)
    assert decision.action == "deliver_partial"
    assert "stop_budget_partial" in decision.reason_codes


def test_stop_failure_without_answerable_ask_finalizes_failure() -> None:
    state = _state()
    state["supervisor_action"] = {"action": "STOP_FAILURE"}
    state["evidence_records"] = [{"evidence_id": "e0"}]
    assert decide_control(state).action == "finalize_failure"


def test_execution_retry_is_preferred_over_stop() -> None:
    state = _answerable(_state())
    state["supervisor_action"] = {"action": "STOP_BUDGET_PARTIAL"}
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
