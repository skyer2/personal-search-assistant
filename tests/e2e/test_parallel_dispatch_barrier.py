import time

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.control.transitions import transition_update
from app.research.domain.contracts import WorkflowPhase
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
    original_ingest = graph_module.ingest_semantics_node
    original_assess = graph_module.assess_node
    original_plan_validate = graph_module.plan_validate_node

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
        return {
            "tasks": {task_id: tasks[task_id]},
            "worker_results": [
                {
                    "task_id": task_id,
                    "task_metadata": dict(payload.get("task_metadata") or {}),
                    "ok": True,
                    "status": "succeeded",
                    "summary": task_id,
                    "payload": {
                        "subject_id": task_id,
                        "dimension": "general",
                        "findings": [{"claim": task_id, "evidence_ids": [f"evidence:{task_id}"]}],
                        "sources": [f"https://{task_id}.example.com"],
                        "evidence_ids": [f"evidence:{task_id}"],
                    },
                }
            ],
            "evidence_refs": [f"evidence:{task_id}"],
        }

    def ingest_node(state):
        progress_waves.append(int(state.get("dispatch_wave_id") or 0))
        return original_ingest(state)

    graph_module.plan_node = plan_node
    graph_module.plan_validate_node = lambda state: transition_update(
        state, WorkflowPhase.PLAN_VALIDATED, {}
    )
    graph_module.ingest_semantics_node = ingest_node
    graph_module.assess_node = lambda state: transition_update(
        state,
        WorkflowPhase.ASSESS,
        {
            "progress_assessment": {"status": "sufficient"},
            "evidence_assessment": {"status": "sufficient"},
            "execution_health": {"status": "healthy"},
            "delivery_readiness": {"status": "ready", "mode": "normal"},
            "control_decision": {
                "action": "finalize_success",
                "mode": "normal",
                "reason_codes": ["barrier_test_complete"],
                "task_ids": [],
                "gap_ids": [],
            },
        },
    )
    try:
        graph = compile_research_graph(invoke_worker=worker, profile="agent")
        result = graph.invoke(
            initial_graph_state(
                run_id=f"r-{order}",
                session_id=f"s-{order}",
                task_query="比较 A B C 全景",
                max_replan_count=0,
            ),
            config={"configurable": {"thread_id": f"s-{order}"}, "recursion_limit": 64},
        )
    finally:
        graph_module.plan_node = globals()["_original_plan_node"]
        graph_module.plan_validate_node = original_plan_validate
        graph_module.ingest_semantics_node = original_ingest
        graph_module.assess_node = original_assess
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
