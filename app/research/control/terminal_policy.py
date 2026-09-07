"""Terminal decision policy and final termination projection."""

from __future__ import annotations

from typing import Any

from app.research.domain.contracts import OutcomeStatus
from app.research.domain.termination import FinalOutcome, decide_terminal_outcome


def terminal_update(
    state: dict[str, Any],
    *,
    reason: str,
    stage: str,
    detected_stage: str = "",
    origin_stage: str = "",
    cause_event_id: str = "",
    research_completed: bool = False,
    synthesis_attempted: bool = False,
    quality_attempted: bool = False,
) -> dict[str, Any]:
    outcome = decide_terminal_outcome(state)
    resolved_reason = reason or outcome.value
    return {
        "lifecycle": {"status": "terminated"},
        "termination": {
            "outcome": outcome.value,
            "reason": resolved_reason,
            "stage": stage,
            "detected_stage": detected_stage or stage,
            "origin_stage": origin_stage or stage,
            "cause_event_id": cause_event_id,
            "research_completed": research_completed,
            "synthesis_attempted": synthesis_attempted,
            "quality_attempted": quality_attempted,
        },
    }


def outcome_value(outcome: FinalOutcome) -> str:
    return OutcomeStatus(outcome.value).value


__all__ = ["outcome_value", "terminal_update"]
