"""
【Phase 14】结构化意图槽位 + 规则置信度 + 歧义检测

个人版：默认 chat 交付；带来源不再自动转 file_md。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

OutputPreference = Literal["auto", "chat", "file_md", "file_pdf"]


@dataclass
class IntentSlots:
    topic: str = ""
    item_count: int | None = None
    require_citations: bool = False
    output_preference: OutputPreference = "auto"
    time_range: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "item_count": self.item_count,
            "require_citations": self.require_citations,
            "output_preference": self.output_preference,
            "time_range": self.time_range,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "IntentSlots":
        if not data:
            return cls()
        pref = str(data.get("output_preference", "auto"))
        if pref not in {"auto", "chat", "file_md", "file_pdf"}:
            pref = "auto"
        item_count = data.get("item_count")
        if item_count is not None:
            try:
                item_count = int(item_count)
            except (TypeError, ValueError):
                item_count = None
        return cls(
            topic=str(data.get("topic") or ""),
            item_count=item_count,
            require_citations=bool(data.get("require_citations", False)),
            output_preference=pref,  # type: ignore[arg-type]
            time_range=str(data.get("time_range") or ""),
            extra={str(k): str(v) for k, v in (data.get("extra") or {}).items()},
        )


_ITEM_COUNT_PATTERN = re.compile(
    r"(?:列出|列举|给出|提供|整理)\s*(\d{1,2})\s*(?:条|项|点|个|种)"
)
_YEAR_PATTERN = re.compile(r"(20\d{2})\s*年?")


def extract_slots(task_query: str) -> IntentSlots:
    q = task_query.strip()
    slots = IntentSlots()
    m = _ITEM_COUNT_PATTERN.search(q)
    if m:
        slots.item_count = int(m.group(1))
    ym = _YEAR_PATTERN.search(q)
    if ym:
        slots.time_range = ym.group(1)
    citation_markers = ("来源", "引用", "链接", "参考文献", "出处", "附来源", "附链接")
    slots.require_citations = any(k in q for k in citation_markers)
    topic = q
    for strip in ("请", "帮我", "使用网络搜索", "检索", "搜索", "查询", "并", "然后"):
        topic = topic.replace(strip, " ")
    topic = re.sub(r"\s+", " ", topic).strip()[:120]
    slots.topic = topic or q[:80]
    return slots


def infer_output_preference(
    query: str,
    *,
    deliverable: str,
    require_citations: bool,
    item_count: int | None,
) -> OutputPreference:
    _ = require_citations, item_count
    if deliverable == "pdf":
        return "file_pdf"
    if deliverable == "md":
        return "file_md"
    if any(k in query for k in ("不要文件", "不需要文件", "只要回答", "简要回答", "对话")):
        return "chat"
    return "auto"


def compute_rule_confidence(
    *,
    query: str,
    needs_network: bool,
    needs_file_read: bool,
    deliverable: str,
    slots: IntentSlots,
    ambiguity_flags: list[str],
) -> float:
    score = 0.55
    if needs_network:
        score += 0.15
    if needs_file_read:
        score += 0.1
    if deliverable in {"md", "pdf"}:
        score += 0.15
    elif deliverable == "text":
        score += 0.1
    if slots.item_count is not None:
        score += 0.05
    if slots.topic and len(slots.topic) >= 4:
        score += 0.05
    for flag in ambiguity_flags:
        if flag == "deliverable_ambiguous":
            score -= 0.22
        elif flag == "output_preference_unclear":
            score -= 0.12
        elif flag == "multi_source":
            score -= 0.05
    return max(0.0, min(1.0, round(score, 3)))


def detect_ambiguity_flags(
    query: str,
    *,
    deliverable: str,
    slots: IntentSlots,
    needs_network: bool,
    needs_file_read: bool,
) -> list[str]:
    flags: list[str] = []
    has_file_hint = any(
        k in query for k in ("markdown", "Markdown", "MD", "md", "PDF", "pdf", "报告", "文件")
    )
    has_chat_hint = any(k in query for k in ("列出", "列举", "简要", "对话", "回答"))
    if has_chat_hint and slots.require_citations and deliverable == "text" and not has_file_hint:
        flags.append("deliverable_ambiguous")
    if slots.output_preference == "auto" and not has_file_hint and has_chat_hint:
        flags.append("output_preference_unclear")
    source_count = sum([needs_network, needs_file_read])
    if source_count >= 2:
        flags.append("multi_source")
    return flags


def build_clarification_question(intent_deliverable: str, flags: list[str], slots: IntentSlots) -> str:
    if "deliverable_ambiguous" in flags or "output_preference_unclear" in flags:
        return (
            "检测到交付形式不明确：您希望我在对话中直接列出带来源的回答，"
            "还是生成可下载的 Markdown 报告文件？"
        )
    if "multi_source" in flags:
        return "本任务涉及网络搜索和附件读取，请确认是否按建议计划继续。"
    if intent_deliverable == "text" and slots.require_citations:
        return "请确认：是否需要在对话回答中附上来源链接？（默认会附上）"
    return "请确认任务理解是否正确，批准后将继续执行。"


def resolve_deliverable_from_slots(slots: IntentSlots, fallback: str) -> str:
    pref = slots.output_preference
    if pref == "file_pdf":
        return "pdf"
    if pref == "file_md":
        return "md"
    if pref == "chat":
        return "text"
    return fallback


def apply_clarification_patch(intent_dict: dict, patch: dict) -> dict:
    merged = dict(intent_dict)
    for key in ("deliverable", "needs_network", "needs_file_read"):
        if key in patch:
            merged[key] = patch[key]
    if "slots" in patch and isinstance(patch["slots"], dict):
        base_slots = IntentSlots.from_dict(merged.get("slots"))
        updated = IntentSlots.from_dict({**base_slots.to_dict(), **patch["slots"]})
        merged["slots"] = updated.to_dict()
        if patch["slots"].get("output_preference"):
            merged["deliverable"] = resolve_deliverable_from_slots(
                updated, str(merged.get("deliverable", "text"))
            )
    merged["needs_clarification"] = False
    merged["clarification_resolved"] = True
    return merged
