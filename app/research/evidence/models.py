"""Canonical raw-evidence references. Raw content stays outside graph state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class EvidenceRecord:
    evidence_id: str
    source_id: str
    source_kind: str
    locator: str
    retrieved_at: str = ""
    published_at: str = ""
    effective_at: str = ""
    source_tier: str = "SECONDARY"
    authority_score: float = 0.5
    excerpt_ref: str = ""
    artifact_ref: str = ""
    language: str = ""
    task_id: str = ""
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EvidenceRecord":
        row = data or {}
        return cls(
            evidence_id=str(row.get("evidence_id") or ""),
            source_id=str(row.get("source_id") or ""),
            source_kind=str(row.get("source_kind") or "web"),
            locator=str(row.get("locator") or ""),
            retrieved_at=str(row.get("retrieved_at") or ""),
            published_at=str(row.get("published_at") or ""),
            effective_at=str(row.get("effective_at") or ""),
            source_tier=str(row.get("source_tier") or "SECONDARY"),
            authority_score=max(0.0, min(1.0, float(row.get("authority_score") or 0.5))),
            excerpt_ref=str(row.get("excerpt_ref") or ""),
            artifact_ref=str(row.get("artifact_ref") or ""),
            language=str(row.get("language") or ""),
            task_id=str(row.get("task_id") or ""),
            run_id=str(row.get("run_id") or ""),
        )


__all__ = ["EvidenceRecord"]
