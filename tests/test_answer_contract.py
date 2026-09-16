from types import SimpleNamespace

from app.research.delivery.answer_contract import (
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
        },
        {
            "finding_id": "f2",
            "claim": "多家机构将身份权限、审计和可靠性作为 agent 落地重点",
            "evidence_ids": ["e2"],
            "confidence": 0.8,
        },
    ]


def test_deterministic_recovery_answers_every_question_and_marks_forecast():
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
    complete = assess_answer_completeness(final, _brief())
    assert complete.complete
    assert final.answers[0].evidence_refs
    assert final.answers[1].claim_type in {"inference", "forecast"}
    rendered = render_final_answer(final, citation_numbers={"e1": 1, "e2": 2})
    assert rendered.startswith("## 直接回答")
    assert "已有以下信息" not in rendered


def test_answerability_requires_real_evidence_alias():
    result = assess_answerability(
        brief=SimpleNamespace(objective="问题", key_questions=("问题",)),
        findings=[{"finding_id": "f1", "claim": "问题有结论", "evidence_ids": ["fake"]}],
        evidence_records=[{"evidence_id": "real"}],
        coverage={"sufficient": True},
    )
    assert not result.answerable
    assert "supporting_evidence" in result.question_status[0].missing_requirements


def test_complete_deterministic_recovery_is_degraded_success():
    state = {
        "synthesis_degraded": True,
        "answer_complete": True,
        "final_content": "直接回答与依据。",
        "evidence_records": [{"evidence_id": "e1"}],
        "evidence_assessment": {"status": "sufficient"},
        "quality_assessment": {"verdict": "pass"},
    }
    assert decide_terminal_outcome(state) is FinalOutcome.DEGRADED_SUCCESS
