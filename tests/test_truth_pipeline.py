from types import SimpleNamespace

from app.research.claims.admission import ClaimDraft, admit_claim
from app.research.coverage.judge import judge_coverage
from app.research.delivery.answer_contract import assess_answerability, compile_deterministic_answer
from app.research.delivery.final_renderer import render_final_view
from app.research.delivery.view_builder import build_answer_view
from app.research.evidence.models import EvidenceRecord
from app.research.runtime.worker import salvage_worker_evidence


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


def test_salvage_has_no_finding_projection():
    empty = salvage_worker_evidence(run_id="no-run", task_id="t1", step_index=0)
    assert "findings" not in empty
    assert empty["salvage_evidence"] == []
