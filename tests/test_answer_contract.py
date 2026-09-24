from types import SimpleNamespace

from app.research.delivery.answer_contract import (
    FinalAnswer,
    QuestionAnswer,
    assess_answer_completeness,
    assess_answerability,
    compile_deterministic_answer,
    render_final_answer,
)
from app.research.domain.termination import FinalOutcome, decide_terminal_outcome


def _brief():
    return SimpleNamespace(
        objective="分析 agent 热点与未来发展方向",
        key_questions=(
            "2026年9月 agent 最新的热点是什么？",
            "你觉得 agent 未来1-2年的发展方向是什么？",
        ),
    )


def _findings():
    return [
        {
            "finding_id": "f1",
            "claim": "2026年9月 agent 热点集中在企业级治理与工具调用",
            "evidence_ids": ["e1"],
            "confidence": 0.9,
            "question_id": "q1",
            "ask_id": "a1",
            "validated": True,
        },
        {
            "finding_id": "f2",
            "claim": "多家机构将身份权限、审计和可靠性作为 agent 落地重点",
            "evidence_ids": ["e2"],
            "confidence": 0.9,
            "question_id": "q2",
            "ask_id": "a2",
            "validated": True,
        },
    ]


def test_evidence_bound_recovery_never_invents_a_forecast():
    """Recovery organizes existing claims; it must not manufacture judgement.

    A forecast question that only has present-tense evidence stays incomplete,
    so a provider failure can never be delivered as a finished answer.
    """
    result = assess_answerability(
        brief=_brief(),
        findings=_findings(),
        evidence_records=[{"evidence_id": "e1"}, {"evidence_id": "e2"}],
        coverage={"sufficient": True},
    )
    assert result.answerable
    final = compile_deterministic_answer(
        objective=_brief().objective,
        brief=_brief(),
        findings=_findings(),
        answerability=result,
    )
    assert final.synthesis_mode == "evidence_bound_recovery"
    assert final.answers[0].evidence_refs
    # No worker claimed a forecast, so recovery must not upgrade the claim type.
    assert final.answers[1].claim_type == "fact"
    complete = assess_answer_completeness(final, _brief())
    assert not complete.complete
    assert "missing_judgement:q2" in complete.issues

    rendered = render_final_answer(final, citation_numbers={"e1": 1, "e2": 2})
    assert rendered.startswith("# 结论摘要")
    assert "直接回答" not in rendered
    for template in (
        "基于当前证据，我判断",
        "可以形成方向性判断",
        "更可能成为可验收交付的一部分",
        "已有以下信息",
    ):
        assert template not in rendered


def test_recovery_keeps_worker_declared_forecast():
    findings = [
        *_findings(),
        {
            "finding_id": "f3",
            "claim": "多家厂商已公布 2027 年 agent runtime 路线图",
            "evidence_ids": ["e2"],
            "claim_type": "forecast",
            "question_ids": ["q2"],
            "confidence": 0.7,
        },
    ]
    brief = _brief()
    result = assess_answerability(
        brief=brief,
        findings=findings,
        evidence_records=[{"evidence_id": "e1"}, {"evidence_id": "e2"}],
        coverage={"sufficient": True},
    )
    final = compile_deterministic_answer(
        objective=brief.objective,
        brief=brief,
        findings=findings,
        answerability=result,
    )
    q2 = next(item for item in final.answers if item.question_id == "q2")
    assert q2.claim_type == "forecast"


def test_recovery_derives_bounded_inference_from_multiple_directional_sources():
    """A provider outage may preserve an explicit, source-bound trend answer."""
    brief = _brief()
    findings = [
        *_findings(),
        {
            "finding_id": "f3",
            "claim": "两家厂商的 2027 路线图都把 agent runtime 可靠性列为下一阶段重点。",
            "evidence_ids": ["e3"],
            "question_id": "q2",
            "ask_id": "a2",
            "validated": True,
            "confidence": 0.85,
        },
    ]
    result = assess_answerability(
        brief=brief,
        findings=findings,
        evidence_records=[{"evidence_id": "e1"}, {"evidence_id": "e2"}, {"evidence_id": "e3"}],
    )
    final = compile_deterministic_answer(
        objective=brief.objective,
        brief=brief,
        findings=findings,
        answerability=result,
    )
    q2 = next(item for item in final.answers if item.question_id == "q2")
    assert q2.claim_type == "inference"
    assert q2.direct_answer.startswith("从本次可绑定来源的共同信号看")
    assert q2.limitations
    assert assess_answer_completeness(final, brief).complete


def test_recovery_refuses_when_no_claim_is_bound():
    brief = SimpleNamespace(objective="问题", key_questions=("问题",))
    result = assess_answerability(
        brief=brief,
        findings=[{"finding_id": "f1", "claim": "结论", "evidence_ids": ["e1"]}],
        evidence_records=[{"evidence_id": "e1"}],
        coverage={"sufficient": True},
    )
    final = compile_deterministic_answer(
        objective="问题",
        brief=brief,
        findings=[],
        answerability=result,
    )
    assert final.answers[0].direct_answer.startswith("当前证据不足")
    assert final.unresolved_questions


def test_answerability_requires_real_evidence_alias():
    result = assess_answerability(
        brief=SimpleNamespace(objective="问题", key_questions=("问题",)),
        findings=[{"finding_id": "f1", "claim": "问题有结论", "evidence_ids": ["fake"]}],
        evidence_records=[{"evidence_id": "real"}],
        coverage={"sufficient": True},
    )
    assert not result.answerable
    assert "supporting_evidence" in result.question_status[0].missing_requirements


def test_complete_delivery_has_the_single_success_outcome():
    state = {
        "synthesis_degraded": True,
        "answer_complete": True,
        "final_content": "直接回答与依据。",
        "evidence_records": [{"evidence_id": "e1"}],
        "evidence_assessment": {"status": "sufficient"},
        "quality_assessment": {"verdict": "pass"},
    }
    assert decide_terminal_outcome(state) is FinalOutcome.SUCCESS


def test_strict_review_failure_cannot_upgrade_complete_contract_to_success():
    state = {
        "brief": {"key_questions": ["Q1"]},
        "answer_contract": {"answers": [{"question_id": "q1", "direct_answer": "Grounded answer", "evidence_refs": ["e1"]}]},
        "quality_assessment": {
            "verdict": "partial",
            "completion_contract": {"passed": False, "outcome": "partial", "failure_reason": "strict_semantic_review_partial"},
        },
        "final_content": "Grounded answer [1]",
        "evidence_records": [{"evidence_id": "e1"}],
    }
    assert decide_terminal_outcome(state) is FinalOutcome.PARTIAL


def test_recovery_answer_limits_body_citations_to_three():
    answer = FinalAnswer(
        objective="Q1",
        answers=[QuestionAnswer(
            question_id="q1",
            direct_answer="A grounded answer",
            evidence_refs=["e1", "e2", "e3", "e4", "e5"],
        )],
        overall_summary="Summary",
        synthesis_mode="evidence_bound_recovery",
        synthesis_degraded=True,
    )
    rendered = render_final_answer(answer, citation_numbers={f"e{i}": i for i in range(1, 6)})
    direct_answer = next(line for line in rendered.splitlines() if line.startswith("**结论**"))
    assert direct_answer.count("[") == 3
