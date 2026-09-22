"""Terminal policy: the only producer of final run outcomes.

The Completion Contract is the sole terminal authority. Quality, coverage,
synthesis degradation and relevance are inputs to that contract, never
independent shortcuts to SUCCESS or PARTIAL.
"""

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


def _quality(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("quality_assessment")
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def _legacy_snapshot(state: dict[str, Any]) -> bool:
    """Pre-contract snapshots have no brief and no answer contract."""
    return not (state.get("brief") or {}) and not (state.get("answer_contract") or {})


def decide_terminal_outcome(state: dict[str, Any]) -> FinalOutcome:
    if str(state.get("cancel_reason") or "") in {"cancelled", "user_cancelled"}:
        return FinalOutcome.CANCELLED

    quality = _quality(state)
    final_content = str(state.get("final_content") or "").strip()
    evidence_records = [row for row in state.get("evidence_records") or [] if isinstance(row, dict)]
    evidence = state.get("evidence_assessment")
    evidence_status = str(evidence.get("status") or "") if isinstance(evidence, dict) else ""
    usable_evidence = evidence_status in {"partial", "sufficient"} or bool(evidence_records)

    # Legacy snapshots predate both the brief and the answer contract; they can
    # only be graded by the quality verdict that was persisted with them.
    if _legacy_snapshot(state):
        if str(quality.get("verdict") or "") == "pass":
            return FinalOutcome.SUCCESS
        if final_content and usable_evidence:
            return FinalOutcome.PARTIAL
        return FinalOutcome.FAILED

    # Quality Gate evaluates the full Completion Contract with coverage,
    # source-authority, citation and relevance inputs. Reuse that exact result;
    # recomputing here with fewer inputs can incorrectly upgrade FAIL to SUCCESS.
    evaluated = quality.get("completion_contract")
    if isinstance(evaluated, dict) and evaluated:
        if bool(evaluated.get("passed")):
            return FinalOutcome.SUCCESS
        if str(evaluated.get("outcome") or "") == "partial":
            return FinalOutcome.PARTIAL
        return FinalOutcome.FAILED

    from app.research.domain.completion import evaluate_completion

    relevance = state.get("relevance_assessment")
    relevance_row = relevance if isinstance(relevance, dict) else {}
    relevance_pass = bool(relevance_row.get("passed", True))
    # PARTIAL uses the lower relevance floor: on-topic and fact-dense, but not
    # required to answer every ask.
    relevance_partial_pass = bool(relevance_row.get("partial_passed", relevance_pass))
    source_quality = state.get("source_quality")
    source_row = source_quality if isinstance(source_quality, dict) else {}
    source_pass = bool(source_row.get("passed", True))
    source_partial_pass = bool(source_row.get("partial_passed", source_pass))

    completion = evaluate_completion(
        brief=state.get("brief") or {},
        answer_contract=state.get("answer_contract") if isinstance(state.get("answer_contract"), dict) else None,
        evidence_records=evidence_records,
        final_content=final_content,
        citation_valid=bool(quality.get("citation_metrics", {}).get("citation_valid", True)),
        unresolved_blocking=list((state.get("coverage_failure") or {}).get("blocking_gaps") or [])
        if isinstance(state.get("coverage_failure"), dict)
        else [],
        source_quality_pass=source_pass,
        relevance_pass=relevance_pass,
        relevance_partial_pass=relevance_partial_pass,
        source_quality_partial_pass=source_partial_pass,
    )
    if completion.passed:
        return FinalOutcome.SUCCESS
    if completion.outcome == "partial":
        return FinalOutcome.PARTIAL
    return FinalOutcome.FAILED


__all__ = ["FinalOutcome", "decide_terminal_outcome"]
