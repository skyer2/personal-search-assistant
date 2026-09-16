from types import SimpleNamespace

from app.research.domain.completion import evaluate_completion
from app.research.coverage.gap_check import gap_check
from app.research.planning.bounded import split_task, validate_task


def test_completion_contract_requires_every_question_and_real_refs():
    brief = {"key_questions": ["Q1", "Q2"]}
    result = evaluate_completion(
        brief=brief,
        answer_contract={
            "answers": [
                {"question_id": "q1", "direct_answer": "answer one", "evidence_refs": ["e1"]},
                {"question_id": "q2", "direct_answer": "answer two", "evidence_refs": ["missing"]},
            ]
        },
        evidence_records=[{"evidence_id": "e1"}],
        final_content="report",
    )
    assert not result.passed
    assert result.outcome == "partial"
    assert result.question_results[1].blocking_gap


def test_completion_contract_success_is_independent_of_synthesis_mode():
    result = evaluate_completion(
        brief=SimpleNamespace(key_questions=("Q1",)),
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "grounded answer", "evidence_refs": ["e1"]}]},
        evidence_records=[{"evidence_id": "e1"}],
        final_content="grounded answer",
    )
    assert result.passed
    assert result.outcome == "success"


def test_bounded_planner_rejects_and_splits_large_worker():
    task = SimpleNamespace(
        task_id="t1",
        metadata={"entities": ["a", "b", "c"], "coverage_keys": ["x", "y", "z"], "estimated_queries": 10},
    )
    assert {item.code for item in validate_task(task)} == {"entities_limit", "dimensions_limit", "estimated_queries_limit"}
    parts = split_task(task)
    assert len(parts) >= 2
    assert all(len(item.metadata["entities"]) <= 2 for item in parts)
    assert all(len(item.metadata["coverage_keys"]) <= 2 for item in parts)
    assert all(item.metadata["estimated_queries"] <= 7 for item in parts)


def test_gap_check_is_diagnostic_and_targets_only_missing_questions():
    result = gap_check(
        brief={"key_questions": ["Q1", "Q2"]},
        answerability={
            "question_status": [
                {"question_id": "q1", "answerable": True},
                {"question_id": "q2", "answerable": False, "missing_requirements": ["evidence"]},
            ]
        },
    )
    assert not result.enough_to_answer
    assert result.blocking_gaps == ["q2"]
    assert result.gaps[0].missing_information == "evidence"
