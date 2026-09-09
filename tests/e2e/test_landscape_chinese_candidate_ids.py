from __future__ import annotations

from app.agent.harness.state import ExecutionPlan
from app.research.control.transitions import transition_allowed
from app.research.domain.task_state import ResultStatus, TaskExecutionStatus, transition_task
from app.research.planning.candidate import stable_candidate_id
from app.research.runtime.graph import (
    assess_node,
    compile_spec_node,
    dispatch_node,
    expand_plan_node,
    ingest_semantics_node,
    plan_node,
    plan_validate_node,
    route_dispatch,
    spec_gate_node,
)
from app.research.runtime.state import empty_research_state


QUERY = "你觉得当下国内 AI 初创有潜力值得加入的公司有哪些？为什么？"


def test_landscape_chinese_candidate_ids_survive_runtime_expansion():
    state = empty_research_state(run_id="e2e-candidate-ids", session_id="s", task_query=QUERY)
    state.update(compile_spec_node(state))
    state.update(spec_gate_node(state))
    state.update(plan_node(state))
    state.update(plan_validate_node(state))
    assert state["planning_failure"] == {}
    state.update(dispatch_node(state))

    task_id = "t_discovery"
    running_tasks = transition_task(
        state["tasks"],
        task_id,
        execution_status=TaskExecutionStatus.RUNNING,
    )
    state["tasks"] = transition_task(
        running_tasks,
        task_id,
        execution_status=TaskExecutionStatus.SUCCEEDED,
        result_status=ResultStatus.COMPLETE,
        evidence_refs=["ev_landscape"],
    )
    state["phase"] = "execute"
    state["worker_results"] = [
        {
            "task_id": task_id,
            "ok": True,
            "status": "succeeded",
            "summary": "landscape discovery",
            "task_metadata": {"task_kind": "discovery", "produces_artifact": "candidate_set"},
            "payload": {
                "candidates": [
                    {
                        "name": "月之暗面",
                        "confidence": 0.92,
                        "evidence_ids": ["ev_landscape"],
                    },
                    {
                        "name": "阶跃星辰",
                        "confidence": 0.88,
                        "evidence_ids": ["ev_landscape"],
                    },
                    {
                        "name": "智谱AI",
                        "confidence": 0.9,
                        "evidence_ids": ["ev_landscape"],
                    },
                ],
                "findings": [
                    {
                        "claim": "月之暗面、阶跃星辰和智谱AI 是国内 AI 初创候选。",
                        "evidence_ids": ["ev_landscape"],
                        "confidence": 0.9,
                    }
                ],
                "sources": ["https://example.com/ai-startups"],
                "evidence_ids": ["ev_landscape"],
            },
        }
    ]
    state.update(ingest_semantics_node(state))
    candidate_rows = state["candidate_set"]["candidates"]
    candidate_ids = [str(row["candidate_id"]) for row in candidate_rows]
    assert state["candidate_set"]["available"] is True
    assert candidate_ids == [
        stable_candidate_id("月之暗面"),
        stable_candidate_id("阶跃星辰"),
        stable_candidate_id("智谱AI"),
    ]
    assert len(set(candidate_ids)) == len(candidate_ids)

    state.update(assess_node(state))
    state.update(dispatch_node(state))
    assert state["control_decision"]["action"] == "expand_plan"
    assert route_dispatch(state) == "expand_plan"
    state.update(expand_plan_node(state))
    plan = ExecutionPlan.from_dict(state["plan"])
    task_ids = [step.task_id for step in plan.steps]
    assert task_ids
    assert len(task_ids) == len(set(task_ids))
    assert all(step.task_id.startswith("t_cand_") for step in plan.steps)
    assert all(step.metadata["candidate_id"] in candidate_ids for step in plan.steps)
    assert all(
        step.task_id == f"t_{step.metadata['candidate_id']}_{dimension}"
        for step in plan.steps
        for dimension in step.metadata.get("dimensions", [])
    )
    assert transition_allowed("dispatch", "expand_plan")
