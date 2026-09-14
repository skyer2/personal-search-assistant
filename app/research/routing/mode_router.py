"""Product routing and experiment-mode resolution.

`agent` remains the production graph mode and `direct` remains the explicit
experiment baseline. This router does not classify research semantics; fast
path eligibility is decided after the canonical StructuredResearchBrief.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

ExperimentMode = Literal["direct", "agent", "deep_debug"]
ResolvedMode = ExperimentMode
SearchModeName = Literal["direct", "agent", "deep_debug", "auto", "research"]
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
DEEP_DEBUG_ALIASES = {"deep_debug", "debug_deep", "deep-research-debug"}


class SearchMode(str, Enum):
    AGENT = "agent"
    DIRECT = "direct"
    DEEP_DEBUG = "deep_debug"
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
    if value in DEEP_DEBUG_ALIASES:
        return "deep_debug"
    return "agent"


def graph_branch_for_mode(mode: str | None) -> Literal["vanilla", "intent"]:
    return "vanilla" if canonicalize_mode(mode) == "direct" else "intent"


def route(
    query: str,
    user_mode: str | SearchMode = SearchMode.AGENT,
    conversation_summary: str = "",
    attachments: list[str] | None = None,
) -> RouteDecision:
    """Resolve only the experiment/product mode."""
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

    if requested == "deep_debug":
        return RouteDecision(
            mode="deep_debug",
            confidence=1.0,
            signals=["deep_debug"],
            user_override=True,
            execution_path="harness_deep_debug",
        )

    return RouteDecision(
        mode="agent",
        confidence=1.0,
        signals=["agent"],
        user_override=False,
        task_shape="",
        execution_path="harness",
    )


def classify_auto(query: str, *, attachments: list[str] | None = None) -> RouteDecision:
    return route(query, user_mode="agent", attachments=attachments)


def budget_for_mode(
    mode: str | ResolvedMode,
    personal: dict | None = None,
) -> dict[str, int | bool]:
    cfg = dict(personal or {})
    canonical = canonicalize_mode(mode)
    experiment = dict(cfg.get("experiment") or {})
    agent = dict(experiment.get("agent") or cfg.get("agent") or cfg.get("research") or cfg.get("deep") or {})
    direct = dict(experiment.get("direct") or cfg.get("direct") or {})
    deep_debug = dict(experiment.get("deep_debug") or cfg.get("deep_debug") or {})
    if canonical == "direct":
        return {
            "max_tool_calls": int(direct.get("max_tool_calls", 8)),
            "max_search_queries": int(direct.get("max_search_queries", 4)),
            "max_replan_count": int(direct.get("max_replan", 0)),
            "parallel": bool(direct.get("parallel", False)),
            "progress_eval": bool(direct.get("progress_eval", False)),
        }
    if canonical == "deep_debug":
        return {
            "max_tool_calls": int(deep_debug.get("max_tool_calls", 600)),
            "max_search_queries": int(agent.get("max_research_tasks", 8)),
            "max_replan_count": int(deep_debug.get("max_replan_count", 6)),
            "parallel": bool(deep_debug.get("parallel", True)),
            "progress_eval": bool(deep_debug.get("progress_eval", True)),
            "max_parallel_workers": int(deep_debug.get("max_parallel_workers", 2)),
            "max_total_tokens": int(deep_debug.get("max_total_tokens", 1_200_000)),
            "max_run_sec": int(deep_debug.get("max_run_sec", 3_600)),
            "max_llm_calls": int(deep_debug.get("max_llm_calls_per_run", 240)),
            "max_llm_calls_per_worker": int(
                deep_debug.get("max_llm_calls_per_worker", 32)
            ),
            "synthesis_reserve_sec": int(deep_debug.get("synthesis_reserve_sec", 300)),
            "worker_idle_timeout_sec": int(
                deep_debug.get("worker_idle_timeout_sec", 300)
            ),
            "step_timeout_sec": int(deep_debug.get("step_timeout_sec", 300)),
            "synthesis_step_timeout_sec": int(
                deep_debug.get("synthesis_step_timeout_sec", 300)
            ),
            "synthesis_retry_timeout_sec": int(
                deep_debug.get("synthesis_retry_timeout_sec", 120)
            ),
        }
    return {
        "max_tool_calls": int(agent.get("max_tool_calls", 40)),
        "max_search_queries": int(agent.get("max_research_tasks", 5)),
        "max_replan_count": int(agent.get("max_replan", 2)),
        "parallel": bool(agent.get("parallel", True)),
        "progress_eval": bool(agent.get("progress_eval", True)),
    }


def run_budget_overrides_for_mode(
    mode: str | ResolvedMode,
    personal: dict | None = None,
) -> dict[str, int]:
    canonical = canonicalize_mode(mode)
    if canonical != "deep_debug":
        return {}
    budget = budget_for_mode(canonical, personal)
    return {
        key: int(budget[key])
        for key in (
            "max_total_tokens",
            "max_tool_calls",
            "max_run_sec",
            "synthesis_reserve_sec",
            "max_llm_calls",
            "max_llm_calls_per_worker",
            "max_parallel_workers",
            "worker_idle_timeout_sec",
            "step_timeout_sec",
            "synthesis_step_timeout_sec",
            "synthesis_retry_timeout_sec",
        )
    }
