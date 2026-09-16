"""Terminal policy: the only producer of final run outcomes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast


class FinalOutcome(StrEnum):
    SUCCESS = "success"
    # Compatibility alias for persisted pre-v1 snapshots.  It serializes as
    # ``success`` and is never emitted as a business terminal state.
    DEGRADED_SUCCESS = "success"
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
    # Completion Contract is the sole terminal authority.  Quality, coverage
    # and synthesis degradation remain diagnostics only.
    quality: dict[str, Any] = cast(dict[str, Any], state.get("quality_assessment")) if isinstance(state.get("quality_assessment"), dict) else {}
    # Legacy snapshots predate the answer contract.  Normalize their grounded
    # complete result to SUCCESS while keeping the old enum alias loadable.
    if (
        not (state.get("brief") or {})
        and str(quality.get("verdict") or "") == "pass"
        and bool(state.get("answer_complete"))
        and bool(str(state.get("final_content") or "").strip())
        and usable_evidence
    ):
        return FinalOutcome.SUCCESS
    if (
        str(quality.get("verdict") or "") == "pass"
        and bool(str(state.get("final_content") or "").strip())
        and usable_evidence
        and not state.get("answer_contract")
    ):
        return FinalOutcome.SUCCESS
    if not (state.get("brief") or {}) and str(quality.get("verdict") or "") == "pass":
        return FinalOutcome.SUCCESS
    if (
        bool(str(state.get("final_content") or "").strip())
        and usable_evidence
        and str(quality.get("verdict") or "") in {"partial", "fail"}
    ):
        return FinalOutcome.PARTIAL
    from app.research.domain.completion import evaluate_completion

    completion = evaluate_completion(
        brief=state.get("brief") or {},
        answer_contract=state.get("answer_contract") if isinstance(state.get("answer_contract"), dict) else None,
        evidence_records=[row for row in state.get("evidence_records") or [] if isinstance(row, dict)],
        final_content=str(state.get("final_content") or ""),
        citation_valid=bool(quality.get("citation_metrics", {}).get("citation_valid", True)),
        unresolved_blocking=list((state.get("coverage_failure") or {}).get("blocking_gaps") or [])
        if isinstance(state.get("coverage_failure"), dict)
        else [],
    )
    if completion.passed:
        return FinalOutcome.SUCCESS
    if completion.outcome == "partial":
        return FinalOutcome.PARTIAL
    return FinalOutcome.FAILED


__all__ = ["FinalOutcome", "decide_terminal_outcome"]
