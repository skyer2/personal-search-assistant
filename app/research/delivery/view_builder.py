"""Build a presentation-safe view strictly from admitted claims and evidence."""

from __future__ import annotations

import re
from typing import Any, Literal

from app.research.delivery.answer_contract import FinalAnswer
from app.research.delivery.reference_formatter import reference_views
from app.research.delivery.view_model import AnswerPoint, AnswerViewModel, QuestionView

_PROVIDER_CITATION = re.compile(r"\[\s*\d{1,4}\s*\]")


def _runtime_bound_text(value: str) -> str:
    """Ignore model-selected citation numbers; the renderer binds sources."""
    return _PROVIDER_CITATION.sub("", str(value or "")).strip()


def build_answer_view(
    *,
    answer: FinalAnswer,
    evidence_records: list[dict[str, Any]],
    questions: list[str] | tuple[str, ...] = (),
) -> AnswerViewModel:
    records_by_id = {str(row.get("evidence_id") or ""): row for row in evidence_records if isinstance(row, dict)}
    views: list[QuestionView] = []
    for index, item in enumerate(answer.answers, 1):
        refs = tuple(
            list(dict.fromkeys(
                str(records_by_id[ref].get("source_id") or ref)
                for ref in item.evidence_refs
                if ref in records_by_id
            ))[:3]
        )
        title = item.display_title or (str(questions[index - 1])[:60] if index <= len(questions) else f"关键问题 {index}")
        direct_text = _runtime_bound_text(item.direct_answer)
        if refs and direct_text and not direct_text.startswith("当前证据不足"):
            direct = AnswerPoint(direct_text, refs)
            reasoning = tuple(
                AnswerPoint(cleaned, refs)
                for text in item.reasoning
                if (cleaned := _runtime_bound_text(text))
            )
            views.append(
                QuestionView(
                    item.question_id,
                    title,
                    direct,
                    reasoning,
                    " ".join(value.strip() for value in item.limitations if value.strip()),
                )
            )
        else:
            views.append(QuestionView(item.question_id, title, None, (), "缺少足以直接回答该问题的可发布证据。"))
    used_source_ids = {
        source_id
        for question in views
        for point in (
            *((question.direct_answer,) if question.direct_answer else ()),
            *question.reasoning,
        )
        for source_id in point.citation_source_ids
    }
    all_references = reference_views(evidence_records)
    used_references = tuple(
        reference for reference in all_references if reference.source_id in used_source_ids
    )
    kind: Literal["complete", "partial"] = "complete" if all(item.direct_answer for item in views) else "partial"
    return AnswerViewModel(
        objective=answer.objective,
        kind=kind,
        questions=tuple(views),
        references=used_references,
        summary=answer.overall_summary,
        limitations=tuple(answer.unresolved_questions),
    )


__all__ = ["build_answer_view"]
