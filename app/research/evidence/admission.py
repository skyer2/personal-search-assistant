"""Deterministic evidence admission boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.research.evidence.models import EvidenceRecord


@dataclass
class AdmissionResult:
    admitted: list[EvidenceRecord]
    rejected: list[EvidenceRecord]
    reasons: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": [item.to_dict() for item in self.admitted],
            "rejected": [item.to_dict() for item in self.rejected],
            "reasons": dict(self.reasons),
        }


def admit_evidence(
    records: list[Any] | None,
    *,
    allowed_source_kinds: set[str] | None = None,
    require_verified_artifact: bool = False,
) -> AdmissionResult:
    allowed = allowed_source_kinds or {"web", "file", "artifact", "internal"}
    admitted: list[EvidenceRecord] = []
    rejected: list[EvidenceRecord] = []
    reasons: dict[str, str] = {}
    seen: set[str] = set()
    for raw in records or []:
        record = raw if isinstance(raw, EvidenceRecord) else EvidenceRecord.from_dict(raw if isinstance(raw, dict) else None)
        if not record.evidence_id:
            reasons[""] = "missing_evidence_id"
            rejected.append(record)
        elif record.evidence_id in seen:
            reasons[record.evidence_id] = "duplicate_evidence_id"
            rejected.append(record)
        elif not record.locator.strip():
            reasons[record.evidence_id] = "missing_locator"
            rejected.append(record)
        elif record.source_kind not in allowed:
            reasons[record.evidence_id] = "forbidden_source_kind"
            rejected.append(record)
        elif require_verified_artifact and record.source_kind == "artifact":
            reasons[record.evidence_id] = "raw_artifact_is_not_evidence"
            rejected.append(record)
        elif require_verified_artifact and not record.artifact_ref and not record.locator.startswith(("http://", "https://")):
            reasons[record.evidence_id] = "missing_resolvable_source_or_artifact"
            rejected.append(record)
        elif require_verified_artifact and record.source_type in {"community", "unknown"}:
            reasons[record.evidence_id] = "source_quality_below_evidence_minimum"
            rejected.append(record)
        else:
            seen.add(record.evidence_id)
            admitted.append(record)
    return AdmissionResult(admitted=admitted, rejected=rejected, reasons=reasons)


__all__ = ["AdmissionResult", "admit_evidence"]
