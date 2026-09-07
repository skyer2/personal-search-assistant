"""Deterministic rendering for the simple-fact fast path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_FULL_DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})[日]?(?!\d)"
)
_YEAR_PATTERN = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_SENTENCE_PATTERN = re.compile(r"[^。！？.!?]+[。！？.!?]?")


@dataclass(frozen=True)
class SimpleFactAnswer:
    content: str
    source_id: str
    source_tier: str
    sufficient: bool


def _normalize_date(match: re.Match[str]) -> str:
    year, month, day = (int(value) for value in match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValueError("invalid date")
    return f"{year}年{month}月{day}日"


def _subject(query: str) -> str:
    subject = re.sub(r"[?？。！!，,]", " ", query)
    for marker in (
        "发布时间", "首次发布是哪一年", "首次发布", "哪一年发表", "哪年发表",
        "发表", "是什么", "是什么？", "发布", "哪一年", "哪年",
    ):
        subject = subject.replace(marker, " ")
    return " ".join(subject.split()).strip() or "该问题"


def _first_sentence(text: str) -> str:
    for match in _SENTENCE_PATTERN.finditer(text):
        sentence = match.group(0).strip()
        if len(sentence) >= 8:
            return sentence
    return text.strip()


def render_simple_fact_answer(
    *,
    query: str,
    worker_result: Any,
    citation_manager: Any,
) -> SimpleFactAnswer:
    """Render an already-collected fact without invoking a synthesis LLM."""
    sources = list(getattr(citation_manager, "sources", []) or [])
    facts = [str(item) for item in list(worker_result.facts or []) if str(item).strip()]
    sufficient = bool(
        getattr(citation_manager, "simple_fact_evidence_sufficient", lambda: False)()
    )
    if not facts or not sources:
        return SimpleFactAnswer(
            content="未能从可信来源确认该事实。",
            source_id="",
            source_tier="UNKNOWN",
            sufficient=False,
        )

    primary_sources = [
        source for source in sources if str(source.source_tier) == "PRIMARY"
    ]
    bound_primary_sources = [
        source for source in primary_sources if str(source.bound_fact or "").strip()
    ]
    selected = (
        bound_primary_sources[0]
        if bound_primary_sources
        else (primary_sources[0] if primary_sources else sources[0])
    )
    source_number = citation_manager.source_number_map().get(selected.source_id, 1)
    evidence_text = " ".join(facts)

    if "哪年" in query or "哪一年" in query or "发布时间" in query:
        full_date = _FULL_DATE_PATTERN.search(evidence_text)
        if full_date:
            try:
                date = _normalize_date(full_date)
                content = f"{_subject(query)}发布于 {date}。"
            except ValueError:
                content = _first_sentence(facts[0])
        else:
            year = _YEAR_PATTERN.search(evidence_text)
            content = (
                f"{_subject(query)}发布于 {year.group(0)} 年。"
                if year
                else _first_sentence(facts[0])
            )
    elif "是什么" in query or "what is" in query.lower():
        title = str(getattr(worker_result, "summary", "") or "").strip()
        content = f"{_subject(query)}是 {title}。" if title else _first_sentence(facts[0])
    else:
        content = _first_sentence(facts[0])

    content = f"{content}[{source_number}]"
    return SimpleFactAnswer(
        content=content,
        source_id=selected.source_id,
        source_tier=str(selected.source_tier),
        sufficient=sufficient,
    )


class SimpleFactFallbackRenderer:
    """Delivery fallback that never degrades an already-grounded answer."""

    def render(
        self,
        *,
        query: str,
        worker_result: Any,
        citation_manager: Any,
    ) -> SimpleFactAnswer:
        return render_simple_fact_answer(
            query=query,
            worker_result=worker_result,
            citation_manager=citation_manager,
        )


__all__ = [
    "SimpleFactAnswer",
    "SimpleFactFallbackRenderer",
    "render_simple_fact_answer",
]
