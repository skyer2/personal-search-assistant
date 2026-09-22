"""User-facing answer presentation contract.

Renderers consume this model only.  Worker, Finding, EvidenceDigest, Artifact
and Gap identifiers are deliberately absent from the presentation boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DeliveryStatus = Literal["success", "partial", "failed"]
SectionStatus = Literal["confirmed", "partial", "unresolved"]
EvidenceQuality = Literal["strong", "medium", "weak"]
ReferenceSourceType = Literal[
    "primary",
    "authoritative_secondary",
    "secondary",
    "community",
    "unknown",
]


@dataclass(frozen=True)
class AnswerPoint:
    text: str
    citation_refs: tuple[str, ...] = ()
    confidence: float = 0.0
    evidence_quality: EvidenceQuality = "weak"

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "citation_refs": list(self.citation_refs),
        }


@dataclass(frozen=True)
class QuestionAnswerSection:
    question_id: str
    question: str
    status: SectionStatus
    answer_points: tuple[AnswerPoint, ...] = ()
    gap_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "answer_points": [item.to_dict() for item in self.answer_points],
        }


@dataclass(frozen=True)
class UnresolvedItem:
    question_id: str
    question: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReferenceEntry:
    reference_id: str
    citation_number: int
    publisher: str
    title: str
    published_at: str | None
    url: str
    source_type: ReferenceSourceType = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnswerViewModel:
    title: str
    status: DeliveryStatus
    sections: tuple[QuestionAnswerSection, ...] = ()
    unresolved_items: tuple[UnresolvedItem, ...] = ()
    references: tuple[ReferenceEntry, ...] = ()
    delivery_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "sections": [item.to_dict() for item in self.sections],
            "unresolved_items": [
                item.to_dict() for item in self.unresolved_items
            ],
            "references": [item.to_dict() for item in self.references],
        }


__all__ = [
    "AnswerPoint",
    "AnswerViewModel",
    "DeliveryStatus",
    "EvidenceQuality",
    "QuestionAnswerSection",
    "ReferenceEntry",
    "ReferenceSourceType",
    "SectionStatus",
    "UnresolvedItem",
]
