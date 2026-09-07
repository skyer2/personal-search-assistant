"""Evidence quality assessment."""

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
    raw = state.get("evidence_assessment")
    value = dict(raw) if isinstance(raw, dict) else {}
    trusted = int(value.get("trusted_evidence_count") or 0)
    primary = int(value.get("primary_source_count") or 0)
    independent = int(value.get("independent_source_count") or 0)
    conflicts = [str(item) for item in value.get("unresolved_conflicts") or []]
    stale = [str(item) for item in value.get("stale_sources") or []]
    if trusted >= 2 or (primary >= 1 and trusted >= 1):
        status = EvidenceStatus.SUFFICIENT
    elif trusted or primary or independent:
        status = EvidenceStatus.PARTIAL
    else:
        status = EvidenceStatus.INSUFFICIENT
    if conflicts:
        status = EvidenceStatus.PARTIAL if trusted else EvidenceStatus.INSUFFICIENT
    return EvidenceAssessment(
        status=str(value.get("status") or status.value),
        evidence_count=int(value.get("evidence_count") or len(state.get("evidence_refs") or [])),
        trusted_evidence_count=trusted,
        primary_source_count=primary,
        independent_source_count=independent,
        supported_claims=int(value.get("supported_claims") or 0),
        unsupported_claims=int(value.get("unsupported_claims") or 0),
        unresolved_conflicts=conflicts,
        stale_sources=stale,
        reason_codes=[str(item) for item in value.get("reason_codes") or []],
    )


__all__ = ["EvidenceAssessment", "EvidenceStatus", "assess_evidence"]
