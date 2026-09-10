"""Criterion-derived progress assessment."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class SemanticProgress(StrEnum):
    SUFFICIENT = "sufficient"
    GAP = "gap"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ProgressAssessment(TypedDict):
    status: str
    coverage_ratio: float
    covered_ids: list[str]
    partial_ids: list[str]
    missing_ids: list[str]
    semantic_gap_ids: list[str]
    unresolved_conflicts: list[str]
    stale_units: list[str]
    candidate_state: str
    semantic_gain: float
    marginal_gain: float
    reason_codes: list[str]
    plan_version: int


def assess_progress(state: dict[str, Any]) -> ProgressAssessment:
    raw = state.get("coverage_judgement")
    plan_version = int(state.get("plan_version") or 1)
    if not isinstance(raw, dict) or not raw:
        return ProgressAssessment(
            status=SemanticProgress.UNKNOWN.value,
            coverage_ratio=0.0,
            covered_ids=[],
            partial_ids=[],
            missing_ids=[],
            semantic_gap_ids=[],
            unresolved_conflicts=[],
            stale_units=[],
            candidate_state="not_required",
            semantic_gain=0.0,
            marginal_gain=0.0,
            reason_codes=["coverage_judgement_missing"],
            plan_version=plan_version,
        )

    criteria = [
        row for row in raw.get("criteria") or [] if isinstance(row, dict)
    ]
    supported = [
        str(row.get("criterion_id") or "")
        for row in criteria
        if str(row.get("status") or "") == "supported"
    ]
    partial = [
        str(row.get("criterion_id") or "")
        for row in criteria
        if str(row.get("status") or "") == "partial"
    ]
    missing = [
        str(row.get("criterion_id") or "")
        for row in criteria
        if str(row.get("status") or "") in {"unsupported", "conflicted", "indeterminate"}
    ]
    delta = raw.get("delta") if isinstance(raw.get("delta"), dict) else {}
    gain = float(
        len(delta.get("new_evidence_ids") or [])
        + len(delta.get("new_supported_claim_ids") or [])
        + len(delta.get("closed_criterion_ids") or [])
        + len(delta.get("resolved_conflict_ids") or [])
    )
    sufficient = bool(raw.get("sufficient"))
    return ProgressAssessment(
        status=SemanticProgress.SUFFICIENT.value if sufficient else SemanticProgress.GAP.value,
        coverage_ratio=1.0 if sufficient else (
            len(supported) / len(criteria) if criteria else 0.0
        ),
        covered_ids=supported,
        partial_ids=partial,
        missing_ids=missing,
        semantic_gap_ids=[*missing, *partial],
        unresolved_conflicts=[str(item) for item in raw.get("conflicts") or []],
        stale_units=[],
        candidate_state="not_required",
        semantic_gain=gain,
        marginal_gain=gain,
        reason_codes=[] if sufficient else ["coverage_gap"],
        plan_version=plan_version,
    )


__all__ = ["ProgressAssessment", "SemanticProgress", "assess_progress"]
