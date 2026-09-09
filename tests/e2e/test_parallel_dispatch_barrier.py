"""Supervisor fan-out keeps one wave and one task snapshot per worker."""

from __future__ import annotations

from app.agent.harness.run_budget import RunBudgetManager
from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.domain.task_state import initialize_tasks
from app.research.runtime.graph import route_supervisor
from app.research.runtime.state import empty_research_state


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        summary="parallel barrier",
        steps=[
            PlanStep(
                step_type="research",
                task_id=f"t_{name}",
                description=name,
                objective=name,
                metadata={"required": True},
            )
            for name in ("a", "b", "c")
        ],
    )


def test_supervisor_dispatch_creates_one_send_per_wave_task() -> None:
    plan = _plan()
    state = empty_research_state(
        run_id="parallel-wave",
        session_id="parallel-wave",
        task_query="Compare A, B, and C",
    )
    state["plan"] = plan.to_dict()
    state["tasks"] = initialize_tasks(plan)
    state["control_decision"] = {
        "action": "dispatch",
        "task_ids": ["t_a", "t_b", "t_c"],
    }

    sends = route_supervisor(state)

    assert len(sends) == 3
    assert [send.node for send in sends] == ["researcher", "researcher", "researcher"]
    for index, send in enumerate(sends):
        task_id = f"t_{chr(ord('a') + index)}"
        assert send.arg["task_id"] == task_id
        assert send.arg["step_index"] == index
        assert send.arg["phase"] == "execute"
        assert send.arg["tasks"][task_id]["attempt"] == 0


def test_actual_wave_size_allocates_all_worker_leases() -> None:
    manager = RunBudgetManager(token_limit=30_000, llm_call_limit=30, tool_call_limit=30)
    wave_size = 3
    leases = [
        manager.reserve_worker_lease(f"supervisor_task_{index}", parallel_workers=wave_size)[0]
        for index in range(1, wave_size + 1)
    ]
    assert all(leases)
    assert len(set(leases)) == wave_size
