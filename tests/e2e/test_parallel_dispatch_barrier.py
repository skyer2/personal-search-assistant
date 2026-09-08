import time

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.control.transitions import transition_update
from app.research.domain.contracts import WorkflowPhase
from app.research.domain.gaps import sync_business_gaps
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    initialize_tasks,
    transition_task,
)
from app.research.runtime import graph as graph_module
from app.research.runtime.graph import compile_research_graph, initial_graph_state


def _plan():
    return ExecutionPlan(
        summary="parallel barrier",
        steps=[
            PlanStep(
                step_type="research",
                task_id=f"t_{name}",
                description=name,
                objective=name,
                metadata={"gap_ids": [f"gap_{name}"], "required": True},
            )
            for name in ("a", "b", "c")
        ],
    )


def _invoke_barrier_graph(order, delays):
    completion_order = []
    progress_waves = []
    original_progress = graph_module.progress_node

    def plan_node(state):
        plan = _plan()
        tasks = initialize_tasks(plan)
        return transition_update(
            state,
            WorkflowPhase.PLAN,
            {
                "plan": plan.to_dict(),
                "plan_version": 1,
                "tasks": tasks,
                "business_gaps": sync_business_gaps(plan, tasks, {}),
            },
        )

    def worker(payload):
        task_id = str(payload["task_id"])
        time.sleep(delays[task_id])
        running = transition_task(payload["tasks"], task_id, execution_status=TaskExecutionStatus.RUNNING)
        tasks = transition_task(
            running,
            task_id,
            execution_status=TaskExecutionStatus.SUCCEEDED,
            result_status=ResultStatus.COMPLETE,
            evidence_refs=[f"evidence:{task_id}"],
        )
        completion_order.append(task_id)
        return transition_update(
            payload,
            WorkflowPhase.EXECUTE,
            {"tasks": {task_id: tasks[task_id]}, "evidence_refs": [f"evidence:{task_id}"]},
        )

    def progress_node(state):
        progress_waves.append(int(state.get("dispatch_wave_id") or 0))
        return original_progress(state)

    graph_module.plan_node = plan_node
    graph_module.progress_node = progress_node
    try:
        graph = compile_research_graph(invoke_worker=worker, profile="agent")
        result = graph.invoke(
            initial_graph_state(
                run_id=f"r-{order}",
                session_id=f"s-{order}",
                task_query="比较 A B C 全景",
                max_replan_count=0,
            ),
            config={"configurable": {"thread_id": f"s-{order}"}, "recursion_limit": 30},
        )
    finally:
        graph_module.plan_node = globals()["_original_plan_node"]
        graph_module.progress_node = original_progress
    return result, completion_order, progress_waves


_original_plan_node = graph_module.plan_node


def test_parallel_dispatch_has_single_progress_barrier_for_abc_bac_cba():
    for order, delays in {
        "abc": {"t_a": 0.03, "t_b": 0.06, "t_c": 0.09},
        "bac": {"t_a": 0.06, "t_b": 0.03, "t_c": 0.09},
        "cba": {"t_a": 0.09, "t_b": 0.06, "t_c": 0.03},
    }.items():
        result, completion_order, progress_waves = _invoke_barrier_graph(order, delays)
        assert completion_order == [f"t_{char}" for char in order], (order, completion_order)
        assert progress_waves == [1], (order, progress_waves)
        assert all(
            task["execution_status"] == "succeeded"
            for task in result["tasks"].values()
        )
