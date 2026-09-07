from __future__ import annotations

import pytest

from app.research.control.terminal_policy import terminal_update
from app.research.domain.termination import FinalOutcome, decide_terminal_outcome


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"cancel_reason": "user_cancelled"}, FinalOutcome.CANCELLED),
        ({"quality_assessment": {"verdict": "pass"}}, FinalOutcome.SUCCESS),
        (
            {
                "quality_assessment": {"verdict": "fail"},
                "final_content": "partial report",
                "evidence_assessment": {"status": "partial"},
            },
            FinalOutcome.PARTIAL,
        ),
        ({"quality_assessment": {"verdict": "fail"}}, FinalOutcome.FAILED),
        ({"quality_assessment": {"verdict": "unknown"}}, FinalOutcome.FAILED),
        ({}, FinalOutcome.FAILED),
    ],
)
def test_terminal_outcome_matrix(state, expected):
    assert decide_terminal_outcome(state) is expected


def test_terminal_update_contains_lifecycle_and_termination():
    update = terminal_update(
        {"quality_assessment": {"verdict": "pass"}},
        reason="completed",
        stage="done",
        research_completed=True,
        synthesis_attempted=True,
        quality_attempted=True,
    )
    assert update["lifecycle"] == {"status": "terminated"}
    assert update["termination"]["outcome"] == "success"
    assert update["termination"]["quality_attempted"] is True


def test_default_failure_reason_is_not_completed():
    update = terminal_update({"quality_assessment": {"verdict": "fail"}}, reason="", stage="done")
    assert update["termination"]["outcome"] == "failed"
    assert update["termination"]["reason"] == "failed"
