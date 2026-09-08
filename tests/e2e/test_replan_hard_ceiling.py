from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.control.policy import decide_control
from app.research.domain.task_state import TaskExecutionStatus, transition_task
from app.research.runtime.state import empty_research_state


def _state():
    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_landscape",
                description="candidate pool",
                metadata={"gap_ids": ["candidate_pool"], "generation": 0},
            )
        ]
    )
    state = empty_research_state(run_id="r", session_id="s", task_query="q", max_replan_count=2)
    tasks = transition_task(
        state["tasks"],
        "t_landscape",
        execution_status=TaskExecutionStatus.RUNNING,
    )
    tasks = transition_task(
        tasks,
        "t_landscape",
        execution_status=TaskExecutionStatus.FAILED,
        evidence_refs=["e1"],
    )
    state.update(
        {
            "plan": plan.to_dict(),
            "tasks": tasks,
            "replan_budget": {"attempted": 2, "applied": 2, "max_attempts": 2},
            "evidence_assessment": {
                "status": "partial",
                "trusted_evidence_count": 1,
                "primary_source_count": 0,
                "independent_source_count": 1,
            },
        }
    )
    return state


def test_replan_hard_ceiling_stops_before_graph_recursion():
    decision = decide_control(_state())
    assert decision["action"] == "deliver_partial"
    assert "recovery_exhausted" in decision["reason_codes"]
