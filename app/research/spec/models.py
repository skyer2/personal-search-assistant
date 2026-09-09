"""Canonical ResearchSpec and success-contract types."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ResearchSubject:
    subject_id: str
    name: str
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ResearchSubject":
        row = data or {}
        return cls(
            subject_id=str(row.get("subject_id") or ""),
            name=str(row.get("name") or ""),
            aliases=[str(item) for item in row.get("aliases") or []],
        )


@dataclass
class ResearchDimension:
    dimension_id: str
    name: str
    description: str = ""
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ResearchDimension":
        row = data or {}
        return cls(
            dimension_id=str(row.get("dimension_id") or ""),
            name=str(row.get("name") or ""),
            description=str(row.get("description") or ""),
            required=bool(row.get("required", True)),
        )


@dataclass
class ReasoningRequirements:
    lookup: bool = False
    filter: bool = False
    aggregate: bool = False
    compare: bool = False
    multi_hop: bool = False
    discovery: bool = False
    synthesis: bool = True


@dataclass
class EvidenceRequirements:
    min_independent_sources: int = 1
    prefer_primary: bool = True
    claim_level_citation: bool = True
    conflict_resolution_required: bool = False
    freshness_required: bool = False
    source_diversity_required: bool = False


@dataclass
class InteractionRequirements:
    allow_auto_resolve_ambiguity: bool = True
    requires_clarification: bool = False
    supports_followup_delta: bool = False


@dataclass
class DeliveryRequirements:
    format: str = "markdown"
    depth: str = "standard"
    max_length: int = 12000
    citation_style: str = "inline"


@dataclass
class Constraint:
    constraint_id: str
    text: str
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Constraint":
        row = data or {}
        return cls(
            constraint_id=str(row.get("constraint_id") or ""),
            text=str(row.get("text") or ""),
            blocking=bool(row.get("blocking")),
        )


@dataclass
class Premise:
    premise_id: str
    text: str
    status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Premise":
        row = data or {}
        return cls(
            premise_id=str(row.get("premise_id") or ""),
            text=str(row.get("text") or ""),
            status=str(row.get("status") or "unknown"),
        )


@dataclass
class Ambiguity:
    ambiguity_id: str
    text: str
    blocking: bool = False
    resolution: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Ambiguity":
        row = data or {}
        return cls(
            ambiguity_id=str(row.get("ambiguity_id") or ""),
            text=str(row.get("text") or ""),
            blocking=bool(row.get("blocking")),
            resolution=str(row.get("resolution") or ""),
        )


@dataclass
class SuccessCriterion:
    criterion_id: str
    text: str
    measurable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SuccessCriterion":
        row = data or {}
        return cls(
            criterion_id=str(row.get("criterion_id") or ""),
            text=str(row.get("text") or ""),
            measurable=bool(row.get("measurable", True)),
        )


@dataclass
class FreshnessPolicy:
    required: bool = False
    max_age_days: int | None = None
    as_of: str = ""


@dataclass
class SourcePolicy:
    allowed: list[str] = field(default_factory=lambda: ["web", "file"])
    preferred: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    require_primary: bool = False


@dataclass
class ResearchSpec:
    spec_id: str
    version: int
    objective: str
    subjects: list[ResearchSubject] = field(default_factory=list)
    dimensions: list[ResearchDimension] = field(default_factory=list)
    reasoning_requirements: ReasoningRequirements = field(default_factory=ReasoningRequirements)
    evidence_requirements: EvidenceRequirements = field(default_factory=EvidenceRequirements)
    interaction_requirements: InteractionRequirements = field(default_factory=InteractionRequirements)
    delivery_requirements: DeliveryRequirements = field(default_factory=DeliveryRequirements)
    constraints: list[Constraint] = field(default_factory=list)
    premises: list[Premise] = field(default_factory=list)
    ambiguities: list[Ambiguity] = field(default_factory=list)
    success_criteria: list[SuccessCriterion] = field(default_factory=list)
    task_shape: str = "SINGLE_TOPIC_DEEP_DIVE"
    freshness: FreshnessPolicy = field(default_factory=FreshnessPolicy)
    source_policy: SourcePolicy = field(default_factory=SourcePolicy)
    assumptions: list[str] = field(default_factory=list)
    language_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ResearchSpec":
        row = data or {}

        def rows(key: str, cls_type: type) -> list[Any]:
            return [cls_type.from_dict(item) for item in row.get(key) or [] if isinstance(item, dict)]

        return cls(
            spec_id=str(row.get("spec_id") or stable_spec_id(str(row.get("objective") or ""))),
            version=max(1, int(row.get("version") or 1)),
            objective=str(row.get("objective") or ""),
            subjects=rows("subjects", ResearchSubject),
            dimensions=rows("dimensions", ResearchDimension),
            reasoning_requirements=ReasoningRequirements(**{
                key: bool(value)
                for key, value in dict(row.get("reasoning_requirements") or {}).items()
                if key in ReasoningRequirements.__dataclass_fields__
            }),
            evidence_requirements=EvidenceRequirements(**{
                key: value
                for key, value in dict(row.get("evidence_requirements") or {}).items()
                if key in EvidenceRequirements.__dataclass_fields__
            }),
            interaction_requirements=InteractionRequirements(**{
                key: bool(value)
                for key, value in dict(row.get("interaction_requirements") or {}).items()
                if key in InteractionRequirements.__dataclass_fields__
            }),
            delivery_requirements=DeliveryRequirements(**{
                key: value
                for key, value in dict(row.get("delivery_requirements") or {}).items()
                if key in DeliveryRequirements.__dataclass_fields__
            }),
            constraints=rows("constraints", Constraint),
            premises=rows("premises", Premise),
            ambiguities=rows("ambiguities", Ambiguity),
            success_criteria=rows("success_criteria", SuccessCriterion),
            task_shape=str(row.get("task_shape") or "SINGLE_TOPIC_DEEP_DIVE"),
            freshness=FreshnessPolicy(**{
                key: value
                for key, value in dict(row.get("freshness") or {}).items()
                if key in FreshnessPolicy.__dataclass_fields__
            }),
            source_policy=SourcePolicy(**{
                key: value
                for key, value in dict(row.get("source_policy") or {}).items()
                if key in SourcePolicy.__dataclass_fields__
            }),
            assumptions=[str(item) for item in row.get("assumptions") or [] if str(item).strip()],
            language_hints=[str(item) for item in row.get("language_hints") or [] if str(item).strip()],
        )


def stable_spec_id(objective: str) -> str:
    digest = hashlib.sha1(re.sub(r"\s+", " ", objective.strip()).encode("utf-8")).hexdigest()[:12]
    return f"spec_{digest}"


__all__ = [
    "Ambiguity",
    "Constraint",
    "DeliveryRequirements",
    "EvidenceRequirements",
    "FreshnessPolicy",
    "InteractionRequirements",
    "Premise",
    "ReasoningRequirements",
    "ResearchDimension",
    "ResearchSpec",
    "ResearchSubject",
    "SourcePolicy",
    "SuccessCriterion",
    "stable_spec_id",
]
