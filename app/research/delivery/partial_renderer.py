"""Compatibility entry point for the unified AnswerViewModel renderer."""

from __future__ import annotations

import re

from app.research.delivery.answer_renderer import render_answer
from app.research.delivery.view_model import AnswerViewModel


_INTERNAL_ID = re.compile(
    r"\b(?:coverage|gap|claim|finding|evidence|task|worker_result)_[A-Za-z0-9_-]{6,}\b",
    re.IGNORECASE,
)

def scrub_internal_ids(content: str) -> str:
    """Remove machine-only semantic IDs from user-facing delivery."""
    return _INTERNAL_ID.sub("内部记录", str(content or ""))


def render_partial_delivery(
    view_model: AnswerViewModel,
) -> str:
    """Render question-oriented partial delivery; raw research is forbidden."""
    return scrub_internal_ids(render_answer(view_model))


__all__ = ["render_partial_delivery", "scrub_internal_ids"]
