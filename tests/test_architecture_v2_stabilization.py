"""Architecture v2 stabilization invariants.

These tests encode the release-blocking properties from the v2 stabilization
SDD. They deliberately exercise Unicode, invalid proposals, and degraded
delivery rather than a single benchmark query.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.control.policy import decide_control
from app.research.domain.contracts import ControlAction
from app.research.planning.candidate import Candidate, build_candidate_set, stable_candidate_id
from app.research.planning.validator import (
    PlanMutationProposal,
    PlanMutationRejected,
    commit_plan_mutation,
    validate_plan_mutation,
)
from app.research.runtime.semantic_ingest import ingest_semantics
from app.research.runtime.state import empty_research_state
from app.research.spec.compiler import compile_research_spec


LANDSCAPE_QUERY = "你觉得当下国内 AI 初创有潜力值得加入的公司有哪些？为什么？"


def _discovery_plan() -> ExecutionPlan:
    return ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_discovery",
                description="Discovery",
                objective="Discovery",
                metadata={
                    "task_kind": "discovery",
                    "produces_artifact": "candidate_set",
                    "allowed_sources": ["web"],
                },
            )
        ],
        plan_version=1,
    )


def test_candidate_id_is_stable_for_chinese_names():
    names = ["月之暗面", "阶跃星辰", "智谱", "银河通用", "MiniMax", "DeepSeek", "公司-AI🚀"]
    first = [stable_candidate_id(name) for name in names]
    second = [stable_candidate_id(name) for name in names]
    assert first == second
    assert len(set(first)) == len(first)
    assert bool(re.fullmatch(r"cand_[0-9a-f]{12}", first[0]))


def test_candidate_id_does_not_use_python_hash():
    source = inspect.getsource(Candidate.from_dict)
    assert "hash(" not in source
    assert stable_candidate_id("月之暗面") == stable_candidate_id("月之暗面")


def test_candidate_admission_requires_evidence_and_rejects_random_facts():
    plan = _discovery_plan()
    random_fact_row = {
        "task_id": "t_discovery",
        "payload": {
            "facts": ["这是一句与公司候选集无关的长句子，不应被当成公司名。"],
            "sources": ["https://example.com/source"],
            "evidence_ids": ["ev_discovery"],
        },
    }
    degraded = build_candidate_set(
        plan,
        worker_rows=[random_fact_row],
        task_status={"t_discovery": "succeeded"},
        query=LANDSCAPE_QUERY,
    )
    assert degraded["available"] is False
    assert degraded["candidates"] == []
    assert degraded["fallback"] is True
    assert degraded["status"] in {"degraded", "partial", "fallback"}

    structured_row = {
        "task_id": "t_discovery",
        "payload": {
            "candidates": [
                {
                    "name": "月之暗面",
                    "aliases": ["Moonshot AI"],
                    "confidence": 0.86,
                    "evidence_ids": ["ev_discovery"],
                    "selection_reason": "consumer AI and funding signal",
                }
            ],
            "sources": ["https://example.com/source"],
            "evidence_ids": ["ev_discovery"],
        },
    }
    admitted = build_candidate_set(
        plan,
        worker_rows=[structured_row],
        task_status={"t_discovery": "succeeded"},
        query=LANDSCAPE_QUERY,
    )
    assert admitted["available"] is True
    candidate = Candidate.from_dict(admitted["candidates"][0])
    assert candidate.name == "月之暗面"
    assert candidate.aliases == ["Moonshot AI"]
    assert candidate.confidence == pytest.approx(0.86)
    assert candidate.evidence_ids == ["ev_discovery"]
    assert candidate.candidate_id == stable_candidate_id("月之暗面")


def test_invalid_plan_mutation_does_not_commit():
    state = empty_research_state(run_id="r", session_id="s", task_query=LANDSCAPE_QUERY)
    state["plan"] = _discovery_plan().to_dict()
    state["plan_version"] = 1
    state["tasks"] = {"t_discovery": {"task_id": "t_discovery", "execution_status": "succeeded"}}
    duplicate_step = PlanStep(
        step_type="research",
        task_id="t_duplicate",
        description="duplicate",
        objective="duplicate",
        metadata={"allowed_sources": ["web"], "task_kind": "deep_dive"},
    )
    proposal = PlanMutationProposal(
        mutation_type="expand_plan",
        proposed_plan=ExecutionPlan(steps=[duplicate_step, duplicate_step], plan_version=2),
        proposed_tasks={},
        metadata={"reason": "forced_duplicate"},
    )
    issues = validate_plan_mutation(proposal, state)
    assert "duplicate_task_id" in issues
    with pytest.raises(PlanMutationRejected):
        commit_plan_mutation(proposal, state)
    assert state["plan"] == _discovery_plan().to_dict()
    assert state["plan_version"] == 1
    assert state["tasks"]["t_discovery"]["execution_status"] == "succeeded"


def test_expand_plan_only_marks_candidate_set_expanded_after_validation():
    state = empty_research_state(run_id="r", session_id="s", task_query=LANDSCAPE_QUERY)
    spec = compile_research_spec(LANDSCAPE_QUERY)
    state["research_spec"] = spec.to_dict()
    state["plan"] = _discovery_plan().to_dict()
    state["plan_version"] = 1
    candidate = Candidate(name="月之暗面", confidence=0.86, evidence_ids=["ev_discovery"])
    candidate_set = {
        "candidate_set_id": "candidate_set_primary",
        "status": "complete",
        "candidates": [candidate.to_dict()],
        "source_task_ids": ["t_discovery"],
        "available": True,
        "expanded": False,
    }
    step = PlanStep(
        step_type="research",
        task_id=f"t_{candidate.candidate_id}_technology",
        description="deep dive",
        objective="deep dive",
        metadata={
            "allowed_sources": ["web"],
            "task_kind": "deep_dive",
            "subject_id": f"candidate:{candidate.candidate_id}",
            "coverage_keys": ["technology"],
            "coverage_ids": ["coverage_technology"],
            "candidate_id": candidate.candidate_id,
        },
    )
    proposal = PlanMutationProposal(
        mutation_type="expand_plan",
        proposed_plan=ExecutionPlan(steps=[step], plan_version=2),
        proposed_tasks={"t_discovery": {"task_id": "t_discovery", "execution_status": "succeeded"}},
        proposed_candidate_set=candidate_set,
        proposed_coverage_contract={"contract_id": "contract", "spec_id": spec.spec_id, "version": 1, "units": []},
        metadata={"reason": "candidate_ready"},
    )
    assert validate_plan_mutation(proposal, state) == []
    assert candidate_set["expanded"] is False
    update = commit_plan_mutation(proposal, state)
    assert update["plan_version"] == 2
    assert update["candidate_set"]["expanded"] is True
    assert update["candidate_set"]["expanded_plan_version"] == 2


def test_plan_validation_failure_is_not_user_cancel():
    state = empty_research_state(run_id="r", session_id="s", task_query=LANDSCAPE_QUERY)
    state["abort_reason"] = "plan_validation_failed:duplicate_task_id"
    state["evidence_assessment"] = {"status": "partial", "evidence_count": 1}
    state["evidence_records"] = [{"evidence_id": "ev_1", "authority_score": 0.8}]
    state["claims"] = [{"claim_id": "claim_1", "evidence_ids": ["ev_1"], "confidence": 0.7}]
    decision = decide_control(state)
    assert decision["action"] != ControlAction.CANCEL.value
    assert decision["action"] == ControlAction.DELIVER_PARTIAL.value


def test_timeout_with_evidence_is_stopped_partial_timeout():
    from app.research.domain.task_state import worker_result_lifecycle

    result = SimpleNamespace(
        ok=False,
        status="partial",
        fail_reason="worker_timeout",
        evidence_refs=["ev_partial"],
        findings=[{"claim": "partial evidence"}],
    )
    execution_status, result_status, stop_reason, failure = worker_result_lifecycle(result)
    assert execution_status.value == "stopped"
    assert result_status.value == "partial"
    assert stop_reason.value == "timeout"
    assert failure["code"] == "worker_timeout"


def test_evidence_never_finishes_with_empty_content():
    from app.research.runtime.graph import synthesize_node

    state = empty_research_state(run_id="r", session_id="s", task_query=LANDSCAPE_QUERY)
    state["research_spec"] = compile_research_spec(LANDSCAPE_QUERY).to_dict()
    state["phase"] = "assess"
    state["evidence_records"] = [{"evidence_id": "ev_1", "authority_score": 0.8}]
    state["claims"] = [{"claim_id": "claim_1", "text": "Company A has funding.", "evidence_ids": ["ev_1"]}]
    state["coverage_state"] = {
        "coverage_ratio": 0.4,
        "missing_ids": ["coverage_missing"],
        "conflicted_ids": [],
    }
    state["evidence_assessment"] = {"status": "partial", "evidence_count": 1}
    update = synthesize_node(state)
    content = str(update["final_content"])
    assert content.strip()
    assert "部分研究结果" in content
    assert "已确认" in content
    assert "证据" in content
    assert "执行限制" in content


def test_semantic_wave_gains_accumulate():
    spec = compile_research_spec(LANDSCAPE_QUERY)
    plan = _discovery_plan()
    state = empty_research_state(run_id="r", session_id="s", task_query=LANDSCAPE_QUERY)
    state.update(
        {
            "research_spec": spec.to_dict(),
            "plan": plan.to_dict(),
            "dispatch_wave_id": 2,
            "tasks": {"t_discovery": {"execution_status": "succeeded"}},
            "semantic_wave_gains": [
                {
                    "wave_id": 1,
                    "coverage_delta": 0.0,
                    "newly_covered_units": 0,
                    "newly_resolved_gaps": 0,
                    "conflicts_resolved": 0,
                    "confidence_delta": 0.0,
                    "new_high_quality_sources": 0,
                    "semantic_gain": 0.0,
                }
            ],
            "worker_results": [
                {
                    "task_id": "t_discovery",
                    "ok": True,
                    "status": "succeeded",
                    "summary": "landscape",
                    "task_metadata": {
                        "task_kind": "discovery",
                        "subject_id": "company_landscape:ai_startup:china",
                        "coverage_keys": ["candidate_set"],
                    },
                    "payload": {
                        "candidates": [{"name": "月之暗面", "confidence": 0.9, "evidence_ids": ["ev_1"]}],
                        "findings": [{"claim": "月之暗面 is an AI startup.", "evidence_ids": ["ev_1"], "confidence": 0.9}],
                        "sources": ["https://example.com/moonshot"],
                        "evidence_ids": ["ev_1"],
                    },
                }
            ],
        }
    )
    update = ingest_semantics(state)
    wave_ids = [int(row["wave_id"]) for row in update["semantic_wave_gains"]]
    assert wave_ids == [1, 2]


def test_landscape_spec_extracts_decision_dimensions():
    spec = compile_research_spec(LANDSCAPE_QUERY)
    dimensions = {dimension.dimension_id for dimension in spec.dimensions}
    assert {
        "candidate_set",
        "technology",
        "commercialization",
        "team",
        "funding",
        "market_position",
        "career_opportunity",
        "risk",
    }.issubset(dimensions)


def test_landscape_query_is_not_whole_subject_name():
    spec = compile_research_spec(LANDSCAPE_QUERY)
    subject = spec.subjects[0]
    assert subject.name != LANDSCAPE_QUERY
    assert subject.subject_type == "company_landscape"
    assert subject.domain == "AI startup"
    assert subject.geography == "China"


def test_trace_failure_origin_and_lineage_are_preserved():
    from app.observability.integrity import check_trace_integrity

    events = [
        {"type": "run.started", "span_id": "research.run", "seq": 1, "attributes": {"search_mode": "agent"}},
        {"type": "plan.created", "span_id": "plan", "parent_span_id": "research.run", "seq": 2},
        {
            "type": "worker.started",
            "span_id": "worker",
            "parent_span_id": "research.run",
            "seq": 3,
            "task_id": "t_discovery",
            "attributes": {"task_id": "t_discovery", "plan_version": 1, "attempt": 1},
        },
        {
            "type": "worker.completed",
            "span_id": "worker",
            "parent_span_id": "research.run",
            "seq": 4,
            "task_id": "t_discovery",
            "attributes": {"task_id": "t_discovery", "evidence_ids": ["ev_1"]},
        },
        {
            "type": "evidence.registered",
            "span_id": "worker",
            "parent_span_id": "research.run",
            "seq": 5,
            "attributes": {"evidence_id": "ev_1", "task_id": "t_discovery"},
        },
        {
            "type": "synthesis.started",
            "span_id": "synthesis",
            "parent_span_id": "research.run",
            "seq": 6,
            "attributes": {"evidence_ids": ["ev_1"]},
        },
        {
            "type": "synthesis.completed",
            "span_id": "synthesis",
            "parent_span_id": "research.run",
            "seq": 7,
            "attributes": {"evidence_ids": ["ev_1"]},
        },
        {
            "type": "quality.assessed",
            "span_id": "quality",
            "parent_span_id": "research.run",
            "seq": 8,
            "attributes": {"passed": True},
        },
        {
            "type": "run.terminated",
            "span_id": "research.run",
            "seq": 9,
            "status": "partial",
            "attributes": {"termination": {"outcome": "partial", "stage": "finalize"}, "final_content_chars": 100},
        },
        {
            "type": "run.failed",
            "span_id": "research.run",
            "seq": 10,
            "status": "partial",
            "attributes": {
                "failure.origin_stage": "plan_validate",
                "termination": {"outcome": "partial", "stage": "finalize"},
                "final_content_chars": 100,
            },
        },
    ]
    result = check_trace_integrity(events, run_status="partial")
    assert result["span_tree"]["root_count"] >= 1
    assert result["lineage_edges"] > 0
    assert result["failure_origin_stage"] == "plan_validate"


def test_ci_references_only_existing_tests_and_gates_real_e2e():
    workflow = Path(".github/workflows/eval-regression.yml").read_text(encoding="utf-8")
    referenced = sorted(set(re.findall(r"(tests/(?:e2e/)?[A-Za-z0-9_./-]+\.py)", workflow)))
    assert referenced, "workflow must reference tests"
    missing = [path for path in referenced if not Path(path).is_file()]
    assert missing == []
    assert "tests/test_architecture_v2_stabilization.py" in workflow
    assert "tests/e2e/" in workflow
