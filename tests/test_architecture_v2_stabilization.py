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
from app.research.planning.candidate import Candidate, build_candidate_set, stable_candidate_id
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
    state["phase"] = "coverage_judge"
    state["evidence_records"] = [{"evidence_id": "ev_1", "authority_score": 0.8}]
    state["coverage_judgement"] = {"sufficient": False, "status": "gap", "missing": ["候选池"]}
    state["claims"] = [{"claim_id": "claim_1", "text": "Company A has funding.", "evidence_ids": ["ev_1"]}]
    state["evidence_assessment"] = {"status": "partial", "evidence_count": 1}
    state["control_decision"] = {"action": "deliver_partial"}
    update = synthesize_node(state)
    content = str(update["final_content"])
    assert content.strip()
    assert "Company A has funding." in content
    assert "ev_1" not in content
    assert "部分交付" in content
    assert "证据" in content
    assert "不能视为完整成功" in content


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
