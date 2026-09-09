from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.control.policy import decide_control
from app.research.coverage.compiler import compile_coverage_contract
from app.research.runtime.state import empty_research_state
from app.research.spec.compiler import compile_research_spec


def _state() -> dict:
    spec = compile_research_spec("Compare the research capability of two products")
    contract = compile_coverage_contract(spec)
    coverage_ids = [unit.coverage_id for unit in contract.units]
    state = empty_research_state(
        run_id="r",
        session_id="s",
        task_query=spec.objective,
        max_replan_count=2,
    )
    state.update(
        {
            "research_spec": spec.to_dict(),
            "coverage_contract": contract.to_dict(),
            "coverage_state": {
                "coverage_ratio": 0.5,
                "covered_ids": coverage_ids[:1],
                "partial_ids": coverage_ids[:1],
                "missing_ids": coverage_ids[1:],
                "conflicted_ids": [],
                "stale_ids": [],
            },
            "evidence_records": [
                {
                    "evidence_id": "e0",
                    "source_id": "example.com",
                    "source_kind": "web",
                    "locator": "https://example.com",
                    "authority_score": 0.9,
                }
            ],
            "claims": [
                {"claim_id": "c0", "text": "The product supports research.", "evidence_ids": ["e0"]}
            ],
            "plan": ExecutionPlan(
                steps=[PlanStep(step_type="research", task_id="t0", description="collect evidence")]
            ).to_dict(),
            "action_budget": {
                "retry": 2,
                "gap_fill": 4,
                "expand_plan": 2,
                "replan": 2,
                "max_retry": 2,
                "max_gap_fill": 4,
                "max_expand_plan": 2,
                "max_replan": 2,
            },
        }
    )
    return state


def test_replan_hard_ceiling_stops_before_graph_recursion():
    decision = decide_control(_state())
    assert decision["action"] == "deliver_partial"
    assert "usable_partial_evidence" in decision["reason_codes"]
