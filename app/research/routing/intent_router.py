"""Deterministic-first semantic intent routing.

The product mode router decides *which runtime* to use.  This module decides
the research shape before any model call.  It intentionally uses conservative
signals only; an unknown query is routed to ``deep_research`` rather than
silently taking a shallow path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import time
from typing import Literal

IntentKind = Literal[
    "simple_fact",
    "simple_search",
    "deep_research",
    "comparison",
    "mechanism",
    "trend",
    "forecast",
    "file_only",
]


@dataclass(frozen=True)
class IntentRoute:
    kind: IntentKind
    confidence: float
    needs_web: bool
    needs_file: bool
    signals: tuple[str, ...] = ()
    latency_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_COMPARISON = ("比较", "对比", "差异", "vs", "versus", "分别适合")
_MECHANISM = ("为什么", "原因", "机制", "驱动因素", "如何实现", "原理")
_TREND = ("热点", "趋势", "进展", "关注", "当前", "最新", "截至", "现状")
_FORECAST = ("未来", "预测", "前景", "展望", "可能成为", "会不会")
_FACT = ("是谁", "是什么", "多少", "何时", "哪一年", "地址", "发布于", "when", "who")


def route_intent(query: str, *, attachments: list[str] | None = None) -> IntentRoute:
    """Classify a query without an LLM and finish in sub-millisecond time."""
    started = time.perf_counter()
    text = str(query or "").strip()
    lower = text.lower()
    files = [str(item) for item in (attachments or []) if str(item).strip()]
    signals: list[str] = []
    if files and not text:
        kind: IntentKind = "file_only"
        signals.append("attachment_without_query")
    elif any(token in lower for token in _COMPARISON):
        kind = "comparison"
        signals.append("comparison_signal")
    elif any(token in text for token in _FORECAST):
        kind = "forecast"
        signals.append("forecast_signal")
    elif any(token in text for token in _TREND):
        kind = "trend"
        signals.append("freshness_or_trend_signal")
    elif any(token in text for token in _MECHANISM):
        kind = "mechanism"
        signals.append("causal_signal")
    elif any(token in lower for token in _FACT) and len(text) <= 80:
        kind = "simple_fact"
        signals.append("atomic_fact_signal")
    elif len(text) <= 100 and not re.search(r"[。！？!?]", text):
        kind = "simple_search"
        signals.append("short_lookup_signal")
    else:
        kind = "deep_research"
        signals.append("open_research_default")
    needs_file = bool(files)
    needs_web = kind != "file_only"
    if needs_file:
        signals.append("attachments_present")
    confidence = 0.9 if kind in {"simple_fact", "comparison", "trend", "forecast", "mechanism"} else 0.7
    return IntentRoute(
        kind=kind,
        confidence=confidence,
        needs_web=needs_web,
        needs_file=needs_file,
        signals=tuple(signals),
        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
    )


__all__ = ["IntentKind", "IntentRoute", "route_intent"]
