"""The sole completion authority for answer-contract v2 runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.research.delivery.unit_models import AnswerUnit
from app.research.spec.models import AnswerSpec

Outcome = Literal["success", "partial", "failed"]


@dataclass(frozen=True)
class AskCompletion:
    ask_id: str
    valid_units: int
    target_units: int
    complete: bool
    partial_eligible: bool
    missing_fields: tuple[str, ...] = ()
    rejected_unit_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompletionResultV2:
    schema_version: int
    spec_revision: int
    evidence_version: str
    answer_version: str
    evaluated_unit_ids: tuple[str, ...]
    per_ask: tuple[AskCompletion, ...]
    outcome: Outcome
    blockers: tuple[str, ...] = ()
    degraded: bool = False
    recovery_mode: str = "none"
    quality_dimensions: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.outcome == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "passed": self.passed,
            "per_ask": [item.to_dict() for item in self.per_ask],
        }


def evaluate_completion_v2(
    answer_spec: AnswerSpec,
    units: list[AnswerUnit | dict[str, Any]],
    *,
    evidence_version: str,
    answer_version: str,
    final_document: str,
    validation_versions: dict[str, str],
    citation_valid: bool | None,
    quality_dimensions: dict[str, str] | None = None,
    degraded: bool = False,
    recovery_mode: str = "none",
) -> CompletionResultV2:
    parsed = [item if isinstance(item, AnswerUnit) else AnswerUnit.from_dict(item) for item in units]
    blockers: list[str] = []
    if validation_versions.get("unit") != "unit-validator-v2":
        blockers.append("unit_validation_version_mismatch")
    if citation_valid is not True:
        blockers.append("citation_validation_unknown" if citation_valid is None else "citation_validation_failed")
    if not final_document.strip():
        blockers.append("final_document_missing")
    dimensions = {
        "citation": "pass" if citation_valid is True else "unknown" if citation_valid is None else "fail",
        **dict(quality_dimensions or {}),
    }
    # Not-run dimensions remain unknown and therefore cannot produce success.
    if any(value != "pass" for value in dimensions.values()):
        blockers.extend(
            f"quality_{key}_{value}"
            for key, value in dimensions.items()
            if value != "pass"
        )
    per_ask: list[AskCompletion] = []
    for ask in answer_spec.asks:
        ask_units = [unit for unit in parsed if unit.ask_id == ask.ask_id]
        revision_units = [unit for unit in ask_units if unit.spec_revision == answer_spec.revision]
        valid_units = [unit for unit in revision_units if unit.validation.status == "valid"]
        if ask.kind == "recommendation":
            seen: set[str] = set()
            deduped: list[AnswerUnit] = []
            for unit in valid_units:
                identity = "|".join(sorted(item.casefold().strip() for item in unit.entity_ids if item.strip()))
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                deduped.append(unit)
            valid_units = deduped
        missing_fields = tuple(
            requirement.field_id
            for requirement in ask.required_fields
            if not requirement.unknown_allowed
            and not any(requirement.field_id in unit.fields for unit in valid_units)
        )
        count = min(len(valid_units), ask.max_units)
        complete = count >= ask.target_units and not missing_fields
        partial_eligible = ask.partial_allowed and count >= ask.min_partial_units
        rejected = tuple(
            unit.unit_id
            for unit in ask_units
            if unit.validation.status != "valid" or unit.spec_revision != answer_spec.revision
        )
        per_ask.append(AskCompletion(
            ask.ask_id,
            count,
            ask.target_units,
            complete,
            partial_eligible,
            missing_fields,
            rejected,
        ))
        if ask.required and not complete:
            blockers.append(f"{ask.ask_id}:target_or_fields_incomplete")
    required = [row for row, ask in zip(per_ask, answer_spec.asks) if ask.required]
    all_complete = bool(required) and all(row.complete for row in required)
    any_partial = any(row.partial_eligible for row in required)
    if all_complete and not blockers:
        outcome: Outcome = "success"
    elif any_partial and final_document.strip():
        outcome = "partial"
    else:
        outcome = "failed"
    return CompletionResultV2(
        schema_version=2,
        spec_revision=answer_spec.revision,
        evidence_version=evidence_version,
        answer_version=answer_version,
        evaluated_unit_ids=tuple(unit.unit_id for unit in parsed),
        per_ask=tuple(per_ask),
        outcome=outcome,
        blockers=tuple(dict.fromkeys(blockers)),
        degraded=degraded,
        recovery_mode=recovery_mode,
        quality_dimensions=dimensions,
    )


__all__ = ["AskCompletion", "CompletionResultV2", "evaluate_completion_v2"]
