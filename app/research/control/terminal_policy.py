"""Terminal decision policy and final termination projection."""

from __future__ import annotations

from typing import Any

from app.research.domain.contracts import OutcomeStatus
from app.research.domain.contracts import RuntimeStatus
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
    runtime_status = RuntimeStatus.CANCELLED.value
    if not str(state.get("cancel_reason") or ""):
        runtime_status = (
            RuntimeStatus.CRASHED.value
            if isinstance(state.get("internal_error"), dict) and state.get("internal_error")
            else RuntimeStatus.FINISHED.value
        )
    quality = state.get("quality_assessment") if isinstance(state.get("quality_assessment"), dict) else {}
    if reason:
        resolved_reason = reason
    elif str(state.get("stop_reason") or "") == "marginal_gain_low":
        resolved_reason = "MARGINAL_GAIN_LOW"
    elif str(state.get("budget_status") or "") == "exhausted" and usable_evidence_like(state):
        resolved_reason = "BUDGET_EXHAUSTED"
    elif str(quality.get("verdict") or "") == "pass":
        resolved_reason = "COVERAGE_SUFFICIENT"
    elif not usable_evidence_like(state):
        resolved_reason = "NO_USABLE_EVIDENCE"
    elif str(quality.get("verdict") or "") in {"partial", "fail"}:
        resolved_reason = "QUALITY_REJECTED"
    else:
        resolved_reason = "INTERNAL_ERROR"
    return {
        "lifecycle": {"status": "terminated"},
        "termination": {
            "runtime_status": runtime_status,
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


def usable_evidence_like(state: dict[str, Any]) -> bool:
    return bool(
        [row for row in state.get("evidence_records") or [] if isinstance(row, dict)]
        or (
            isinstance(state.get("evidence_assessment"), dict)
            and int(state.get("evidence_assessment", {}).get("evidence_count") or 0) > 0
        )
    )


def outcome_value(outcome: FinalOutcome) -> str:
    return OutcomeStatus(outcome.value).value


__all__ = ["outcome_value", "terminal_update"]
