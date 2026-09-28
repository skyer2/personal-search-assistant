"""Deterministic presentation of already validated answer units."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.research.claims.models import ClaimRecord, SupportEdge
from app.research.delivery.unit_models import AnswerUnit
from app.research.spec.models import AnswerSpec


@dataclass(frozen=True)
class RenderedUnitReport:
    content: str
    citation_map: dict[str, dict[str, Any]]
    rendered_unit_ids: tuple[str, ...]


def render_validated_units(
    answer_spec: AnswerSpec,
    units: list[AnswerUnit | dict[str, Any]],
    claims: list[ClaimRecord | dict[str, Any]],
    support_edges: list[SupportEdge | dict[str, Any]],
    evidence_records: list[dict[str, Any]],
    citation_numbers: dict[str, int],
) -> RenderedUnitReport:
    parsed = [item if isinstance(item, AnswerUnit) else AnswerUnit.from_dict(item) for item in units]
    valid = [
        unit for unit in parsed
        if unit.spec_revision == answer_spec.revision and unit.validation.status == "valid"
    ]
    claim_by_id = {
        item.claim_id: item
        for item in (row if isinstance(row, ClaimRecord) else ClaimRecord.from_dict(row) for row in claims)
    }
    edges = [row if isinstance(row, SupportEdge) else SupportEdge.from_dict(row) for row in support_edges]
    evidence_by_id = {
        str(row.get("evidence_id") or ""): row for row in evidence_records if row.get("evidence_id")
    }
    cited_ids: list[str] = []

    def citations(claim_ids: tuple[str, ...]) -> str:
        evidence_ids: list[str] = []
        for claim_id in claim_ids:
            claim = claim_by_id.get(claim_id)
            if claim is None or claim.validation_status != "supported":
                continue
            for edge in edges:
                if edge.claim_id == claim_id and edge.relation == "supports" and edge.evidence_id in citation_numbers:
                    if edge.evidence_id not in evidence_ids:
                        evidence_ids.append(edge.evidence_id)
                    if edge.evidence_id not in cited_ids:
                        cited_ids.append(edge.evidence_id)
        return "".join(f"[{citation_numbers[item]}]" for item in evidence_ids)

    lines = [f"# {answer_spec.objective}", "", f"> 口径：截至 {answer_spec.as_of}（{answer_spec.timezone}）。", ""]
    rendered: list[str] = []
    for ask in answer_spec.asks:
        ask_units = [unit for unit in valid if unit.ask_id == ask.ask_id][: ask.max_units]
        if ask.kind == "recommendation":
            deduped: list[AnswerUnit] = []
            seen: set[str] = set()
            for unit in ask_units:
                key = "|".join(sorted(item.casefold() for item in unit.entity_ids))
                if not key or key in seen:
                    continue
                seen.add(key)
                deduped.append(unit)
            ask_units = deduped
        lines.extend([f"## {ask.original_text}", ""])
        if not ask_units:
            lines.extend(["本次没有通过验证的答案单元，不能可靠作答。", ""])
            continue
        for index, unit in enumerate(ask_units, 1):
            rendered.append(unit.unit_id)
            title_field = unit.fields.get("company") or unit.fields.get("answer") or unit.fields.get("status")
            title = str(title_field.value) if title_field and title_field.value not in (None, "") else (
                unit.entity_ids[0] if unit.entity_ids else f"答案 {index}"
            )
            lines.extend([f"### {index}. {title}", ""])
            for requirement in ask.required_fields:
                field = unit.fields.get(requirement.field_id)
                if field is None or field.value in (None, "", [], {}):
                    if requirement.unknown_allowed:
                        lines.append(f"- **{requirement.field_id}**：未验证")
                    continue
                claim_ids = tuple(dict.fromkeys((*field.claim_ids, *field.premise_claim_ids)))
                refs = citations(claim_ids)
                label = requirement.field_id.replace("_", " ")
                lines.append(f"- **{label}**：{field.value}{refs}")
                if field.rationale:
                    lines.append(f"  - 依据：{field.rationale}")
            lines.append("")
        if len(ask_units) < ask.target_units:
            lines.extend([
                f"> 本次完成 {len(ask_units)}/{ask.target_units} 个经验证答案单元；未完成部分不以未验证材料补齐。",
                "",
            ])
    citation_map = {
        evidence_id: {
            "display_number": citation_numbers[evidence_id],
            "source_metadata": evidence_by_id.get(evidence_id, {}),
        }
        for evidence_id in cited_ids
    }
    if cited_ids:
        lines.extend(["## 参考来源", ""])
        for evidence_id in sorted(cited_ids, key=lambda item: citation_numbers[item]):
            source = evidence_by_id.get(evidence_id, {})
            url = str(source.get("locator") or source.get("canonical_url") or "")
            if not url.startswith(("https://", "http://")):
                continue
            title = str(source.get("title") or source.get("publisher") or source.get("source_id") or url)
            published = str(source.get("published_at") or "")
            date = f"，{published}" if published else ""
            lines.append(f"[{citation_numbers[evidence_id]}] 《{title}》{date}，{url}")
        lines.append("")
    return RenderedUnitReport("\n".join(lines).strip() + "\n", citation_map, tuple(rendered))


__all__ = ["RenderedUnitReport", "render_validated_units"]
