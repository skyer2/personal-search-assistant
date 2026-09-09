"""Coverage-derived progress assessment. Task state stays in execution health."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict

from app.research.spec.models import ResearchSpec
from app.research.spec.validator import validate_research_spec


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
    raw_spec = state.get("research_spec")
    raw_coverage = state.get("coverage_state")
    plan_version = int(state.get("plan_version") or 1)
    if not isinstance(raw_spec, dict) or not isinstance(raw_coverage, dict):
        return _assessment(
            SemanticProgress.UNKNOWN,
            0.0,
            [],
            [],
            [],
            [],
            [],
            [],
            "unknown",
            0.0,
            0.0,
            ["missing_spec_or_coverage"],
            plan_version,
        )

    spec = ResearchSpec.from_dict(raw_spec)
    spec_issues = validate_research_spec(spec)
    if spec_issues:
        return _assessment(
            SemanticProgress.BLOCKED,
            0.0,
            [],
            [],
            [],
            [],
            [],
            [],
            "unknown",
            0.0,
            0.0,
            spec_issues,
            plan_version,
        )

    coverage_ratio = float(raw_coverage.get("coverage_ratio") or 0.0)
    covered = [str(item) for item in raw_coverage.get("covered_ids") or []]
    partial = [str(item) for item in raw_coverage.get("partial_ids") or []]
    missing = [str(item) for item in raw_coverage.get("missing_ids") or []]
    conflicts = [str(item) for item in raw_coverage.get("conflicted_ids") or []]
    stale = [str(item) for item in raw_coverage.get("stale_ids") or []]
    gaps = {
        str(gap_id): gap
        for gap_id, gap in (state.get("semantic_gaps") or {}).items()
        if isinstance(gap, dict)
    }
    candidate = state.get("candidate_set") if isinstance(state.get("candidate_set"), dict) else {}
    if candidate and not candidate.get("available"):
        candidate_state = str(candidate.get("status") or "pending")
    elif candidate and not bool(candidate.get("expanded")):
        candidate_state = "ready_not_expanded"
    elif candidate:
        candidate_state = "expanded"
    else:
        candidate_state = "not_required"
    gain = float((state.get("marginal_gain") or {}).get("semantic_gain") or 0.0)
    marginal = float((state.get("marginal_gain") or {}).get("marginal_gain") or gain)

    if not gaps and coverage_ratio >= 1.0:
        status = SemanticProgress.SUFFICIENT
        reasons = ["coverage_sufficient"]
    elif candidate_state == "ready_not_expanded":
        status = SemanticProgress.GAP
        reasons = ["candidate_ready_not_expanded"]
    else:
        status = SemanticProgress.GAP
        reasons = ["semantic_gap"]
    return _assessment(
        status,
        coverage_ratio,
        covered,
        partial,
        missing,
        list(gaps),
        conflicts,
        stale,
        candidate_state,
        gain,
        marginal,
        reasons,
        plan_version,
    )


def _assessment(
    status: SemanticProgress,
    coverage_ratio: float,
    covered: list[str],
    partial: list[str],
    missing: list[str],
    gaps: list[str],
    conflicts: list[str],
    stale: list[str],
    candidate_state: str,
    semantic_gain: float,
    marginal_gain: float,
    reasons: list[str],
    plan_version: int,
) -> ProgressAssessment:
    return ProgressAssessment(
        status=status.value,
        coverage_ratio=coverage_ratio,
        covered_ids=covered,
        partial_ids=partial,
        missing_ids=missing,
        semantic_gap_ids=gaps,
        unresolved_conflicts=conflicts,
        stale_units=stale,
        candidate_state=candidate_state,
        semantic_gain=semantic_gain,
        marginal_gain=marginal_gain,
        reason_codes=reasons,
        plan_version=plan_version,
    )


__all__ = ["ProgressAssessment", "SemanticProgress", "assess_progress"]
