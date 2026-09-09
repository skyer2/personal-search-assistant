"""Validation for ResearchSpec and its derived success contract."""

from __future__ import annotations

from typing import Any

from app.research.spec.models import ResearchSpec


def validate_research_spec(spec: ResearchSpec | dict[str, Any] | None) -> list[str]:
    value = spec if isinstance(spec, ResearchSpec) else ResearchSpec.from_dict(spec)
    issues: list[str] = []
    if not value.objective.strip():
        issues.append("empty_objective")
    if not value.subjects:
        issues.append("no_subjects")
    if not value.dimensions:
        issues.append("no_dimensions")
    if not value.success_criteria:
        issues.append("no_success_criteria")
    if any(ambiguity.blocking and not ambiguity.resolution for ambiguity in value.ambiguities):
        issues.append("blocking_ambiguity")
    if any(premise.status == "contradicted" for premise in value.premises):
        issues.append("contradicted_premise")
    if value.evidence_requirements.min_independent_sources < 1:
        issues.append("invalid_source_minimum")
    return issues


__all__ = ["validate_research_spec"]
