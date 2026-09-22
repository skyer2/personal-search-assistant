"""Build a presentation-safe view strictly from admitted claims and evidence."""

from __future__ import annotations

from typing import Any, Literal

from app.research.delivery.answer_contract import FinalAnswer
from app.research.delivery.reference_formatter import reference_views
from app.research.delivery.view_model import AnswerPoint, AnswerViewModel, QuestionView


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
            dict.fromkeys(
                str(records_by_id[ref].get("source_id") or ref)
                for ref in item.evidence_refs
                if ref in records_by_id
            )
        )
        title = item.display_title or (str(questions[index - 1])[:60] if index <= len(questions) else f"关键问题 {index}")
        if refs and not item.direct_answer.startswith("当前证据不足"):
            direct = AnswerPoint(item.direct_answer.strip(), refs)
            reasoning = tuple(AnswerPoint(text.strip(), refs) for text in item.reasoning if text.strip())
            views.append(QuestionView(item.question_id, title, direct, reasoning))
        else:
            views.append(QuestionView(item.question_id, title, None, (), "缺少足以直接回答该问题的可发布证据。"))
    kind: Literal["complete", "partial"] = "complete" if all(item.direct_answer for item in views) else "partial"
    return AnswerViewModel(
        objective=answer.objective,
        kind=kind,
        questions=tuple(views),
        references=reference_views(evidence_records),
        summary=answer.overall_summary,
        limitations=tuple(answer.unresolved_questions),
    )


__all__ = ["build_answer_view"]
