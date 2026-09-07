from __future__ import annotations

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.control.policy import decide_control
from app.research.domain.failure import classify_failure
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    new_task_state,
    transition_task,
)
from app.research.runtime.state import empty_research_state
from app.research.runtime.graph import retry_node, route_dispatch


def state_with_task(
    *,
    execution_status: TaskExecutionStatus,
    result_status: ResultStatus = ResultStatus.NONE,
    attempt: int = 1,
    failure: dict | None = None,
    evidence_refs: list[str] | None = None,
    progress: str = "unknown",
    evidence: str = "insufficient",
    budget_status: str = "available",
    max_replan: int = 0,
    quality: dict | None = None,
    cancel_reason: str = "",
    plan: bool = True,
) -> dict:
    step = PlanStep(step_type="research", task_id="t0", description="collect evidence")
    state = empty_research_state(
        run_id="r",
        session_id="s",
        task_query="q",
        max_replan_count=max_replan,
    )
    if plan:
        state["plan"] = ExecutionPlan(steps=[step], summary="test").to_dict()
    tasks = {"t0": new_task_state("t0")}
    if execution_status != TaskExecutionStatus.PENDING:
        tasks = transition_task(tasks, "t0", execution_status=TaskExecutionStatus.RUNNING, attempt=attempt)
        tasks = transition_task(
            tasks,
            "t0",
            execution_status=execution_status,
            result_status=result_status,
            attempt=attempt,
            failure=failure,
            evidence_refs=evidence_refs,
        )
    state.update(
        {
            "tasks": tasks,
            "budget_status": budget_status,
            "progress_assessment": {"status": progress},
            "evidence_assessment": {
                "status": evidence,
                "trusted_evidence_count": 2 if evidence == "sufficient" else 1 if evidence == "partial" else 0,
            },
            **({"quality_assessment": quality} if quality is not None else {}),
            **({"cancel_reason": cancel_reason} if cancel_reason else {}),
        }
    )
    return state


def test_policy_decision_matrix():
    actionable_gap_state = state_with_task(
        execution_status=TaskExecutionStatus.SUCCEEDED,
        progress="gap",
        max_replan=2,
    )
    actionable_gap_state["progress_assessment"]["missing_dimensions"] = ["team_background"]
    cases = [
        (
            state_with_task(
                execution_status=TaskExecutionStatus.PENDING,
                attempt=0,
                cancel_reason="user_cancelled",
            ),
            "cancel",
        ),
        (state_with_task(execution_status=TaskExecutionStatus.PENDING, attempt=0, plan=False), "finalize_failure"),
        (state_with_task(execution_status=TaskExecutionStatus.RUNNING), "wait"),
        (state_with_task(execution_status=TaskExecutionStatus.PENDING, attempt=0), "dispatch"),
        (
            state_with_task(
                execution_status=TaskExecutionStatus.FAILED,
                failure=dict(classify_failure("timeout")),
            ),
            "retry",
        ),
        (actionable_gap_state, "replan"),
        (
            state_with_task(
                execution_status=TaskExecutionStatus.FAILED,
                failure=dict(classify_failure("timeout")),
                attempt=2,
                progress="gap",
                max_replan=2,
            ),
            "finalize_failure",
        ),
        (
            state_with_task(
                execution_status=TaskExecutionStatus.FAILED,
                result_status=ResultStatus.PARTIAL,
                evidence_refs=["e1"],
                progress="sufficient",
                evidence="partial",
            ),
            "deliver_partial",
        ),
        (
            state_with_task(
                execution_status=TaskExecutionStatus.SUCCEEDED,
                progress="sufficient",
                evidence="partial",
                budget_status="exhausted",
                max_replan=3,
            ),
            "deliver_partial",
        ),
        (
            state_with_task(
                execution_status=TaskExecutionStatus.SUCCEEDED,
                progress="sufficient",
                evidence="sufficient",
                max_replan=3,
            ),
            "synthesize",
        ),
        (
            state_with_task(
                execution_status=TaskExecutionStatus.SUCCEEDED,
                progress="sufficient",
                evidence="sufficient",
                quality={"verdict": "fail", "issues": ["evidence_partial"]},
            ),
            "finalize_failure",
        ),
    ]
    for state, expected in cases:
        decision = decide_control(state)
        assert decision["action"] == expected


def test_retry_node_only_mutates_task_state():
    state = state_with_task(
        execution_status=TaskExecutionStatus.FAILED,
        failure=dict(classify_failure("timeout")),
    )
    state["control_decision"] = {"action": "retry", "task_ids": ["t0"]}
    update = retry_node(state)
    assert set(update) == {"tasks"}
    assert update["tasks"]["t0"]["execution_status"] == "pending"
    assert "phase" not in update


def test_dispatch_send_carries_task_attempt_snapshot():
    state = state_with_task(execution_status=TaskExecutionStatus.PENDING, attempt=1)
    state["tasks"]["t0"]["attempt"] = 1
    state["control_decision"] = {"action": "dispatch", "task_ids": ["t0"]}
    sends = route_dispatch(state)
    assert len(sends) == 1
    assert sends[0].arg["tasks"]["t0"]["attempt"] == 1
