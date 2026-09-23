"""One Markdown renderer for success, recovery and partial delivery."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.research.delivery.view_model import AnswerPoint, AnswerViewModel

_CITATION = re.compile(r"\[(\d+)\]")
_REFERENCE_LINE = re.compile(r"^\[(\d+)\]\s", re.MULTILINE)

_STATUS_LABEL = {
    "confirmed": "已有较充分证据",
    "partial": "部分确认",
    "unresolved": "尚未确认",
}
_SOURCE_LABEL = {
    "primary": "一手来源",
    "authoritative_secondary": "权威二手来源",
    "secondary": "二手来源",
    "community": "行业社区来源",
    "unknown": "来源等级未知",
}


@dataclass(frozen=True)
class ReferenceClosure:
    passed: bool
    body_citations: frozenset[int]
    reference_numbers: frozenset[int]
    missing: frozenset[int]
    orphan: frozenset[int]


def _render_point(point: AnswerPoint, number_by_reference: dict[str, int]) -> str:
    numbers = [
        number_by_reference[reference_id]
        for reference_id in point.citation_refs
        if reference_id in number_by_reference
    ]
    markers = "".join(f"[{number}]" for number in dict.fromkeys(numbers))
    text = point.text.rstrip()
    return f"{text}{markers}"


def render_answer(view: AnswerViewModel) -> str:
    number_by_reference = {
        item.reference_id: item.citation_number for item in view.references
    }
    lines = [f"# {view.title}", ""]
    if view.delivery_note:
        lines.extend([f"> {view.delivery_note}", ""])
    for index, section in enumerate(view.sections, 1):
        lines.extend(
            [
                f"## {index}. {section.question}",
                "",
                f"**状态：{_STATUS_LABEL[section.status]}**",
                "",
            ]
        )
        if section.answer_points:
            for point_index, point in enumerate(section.answer_points, 1):
                lines.append(
                    f"{point_index}. {_render_point(point, number_by_reference)}"
                )
        else:
            lines.append("本轮尚未形成可确认的答案。")
        if section.gap_note:
            lines.extend(
                [
                    "",
                    "**仍待确认：**",
                    f"- {section.gap_note}",
                ]
            )
        lines.append("")

    if view.unresolved_items:
        lines.extend(["## 本轮未完成的部分", ""])
        for item in view.unresolved_items:
            prefix = f"**{item.question}**：" if item.question else ""
            lines.append(f"- {prefix}{item.description}")
        lines.append("")

    if view.references:
        lines.extend(["## 参考来源", ""])
        for item in view.references:
            source_note = _SOURCE_LABEL[item.source_type]
            date = f"，{item.published_at}" if item.published_at else ""
            lines.append(
                f"[{item.citation_number}] {item.publisher}，"
                f"《{item.title}》{date}，{item.url}（{source_note}）"
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def collect_body_citations(report: str) -> set[int]:
    body = re.split(
        r"## 参考(?:来源|文献)", str(report or ""), maxsplit=1
    )[0]
    return {int(item) for item in _CITATION.findall(body)}


def collect_reference_numbers(report: str) -> set[int]:
    parts = re.split(
        r"## 参考(?:来源|文献)", str(report or ""), maxsplit=1
    )
    if len(parts) < 2:
        return set()
    return {int(item) for item in _REFERENCE_LINE.findall(parts[1])}


def validate_reference_closure(report: str) -> ReferenceClosure:
    body = frozenset(collect_body_citations(report))
    refs = frozenset(collect_reference_numbers(report))
    missing = frozenset(body - refs)
    orphan = frozenset(refs - body)
    return ReferenceClosure(not missing and not orphan, body, refs, missing, orphan)


__all__ = [
    "ReferenceClosure",
    "collect_body_citations",
    "collect_reference_numbers",
    "render_answer",
    "validate_reference_closure",
]
