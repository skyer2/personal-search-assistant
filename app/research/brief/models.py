"""Canonical structured research intent models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.research.intent.user_ask import UserAsk


@dataclass(frozen=True)
class SourceRequirements:
    min_independent_sources: int = 1
    primary_required: bool = False
    preferred: tuple[str, ...] = ("official", "primary")
    forbidden: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreshnessRequirements:
    required: bool = False
    time_horizon: str = "any"


@dataclass(frozen=True)
class DeliverableRequirements:
    format: str = "text"
    depth: str = "standard"


@dataclass(frozen=True)
class ResearchQuestion:
    """A researchable question that keeps lineage back to one UserAsk.

    The text may be rewritten for searchability; ``ask_id`` may not be dropped.
    """

    question_id: str
    ask_id: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ResearchQuestion":
        row = data or {}
        return cls(
            question_id=str(row.get("question_id") or ""),
            ask_id=str(row.get("ask_id") or ""),
            text=str(row.get("text") or ""),
        )


@dataclass(frozen=True)
class StructuredResearchBrief:
    brief_id: str
    version: int
    objective: str
    user_intent: str
    explicit_subjects: tuple[str, ...] = ()
    key_questions: tuple[str, ...] = ()
    user_asks: tuple[UserAsk, ...] = ()
    research_questions: tuple[ResearchQuestion, ...] = ()
    constraints: tuple[str, ...] = ()
    source_requirements: SourceRequirements = field(default_factory=SourceRequirements)
    freshness_requirements: FreshnessRequirements = field(default_factory=FreshnessRequirements)
    deliverable: DeliverableRequirements = field(default_factory=DeliverableRequirements)
    success_criteria: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    clarification_needed: bool = False
    compiler_source: str = "deterministic_fallback"
    confidence: float = 0.5
    raw_query: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def required_ask_ids(self) -> tuple[str, ...]:
        return tuple(ask.ask_id for ask in self.user_asks if ask.required and ask.ask_id)

    def ask_id_for_question_index(self, index: int) -> str:
        """Map a 1-based key-question index onto its originating ask."""
        qid = f"q{index}"
        for item in self.research_questions:
            if item.question_id == qid:
                return item.ask_id
        if 0 < index <= len(self.research_questions):
            return self.research_questions[index - 1].ask_id
        return ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StructuredResearchBrief":
        row = data or {}
        source = row.get("source_requirements") if isinstance(row.get("source_requirements"), dict) else {}
        freshness = row.get("freshness_requirements") if isinstance(row.get("freshness_requirements"), dict) else {}
        deliverable = row.get("deliverable") if isinstance(row.get("deliverable"), dict) else {}
        return cls(
            brief_id=str(row.get("brief_id") or ""),
            version=max(1, int(row.get("version") or 1)),
            objective=str(row.get("objective") or ""),
            user_intent=str(row.get("user_intent") or "research"),
            explicit_subjects=tuple(str(item) for item in row.get("explicit_subjects") or [] if str(item).strip()),
            key_questions=tuple(str(item) for item in row.get("key_questions") or [] if str(item).strip()),
            user_asks=tuple(
                UserAsk.from_dict(item)
                for item in row.get("user_asks") or []
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ),
            research_questions=tuple(
                ResearchQuestion.from_dict(item)
                for item in row.get("research_questions") or []
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ),
            constraints=tuple(str(item) for item in row.get("constraints") or [] if str(item).strip()),
            source_requirements=SourceRequirements(
                min_independent_sources=max(1, int(source.get("min_independent_sources") or 1)),
                primary_required=bool(source.get("primary_required")),
                preferred=tuple(str(item) for item in source.get("preferred") or ("official", "primary")),
                forbidden=tuple(str(item) for item in source.get("forbidden") or []),
            ),
            freshness_requirements=FreshnessRequirements(
                required=bool(freshness.get("required")),
                time_horizon=str(freshness.get("time_horizon") or "any"),
            ),
            deliverable=DeliverableRequirements(
                format=str(deliverable.get("format") or "text"),
                depth=str(deliverable.get("depth") or "standard"),
            ),
            success_criteria=tuple(str(item) for item in row.get("success_criteria") or [] if str(item).strip()),
            assumptions=tuple(str(item) for item in row.get("assumptions") or [] if str(item).strip()),
            clarification_needed=bool(row.get("clarification_needed")),
            compiler_source=str(row.get("compiler_source") or "deterministic_fallback"),
            confidence=max(0.0, min(1.0, float(row.get("confidence") or 0.5))),
            raw_query=str(row.get("raw_query") or ""),
        )


@dataclass(frozen=True)
class FastPathEligibility:
    eligible: bool
    reasons: tuple[str, ...] = ()

    @classmethod
    def from_brief(cls, brief: StructuredResearchBrief) -> "FastPathEligibility":
        reasons: list[str] = []
        if not brief.objective.strip():
            reasons.append("empty_objective")
        if brief.user_intent != "atomic_fact":
            reasons.append("not_atomic_fact")
        if len(brief.explicit_subjects) != 1:
            reasons.append("subject_count_not_one")
        if len(brief.key_questions) != 1:
            reasons.append("question_count_not_one")
        if brief.clarification_needed:
            reasons.append("clarification_needed")
        if brief.deliverable.format != "text" or brief.deliverable.depth != "brief":
            reasons.append("deliverable_not_brief_text")
        if brief.freshness_requirements.required and brief.freshness_requirements.time_horizon != "point_in_time":
            reasons.append("freshness_not_point_in_time")
        return cls(eligible=not reasons, reasons=tuple(reasons))


__all__ = [
    "DeliverableRequirements",
    "FastPathEligibility",
    "FreshnessRequirements",
    "ResearchQuestion",
    "SourceRequirements",
    "StructuredResearchBrief",
]
