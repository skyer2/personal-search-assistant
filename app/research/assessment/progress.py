"""Semantic research progress assessment."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class SemanticProgress(StrEnum):
    SUFFICIENT = "sufficient"
    GAP = "gap"
    UNKNOWN = "unknown"


class ProgressAssessment(TypedDict):
    status: str
    coverage_gaps: list[str]
    missing_dimensions: list[str]
    unresolved_conflicts: list[str]
    low_confidence_claims: list[str]
    stale_evidence: list[str]
    unmet_success_criteria: list[str]
    reason_codes: list[str]


def assess_progress(state: dict[str, Any]) -> ProgressAssessment:
    raw = state.get("progress_assessment")
    value = dict(raw) if isinstance(raw, dict) else {}
    status = str(value.get("status") or SemanticProgress.UNKNOWN.value)
    if status not in {item.value for item in SemanticProgress}:
        status = SemanticProgress.UNKNOWN.value
    def strings(key: str) -> list[str]:
        return [str(item) for item in value.get(key) or [] if str(item).strip()]
    return ProgressAssessment(
        status=status,
        coverage_gaps=strings("coverage_gaps"),
        missing_dimensions=strings("missing_dimensions"),
        unresolved_conflicts=strings("unresolved_conflicts"),
        low_confidence_claims=strings("low_confidence_claims"),
        stale_evidence=strings("stale_evidence"),
        unmet_success_criteria=strings("unmet_success_criteria"),
        reason_codes=[str(item) for item in value.get("reason_codes") or [] if str(item).strip()],
    )


__all__ = ["ProgressAssessment", "SemanticProgress", "assess_progress"]
