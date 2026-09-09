from __future__ import annotations

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.control.policy import decide_control
from app.research.coverage.compiler import compile_coverage_contract
from app.research.domain.failure import classify_failure
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    new_task_state,
    transition_task,
)
from app.research.runtime.graph import retry_node, route_dispatch
from app.research.runtime.state import empty_research_state
from app.research.spec.compiler import compile_research_spec


def _state(
    *,
    execution_status: TaskExecutionStatus = TaskExecutionStatus.SUCCEEDED,
    coverage_ratio: float = 1.0,
    evidence: bool = True,
    partial: bool = False,
    semantic_gap: bool = False,
    candidate_ready: bool = False,
    semantic_stall: int = 0,
    exhausted: bool = False,
) -> dict:
    spec = compile_research_spec("Compare the research capability of two products")
    contract = compile_coverage_contract(spec)
    state = empty_research_state(
        run_id="r",
        session_id="s",
        task_query=spec.objective,
        max_replan_count=2,
    )
    state["research_spec"] = spec.to_dict()
    state["coverage_contract"] = contract.to_dict()
    state["plan"] = ExecutionPlan(
        steps=[PlanStep(step_type="research", task_id="t0", description="collect evidence")],
        summary="test",
    ).to_dict()
    covered_ids = [unit.coverage_id for unit in contract.units]
    state["coverage_state"] = {
        "coverage_ratio": coverage_ratio,
        "covered_ids": covered_ids if coverage_ratio >= 1.0 else covered_ids[:1],
        "partial_ids": covered_ids if partial else [],
        "missing_ids": [] if coverage_ratio >= 1.0 else covered_ids[1:],
        "conflicted_ids": [],
        "stale_ids": [],
    }
    if evidence:
        state["evidence_records"] = [
            {
                "evidence_id": "e0",
                "source_id": "example.com",
                "source_kind": "web",
                "locator": "https://example.com",
                "authority_score": 0.9,
            }
        ]
        state["claims"] = [
            {
                "claim_id": "c0",
                "text": "The product supports research.",
                "evidence_ids": ["e0"],
                "subject_id": covered_ids[0],
                "dimension_id": covered_ids[0],
            }
        ]
    if semantic_gap:
        state["semantic_gaps"] = {
            "gap_0": {"gap_id": "gap_0", "coverage_id": covered_ids[0], "actionable": True, "attempt_count": 0}
        }
    if candidate_ready:
        state["candidate_set"] = {
            "candidate_set_id": "cs0",
            "available": True,
            "expanded": False,
            "items": [{"candidate_id": "candidate_0", "name": "Product A"}],
            "source_task_ids": ["t0"],
        }
    state["semantic_stall"] = semantic_stall
    state["budget"]["exhausted"] = exhausted
    state["budget_status"] = "exhausted" if exhausted else "available"

    tasks = {"t0": new_task_state("t0")}
    if execution_status == TaskExecutionStatus.RUNNING:
        tasks = transition_task(tasks, "t0", execution_status=TaskExecutionStatus.RUNNING)
    elif execution_status != TaskExecutionStatus.PENDING:
        tasks = transition_task(tasks, "t0", execution_status=TaskExecutionStatus.RUNNING)
        tasks = transition_task(
            tasks,
            "t0",
            execution_status=execution_status,
            attempt=1,
            result_status=ResultStatus.COMPLETE
            if execution_status == TaskExecutionStatus.SUCCEEDED
            else ResultStatus.PARTIAL,
            failure=dict(classify_failure("timeout"))
            if execution_status == TaskExecutionStatus.FAILED
            else None,
        )
    state["tasks"] = tasks
    return state


def test_runnable_task_dispatches():
    assert decide_control(_state(execution_status=TaskExecutionStatus.PENDING))["action"] == "dispatch"


def test_transient_failure_retries():
    state = _state(execution_status=TaskExecutionStatus.FAILED, evidence=False, coverage_ratio=0.0)
    assert decide_control(state)["action"] == "retry"


def test_candidate_set_ready_expands_plan():
    state = _state(candidate_ready=True, coverage_ratio=0.0, semantic_gap=True)
    assert decide_control(state)["action"] == "expand_plan"


def test_explicit_semantic_gap_uses_gap_fill():
    state = _state(coverage_ratio=0.5, semantic_gap=True)
    assert decide_control(state)["action"] == "gap_fill"


def test_semantic_stall_with_alternative_replans():
    state = _state(
        coverage_ratio=0.5,
        semantic_gap=True,
        semantic_stall=2,
    )
    state["semantic_gaps"]["gap_0"]["attempt_count"] = 2
    assert decide_control(state)["action"] == "replan"


def test_sufficient_coverage_synthesizes():
    assert decide_control(_state())["action"] == "synthesize"


def test_low_gain_with_usable_evidence_delivers_partial():
    state = _state(partial=True)
    state["marginal_gain"] = {"stalled": True, "semantic_gain": 0.0}
    state["semantic_gaps"] = {}
    assert decide_control(state)["action"] == "deliver_partial"


def test_exhausted_without_evidence_fails():
    state = _state(evidence=False, coverage_ratio=0.0, exhausted=True)
    assert decide_control(state)["action"] == "finalize_failure"


def test_retry_node_records_action_budget():
    state = _state(execution_status=TaskExecutionStatus.FAILED, evidence=False, coverage_ratio=0.0)
    state["control_decision"] = {"action": "retry", "task_ids": ["t0"]}
    update = retry_node(state)
    assert update["tasks"]["t0"]["execution_status"] == "pending"
    assert update["action_budget"]["retry"] == 1
    assert update["control_decision"] == {}


def test_dispatch_send_carries_task_attempt_snapshot():
    state = _state(execution_status=TaskExecutionStatus.PENDING)
    state["control_decision"] = {"action": "dispatch", "task_ids": ["t0"]}
    sends = route_dispatch(state)
    assert len(sends) == 1
    assert sends[0].arg["tasks"]["t0"]["attempt"] == 0
