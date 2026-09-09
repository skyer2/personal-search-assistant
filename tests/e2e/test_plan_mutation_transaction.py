from __future__ import annotations

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.coverage.compiler import compile_coverage_contract
from app.research.planning.candidate import Candidate
from app.research.planning.expansion import ExpandPlanResult
from app.research.planning.validator import PlanMutationProposal
from app.research.runtime.graph import expand_plan_node
from app.research.runtime.state import empty_research_state
from app.research.spec.compiler import compile_research_spec


QUERY = "你觉得当下国内 AI 初创有潜力值得加入的公司有哪些？为什么？"


def test_invalid_graph_plan_mutation_is_atomic(monkeypatch):
    spec = compile_research_spec(QUERY)
    contract = compile_coverage_contract(spec)
    candidate = Candidate(name="月之暗面", confidence=0.9, evidence_ids=["ev_1"])
    candidate_set = {
        "candidate_set_id": "candidate_set_primary",
        "status": "complete",
        "candidates": [candidate.to_dict()],
        "source_task_ids": ["t_discovery"],
        "available": True,
        "expanded": False,
    }
    initial_plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_discovery",
                description="Discovery",
                objective="Discovery",
                metadata={"task_kind": "discovery", "produces_artifact": "candidate_set"},
            )
        ],
        plan_version=1,
    )
    duplicate_step = PlanStep(
        step_type="research",
        task_id=f"t_{candidate.candidate_id}_technology",
        description="duplicate",
        objective="duplicate",
        metadata={
            "task_kind": "deep_dive",
            "subject_id": f"candidate:{candidate.candidate_id}",
            "candidate_id": candidate.candidate_id,
            "coverage_ids": ["coverage_1"],
            "coverage_keys": ["technology"],
        },
    )
    invalid_plan = ExecutionPlan(steps=[duplicate_step, duplicate_step], plan_version=2)
    proposal = PlanMutationProposal(
        mutation_type="expand_plan",
        proposed_plan=invalid_plan,
        proposed_candidate_set=candidate_set,
        proposed_coverage_contract=contract,
    )

    state = empty_research_state(run_id="e2e-mutation", session_id="s", task_query=QUERY)
    state.update(
        {
            "phase": "dispatch",
            "research_spec": spec.to_dict(),
            "coverage_contract": contract.to_dict(),
            "candidate_set": dict(candidate_set),
            "plan": initial_plan.to_dict(),
            "plan_version": 1,
            "tasks": {"t_discovery": {"task_id": "t_discovery", "execution_status": "succeeded"}},
        }
    )

    def invalid_expansion(*args, **kwargs):
        return ExpandPlanResult(
            applied=True,
            reason="candidate_deep_dive_created",
            plan=invalid_plan,
            candidate_set=dict(candidate_set),
            coverage_contract=contract,
            proposal=proposal,
        )

    monkeypatch.setattr("app.research.runtime.graph.expand_plan", invalid_expansion)
    update = expand_plan_node(state)
    assert "duplicate_task_id" in update["planning_failure"]["issues"]
    assert update["planning_failure"]["origin_stage"] == "expand_plan"
    assert "plan" not in update
    assert "tasks" not in update
    assert "candidate_set" not in update
    assert "coverage_contract" not in update
    assert state["plan"] == initial_plan.to_dict()
    assert state["plan_version"] == 1
    assert state["candidate_set"]["expanded"] is False
