from types import SimpleNamespace

from app.research.claims.admission import ClaimDraft, admit_claim
from app.research.coverage.judge import judge_coverage
from app.research.delivery.answer_contract import assess_answerability, compile_deterministic_answer
from app.research.delivery.answer_renderer import validate_reference_closure
from app.research.delivery.final_renderer import render_final_view
from app.research.delivery.view_builder import build_answer_view
from app.research.evidence.models import EvidenceRecord
from app.research.runtime.worker import salvage_worker_evidence
from app.research.brief.compiler import compile_structured_brief
from app.research.coverage.judge import judge_coverage
from app.research.supervisor.agent import SupervisorAgent


def _record() -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="e1", source_id="official.example", source_kind="web",
        locator="https://official.example/release", source_type="primary",
        authority_score=0.92, directness_score=0.90, completeness_score=0.9,
        question_id="q1", ask_id="a1",
    )


def test_navigation_excerpt_is_not_a_publishable_claim():
    result = admit_claim(
        ClaimDraft("d1", "a1", "q1", "t1", "Skip to main Skip to search Subscribe", evidence_refs=["e1"]),
        {"e1": _record()},
    )
    assert not result.admitted
    assert "ui_navigation_text" in result.reasons


def test_claim_requires_question_and_ask_lineage():
    result = admit_claim(
        ClaimDraft("d1", "", "", "t1", "官方公告显示该协议在 2026 年发布。", evidence_refs=["e1"]),
        {"e1": _record()},
    )
    assert not result.admitted
    assert "missing_question_lineage" in result.reasons


def test_answerability_does_not_use_keyword_overlap():
    brief = SimpleNamespace(objective="Agent 研究", key_questions=("Agent 当前热点是什么？",))
    result = assess_answerability(
        brief=brief,
        findings=[{
            "finding_id": "f1", "claim": "Agent 一词出现在网页标题中。", "evidence_ids": ["e1"],
            "validated": True, "question_id": "q2", "ask_id": "a2",
        }],
        evidence_records=[_record().to_dict()],
    )
    assert not result.answerable
    assert result.question_status[0].status == "UNANSWERABLE"


def test_coverage_ignores_unvalidated_claims():
    brief = SimpleNamespace(
        objective="何时发布", key_questions=("何时发布",), success_criteria=(),
        source_requirements=SimpleNamespace(min_independent_sources=1, primary_required=False),
        freshness_requirements=SimpleNamespace(required=False, time_horizon="recent"), user_intent="atomic_fact",
    )
    result = judge_coverage(
        brief, [], claims=[{
            "claim_id": "c1", "text": "产品于 2026 年发布。", "criterion_id": "何时发布",
            "evidence_ids": ["e1"], "validated": False,
        }], evidence=[_record().to_dict()],
    )
    assert not result.sufficient
    assert result.key_question_coverage[0].status == "uncovered"


def test_renderer_only_renders_bound_answer_points():
    brief = SimpleNamespace(objective="问题", key_questions=("问题",))
    claims = [{
        "claim_id": "c1", "text": "官方公告显示产品于 2026 年发布。", "question_id": "q1", "ask_id": "a1",
        "validated": True, "evidence_ids": ["e1"], "confidence": 0.9,
    }]
    answerability = assess_answerability(brief=brief, findings=[], validated_claims=claims, evidence_records=[_record().to_dict()])
    answer = compile_deterministic_answer(objective="问题", brief=brief, findings=claims, answerability=answerability)
    content = render_final_view(build_answer_view(answer=answer, evidence_records=[_record().to_dict()], questions=["问题"]), citation_numbers={"official.example": 1})
    assert "[1]" in content
    assert "https://official.example/release" in content
    assert "Skip to" not in content


def test_recovery_renderer_omits_unreferenced_ledger_sources():
    brief = SimpleNamespace(objective="问题", key_questions=("问题",))
    claims = [{
        "claim_id": "c1", "text": "官方公告显示产品于 2026 年发布。", "question_id": "q1", "ask_id": "a1",
        "validated": True, "evidence_ids": ["e1"], "confidence": 0.9,
    }]
    cited_record = _record().to_dict()
    unused_record = EvidenceRecord(
        evidence_id="e2", source_id="unused.example", source_kind="web",
        locator="https://unused.example/article", source_type="secondary",
        authority_score=0.7, directness_score=0.7, completeness_score=0.7,
        question_id="q1", ask_id="a1",
    ).to_dict()
    answerability = assess_answerability(
        brief=brief, findings=[], validated_claims=claims,
        evidence_records=[cited_record, unused_record],
    )
    answer = compile_deterministic_answer(
        objective="问题", brief=brief, findings=claims, answerability=answerability,
    )
    report = render_final_view(
        build_answer_view(
            answer=answer,
            evidence_records=[cited_record, unused_record],
            questions=["问题"],
        ),
        citation_numbers={"official.example": 1, "unused.example": 2},
    )
    closure = validate_reference_closure(report)
    assert closure.passed
    assert "https://unused.example/article" not in report


def test_salvage_has_no_finding_projection():
    empty = salvage_worker_evidence(run_id="no-run", task_id="t1", step_index=0)
    assert "findings" not in empty
    assert empty["salvage_evidence"] == []


def test_repair_task_preserves_ask_lineage_and_finalize_call_reserve():
    brief = compile_structured_brief(
        "2026年9月 agent 最新热点是什么？未来 1-2 年会如何发展？"
    )
    judgement = judge_coverage(brief, [], claims=[], evidence=[])
    action = SupervisorAgent(agent=None).fallback_action(brief, judgement, {})
    assert action.research_tasks
    for task in action.research_tasks:
        assert task.question_id.startswith("q")
        assert task.ask_id
        assert task.max_llm_calls >= 3


def test_partial_worker_with_admitted_evidence_does_not_create_worker_failed_gap():
    brief = compile_structured_brief("2026年9月 agent 最新的热点是什么？")
    evidence = [_record().to_dict()]
    claims = [{
        "claim_id": "c1",
        "text": "官方公告显示产品于 2026 年发布。",
        "question_id": "q1",
        "ask_id": "A1",
        "validated": True,
        "evidence_ids": ["e1"],
        "confidence": 0.9,
    }]
    judgement = judge_coverage(
        brief,
        [],
        evidence=evidence,
        claims=claims,
        worker_results=[{
            "task_id": "task_1",
            "ok": False,
            "status": "partial",
            "result_status": "partial",
            "task_metadata": {"question_id": "q1"},
            "admitted_evidence_count": 1,
        }],
    )
    row = judgement.key_question_coverage[0]
    assert "worker_failed" not in row.missing_evidence_types
