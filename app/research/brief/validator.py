"""Schema-only validation for the canonical research brief."""

from __future__ import annotations

from app.research.brief.models import StructuredResearchBrief

_DELIVERABLE_FORMATS = {"text", "markdown", "pdf"}
_DELIVERABLE_DEPTHS = {"brief", "standard", "long"}


def validate_structured_brief(brief: StructuredResearchBrief) -> list[str]:
    issues: list[str] = []
    if not brief.objective.strip():
        issues.append("empty_objective")
    if not brief.user_intent.strip():
        issues.append("empty_user_intent")
    if not brief.key_questions:
        issues.append("missing_key_questions")
    if len(brief.key_questions) > 8:
        issues.append("too_many_key_questions")
    if not brief.success_criteria:
        issues.append("missing_success_criteria")
    if brief.source_requirements.min_independent_sources < 1:
        issues.append("invalid_source_requirements")
    if brief.deliverable.format not in _DELIVERABLE_FORMATS:
        issues.append("unsupported_deliverable_format")
    if brief.deliverable.depth not in _DELIVERABLE_DEPTHS:
        issues.append("unsupported_deliverable_depth")
    if len(brief.explicit_subjects) > 12:
        issues.append("too_many_explicit_subjects")
    return issues


__all__ = ["validate_structured_brief"]
