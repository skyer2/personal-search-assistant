"""Deterministic AnswerUnit validation against one frozen AnswerSpec revision."""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Sequence
from typing import Any

from app.research.claims.models import ClaimRecord, SupportEdge
from app.research.delivery.unit_models import AnswerUnit, UnitStatus, UnitValidation, ValidationCheck
from app.research.spec.models import AnswerSpec

VALIDATOR_VERSION = "unit-validator-v2"


def validate_answer_units(
    answer_spec: AnswerSpec,
    units: Sequence[AnswerUnit | dict[str, Any]],
    claims: Sequence[ClaimRecord | dict[str, Any]],
    support_edges: Sequence[SupportEdge | dict[str, Any]],
    *,
    evidence_version: str,
) -> list[AnswerUnit]:
    parsed_units = [item if isinstance(item, AnswerUnit) else AnswerUnit.from_dict(item) for item in units]
    parsed_claims = [item if isinstance(item, ClaimRecord) else ClaimRecord.from_dict(item) for item in claims]
    parsed_edges = [item if isinstance(item, SupportEdge) else SupportEdge.from_dict(item) for item in support_edges]
    claim_by_id = {claim.claim_id: claim for claim in parsed_claims if claim.claim_id}
    supporting_claims = {
        edge.claim_id
        for edge in parsed_edges
        if edge.relation == "supports" and edge.evidence_id and edge.validator_version
    }
    ask_by_id = {ask.ask_id: ask for ask in answer_spec.asks}
    validated: list[AnswerUnit] = []
    for unit in parsed_units:
        checks: list[ValidationCheck] = []

        def check(check_id: str, passed: bool | None, *reasons: str) -> None:
            status = "unknown" if passed is None else "pass" if passed else "fail"
            checks.append(ValidationCheck(check_id, status, tuple(reason for reason in reasons if reason)))  # type: ignore[arg-type]

        ask = ask_by_id.get(unit.ask_id)
        check("identity", bool(unit.unit_id and ask), "unknown_ask_or_unit")
        check("spec_revision", unit.spec_revision == answer_spec.revision, "spec_revision_mismatch")
        check("kind", bool(ask and unit.kind == ask.kind), "kind_mismatch")
        if ask is None:
            validated.append(replace(unit, validation=UnitValidation("invalid", VALIDATOR_VERSION, tuple(checks), evidence_version)))
            continue
        for requirement in ask.required_fields:
            field = unit.fields.get(requirement.field_id)
            if field is None:
                check(f"field:{requirement.field_id}", requirement.unknown_allowed, "required_field_missing")
                continue
            value_present = field.value not in (None, "", [], {})
            if not value_present and not requirement.unknown_allowed:
                check(f"field:{requirement.field_id}", False, "required_value_missing")
                continue
            if not value_present and requirement.unknown_allowed:
                check(f"field:{requirement.field_id}", True)
                continue
            fact_claim_ids = tuple(dict.fromkeys(field.claim_ids))
            premise_ids = tuple(dict.fromkeys(field.premise_claim_ids))
            bound_ids = premise_ids if requirement.value_type in {"inference", "forecast"} else fact_claim_ids
            if requirement.value_type == "limitation" and field.limitation_status in {"unknown", "not_verified", "explicit"}:
                check(f"field:{requirement.field_id}", True)
                continue
            supported = bool(bound_ids) and all(
                claim_id in claim_by_id
                and claim_by_id[claim_id].validation_status == "supported"
                and claim_id in supporting_claims
                for claim_id in bound_ids
            )
            check(f"field:{requirement.field_id}", supported, "unsupported_claim_binding")
            if requirement.value_type in {"inference", "forecast"}:
                check(f"rationale:{requirement.field_id}", bool(field.rationale.strip()), "missing_grounded_rationale")
        if ask.kind == "recommendation":
            check("recommendation_entity", len(unit.entity_ids) == 1, "recommendation_requires_one_entity")
            entity = " ".join(unit.entity_ids).casefold()
            check("entity_not_ranking", not any(token in entity for token in ("榜单", "ranking", "top 10", "listicle")), "ranking_is_not_entity")
        if ask.kind == "comparison":
            required_subjects = tuple(str(item).strip().casefold() for item in ask.entity_scope.get("subjects") or [] if str(item).strip())
            if required_subjects:
                delivered_subjects = tuple(str(item).strip().casefold() for item in unit.entity_ids if str(item).strip())
                check(
                    "comparison_subjects",
                    len(delivered_subjects) == len(required_subjects)
                    and set(delivered_subjects) == set(required_subjects),
                    "comparison_subjects_mismatch",
                )
        status: UnitStatus = "valid" if checks and all(item.status == "pass" for item in checks) else (
            "unknown" if checks and not any(item.status == "fail" for item in checks) else "invalid"
        )
        validated.append(replace(unit, validation=UnitValidation(status, VALIDATOR_VERSION, tuple(checks), evidence_version)))
    return validated


__all__ = ["VALIDATOR_VERSION", "validate_answer_units"]
