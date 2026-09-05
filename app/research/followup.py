"""FollowUpResolver：判断追问类型，显式选择相关历史 RunSummary。

跨 Run 上下文只能通过显式 Context Reference 流动：
- standalone        独立新问题，不继承历史
- semantic_followup 语义追问，继承最相关的 1~3 个 RunSummary
- explicit_reference 显式提到"上一轮/刚才那个问题"
- artifact_followup 追问某个历史交付物（如"那份 PDF 的结论"）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.research.run_summary import RunSummary, load_session_run_summaries


class FollowupType(str, Enum):
    STANDALONE = "standalone"
    SEMANTIC_FOLLOWUP = "semantic_followup"
    EXPLICIT_REFERENCE = "explicit_reference"
    ARTIFACT_FOLLOWUP = "artifact_followup"


_EXPLICIT_MARKERS = ("上一轮", "上一次", "刚才", "之前那个", "前面提到", "previous", "above")
_ARTIFACT_MARKERS = ("那份pdf", "那个pdf", "上一份报告", "刚才的报告", "之前生成的", "the pdf", "the report")
_FOLLOWUP_PRONOUNS = ("它", "这个", "那个", "为什么", "呢")
_CJK = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class FollowupContext:
    followup_type: FollowupType
    selected_run_ids: list[str] = field(default_factory=list)
    context_block: str = ""
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "followup_type": self.followup_type.value,
            "selected_run_ids": list(self.selected_run_ids),
            "context_block": self.context_block,
            "confidence": self.confidence,
            "signals": list(self.signals),
        }


def _tokenize(text: str) -> set[str]:
    """CJK 用字符二元组（bigram），拉丁词用原词，保证中文相关性可计算。"""
    tokens: set[str] = set()
    for segment in re.split(r"[\s,，、。？?！!：:；;]+", str(text or "")):
        if not segment:
            continue
        if _CJK.search(segment):
            cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", segment).lower()
            if len(cleaned) == 1:
                tokens.add(cleaned)
            else:
                tokens.update(cleaned[i : i + 2] for i in range(len(cleaned) - 1))
        elif len(segment) >= 2:
            tokens.add(segment.lower())
    return tokens


def _relevance(query_tokens: set[str], summary: RunSummary) -> float:
    haystack = " ".join(
        [
            summary.query,
            summary.intent_summary,
            " ".join(summary.entities),
            " ".join(summary.conclusions),
        ]
    )
    summary_tokens = _tokenize(haystack)
    if not query_tokens or not summary_tokens:
        return 0.0
    overlap = len(query_tokens & summary_tokens)
    return overlap / max(1, len(query_tokens))


def resolve_followup(
    query: str,
    session_dir: Any,
    *,
    current_run_id: str = "",
    limit: int = 3,
) -> FollowupContext:
    """分类追问并选择相关历史 RunSummary，渲染显式上下文块。"""
    q = str(query or "").strip()
    ql = q.lower()
    signals: list[str] = []

    if any(marker in q or marker in ql for marker in _ARTIFACT_MARKERS):
        followup_type = FollowupType.ARTIFACT_FOLLOWUP
        signals.append("artifact_marker")
    elif any(marker in q or marker in ql for marker in _EXPLICIT_MARKERS):
        followup_type = FollowupType.EXPLICIT_REFERENCE
        signals.append("explicit_marker")
    else:
        followup_type = FollowupType.STANDALONE

    summaries = load_session_run_summaries(
        session_dir, limit=max(limit, 8), exclude_run_id=current_run_id
    )
    if not summaries:
        if followup_type != FollowupType.STANDALONE:
            followup_type = FollowupType.STANDALONE
            signals.append("no_history_fallback")
        return FollowupContext(followup_type, signals=signals)

    query_tokens = _tokenize(q)
    scored = [(summary, _relevance(query_tokens, summary)) for summary in summaries]
    best_score = max(score for _, score in scored) if scored else 0.0

    if followup_type == FollowupType.STANDALONE:
        if best_score < 0.25 and not any(p in q for p in _FOLLOWUP_PRONOUNS):
            return FollowupContext(FollowupType.STANDALONE, signals=["low_overlap"])
        followup_type = FollowupType.SEMANTIC_FOLLOWUP
        signals.append("semantic_overlap")

    if followup_type == FollowupType.SEMANTIC_FOLLOWUP:
        selected = [
            summary
            for summary, score in sorted(scored, key=lambda x: x[1], reverse=True)
            if score > 0
        ][: max(1, limit)]
    else:
        selected = summaries[: max(1, limit)]
    if not selected:
        selected = summaries[:1]

    lines: list[str] = []
    for summary in selected:
        lines.append(f"[previous_run {summary.run_id}] query: {summary.query}")
        if summary.intent_summary:
            lines.append(f"  summary: {summary.intent_summary}")
        for conclusion in summary.conclusions[:3]:
            lines.append(f"  conclusion: {conclusion}")
        for question in summary.unresolved_questions[:2]:
            lines.append(f"  unresolved: {question}")
        for artifact in summary.artifact_refs[:3]:
            lines.append(f"  artifact: {artifact}")

    return FollowupContext(
        followup_type=followup_type,
        selected_run_ids=[s.run_id for s in selected],
        context_block="\n".join(lines),
        confidence=min(
            1.0,
            best_score + (0.3 if followup_type != FollowupType.SEMANTIC_FOLLOWUP else 0.0),
        ),
        signals=signals,
    )


__all__ = ["FollowupContext", "FollowupType", "resolve_followup"]
