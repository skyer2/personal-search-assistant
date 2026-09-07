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
    if str(state.get("cancel_reason") or state.get("abort_reason") or "") in {"cancelled", "user_cancelled"}:
        return FinalOutcome.CANCELLED
    quality = state.get("quality_assessment")
    if not isinstance(quality, dict):
        return FinalOutcome.FAILED
    verdict = str(quality.get("verdict") or "")
    if verdict == "pass":
        return FinalOutcome.SUCCESS
    evidence = state.get("evidence_assessment")
    evidence_status = str(evidence.get("status") or "") if isinstance(evidence, dict) else ""
    if (
        verdict == "fail"
        and bool(state.get("final_content"))
        and evidence_status in {"partial", "sufficient"}
    ):
        return FinalOutcome.PARTIAL
    return FinalOutcome.FAILED


__all__ = ["FinalOutcome", "decide_terminal_outcome"]
