"""Typed coverage-contract models."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CoverageUnit:
    coverage_id: str
    subject_id: str
    dimension_id: str
    claim_requirement: str
    importance: str = "required"
    min_evidence: int = 1
    min_independent_sources: int = 1
    min_authority_score: float = 0.0
    freshness_policy: dict[str, Any] = field(default_factory=dict)
    conflict_policy: dict[str, Any] = field(default_factory=lambda: {"blocking": True})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CoverageUnit":
        row = data or {}
        return cls(
            coverage_id=str(row.get("coverage_id") or stable_coverage_id(str(row.get("subject_id") or ""), str(row.get("dimension_id") or ""))),
            subject_id=str(row.get("subject_id") or ""),
            dimension_id=str(row.get("dimension_id") or ""),
            claim_requirement=str(row.get("claim_requirement") or ""),
            importance=str(row.get("importance") or "required"),
            min_evidence=max(0, int(row.get("min_evidence") or 1)),
            min_independent_sources=max(0, int(row.get("min_independent_sources") or 1)),
            min_authority_score=max(0.0, min(1.0, float(row.get("min_authority_score") or 0.0))),
            freshness_policy=dict(row.get("freshness_policy") or {}),
            conflict_policy=dict(row.get("conflict_policy") or {"blocking": True}),
        )


@dataclass
class CoverageContract:
    contract_id: str
    spec_id: str
    version: int
    units: list[CoverageUnit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "spec_id": self.spec_id,
            "version": self.version,
            "units": [unit.to_dict() for unit in self.units],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CoverageContract":
        row = data or {}
        return cls(
            contract_id=str(row.get("contract_id") or ""),
            spec_id=str(row.get("spec_id") or ""),
            version=max(1, int(row.get("version") or 1)),
            units=[CoverageUnit.from_dict(item) for item in row.get("units") or [] if isinstance(item, dict)],
        )


@dataclass
class AssessedCoverageUnit:
    coverage_id: str
    subject_id: str
    dimension_id: str
    status: str
    evidence_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason_codes: list[str] = field(default_factory=list)


@dataclass
class CoverageState:
    contract_id: str
    spec_id: str
    units: list[AssessedCoverageUnit] = field(default_factory=list)
    coverage_ratio: float = 0.0
    covered_ids: list[str] = field(default_factory=list)
    partial_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    conflicted_ids: list[str] = field(default_factory=list)
    stale_ids: list[str] = field(default_factory=list)
    waived_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "spec_id": self.spec_id,
            "units": [asdict(unit) for unit in self.units],
            "coverage_ratio": self.coverage_ratio,
            "covered_ids": list(self.covered_ids),
            "partial_ids": list(self.partial_ids),
            "missing_ids": list(self.missing_ids),
            "conflicted_ids": list(self.conflicted_ids),
            "stale_ids": list(self.stale_ids),
            "waived_ids": list(self.waived_ids),
        }


def stable_coverage_id(subject_id: str, dimension_id: str) -> str:
    digest = hashlib.sha1(f"{subject_id}|{dimension_id}".encode("utf-8")).hexdigest()[:12]
    return f"coverage_{digest}"


__all__ = [
    "AssessedCoverageUnit",
    "CoverageContract",
    "CoverageState",
    "CoverageUnit",
    "stable_coverage_id",
]
