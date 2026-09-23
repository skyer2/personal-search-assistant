"""Typed, user-facing delivery model.  It contains no artifact or runtime state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


DeliveryKind = Literal["complete", "partial"]
DeliveryStatus = Literal["success", "partial", "failed"]
SectionStatus = Literal["confirmed", "partial", "unresolved"]
EvidenceQuality = Literal["strong", "medium", "weak"]
ReferenceSourceType = Literal[
    "primary", "authoritative_secondary", "secondary", "community", "unknown"
]


@dataclass(frozen=True)
class AnswerPoint:
    text: str
    citation_source_ids: tuple[str, ...] = ()
    # ``citation_refs`` is the canonical public-presentation contract.  Keep
    # the former source-id field during the v6 migration so persisted recovery
    # views remain renderable.
    citation_refs: tuple[str, ...] = ()
    confidence: float = 0.0
    evidence_quality: EvidenceQuality = "weak"

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "citation_source_ids": list(self.citation_source_ids),
            "citation_refs": list(self.citation_refs),
        }


@dataclass(frozen=True)
class QuestionView:
    question_id: str
    title: str
    direct_answer: AnswerPoint | None = None
    reasoning: tuple[AnswerPoint, ...] = ()
    limitation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "title": self.title,
            "direct_answer": self.direct_answer.to_dict() if self.direct_answer else None,
            "reasoning": [item.to_dict() for item in self.reasoning],
            "limitation": self.limitation,
        }


@dataclass(frozen=True)
class ReferenceView:
    source_id: str
    title: str
    locator: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QuestionAnswerSection:
    question_id: str
    question: str
    status: SectionStatus
    answer_points: tuple[AnswerPoint, ...] = ()
    gap_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "answer_points": [item.to_dict() for item in self.answer_points]}


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
    # The first fields are the v6 recovery-view contract.  The latter fields
    # are the canonical answer-presentation contract used by normal delivery.
    objective: str = ""
    kind: DeliveryKind = "partial"
    questions: tuple[QuestionView, ...] = ()
    # The migration accepts both the old recovery reference shape and the
    # canonical presentation reference shape.  Renderers select one contract
    # at a time, so this serialization boundary intentionally remains opaque.
    references: tuple[Any, ...] = ()
    summary: str = ""
    limitations: tuple[str, ...] = ()
    title: str = ""
    status: DeliveryStatus = "partial"
    sections: tuple[QuestionAnswerSection, ...] = ()
    unresolved_items: tuple[UnresolvedItem, ...] = ()
    delivery_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "kind": self.kind,
            "questions": [item.to_dict() for item in self.questions],
            "references": [item.to_dict() for item in self.references],
            "summary": self.summary,
            "limitations": list(self.limitations),
            "title": self.title,
            "status": self.status,
            "sections": [item.to_dict() for item in self.sections],
            "unresolved_items": [item.to_dict() for item in self.unresolved_items],
            "delivery_note": self.delivery_note,
        }


__all__ = [
    "AnswerPoint", "AnswerViewModel", "DeliveryKind", "DeliveryStatus",
    "EvidenceQuality", "QuestionAnswerSection", "QuestionView", "ReferenceEntry",
    "ReferenceSourceType", "ReferenceView", "SectionStatus", "UnresolvedItem",
]
