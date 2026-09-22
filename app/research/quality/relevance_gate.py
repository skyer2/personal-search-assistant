"""Relevance Gate: did the delivered answer actually answer the user?

Citation and coverage checks can all pass while the report never addresses the
question. This gate blocks two specific failures:

1. a required user ask has no direct answer;
2. the answer is an abstract template with too little concrete fact density.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Hedging/abstract scaffolding that previously came from deterministic templates.
_BOILERPLATE_PATTERNS = (
    re.compile(r"现有研究证据显示"),
    re.compile(r"需要持续验证"),
    re.compile(r"更可能成为"),
    re.compile(r"可观察里程碑"),
    re.compile(r"竞争重点正在从"),
    re.compile(r"从(?:孤立|单点)(?:试验|观察|能力展示)(?:转|走)向"),
    re.compile(r"可验收交付"),
    re.compile(r"正在从.{0,12}转向"),
    re.compile(r"方向性判断"),
    re.compile(r"值得(?:持续)?关注"),
    re.compile(r"总体来看|综上所述|不难看出"),
)

# Concrete, checkable signals: named entities, versions, numbers, dates, links.
_ENTITY = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9.+#-]{2,}(?:\s+[A-Z][A-Za-z0-9.+#-]{1,})?)\b"
)
_VERSIONED = re.compile(r"\b[A-Za-z][A-Za-z0-9.+#-]*\s?\d+(?:\.\d+)*\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?\s*(?:%|亿|万|千|美元|元|倍|个|家|人|token|k|B|M)?")
_DATE = re.compile(r"20\d{2}\s*年?(?:\s*(?:0?[1-9]|1[0-2])\s*月)?|Q[1-4]\s*20\d{2}")
_URL = re.compile(r"https?://\S+")
_CITATION = re.compile(r"\[\d+\]")

_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_HEADING = re.compile(r"^\s*#{1,6}\s")

_MIN_SUBJECT_ALIGNMENT = 0.8
_MIN_SPECIFICITY = 0.35
_MAX_BOILERPLATE = 0.5
# A degraded partial answers fewer asks, but it still may not be boilerplate.
_MIN_PARTIAL_SPECIFICITY = 0.2


def _normalize(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _sentences(content: str) -> list[str]:
    """Prose sentences only; markdown headings are structure, not content."""
    output: list[str] = []
    for line in str(content or "").splitlines():
        if _HEADING.match(line):
            continue
        for item in _SENTENCE.findall(line):
            cleaned = item.strip().lstrip("-*0123456789. ").strip()
            if cleaned:
                output.append(cleaned)
    return output


def boilerplate_ratio(content: str) -> float:
    """Share of sentences that are pure abstract scaffolding."""
    sentences = _sentences(content)
    if not sentences:
        return 0.0
    hits = 0
    for sentence in sentences:
        if not any(pattern.search(sentence) for pattern in _BOILERPLATE_PATTERNS):
            continue
        concrete = (
            _URL.search(sentence)
            or _CITATION.search(sentence)
            or _DATE.search(sentence)
            or _VERSIONED.search(sentence)
        )
        if not concrete:
            hits += 1
    return round(hits / len(sentences), 4)


def specificity_score(content: str) -> float:
    """Share of sentences that carry at least one concrete, checkable signal."""
    sentences = _sentences(content)
    if not sentences:
        return 0.0
    concrete = 0
    for sentence in sentences:
        if (
            _URL.search(sentence)
            or _CITATION.search(sentence)
            or _DATE.search(sentence)
            or _VERSIONED.search(sentence)
            or _ENTITY.search(sentence)
            or _NUMBER.search(sentence)
        ):
            concrete += 1
    return round(concrete / len(sentences), 4)


def _subject_present(subject: str, blob: str) -> bool:
    target = _normalize(subject)
    if not target:
        return True
    if target in blob:
        return True
    chars = [ch for ch in target if ch.isalnum()]
    if not chars:
        return True
    return sum(1 for ch in chars if ch in blob) / len(chars) >= 0.75


@dataclass(frozen=True)
class RelevanceMetrics:
    """Two thresholds: ``passed`` gates SUCCESS, ``partial_passed`` gates PARTIAL."""

    passed: bool
    partial_passed: bool = False
    ask_answer_rate: float = 0.0
    subject_alignment: float = 0.0
    time_scope_alignment: float = 0.0
    unsupported_abstraction_ratio: float = 0.0
    boilerplate_ratio: float = 0.0
    specificity: float = 0.0
    blocking_reasons: tuple[str, ...] = field(default_factory=tuple)
    partial_blocking_reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_relevance(
    *,
    brief: Any,
    answer_contract: dict[str, Any] | None,
    final_content: str,
) -> RelevanceMetrics:
    """Check the delivered answer against the user's asks."""
    content = str(final_content or "")
    blob = _normalize(content)
    asks = [
        item
        for item in (getattr(brief, "user_asks", None) or ())
        if bool(getattr(item, "required", True))
    ]
    rows = []
    raw = (answer_contract or {}).get("answers") or (answer_contract or {}).get("question_answers") or []
    if not raw and isinstance((answer_contract or {}).get("final_answer"), dict):
        nested = (answer_contract or {})["final_answer"]
        raw = nested.get("answers") or nested.get("question_answers") or []
    rows = [item for item in raw if isinstance(item, dict)]

    resolver = getattr(brief, "ask_id_for_question_index", None)
    answered: dict[str, bool] = {}
    for index, row in enumerate(rows, 1):
        qid = str(row.get("question_id") or f"q{index}")
        try:
            position = int(qid.removeprefix("q"))
        except ValueError:
            position = index
        ask_id = str(resolver(position) or "") if callable(resolver) else ""
        direct = str(row.get("direct_answer") or "").strip()
        ok = bool(direct) and "证据不足" not in direct and "无法可靠回答" not in direct
        if ask_id:
            answered[ask_id] = answered.get(ask_id, False) or ok

    reasons: list[str] = []
    if asks:
        answer_rate = sum(1 for ask in asks if answered.get(str(ask.ask_id), False)) / len(asks)
        subject_rate = sum(
            1
            for ask in asks
            if str(getattr(ask, "ask_type", "") or "") == "fact"
            or _subject_present(str(getattr(ask, "subject", "") or ""), blob)
        ) / len(asks)
        scope_rate = sum(
            1
            for ask in asks
            if not str(getattr(ask, "time_scope", "") or "").strip()
            or _subject_present(str(getattr(ask, "time_scope", "") or ""), blob)
        ) / len(asks)
    else:
        answer_rate = 1.0 if rows else 0.0
        subject_rate = 1.0
        scope_rate = 1.0

    ratio = boilerplate_ratio(content)
    specificity = specificity_score(content)

    if answer_rate < 1.0:
        reasons.append("required_ask_without_direct_answer")
    if subject_rate < _MIN_SUBJECT_ALIGNMENT:
        reasons.append("subject_alignment_below_threshold")
    if content and specificity < _MIN_SPECIFICITY:
        reasons.append("low_specificity")
    if content and ratio > _MAX_BOILERPLATE:
        reasons.append("boilerplate_heavy")
    if not content.strip():
        reasons.append("empty_answer")

    # The partial floor drops the "every ask answered" requirement, but keeps
    # the anti-boilerplate and minimum-fact-density rules.
    partial_reasons: list[str] = []
    if not content.strip():
        partial_reasons.append("empty_answer")
    if content and ratio > _MAX_BOILERPLATE:
        partial_reasons.append("boilerplate_heavy")
    if content and specificity < _MIN_PARTIAL_SPECIFICITY:
        partial_reasons.append("low_specificity")

    return RelevanceMetrics(
        passed=not reasons,
        partial_passed=not partial_reasons,
        ask_answer_rate=round(answer_rate, 4),
        subject_alignment=round(subject_rate, 4),
        time_scope_alignment=round(scope_rate, 4),
        unsupported_abstraction_ratio=round(max(0.0, 1.0 - specificity), 4),
        boilerplate_ratio=ratio,
        specificity=specificity,
        blocking_reasons=tuple(reasons),
        partial_blocking_reasons=tuple(partial_reasons),
    )


__all__ = [
    "RelevanceMetrics",
    "boilerplate_ratio",
    "evaluate_relevance",
    "specificity_score",
]
