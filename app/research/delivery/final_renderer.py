"""Render typed AnswerViewModel; this module cannot select or invent claims."""

from __future__ import annotations

from app.research.delivery.view_model import AnswerPoint, AnswerViewModel


def _point(point: AnswerPoint, numbers: dict[str, int]) -> str:
    citations = "".join(f"[{numbers[source]}]" for source in point.citation_source_ids if source in numbers)
    return f"{point.text}{citations}".strip()


def render_final_view(view: AnswerViewModel, *, citation_numbers: dict[str, int] | None = None) -> str:
    numbers = citation_numbers or {
        row.source_id: index for index, row in enumerate(view.references, 1)
    }
    lines = ["# 研究结论", ""]
    direct_answers = {
        item.direct_answer.text.strip()
        for item in view.questions
        if item.direct_answer is not None
    }
    if view.summary and view.summary.strip() not in direct_answers:
        lines.extend([view.summary.strip(), ""])
    for item in view.questions:
        lines.extend([f"## {item.title}", ""])
        if item.direct_answer is None:
            lines.extend(["当前证据不足，无法可靠回答这一问题。", ""])
            if item.limitation:
                lines.extend([f"说明：{item.limitation}", ""])
            continue
        lines.extend([f"**回答**：{_point(item.direct_answer, numbers)}", ""])
        if item.reasoning:
            lines.append("**依据**：")
            lines.extend(f"- {_point(point, numbers)}" for point in item.reasoning)
            lines.append("")
        if item.limitation:
            lines.extend([f"**限制与验证：** {item.limitation}", ""])
    if view.limitations:
        lines.extend(["## 主要限制", ""])
        lines.extend(f"- {item}" for item in view.limitations if item.strip())
        lines.append("")
    if view.references:
        lines.extend(["## 参考来源", ""])
        for reference in view.references:
            number = numbers.get(reference.source_id)
            if number is not None:
                lines.append(f"[{number}] {reference.title} — {reference.locator}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


__all__ = ["render_final_view"]
