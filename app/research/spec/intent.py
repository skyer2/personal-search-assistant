"""Single authoritative ask-kind classifier for answer-contract v2."""

from __future__ import annotations

from typing import Literal

AskKind = Literal[
    "fact",
    "recommendation",
    "comparison",
    "explanation",
    "current_state",
    "forecast",
]

_FORECAST = ("未来", "接下来", "下一步", "趋势", "预测", "前景", "展望", "会怎样", "将会", "发展方向")
_COMPARISON = ("对比", "比较", "区别", "差异", " vs ", "versus", "哪个更", "谁更")
_RECOMMENDATION = (
    "推荐", "值得", "应该选", "该选", "建议", "怎么选", "如何选", "适合",
    "有意思", "有趣", "值得关注", "值得加入", "哪些公司", "哪些工具",
)
_CURRENT = ("最新", "当前", "现在", "目前", "截至", "近期", "今年", "热点", "进展", "现状", "稳定版")
_EXPLANATION = ("为什么", "为何", "原因", "怎么做到", "如何实现", "机制", "解释")
_FACT = ("是谁", "哪一年", "哪年", "什么时候", "何时", "多少", "发布时间", "发布日期", "首次发布", "总部", "是什么", "哪些参数")


def classify_ask_kind(text: str) -> AskKind:
    """Classify once; all downstream code consumes the persisted result."""
    blob = str(text or "")
    lowered = f" {blob.casefold()} "
    if any(token in blob for token in _FORECAST):
        return "forecast"
    if any(token in blob or token in lowered for token in _COMPARISON):
        return "comparison"
    if any(token in blob for token in _RECOMMENDATION):
        return "recommendation"
    if any(token in blob for token in _CURRENT):
        return "current_state"
    if any(token in blob for token in _EXPLANATION):
        return "explanation"
    if any(token in blob for token in _FACT):
        return "fact"
    return "current_state"


__all__ = ["AskKind", "classify_ask_kind"]
