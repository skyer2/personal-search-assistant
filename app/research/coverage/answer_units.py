"""Field-slot coverage derived only from AnswerSpec and validated AnswerUnits."""

from __future__ import annotations

from typing import Any

from app.research.delivery.unit_models import AnswerUnit
from app.research.spec.models import AnswerSpec


def answer_unit_coverage(answer_spec: AnswerSpec, units: list[AnswerUnit | dict[str, Any]]) -> dict[str, Any]:
    parsed = [item if isinstance(item, AnswerUnit) else AnswerUnit.from_dict(item) for item in units]
    total_slots = 0
    covered_slots = 0
    gaps: list[dict[str, Any]] = []
    per_ask: list[dict[str, Any]] = []
    key_questions: list[dict[str, Any]] = []
    for ask in answer_spec.asks:
        valid = [
            unit for unit in parsed
            if unit.ask_id == ask.ask_id
            and unit.spec_revision == answer_spec.revision
            and unit.validation.status == "valid"
        ]
        if ask.kind == "recommendation":
            identities: set[str] = set()
            deduped: list[AnswerUnit] = []
            for unit in valid:
                identity = "|".join(sorted(item.casefold() for item in unit.entity_ids))
                if not identity or identity in identities:
                    continue
                identities.add(identity)
                deduped.append(unit)
            valid = deduped
        valid = valid[: ask.max_units]
        fields_per_unit = len(ask.required_fields)
        ask_total = ask.target_units * fields_per_unit
        ask_covered = 0
        for slot in range(ask.target_units):
            unit = valid[slot] if slot < len(valid) else None
            for requirement in ask.required_fields:
                total_slots += 1
                satisfied = bool(
                    unit is not None
                    and (
                        requirement.field_id in unit.fields
                        or requirement.unknown_allowed
                    )
                )
                if satisfied:
                    covered_slots += 1
                    ask_covered += 1
                else:
                    gaps.append({
                        "gap_id": f"gap:{ask.ask_id}:slot{slot + 1}:{requirement.field_id}",
                        "ask_id": ask.ask_id,
                        "question_id": ask.question_id,
                        "field_id": requirement.field_id,
                        "slot": slot + 1,
                        "description": f"{ask.ask_id} slot {slot + 1} missing {requirement.field_id}",
                        "missing_evidence_type": [requirement.support_policy],
                        "blocking": ask.required,
                    })
        complete = len(valid) >= ask.target_units and ask_covered == ask_total
        per_ask.append({
            "ask_id": ask.ask_id,
            "valid_units": len(valid),
            "target_units": ask.target_units,
            "covered_required_field_slots": ask_covered,
            "required_field_slots": ask_total,
            "complete": complete,
            "partial_eligible": ask.partial_allowed and len(valid) >= ask.min_partial_units,
        })
        key_questions.append({
            "question_id": ask.question_id,
            "status": "covered" if complete else "partial" if valid else "uncovered",
            "blocking": ask.required and not complete,
            "question": ask.original_text,
            "support_count": len(valid),
            "reason": "target answer units satisfied" if complete else "validated answer-unit slots remain open",
        })
    required_complete = all(
        row["complete"] for row, ask in zip(per_ask, answer_spec.asks) if ask.required
    ) and any(ask.required for ask in answer_spec.asks)
    ratio = covered_slots / total_slots if total_slots else 0.0
    return {
        "schema_version": 2,
        "spec_revision": answer_spec.revision,
        "sufficient": required_complete,
        "status": "sufficient" if required_complete else "gap",
        "coverage_ratio": round(ratio, 4),
        "covered_required_field_slots": covered_slots,
        "required_field_slots": total_slots,
        "per_ask": per_ask,
        "gaps": gaps,
        "missing": [row["description"] for row in gaps],
        "conflicts": [],
        "weak_claims": [],
        "recommended_next_questions": [row["description"] for row in gaps[:4]],
        "key_question_coverage": key_questions,
        "reason": "coverage derived from validated AnswerUnit field slots",
        "source": "answer_contract_v2",
    }


__all__ = ["answer_unit_coverage"]
