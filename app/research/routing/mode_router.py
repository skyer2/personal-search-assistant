"""Task Router：产品入口分流 ANSWER / SEARCH / RESEARCH。

SearchMode（产品档）≠ PlanningMode（研搜内部 DIRECT/TEMPLATE/DYNAMIC）。

  ANSWER    概念题，LLM 直答，不检索
  SEARCH    需要最新网页的单目标事实
  RESEARCH  多实体 / 多维度 / 要对证，进 StateGraph

兼容旧 API：quick→search，deep→research，direct→answer。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

TaskModeName = Literal["auto", "answer", "search", "research"]
ResolvedMode = Literal["answer", "search", "research"]
SearchModeName = Literal["auto", "quick", "deep", "answer", "search", "research"]

CANONICAL_MODES: tuple[str, ...] = ("auto", "answer", "search", "research")
LEGACY_TO_CANONICAL = {
    "quick": "search",
    "fast": "search",
    "deep": "research",
    "direct": "answer",
    "direct_answer": "answer",
}


class SearchMode(str, Enum):
    AUTO = "auto"
    ANSWER = "answer"
    SEARCH = "search"
    RESEARCH = "research"
    # 旧值仍可构造，normalize 会映射到上面三档
    QUICK = "quick"
    DEEP = "deep"


ANSWER_MARKERS = (
    "是什么",
    "什么是",
    "定义",
    "怎么理解",
    "如何理解",
    "含义",
    "什么意思",
    "区别是什么",
)

FRESHNESS_MARKERS = (
    "今天",
    "现在",
    "最新",
    "刚刚",
    "实时",
    "休市",
    "几点",
    "股价",
    "天气",
    "放假",
    "最新价",
    "release notes",
    "changelog",
    "release note",
    "改了什么",
    "更新了什么",
    "多少",
    "谁是",
    "哪天",
)

RESEARCH_MARKERS = (
    "比较",
    "对比",
    " vs ",
    " VS ",
    "versus",
    "差异",
    "修订",
    "对照",
    "多维度",
    "综合",
    "多份",
    "多个 pdf",
    "多个PDF",
    "官方",
    "白皮书",
    "调研",
)

REPORT_MARKERS = (
    "生成报告",
    "生成 markdown",
    "生成 Markdown",
    "生成md",
    "生成 MD",
    "导出 pdf",
    "导出 PDF",
    "生成pdf",
    "生成 PDF",
    "写一份报告",
    "整理成报告",
    "markdown 报告",
    "Markdown 报告",
)

MULTI_ENTITY_MARKERS = (" / ", "、", "和", "与")

# 旧测试/调用方仍 import 这些名字
QUICK_MARKERS = FRESHNESS_MARKERS
DEEP_MARKERS = RESEARCH_MARKERS


@dataclass
class RouteDecision:
    mode: ResolvedMode
    confidence: float
    signals: list[str] = field(default_factory=list)
    user_override: bool = False

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "user_override": self.user_override,
        }


def canonicalize_mode(raw: str | SearchMode | None) -> str:
    value = raw.value if isinstance(raw, SearchMode) else str(raw or "auto").strip().lower()
    if value in LEGACY_TO_CANONICAL:
        return LEGACY_TO_CANONICAL[value]
    if value in {"auto", "answer", "search", "research"}:
        return value
    return "auto"


def _normalize_mode(raw: str | SearchMode | None) -> SearchMode:
    canonical = canonicalize_mode(raw)
    return SearchMode(canonical)


def _looks_like_compare(query: str) -> bool:
    if any(m in query for m in ("比较", "对比", " vs ", " VS ", "versus")):
        return True
    if query.count(" / ") >= 1 and any(m in query for m in ("和", "与", "、")):
        return True
    return False


def _needs_freshness(query: str) -> bool:
    q = query or ""
    lower = q.lower()
    if any(m in q for m in FRESHNESS_MARKERS):
        return True
    if any(m in lower for m in ("release notes", "changelog", "release note")):
        return True
    return False


def classify_auto(query: str, *, attachments: list[str] | None = None) -> RouteDecision:
    q = (query or "").strip()
    files = [str(x) for x in (attachments or []) if x]
    pdfs = [f for f in files if f.lower().endswith(".pdf")]
    signals: list[str] = []

    if any(m in q for m in REPORT_MARKERS) or ("markdown" in q.lower() and "生成" in q):
        signals.append("explicit_report")
        return RouteDecision(mode="research", confidence=0.9, signals=signals)
    if any(m in q for m in RESEARCH_MARKERS) or _looks_like_compare(q):
        signals.append("compare_or_multi_dim")
        return RouteDecision(mode="research", confidence=0.86, signals=signals)
    if len(pdfs) >= 2 or ("上传" in q and "pdf" in q.lower() and ("多" in q or "几" in q)):
        signals.append("multi_pdf")
        return RouteDecision(mode="research", confidence=0.84, signals=signals)
    if sum(q.count(m) for m in MULTI_ENTITY_MARKERS) >= 2 and len(q) > 18:
        signals.append("multi_entity")
        return RouteDecision(mode="research", confidence=0.72, signals=signals)

    if _needs_freshness(q):
        signals.append("needs_freshness")
        return RouteDecision(mode="search", confidence=0.84, signals=signals)

    if any(m in q for m in ANSWER_MARKERS) and not _needs_freshness(q):
        signals.append("definitional")
        return RouteDecision(mode="answer", confidence=0.82, signals=signals)

    if len(q) <= 24 and not _needs_freshness(q):
        signals.append("short_conceptual")
        return RouteDecision(mode="answer", confidence=0.7, signals=signals)

    signals.append("default_search")
    return RouteDecision(mode="search", confidence=0.55, signals=signals)


def route(
    query: str,
    user_mode: str | SearchMode = SearchMode.AUTO,
    conversation_summary: str = "",
    attachments: list[str] | None = None,
) -> RouteDecision:
    """用户显式三档尊重用户；Auto 由信号判定。"""
    requested = canonicalize_mode(user_mode)
    if requested == "answer":
        return RouteDecision(mode="answer", confidence=1.0, signals=["user_override"], user_override=True)
    if requested == "search":
        return RouteDecision(mode="search", confidence=1.0, signals=["user_override"], user_override=True)
    if requested == "research":
        return RouteDecision(mode="research", confidence=1.0, signals=["user_override"], user_override=True)

    combined = query or ""
    if conversation_summary and len((query or "").strip()) <= 24:
        combined = f"{conversation_summary}\n{query}"
    return classify_auto(combined, attachments=attachments)


def budget_for_mode(mode: str | ResolvedMode, personal: dict | None = None) -> dict[str, int | bool]:
    """按产品档返回 runner 使用的预算。兼容 quick/deep 旧 key。"""
    cfg = dict(personal or {})
    canonical = canonicalize_mode(mode) if mode != "auto" else str(mode)
    if canonical == "auto":
        canonical = "search"
    answer = dict(cfg.get("answer") or {})
    search = dict(cfg.get("search") or cfg.get("quick") or {})
    research = dict(cfg.get("research") or cfg.get("deep") or {})
    if canonical == "answer":
        return {
            "max_tool_calls": int(answer.get("max_tool_calls", 0)),
            "max_search_queries": int(answer.get("max_search_queries", 0)),
            "max_replan_count": int(answer.get("max_replan", 0)),
            "parallel": bool(answer.get("parallel", False)),
            "progress_eval": bool(answer.get("progress_eval", False)),
        }
    if canonical == "search":
        return {
            "max_tool_calls": int(search.get("max_tool_calls", 3)),
            "max_search_queries": int(search.get("max_search_queries", 2)),
            "max_replan_count": int(search.get("max_replan", 0)),
            "parallel": bool(search.get("parallel", False)),
            "progress_eval": bool(search.get("progress_eval", False)),
        }
    return {
        "max_tool_calls": int(research.get("max_tool_calls", 15)),
        "max_search_queries": int(research.get("max_research_tasks", 5)),
        "max_replan_count": int(research.get("max_replan", 2)),
        "parallel": bool(research.get("parallel", True)),
        "progress_eval": bool(research.get("progress_eval", True)),
    }


def graph_branch_for_mode(mode: str | None) -> Literal["direct_answer", "quick_search", "intent"]:
    canonical = canonicalize_mode(mode) if mode else "research"
    if canonical == "answer":
        return "direct_answer"
    if canonical == "search":
        return "quick_search"
    return "intent"
