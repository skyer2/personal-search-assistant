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
    supporting_fact: str = ""


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

    # Extraction and citation selection operate on the same bound pair.  Never
    # extract a date from source B and then cite a more authoritative source A.
    candidates = [
        (str(source.bound_fact or "").strip(), source)
        for source in sources
        if str(source.bound_fact or "").strip()
    ]
    if not candidates:
        return SimpleFactAnswer(
            content="未能确认事实与来源的绑定关系。",
            source_id="",
            source_tier="UNKNOWN",
            sufficient=False,
        )

    def extraction(pair: tuple[str, Any]) -> re.Match[str] | None:
        fact, _ = pair
        if "哪年" in query or "哪一年" in query or "发布时间" in query:
            return _FULL_DATE_PATTERN.search(fact) or _YEAR_PATTERN.search(fact)
        return re.match(r".", fact)

    supported = [pair for pair in candidates if extraction(pair)]
    if not supported:
        supported = candidates
    supported.sort(key=lambda pair: 0 if str(pair[1].source_tier) == "PRIMARY" else 1)
    evidence_text, selected = supported[0]
    source_number = citation_manager.source_number_map().get(selected.source_id, 1)

    if "哪年" in query or "哪一年" in query or "发布时间" in query:
        full_date = _FULL_DATE_PATTERN.search(evidence_text)
        if full_date:
            try:
                date = _normalize_date(full_date)
                content = f"{_subject(query)}发布于 {date}。"
            except ValueError:
                content = _first_sentence(evidence_text)
        else:
            year = _YEAR_PATTERN.search(evidence_text)
            content = (
                f"{_subject(query)}发布于 {year.group(0)} 年。"
                if year
                else _first_sentence(evidence_text)
            )
    elif "是什么" in query or "what is" in query.lower():
        content = _first_sentence(evidence_text)
    else:
        content = _first_sentence(evidence_text)

    content = f"{content}[{source_number}]"
    return SimpleFactAnswer(
        content=content,
        source_id=selected.source_id,
        source_tier=str(selected.source_tier),
        sufficient=sufficient,
        supporting_fact=evidence_text,
    )


__all__ = [
    "SimpleFactAnswer",
    "render_simple_fact_answer",
]
