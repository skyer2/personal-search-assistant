"""Report-only repair: fill missing direct answers from existing evidence."""

from __future__ import annotations

from typing import Any

from app.research.delivery.answer_contract import (
    AnswerabilityResult,
    assess_answer_completeness,
    compile_deterministic_answer,
    render_final_answer,
)


def repair_report(
    *, objective: str, brief: Any, findings: list[dict[str, Any]], answerability: AnswerabilityResult,
    citation_numbers: dict[str, int] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Repair answer shape without invoking search, fetch or a worker."""
    answer = compile_deterministic_answer(
        objective=objective, brief=brief, findings=findings,
        answerability=answerability, synthesis_degraded=False,
    )
    completeness = assess_answer_completeness(answer, brief)
    return render_final_answer(answer, citation_numbers=citation_numbers), {
        "answer": answer.to_dict(), "completeness": completeness.to_dict(), "repair_scope": "report_only"
    }


__all__ = ["repair_report"]
