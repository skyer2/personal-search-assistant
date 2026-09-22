"""Typed, user-facing delivery model.  It contains no artifact or runtime state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


DeliveryKind = Literal["complete", "partial"]


@dataclass(frozen=True)
class AnswerPoint:
    text: str
    citation_source_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "citation_source_ids": list(self.citation_source_ids)}


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
class AnswerViewModel:
    objective: str
    kind: DeliveryKind
    questions: tuple[QuestionView, ...]
    references: tuple[ReferenceView, ...] = ()
    summary: str = ""
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "kind": self.kind,
            "questions": [item.to_dict() for item in self.questions],
            "references": [item.to_dict() for item in self.references],
            "summary": self.summary,
            "limitations": list(self.limitations),
        }


__all__ = ["AnswerPoint", "AnswerViewModel", "QuestionView", "ReferenceView"]
