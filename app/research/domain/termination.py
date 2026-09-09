"""Terminal policy: the only producer of final run outcomes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class FinalOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


def decide_terminal_outcome(state: dict[str, Any]) -> FinalOutcome:
    if str(state.get("cancel_reason") or "") in {"cancelled", "user_cancelled"}:
        return FinalOutcome.CANCELLED
    evidence = state.get("evidence_assessment")
    evidence_status = str(evidence.get("status") or "") if isinstance(evidence, dict) else ""
    usable_evidence = evidence_status in {"partial", "sufficient"} or bool(
        [row for row in state.get("evidence_records") or [] if isinstance(row, dict)]
    )
    typed_failure = any(
        isinstance(state.get(key), dict) and state.get(key)
        for key in ("planning_failure", "internal_error")
    ) or str(state.get("stop_reason") or "") in {"timeout", "budget"}
    if typed_failure and bool(str(state.get("final_content") or "").strip()) and usable_evidence:
        return FinalOutcome.PARTIAL
    quality = state.get("quality_assessment")
    if not isinstance(quality, dict):
        return FinalOutcome.FAILED
    verdict = str(quality.get("verdict") or "")
    if verdict == "pass":
        decision = state.get("control_decision")
        degraded_delivery = bool(state.get("synthesis_failed")) or (
            isinstance(decision, dict) and str(decision.get("action") or "") == "deliver_partial"
        )
        if degraded_delivery and bool(str(state.get("final_content") or "").strip()) and usable_evidence:
            return FinalOutcome.PARTIAL
        return FinalOutcome.SUCCESS
    if (
        verdict == "fail"
        and bool(state.get("final_content"))
        and usable_evidence
    ):
        return FinalOutcome.PARTIAL
    return FinalOutcome.FAILED


__all__ = ["FinalOutcome", "decide_terminal_outcome"]
