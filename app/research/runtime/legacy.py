"""Explicit legacy-role registry after the semantic cutover."""

from __future__ import annotations

from typing import Any

from app.research.brief.models import StructuredResearchBrief
from app.research.spec.models import (
    DeliveryRequirements,
    EvidenceRequirements,
    FreshnessPolicy,
    ResearchDimension,
    ResearchSpec,
    ResearchSubject,
    SourcePolicy,
    SuccessCriterion,
    stable_spec_id,
)


def legacy_registry() -> dict[str, str]:
    return {
        "research_spec": "projection",
        "coverage_contract": "projection",
        "coverage_state": "projection",
        "semantic_gaps": "projection",
        "structured_brief": "canonical",
        "supervisor": "canonical",
        "runtime_policy": "canonical",
    }


def project_research_spec(brief: StructuredResearchBrief) -> ResearchSpec:
    """Project the canonical Brief into the legacy semantic ingest adapter."""
    dimensions = [
        ResearchDimension(f"question_{index}", f"研究问题 {index}", question, True)
        for index, question in enumerate(brief.key_questions, start=1)
    ]
    return ResearchSpec(
        spec_id=stable_spec_id(brief.objective),
        version=brief.version,
        objective=brief.objective,
        subjects=[ResearchSubject("research_objective", brief.objective[:160], "objective")],
        dimensions=dimensions,
        evidence_requirements=EvidenceRequirements(
            min_independent_sources=brief.source_requirements.min_independent_sources,
            prefer_primary=brief.source_requirements.primary_required,
            claim_level_citation=True,
            conflict_resolution_required=brief.user_intent == "conflict_analysis",
            freshness_required=brief.freshness_requirements.required,
            source_diversity_required=brief.source_requirements.min_independent_sources > 1,
        ),
        delivery_requirements=DeliveryRequirements(
            format=brief.deliverable.format,
            depth=brief.deliverable.depth,
        ),
        constraints=[],
        success_criteria=[SuccessCriterion(f"criterion_{index}", item, True) for index, item in enumerate(brief.success_criteria, start=1)],
        task_shape="PROJECTION",
        freshness=FreshnessPolicy(required=brief.freshness_requirements.required),
        source_policy=SourcePolicy(
            allowed=["web"],
            preferred=list(brief.source_requirements.preferred),
            forbidden=list(brief.source_requirements.forbidden),
            require_primary=brief.source_requirements.primary_required,
        ),
        assumptions=list(brief.assumptions),
    )


__all__ = ["legacy_registry", "project_research_spec"]
