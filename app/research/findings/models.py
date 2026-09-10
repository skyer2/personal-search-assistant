"""Evidence-backed compressed finding models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ResearchFinding:
    finding_id: str
    task_id: str
    summary: str
    wave_id: int = 0
    supported_criteria: tuple[str, ...] = ()
    target_gaps: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    claims: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    unresolved_questions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ResearchFinding":
        row = data or {}
        return cls(
            finding_id=str(row.get("finding_id") or ""),
            task_id=str(row.get("task_id") or ""),
            summary=str(row.get("summary") or ""),
            wave_id=max(0, int(row.get("wave_id") or 0)),
            supported_criteria=tuple(str(item) for item in row.get("supported_criteria") or []),
            target_gaps=tuple(str(item) for item in row.get("target_gaps") or []),
            claim_ids=tuple(str(item) for item in row.get("claim_ids") or []),
            claims=tuple(str(item) for item in row.get("claims") or []),
            evidence_ids=tuple(str(item) for item in row.get("evidence_ids") or []),
            source_ids=tuple(str(item) for item in row.get("source_ids") or []),
            confidence=max(0.0, min(1.0, float(row.get("confidence") or 0.0))),
            unresolved_questions=tuple(str(item) for item in row.get("unresolved_questions") or []),
            limitations=tuple(str(item) for item in row.get("limitations") or []),
        )


__all__ = ["ResearchFinding"]
