"""Deterministic assembly of worker candidates into the frozen AnswerSpec shape."""

from __future__ import annotations

import re
from typing import Any

from app.research.delivery.unit_models import AnswerUnit, UnitField
from app.research.spec.models import AnswerSpec, AskSpec


def _value_appears_in_claim(value: Any, claim_text: str) -> bool:
    rendered = str(value or "").strip()
    text = str(claim_text or "")
    if not rendered or not text:
        return False
    if rendered.casefold() in text.casefold():
        return True
    iso_date = re.fullmatch(r"(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])", rendered)
    if iso_date:
        year, month, day = (int(part) for part in iso_date.groups())
        return bool(re.search(rf"{year}\s*年\s*0?{month}\s*月\s*0?{day}\s*日", text))
    return False


def assemble_candidate_unit(
    answer_spec: AnswerSpec,
    ask: AskSpec,
    candidate: dict[str, Any],
    *,
    task_id: str,
    index: int,
    supported_claim_ids: list[str],
    claim_text_by_id: dict[str, str],
) -> AnswerUnit:
    """Project an untrusted candidate onto the immutable required-field set."""
    raw_fields = dict(candidate.get("fields") or {})
    entity_ids = tuple(str(item) for item in candidate.get("entity_ids") or [] if str(item))
    fields: dict[str, UnitField] = {}
    def resolved_claim_id(value: Any) -> str:
        local = str(value or "")
        scoped = local if local.startswith(f"{task_id}:") else f"{task_id}:{local}"
        return scoped if scoped in supported_claim_ids else ""

    for requirement in ask.required_fields:
        raw = raw_fields.get(requirement.field_id)
        if not isinstance(raw, dict):
            continue
        raw_dict = dict(raw)
        value = raw_dict.get("value")
        if requirement.value_type == "entity_list" and entity_ids:
            # An identity list is not a literal web-page substring. Resolve
            # each member independently against admitted claims, then expose
            # only the verified names rather than the model's extra prose.
            member_claims: list[str] = []
            for entity in entity_ids:
                matches = [
                    claim_id for claim_id in supported_claim_ids
                    if _value_appears_in_claim(entity, claim_text_by_id.get(claim_id, ""))
                ]
                if not matches:
                    member_claims = []
                    break
                member_claims.append(matches[0])
            fields[requirement.field_id] = UnitField(
                value=list(entity_ids),
                value_kind=requirement.value_type,
                claim_ids=tuple(dict.fromkeys(member_claims)),
            )
            continue
        claim_ids = tuple(
            scoped for claim_id in raw_dict.get("claim_ids") or []
            if (scoped := resolved_claim_id(claim_id))
            and _value_appears_in_claim(value, claim_text_by_id.get(scoped, ""))
        )
        if not claim_ids:
            # A model can renumber local IDs between findings and its unit.
            # Rebind only to already-admitted claims from this task whose
            # actual text contains the candidate value verbatim.
            claim_ids = tuple(
                claim_id for claim_id in supported_claim_ids
                if _value_appears_in_claim(value, claim_text_by_id.get(claim_id, ""))
            )[:3]
        premise_ids = tuple(
            scoped for claim_id in raw_dict.get("premise_claim_ids") or []
            if (scoped := resolved_claim_id(claim_id))
        )
        if requirement.value_type in {"inference", "forecast"} and not premise_ids:
            # A model may renumber local claims. Ground the inference only in
            # facts already bound to fields of this same candidate unit.
            premise_ids = tuple(dict.fromkeys(
                claim_id
                for previous in fields.values()
                for claim_id in previous.claim_ids
            ))[:3]

        if requirement.value_type in {"inference", "forecast"}:
            fields[requirement.field_id] = UnitField(
                value=value,
                value_kind=requirement.value_type,
                premise_claim_ids=premise_ids,
                rationale=str(raw_dict.get("rationale") or ""),
            )
        elif requirement.value_type == "limitation":
            fields[requirement.field_id] = UnitField(
                value=value,
                value_kind="limitation",
                limitation_status=str(raw_dict.get("limitation_status") or ""),
            )
        else:
            fields[requirement.field_id] = UnitField(
                value=value,
                value_kind=requirement.value_type,
                claim_ids=claim_ids,
            )

    local_unit_id = str(candidate.get("unit_id") or index)
    return AnswerUnit(
        unit_id=f"unit:{task_id}:{local_unit_id}",
        ask_id=ask.ask_id,
        spec_revision=answer_spec.revision,
        kind=ask.kind,
        entity_ids=entity_ids,
        fields=fields,
    )


__all__ = ["assemble_candidate_unit"]
