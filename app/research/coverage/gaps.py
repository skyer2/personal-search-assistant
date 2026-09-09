"""Canonical semantic gaps derived from coverage state."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.research.coverage.models import CoverageState

GapType = Literal["coverage", "conflict", "freshness", "premise", "candidate_discovery", "source_quality"]


@dataclass
class SemanticGap:
    gap_id: str
    gap_type: GapType
    subject_id: str
    dimension_id: str
    severity: str
    blocking: bool
    actionable: bool
    coverage_id: str | None = None
    claim_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    attempted_actions: list[str] = field(default_factory=list)
    attempt_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SemanticGap":
        row = data or {}
        return cls(
            gap_id=str(row.get("gap_id") or ""),
            gap_type=str(row.get("gap_type") or "coverage"),  # type: ignore[arg-type]
            subject_id=str(row.get("subject_id") or ""),
            dimension_id=str(row.get("dimension_id") or ""),
            severity=str(row.get("severity") or "normal"),
            blocking=bool(row.get("blocking")),
            actionable=bool(row.get("actionable", True)),
            coverage_id=str(row.get("coverage_id") or "") or None,
            claim_ids=[str(item) for item in row.get("claim_ids") or []],
            evidence_ids=[str(item) for item in row.get("evidence_ids") or []],
            attempted_actions=[str(item) for item in row.get("attempted_actions") or []],
            attempt_count=max(0, int(row.get("attempt_count") or 0)),
        )


def stable_gap_id(subject_id: str, dimension_id: str, gap_type: str) -> str:
    digest = hashlib.sha1(f"{subject_id}|{dimension_id}|{gap_type}".encode("utf-8")).hexdigest()[:12]
    return f"gap_{digest}"


def build_semantic_gaps(
    coverage_state: CoverageState | dict[str, Any] | None,
    *,
    candidate_available: bool = False,
    discovery_required: bool = False,
    previous_gaps: list[Any] | None = None,
) -> list[SemanticGap]:
    value = coverage_state if isinstance(coverage_state, CoverageState) else _state_from_dict(coverage_state)
    previous = {
        str(row.get("gap_id") or "") if isinstance(row, dict) else str(getattr(row, "gap_id", "")): row
        for row in previous_gaps or []
    }
    gaps: list[SemanticGap] = []
    for unit in value.units:
        if unit.status in {"covered", "waived"}:
            continue
        gap_type = "conflict" if unit.status == "conflicted" else "freshness" if unit.status == "stale" else "coverage"
        if unit.status == "partial" and any("independent" in reason for reason in unit.reason_codes):
            gap_type = "source_quality"
        gap = SemanticGap(
            gap_id=stable_gap_id(unit.subject_id, unit.dimension_id, gap_type),
            gap_type=gap_type,  # type: ignore[arg-type]
            subject_id=unit.subject_id,
            dimension_id=unit.dimension_id,
            severity="high" if unit.status in {"conflicted", "missing"} else "normal",
            blocking=True,
            actionable=True,
            coverage_id=unit.coverage_id,
            claim_ids=unit.claim_ids,
            evidence_ids=unit.evidence_ids,
        )
        prior = previous.get(gap.gap_id)
        if isinstance(prior, dict):
            gap.attempted_actions = [str(item) for item in prior.get("attempted_actions") or []]
            gap.attempt_count = int(prior.get("attempt_count") or 0)
        elif prior is not None:
            gap.attempted_actions = list(getattr(prior, "attempted_actions", []) or [])
            gap.attempt_count = int(getattr(prior, "attempt_count", 0) or 0)
        gaps.append(gap)
    if discovery_required and not candidate_available:
        gap = SemanticGap(
            gap_id=stable_gap_id("research", "candidate_set", "candidate_discovery"),
            gap_type="candidate_discovery",
            subject_id="research",
            dimension_id="candidate_set",
            severity="high",
            blocking=True,
            actionable=True,
        )
        gaps.append(gap)
    return gaps


def _state_from_dict(data: dict[str, Any] | None) -> CoverageState:
    row = data or {}
    from app.research.coverage.models import AssessedCoverageUnit

    return CoverageState(
        contract_id=str(row.get("contract_id") or ""),
        spec_id=str(row.get("spec_id") or ""),
        units=[
            AssessedCoverageUnit(
                coverage_id=str(item.get("coverage_id") or ""),
                subject_id=str(item.get("subject_id") or ""),
                dimension_id=str(item.get("dimension_id") or ""),
                status=str(item.get("status") or "missing"),
                evidence_ids=[str(value) for value in item.get("evidence_ids") or []],
                claim_ids=[str(value) for value in item.get("claim_ids") or []],
                confidence=float(item.get("confidence") or 0.0),
                reason_codes=[str(value) for value in item.get("reason_codes") or []],
            )
            for item in row.get("units") or []
            if isinstance(item, dict)
        ],
        coverage_ratio=float(row.get("coverage_ratio") or 0.0),
        covered_ids=[str(item) for item in row.get("covered_ids") or []],
        partial_ids=[str(item) for item in row.get("partial_ids") or []],
        missing_ids=[str(item) for item in row.get("missing_ids") or []],
        conflicted_ids=[str(item) for item in row.get("conflicted_ids") or []],
        stale_ids=[str(item) for item in row.get("stale_ids") or []],
        waived_ids=[str(item) for item in row.get("waived_ids") or []],
    )


__all__ = ["GapType", "SemanticGap", "build_semantic_gaps", "stable_gap_id"]
