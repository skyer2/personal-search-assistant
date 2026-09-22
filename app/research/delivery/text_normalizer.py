"""Normalize and validate user-visible claim text."""

from __future__ import annotations

import re
import unicodedata

_ARTICLE_FRAME = re.compile(
    r"^(?:智通财经APP获悉|记者(?:报道|获悉|注意到)|文\s*[|｜]\s*[^，。]{0,20}|"
    r"IT时代网\s*\d{1,2}月\d{1,2}日(?:消息|讯)|三句话读懂|本文认为|"
    r"中国报告大厅网讯|据(?:媒体|悉|报道)|消息称)[，,:：\s]*",
    re.IGNORECASE,
)
_SEARCH_FRAME = re.compile(
    r"(?:搜索结果|网页快照|相关结果|点击查看|查看更多|摘要[:：]|"
    r"[，,]\s*(?:详情|更多)$)",
    re.IGNORECASE,
)
_TRUNCATED_END = re.compile(
    r"(?:在全|等方|以及|包括|其中|例如|主要|分别|正在|仍在|已经|将于|"
    r"的第|领域近来|方面的|由于|因此|同时|此外)[，,]?$"
)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_INTERNAL = re.compile(
    r"\b(?:(?:evidence|finding|claim|gap|coverage|task|worker_result)_[A-Za-z0-9_-]+|"
    r"(?:worker|provider|synthesis|tool|llm|token|budget|step)_[A-Za-z0-9_-]+)\b",
    re.IGNORECASE,
)
_ASCII_QUOTE = re.compile(r"(?<![A-Za-z0-9])['\"]([^'\"]{2,80})['\"]")
_WHITESPACE = re.compile(r"\s+")


def is_article_frame(text: str) -> bool:
    value = str(text or "").strip()
    return bool(_ARTICLE_FRAME.search(value))


def is_search_snippet(text: str) -> bool:
    value = str(text or "").strip()
    if _SEARCH_FRAME.search(value):
        return True
    return bool(_TRUNCATED_END.search(value.rstrip("。！？!?")))


def is_complete_sentence(text: str, *, minimum_length: int = 10) -> bool:
    value = str(text or "").strip()
    if len(value) < minimum_length:
        return False
    if _INTERNAL.search(value) or _URL.search(value):
        return False
    if is_article_frame(value) or is_search_snippet(value):
        return False
    if value[-1:] not in "。！？":
        return False
    # Unclosed paired punctuation is almost always a clipped snippet.
    for left, right in (("“", "”"), ("（", "）"), ("《", "》")):
        if value.count(left) != value.count(right):
            return False
    return True


def normalize_claim_text(text: str) -> str:
    """Return polished Chinese claim text, or ``""`` when unsafe to publish."""
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not value:
        return ""
    if is_article_frame(value):
        return ""
    value = _URL.sub("", value)
    value = _ARTICLE_FRAME.sub("", value)
    value = _ASCII_QUOTE.sub(lambda m: f"“{m.group(1).strip()}”", value)
    value = re.sub(r"\.{3,}", "……", value)
    value = _WHITESPACE.sub(" ", value).strip(" \t\r\n-*")
    # Full-width punctuation for Chinese prose.  Preserve decimal/version dots.
    value = re.sub(r",\s*", "，", value)
    value = re.sub(r";\s*", "；", value)
    value = re.sub(r":\s*", "：", value)
    value = re.sub(r"\s+([，。！？；：])", r"\1", value)
    value = re.sub(r"([，。！？；：]){2,}", r"\1", value)
    if not value:
        return ""
    if value[-1:] in ".!?":
        value = value[:-1] + {".": "。", "!": "！", "?": "？"}[value[-1]]
    elif value[-1:] not in "。！？":
        value += "。"
    return value if is_complete_sentence(value) else ""


__all__ = [
    "is_article_frame",
    "is_complete_sentence",
    "is_search_snippet",
    "normalize_claim_text",
]
