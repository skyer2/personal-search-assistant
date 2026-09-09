"""Deterministic coverage assessment from Spec, Claims, and Evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.research.claims.models import ClaimRecord
from app.research.coverage.models import (
    AssessedCoverageUnit,
    CoverageContract,
    CoverageState,
)
from app.research.evidence.models import EvidenceRecord


def _claims(value: Any) -> list[ClaimRecord]:
    if isinstance(value, list):
        return [item if isinstance(item, ClaimRecord) else ClaimRecord.from_dict(item) for item in value if isinstance(item, (dict, ClaimRecord))]
    raw = value.get("claims") if isinstance(value, dict) else []
    return [ClaimRecord.from_dict(item) for item in raw or [] if isinstance(item, dict)]


def _evidence(value: Any) -> list[EvidenceRecord]:
    if isinstance(value, list):
        return [item if isinstance(item, EvidenceRecord) else EvidenceRecord.from_dict(item) for item in value if isinstance(item, (dict, EvidenceRecord))]
    raw = value.get("evidence_records") if isinstance(value, dict) else []
    return [EvidenceRecord.from_dict(item) for item in raw or [] if isinstance(item, dict)]


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _is_fresh(evidence: EvidenceRecord, max_age_days: int | None) -> bool:
    if not max_age_days:
        return True
    stamp = _parse_date(evidence.effective_at or evidence.published_at)
    if stamp is None:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds() / 86400
    return age <= max_age_days and age >= -365


def assess_coverage(
    contract: CoverageContract | dict[str, Any] | None,
    *,
    claims: Any,
    evidence: Any,
    claim_conflicts: list[Any] | None = None,
    claim_resolutions: list[Any] | None = None,
) -> CoverageState:
    value = contract if isinstance(contract, CoverageContract) else CoverageContract.from_dict(contract)
    claim_rows = _claims(claims)
    evidence_rows = _evidence(evidence)
    evidence_by_id = {row.evidence_id: row for row in evidence_rows}
    conflict_claim_ids = {
        claim_id
        for edge in claim_conflicts or []
        for claim_id in (
            str(edge.get("left_id") or "") if isinstance(edge, dict) else str(getattr(edge, "left_id", "")),
            str(edge.get("right_id") or "") if isinstance(edge, dict) else str(getattr(edge, "right_id", "")),
        )
        if claim_id
    }
    resolved_edge_ids = {
        str(row.get("edge_id") or "") if isinstance(row, dict) else str(getattr(row, "edge_id", ""))
        for row in claim_resolutions or []
        if str((row.get("status") if isinstance(row, dict) else getattr(row, "status", "")) or "") in {"resolved", "disclosed"}
    }
    blocking_conflict_ids = {
        claim_id
        for edge in claim_conflicts or []
        if str((edge.get("edge_id") if isinstance(edge, dict) else getattr(edge, "edge_id", "")) or "") not in resolved_edge_ids
        for claim_id in (
            str(edge.get("left_id") or "") if isinstance(edge, dict) else str(getattr(edge, "left_id", "")),
            str(edge.get("right_id") or "") if isinstance(edge, dict) else str(getattr(edge, "right_id", "")),
        )
        if claim_id
    }

    assessed: list[AssessedCoverageUnit] = []
    for unit in value.units:
        supporting = [
            claim
            for claim in claim_rows
            if claim.subject_id == unit.subject_id
            and claim.dimension_id in {unit.dimension_id, "", "key_fact"}
        ]
        evidence_ids = list(dict.fromkeys(item for claim in supporting for item in claim.evidence_ids if item in evidence_by_id))
        source_domains = list(dict.fromkeys(evidence_by_id[item].source_id for item in evidence_ids if evidence_by_id[item].source_id))
        authorities = [float(evidence_by_id[item].authority_score or 0.0) for item in evidence_ids]
        max_authority = max(authorities, default=0.0)
        confidence = max((float(claim.confidence or 0.0) for claim in supporting), default=0.0)
        reasons: list[str] = []
        if not supporting:
            status = "missing"
            reasons.append("no_claim")
        elif bool(blocking_conflict_ids & {claim.claim_id for claim in supporting}):
            status = "conflicted"
            reasons.append("unresolved_conflict")
        elif any(
            not _is_fresh(evidence_by_id[item], unit.freshness_policy.get("max_age_days"))
            for item in evidence_ids
        ) and bool(unit.freshness_policy.get("required")):
            status = "stale"
            reasons.append("stale_evidence")
        elif (
            len(evidence_ids) >= unit.min_evidence
            and len(source_domains) >= unit.min_independent_sources
            and max_authority >= unit.min_authority_score
        ):
            status = "covered"
        else:
            status = "partial"
            if len(evidence_ids) < unit.min_evidence:
                reasons.append("insufficient_evidence")
            if len(source_domains) < unit.min_independent_sources:
                reasons.append("insufficient_independent_sources")
            if max_authority < unit.min_authority_score:
                reasons.append("low_authority")
        assessed.append(
            AssessedCoverageUnit(
                coverage_id=unit.coverage_id,
                subject_id=unit.subject_id,
                dimension_id=unit.dimension_id,
                status=status,
                evidence_ids=evidence_ids,
                claim_ids=[claim.claim_id for claim in supporting],
                confidence=confidence,
                reason_codes=reasons,
            )
        )

    covered = [row.coverage_id for row in assessed if row.status == "covered"]
    partial = [row.coverage_id for row in assessed if row.status == "partial"]
    missing = [row.coverage_id for row in assessed if row.status == "missing"]
    conflicted = [row.coverage_id for row in assessed if row.status == "conflicted"]
    stale = [row.coverage_id for row in assessed if row.status == "stale"]
    waived = [row.coverage_id for row in assessed if row.status == "waived"]
    denominator = max(1, len(assessed))
    ratio = (len(covered) + 0.5 * len(partial)) / denominator
    return CoverageState(
        contract_id=value.contract_id,
        spec_id=value.spec_id,
        units=assessed,
        coverage_ratio=ratio,
        covered_ids=covered,
        partial_ids=partial,
        missing_ids=missing,
        conflicted_ids=conflicted,
        stale_ids=stale,
        waived_ids=waived,
    )


__all__ = ["assess_coverage"]
