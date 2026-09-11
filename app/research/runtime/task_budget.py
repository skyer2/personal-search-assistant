"""Single source of truth for per-worker execution budgets."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskBudgetProfile:
    token_ceiling: int
    max_llm_calls: int
    max_search_queries: int
    max_fetch_sources: int
    max_tool_invocations: int
    max_output_tokens_per_call: int


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


_PROFILE_DEFAULTS = {
    "small": (40_000, 10, 6, 10, 10, 2_500),
    "medium": (80_000, 16, 10, 16, 16, 3_500),
    "large": (120_000, 24, 16, 24, 24, 5_000),
}


def task_budget_profile(effort: str) -> TaskBudgetProfile:
    normalized = str(effort or "medium").lower()
    if normalized not in _PROFILE_DEFAULTS:
        normalized = "medium"
    defaults = _PROFILE_DEFAULTS[normalized]
    prefix = f"HARNESS_TASK_{normalized.upper()}_"
    return TaskBudgetProfile(
        token_ceiling=_env_int(f"{prefix}TOKEN_CEILING", defaults[0]),
        max_llm_calls=_env_int(f"{prefix}MAX_LLM_CALLS", defaults[1]),
        max_search_queries=_env_int(f"{prefix}MAX_SEARCH_QUERIES", defaults[2]),
        max_fetch_sources=_env_int(f"{prefix}MAX_FETCH_SOURCES", defaults[3]),
        max_tool_invocations=_env_int(f"{prefix}MAX_TOOL_INVOCATIONS", defaults[4]),
        max_output_tokens_per_call=_env_int(
            f"{prefix}MAX_OUTPUT_TOKENS_PER_CALL", defaults[5]
        ),
    )


def task_budget_metadata(profile: TaskBudgetProfile) -> dict[str, int]:
    return {
        "max_llm_calls": profile.max_llm_calls,
        "max_search_queries": profile.max_search_queries,
        "max_fetch_sources": profile.max_fetch_sources,
        "max_tool_invocations": profile.max_tool_invocations,
        "max_output_tokens_per_call": profile.max_output_tokens_per_call,
    }


TASK_BUDGET_PROFILES = {
    effort: task_budget_profile(effort) for effort in _PROFILE_DEFAULTS
}


__all__ = [
    "TASK_BUDGET_PROFILES",
    "TaskBudgetProfile",
    "task_budget_metadata",
    "task_budget_profile",
]
