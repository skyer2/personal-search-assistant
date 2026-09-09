"""Coverage-unit-oriented evidence assessment."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class EvidenceStatus(StrEnum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


class EvidenceAssessment(TypedDict):
    status: str
    evidence_count: int
    trusted_evidence_count: int
    primary_source_count: int
    independent_source_count: int
    supported_claims: int
    unsupported_claims: int
    unresolved_conflicts: list[str]
    stale_sources: list[str]
    reason_codes: list[str]


def assess_evidence(state: dict[str, Any]) -> EvidenceAssessment:
    records = [row for row in state.get("evidence_records") or [] if isinstance(row, dict)]
    claims = [row for row in state.get("claims") or [] if isinstance(row, dict)]
    coverage = state.get("coverage_state") if isinstance(state.get("coverage_state"), dict) else {}
    if not coverage:
        return EvidenceAssessment(
            status=EvidenceStatus.UNKNOWN.value,
            evidence_count=0,
            trusted_evidence_count=0,
            primary_source_count=0,
            independent_source_count=0,
            supported_claims=0,
            unsupported_claims=0,
            unresolved_conflicts=[],
            stale_sources=[],
            reason_codes=["coverage_unknown"],
        )
    evidence_ids = {str(row.get("evidence_id") or "") for row in records}
    supported = [claim for claim in claims if any(str(item) in evidence_ids for item in claim.get("evidence_ids") or [])]
    unsupported = [claim for claim in claims if claim not in supported]
    primary = sum(str(row.get("source_tier") or "") == "PRIMARY" for row in records)
    trusted = sum(float(row.get("authority_score") or 0.0) >= 0.7 for row in records)
    independent = len({str(row.get("source_id") or "") for row in records if row.get("source_id")})
    conflicts = [str(item) for item in coverage.get("conflicted_ids") or []]
    stale = [str(item) for item in coverage.get("stale_ids") or []]
    partial_units = bool(coverage.get("partial_ids"))
    if records and not partial_units and not conflicts and not stale:
        status = EvidenceStatus.SUFFICIENT
        reasons = ["coverage_evidence_sufficient"]
    elif records or partial_units:
        status = EvidenceStatus.PARTIAL
        reasons = ["coverage_evidence_partial"]
    else:
        status = EvidenceStatus.INSUFFICIENT
        reasons = ["no_admitted_evidence"]
    if conflicts:
        reasons.append("unresolved_conflict")
    if stale:
        reasons.append("stale_evidence")
    return EvidenceAssessment(
        status=status,
        evidence_count=len(records),
        trusted_evidence_count=trusted,
        primary_source_count=primary,
        independent_source_count=independent,
        supported_claims=len(supported),
        unsupported_claims=len(unsupported),
        unresolved_conflicts=conflicts,
        stale_sources=stale,
        reason_codes=reasons,
    )


__all__ = ["EvidenceAssessment", "EvidenceStatus", "assess_evidence"]
