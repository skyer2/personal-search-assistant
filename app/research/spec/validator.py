"""Validation for ResearchSpec and its derived success contract."""

from __future__ import annotations

from typing import Any

from datetime import datetime

from app.research.spec.models import ANSWER_SCHEMA_VERSION, AnswerSpec, ResearchSpec


def validate_answer_spec(spec: AnswerSpec | dict[str, Any] | None) -> list[str]:
    try:
        value = spec if isinstance(spec, AnswerSpec) else AnswerSpec.from_dict(spec or {})
    except (TypeError, ValueError):
        return ["contract_invalid"]
    issues: list[str] = []
    if value.schema_version != ANSWER_SCHEMA_VERSION:
        issues.append("invalid_schema_version")
    if not value.spec_id or value.revision < 1 or not value.objective.strip():
        issues.append("invalid_identity")
    try:
        if not value.as_of or datetime.fromisoformat(value.as_of).tzinfo is None:
            issues.append("invalid_as_of")
    except ValueError:
        issues.append("invalid_as_of")
    if not value.timezone or not value.policy_version:
        issues.append("missing_policy_context")
    if not value.asks:
        issues.append("no_asks")
    ask_ids = [ask.ask_id for ask in value.asks]
    question_ids = [ask.question_id for ask in value.asks]
    if len(ask_ids) != len(set(ask_ids)) or len(question_ids) != len(set(question_ids)):
        issues.append("duplicate_ask_identity")
    for ask in value.asks:
        if not ask.ask_id or not ask.question_id or not ask.original_text.strip():
            issues.append(f"invalid_ask_identity:{ask.ask_id or '?'}")
        if not 1 <= ask.min_partial_units <= ask.target_units <= ask.max_units:
            issues.append(f"invalid_unit_bounds:{ask.ask_id}")
        field_ids = [field.field_id for field in ask.required_fields]
        if not field_ids or len(field_ids) != len(set(field_ids)):
            issues.append(f"invalid_required_fields:{ask.ask_id}")
        for requirement in ask.required_fields:
            if not requirement.field_id or not requirement.value_type or not requirement.support_policy:
                issues.append(f"invalid_field:{ask.ask_id}")
    return list(dict.fromkeys(issues))


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
    if value.engine_version == "answer_contract_v2":
        issues.extend(validate_answer_spec(value.answer_spec))
        if value.answer_spec and (
            value.answer_spec.spec_id != value.spec_id
            or value.answer_spec.revision != value.version
        ):
            issues.append("answer_spec_revision_mismatch")
    return issues


__all__ = ["validate_answer_spec", "validate_research_spec"]
