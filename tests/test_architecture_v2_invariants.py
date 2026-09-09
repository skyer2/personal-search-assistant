"""Architecture v2 invariants from the contract-driven research harness SDD."""

from __future__ import annotations

import pytest

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.assessment.progress import assess_progress
from app.research.coverage.assessor import assess_coverage
from app.research.coverage.compiler import compile_coverage_contract
from app.research.coverage.gaps import build_semantic_gaps
from app.research.coverage.models import CoverageContract
from app.research.domain.contracts import WorkflowPhase
from app.research.domain.task_state import ResultStatus, TaskExecutionStatus, transition_task
from app.research.planning.expansion import expand_plan
from app.research.planning.gap_fill import gap_fill
from app.research.planning.planner import plan_for_spec
from app.research.planning.replan import replan, strategy_fingerprint
from app.research.runtime.graph import quality_gate_node, retry_node, route_dispatch, synthesize_node
from app.research.runtime.semantic_ingest import ingest_semantics
from app.research.runtime.state import empty_research_state
from app.research.spec.compiler import compile_research_spec


def _spec(query: str = "OpenAI 的收入是多少？"):
    return compile_research_spec(query)


def _semantic_gap(gap_id: str = "gap_01") -> dict:
    return {
        "gap_id": gap_id,
        "gap_type": "coverage",
        "subject_id": "subject_1",
        "dimension_id": "key_fact",
        "severity": "high",
        "blocking": True,
        "actionable": True,
        "coverage_id": "coverage_01",
        "claim_ids": [],
        "evidence_ids": [],
        "attempted_actions": [],
        "attempt_count": 0,
    }


def test_inv_01_research_state_has_one_success_contract_authority():
    state = empty_research_state(run_id="r", session_id="s", task_query="q")
    assert state["research_spec"] == {}
    assert state["coverage_contract"] == {}
    for legacy_field in ("brief", "business_gaps", "recovery_snapshot", "replan_budget", "rejected_patch_hashes", "stalled_cycles"):
        assert legacy_field not in state


def test_inv_02_control_policy_is_the_only_route_authority(monkeypatch):
    import app.research.runtime.graph as graph

    monkeypatch.setattr(graph, "decide_control", lambda _state: {"action": "gap_fill"})
    state = empty_research_state(run_id="r", session_id="s", task_query="q")
    state["control_decision"] = {}
    assert route_dispatch(state) == "gap_fill"


def test_inv_03_task_completion_is_not_research_completion():
    state = empty_research_state(run_id="r", session_id="s", task_query="OpenAI 的收入是多少？")
    state["research_spec"] = _spec().to_dict()
    state["coverage_state"] = {"coverage_ratio": 0.0, "missing_ids": ["coverage_01"]}
    state["tasks"] = {
        "t_openai": {
            "task_id": "t_openai",
            "execution_status": TaskExecutionStatus.SUCCEEDED.value,
            "result_status": ResultStatus.COMPLETE.value,
            "attempt": 1,
        }
    }
    assert assess_progress(state)["status"] == "gap"


def test_inv_04_semantic_gap_id_does_not_depend_on_task_or_claim_ids():
    unit = {
        "coverage_id": "coverage_01",
        "subject_id": "DeepSeek",
        "dimension_id": "commercialization",
        "status": "missing",
        "evidence_ids": [],
        "claim_ids": ["claim_a"],
    }
    first = {
        "contract_id": "contract_01",
        "spec_id": "spec_01",
        "units": [unit],
        "coverage_ratio": 0.0,
        "missing_ids": ["coverage_01"],
    }
    second = {
        **first,
        "units": [{**unit, "claim_ids": ["claim_b"], "evidence_ids": ["evidence_b"]}],
    }
    first_gaps = build_semantic_gaps(first)
    second_gaps = build_semantic_gaps(second)
    assert [gap.gap_id for gap in first_gaps] == [gap.gap_id for gap in second_gaps]


def test_inv_05_replan_changes_strategy_fingerprint():
    spec = _spec()
    contract = compile_coverage_contract(spec)
    plan = plan_for_spec(spec, contract)
    result = replan(spec, plan, {"gap_01": _semantic_gap()}, {}, reason="semantic_gain_low", plan_version=1)
    assert result.applied
    assert result.proposal.changes
    assert strategy_fingerprint(result.plan) != strategy_fingerprint(plan)


def test_inv_06_retry_does_not_expand_plan_semantic_scope():
    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_openai",
                description="OpenAI revenue",
                objective="OpenAI revenue",
                metadata={"coverage_ids": ["coverage_01"], "coverage_keys": ["key_fact"]},
            )
        ],
        plan_version=1,
    )
    tasks = transition_task({}, "t_openai", execution_status=TaskExecutionStatus.RUNNING)
    tasks = transition_task(
        tasks,
        "t_openai",
        execution_status=TaskExecutionStatus.FAILED,
        failure={"retryable": True},
    )
    state = empty_research_state(run_id="r", session_id="s", task_query="q")
    state["plan"] = plan.to_dict()
    state["tasks"] = tasks
    state["control_decision"] = {"action": "retry", "task_ids": ["t_openai"]}
    update = retry_node(state)
    assert "plan" not in update
    assert state["plan"]["steps"][0]["metadata"]["coverage_ids"] == ["coverage_01"]


