"""SearchMode Router：产品入口分流 Quick / Deep。

SearchMode ≠ PlanningMode。
  SearchMode    决定要不要进 Deep Research
  PlanningMode  进 Deep 之后怎么规划（DIRECT / TEMPLATE / DYNAMIC）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

SearchModeName = Literal["auto", "quick", "deep"]
ResolvedMode = Literal["quick", "deep"]


class SearchMode(str, Enum):
    AUTO = "auto"
    QUICK = "quick"
    DEEP = "deep"


QUICK_MARKERS = (
    "是什么",
    "什么是",
    "定义",
    "休市",
    "几点",
    "多少",
    "股价",
    "代码",
    "api",
    "API",
    "天气",
    "放假",
    "今天",
    "现在",
    "最新价",
    "谁是",
    "哪天",
)

DEEP_MARKERS = (
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


def _normalize_mode(raw: str | SearchMode | None) -> SearchMode:
    value = raw.value if isinstance(raw, SearchMode) else str(raw or "auto").strip().lower()
    if value in {"quick", "fast"}:
        return SearchMode.QUICK
    if value in {"deep", "research"}:
        return SearchMode.DEEP
    return SearchMode.AUTO


def _looks_like_compare(query: str) -> bool:
    if any(m in query for m in ("比较", "对比", " vs ", " VS ", "versus")):
        return True
    if query.count(" / ") >= 1 and any(m in query for m in ("和", "与", "、")):
        return True
    return False


def classify_auto(query: str, *, attachments: list[str] | None = None) -> RouteDecision:
    q = (query or "").strip()
    files = [str(x) for x in (attachments or []) if x]
    pdfs = [f for f in files if f.lower().endswith(".pdf")]
    signals: list[str] = []

    if any(m in q for m in REPORT_MARKERS) or ("markdown" in q.lower() and "生成" in q):
        signals.append("explicit_report")
        return RouteDecision(mode="deep", confidence=0.9, signals=signals)
    if any(m in q for m in DEEP_MARKERS) or _looks_like_compare(q):
        signals.append("compare_or_multi_dim")
        return RouteDecision(mode="deep", confidence=0.86, signals=signals)
    if len(pdfs) >= 2 or ("上传" in q and "pdf" in q.lower() and ("多" in q or "几" in q)):
        signals.append("multi_pdf")
        return RouteDecision(mode="deep", confidence=0.84, signals=signals)
    if sum(q.count(m) for m in MULTI_ENTITY_MARKERS) >= 2 and len(q) > 18:
        signals.append("multi_entity")
        return RouteDecision(mode="deep", confidence=0.72, signals=signals)

    if any(m in q for m in QUICK_MARKERS) or len(q) <= 24:
        signals.append("fact_or_short")
        return RouteDecision(mode="quick", confidence=0.8, signals=signals)

    signals.append("default_quick")
    return RouteDecision(mode="quick", confidence=0.55, signals=signals)


def route(
    query: str,
    user_mode: str | SearchMode = SearchMode.AUTO,
    conversation_summary: str = "",
    attachments: list[str] | None = None,
) -> RouteDecision:
    """用户显式 Quick/Deep 尊重用户；Auto 由信号判定。"""
    requested = _normalize_mode(user_mode)
    if requested is SearchMode.QUICK:
        return RouteDecision(mode="quick", confidence=1.0, signals=["user_override"], user_override=True)
    if requested is SearchMode.DEEP:
        return RouteDecision(mode="deep", confidence=1.0, signals=["user_override"], user_override=True)

    combined = query or ""
    if conversation_summary and len((query or "").strip()) <= 24:
        combined = f"{conversation_summary}\n{query}"
    return classify_auto(combined, attachments=attachments)


def budget_for_mode(mode: ResolvedMode, personal: dict | None = None) -> dict[str, int | bool]:
    """按 SearchMode 返回 runner 使用的预算。"""
    cfg = dict(personal or {})
    quick = dict(cfg.get("quick") or {})
    deep = dict(cfg.get("deep") or {})
    if mode == "quick":
        return {
            "max_tool_calls": int(quick.get("max_tool_calls", 3)),
            "max_search_queries": int(quick.get("max_search_queries", 2)),
            "max_replan_count": int(quick.get("max_replan", 0)),
            "parallel": bool(quick.get("parallel", False)),
            "progress_eval": bool(quick.get("progress_eval", False)),
        }
    return {
        "max_tool_calls": int(deep.get("max_tool_calls", 15)),
        "max_search_queries": int(deep.get("max_research_tasks", 5)),
        "max_replan_count": int(deep.get("max_replan", 2)),
        "parallel": bool(deep.get("parallel", True)),
        "progress_eval": bool(deep.get("progress_eval", True)),
    }
