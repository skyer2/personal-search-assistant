"""Product routing and experiment-mode resolution.

`agent` remains the production graph mode and `direct` remains the explicit
experiment baseline. Inside the agent mode, task shape decides whether a
simple fact takes the deterministic fast path or the full research graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

ExperimentMode = Literal["direct", "agent"]
ResolvedMode = ExperimentMode
SearchModeName = Literal["direct", "agent", "auto", "research"]
TaskModeName = SearchModeName

DIRECT_ALIASES = {"direct", "vanilla", "baseline"}
AGENT_ALIASES = {
    "agent",
    "auto",
    "research",
    "harness",
    "deep",
    "search",
    "quick",
    "answer",
    "fast",
}


class SearchMode(str, Enum):
    AGENT = "agent"
    DIRECT = "direct"
    AUTO = "auto"
    RESEARCH = "research"


@dataclass
class RouteDecision:
    mode: ResolvedMode
    confidence: float
    signals: list[str] = field(default_factory=list)
    user_override: bool = False
    task_shape: str = ""
    execution_path: str = ""

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "user_override": self.user_override,
            "task_shape": self.task_shape,
            "execution_path": self.execution_path,
        }


def canonicalize_mode(raw: str | SearchMode | None) -> ExperimentMode:
    value = raw.value if isinstance(raw, SearchMode) else str(raw or "agent").strip().lower()
    if value in DIRECT_ALIASES:
        return "direct"
    return "agent"


def graph_branch_for_mode(mode: str | None) -> Literal["vanilla", "intent"]:
    return "vanilla" if canonicalize_mode(mode) == "direct" else "intent"


def route(
    query: str,
    user_mode: str | SearchMode = SearchMode.AGENT,
    conversation_summary: str = "",
    attachments: list[str] | None = None,
) -> RouteDecision:
    """Resolve experiment mode and the production execution path."""
    _ = (conversation_summary, attachments)
    requested = canonicalize_mode(user_mode)
    if requested == "direct":
        return RouteDecision(
            mode="direct",
            confidence=1.0,
            signals=["experiment_direct"],
            user_override=True,
            execution_path="experiment_direct",
        )

    from app.research.routing.task_shape import classify_task_shape

    decision = classify_task_shape(query)
    fast_path = decision.shape.value == "simple_fact"
    signals = [f"task_shape:{decision.shape.value}"]
    if fast_path:
        signals.append("simple_fact_fast_path")
    return RouteDecision(
        mode="agent",
        confidence=decision.confidence,
        signals=signals,
        user_override=False,
        task_shape=decision.shape.value,
        execution_path="fast_path" if fast_path else "harness",
    )


def classify_auto(query: str, *, attachments: list[str] | None = None) -> RouteDecision:
    return route(query, user_mode="agent", attachments=attachments)


def budget_for_mode(mode: str | ResolvedMode, personal: dict | None = None) -> dict[str, int | bool]:
    cfg = dict(personal or {})
    canonical = canonicalize_mode(mode)
    experiment = dict(cfg.get("experiment") or {})
    agent = dict(experiment.get("agent") or cfg.get("agent") or cfg.get("research") or cfg.get("deep") or {})
    direct = dict(experiment.get("direct") or cfg.get("direct") or {})
    if canonical == "direct":
        return {
            "max_tool_calls": int(direct.get("max_tool_calls", 8)),
            "max_search_queries": int(direct.get("max_search_queries", 4)),
            "max_replan_count": int(direct.get("max_replan", 0)),
            "parallel": bool(direct.get("parallel", False)),
            "progress_eval": bool(direct.get("progress_eval", False)),
        }
    return {
        "max_tool_calls": int(agent.get("max_tool_calls", 40)),
        "max_search_queries": int(agent.get("max_research_tasks", 5)),
        "max_replan_count": int(agent.get("max_replan", 2)),
        "parallel": bool(agent.get("parallel", True)),
        "progress_eval": bool(agent.get("progress_eval", True)),
    }
