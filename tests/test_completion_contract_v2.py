from __future__ import annotations

from app.research.control.gap_precheck import precheck_gap
from app.research.domain.completion import evaluate_completion
from app.research.findings.integrity import complete_sentence


def test_blocking_coverage_never_completes() -> None:
    result = evaluate_completion(
        brief={"key_questions": ["Q1"]},
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "answer", "evidence_refs": ["E1"]}]},
        evidence_records=[{"evidence_id": "E1", "source_tier": "PRIMARY", "authority_score": 0.9}],
        final_content="answer [1]",
        coverage={"key_question_coverage": [{"question_id": "q1", "status": "partially_covered", "blocking": True}]},
    )
    assert not result.passed
    assert "q1:blocking_coverage_gap" in result.unresolved_blocking


def test_failed_blocking_worker_requests_targeted_repair() -> None:
    precheck = precheck_gap({
        "dispatch_wave_id": 1,
        "budget": {"max_replan_count": 1},
        "coverage_judgement": {
            "sufficient": False,
            "gaps": [{"gap_id": "gap_q1", "blocking": True}],
            "key_question_coverage": [{"question_id": "q1", "blocking": True, "missing_evidence_types": ["worker_failed"]}],
        },
    })
    assert precheck.action == "TARGETED_RESEARCH"
    assert precheck.blocking_worker_failures == ("q1",)


def test_broken_sentence_is_rejected() -> None:
    assert complete_sentence("目前全球已有超过5") == (False, "dangling_number")