def test_inv_07_gap_fill_tasks_bind_semantic_gap_ids():
    result = gap_fill(_spec(), {"gap_01": _semantic_gap()}, plan_version=1)
    assert result.applied
    assert result.plan.steps
    assert all(step.metadata["resolves_gap_ids"] == ["gap_01"] for step in result.plan.steps)


def test_inv_08_expand_plan_consumes_candidate_set():
    spec = _spec("国内有哪些值得关注的 AI 初创公司？")
    unavailable = expand_plan(spec, {"candidates": [], "items": []}, plan_version=1)
    assert not unavailable.applied
    candidate_set = {
        "candidate_set_id": "candidate_set_01",
        "status": "complete",
        "candidates": [{"candidate_id": "alpha", "name": "Alpha"}],
        "items": ["Alpha"],
        "available": True,
    }
    result = expand_plan(spec, candidate_set, plan_version=1)
    assert result.applied
    assert result.plan.steps
    assert {step.metadata["candidate_id"] for step in result.plan.steps} == {"alpha"}


def test_inv_09_coverage_state_is_recomputable_from_semantic_inputs():
    spec = _spec()
    contract: CoverageContract = compile_coverage_contract(spec)
    claims = [
        {
            "claim_id": "claim_01",
            "text": "OpenAI revenue 10 亿美元",
            "subject_id": "subject_1",
            "dimension_id": "key_fact",
            "evidence_ids": ["evidence_01", "evidence_02"],
            "confidence": 0.9,
        }
    ]
    evidence = [
        {"evidence_id": "evidence_01", "source_id": "openai.com", "locator": "https://openai.com/a", "authority_score": 0.9},
        {"evidence_id": "evidence_02", "source_id": "reuters.com", "locator": "https://reuters.com/a", "authority_score": 0.75},
    ]
    first = assess_coverage(contract, claims=claims, evidence=evidence)
    second = assess_coverage(contract, claims=claims, evidence=evidence)
    assert first.to_dict() == second.to_dict()
    assert first.coverage_ratio == 1.0


def test_inv_10_stopped_partial_does_not_become_failed():
    tasks = transition_task(
        {},
        "t_openai",
        execution_status=TaskExecutionStatus.STOPPED,
        result_status=ResultStatus.PARTIAL,
        evidence_refs=["evidence_01"],
    )
    with pytest.raises(ValueError, match="illegal transition"):
        transition_task(tasks, "t_openai", execution_status=TaskExecutionStatus.FAILED)


def test_inv_11_synthesis_does_not_modify_coverage_or_claims():
    state = empty_research_state(run_id="r", session_id="s", task_query="OpenAI 的收入是多少？")
    state["phase"] = WorkflowPhase.ASSESS.value
    state["research_spec"] = _spec().to_dict()
    state["coverage_state"] = {"coverage_ratio": 1.0, "covered_ids": ["coverage_01"]}
    state["claims"] = [{"claim_id": "claim_01", "evidence_ids": ["evidence_01"]}]
    update = synthesize_node(state)
    for semantic_field in ("research_spec", "coverage_contract", "coverage_state", "claims", "evidence_records", "semantic_gaps"):
        assert semantic_field not in update


def test_inv_12_unadmitted_evidence_never_supports_claims():
    state = empty_research_state(run_id="r", session_id="s", task_query="OpenAI 的收入是多少？")
    spec = _spec()
    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_openai",
                description="OpenAI revenue",
                objective="OpenAI revenue",
                metadata={"subject_id": "subject_1", "coverage_keys": ["key_fact"]},
            )
        ]
    )
    state["research_spec"] = spec.to_dict()
    state["plan"] = plan.to_dict()
    state["worker_results"] = [
        {
            "task_id": "t_openai",
            "ok": True,
            "summary": "unsupported claim",
            "payload": {"findings": [{"claim": "unsupported claim"}], "sources": [], "evidence_ids": []},
        }
    ]
    update = ingest_semantics(state)
    assert update["evidence_records"] == []
    assert update["claims"] == []


def test_inv_13_blocking_conflict_blocks_normal_success():
    state = empty_research_state(run_id="r", session_id="s", task_query="OpenAI 的收入是多少？")
    state["phase"] = WorkflowPhase.SYNTHESIS.value
    state["research_spec"] = _spec().to_dict()
    state["coverage_state"] = {
        "coverage_ratio": 1.0,
        "covered_ids": ["coverage_01"],
        "conflicted_ids": ["coverage_01"],
    }
    state["final_content"] = "grounded answer"
    update = quality_gate_node(state)
    assessment = update["quality_assessment"]
    assert "blocking_conflict_unresolved" in assessment["issues"]
    assert update["control_decision"]["action"] != "finalize_success"


def test_inv_14_final_success_requires_minimum_coverage():
    state = empty_research_state(run_id="r", session_id="s", task_query="OpenAI 的收入是多少？")
    state["phase"] = WorkflowPhase.SYNTHESIS.value
    state["research_spec"] = _spec().to_dict()
    state["coverage_state"] = {"coverage_ratio": 0.5, "partial_ids": ["coverage_01"]}
    state["semantic_gaps"] = {"gap_01": _semantic_gap()}
    state["final_content"] = "partial answer"
    update = quality_gate_node(state)
    assessment = update["quality_assessment"]
    assert "coverage_gate_failed" in assessment["issues"]
    assert update["control_decision"]["action"] != "finalize_success"
