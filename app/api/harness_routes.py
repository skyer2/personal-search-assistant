"""Harness 能力清单：实验档 agent / direct，不是搜索产品。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.config.loader import get_harness_config
from app.research.planning.effort import HardCeiling

router = APIRouter(prefix="/api/harness", tags=["harness"])


@router.get("/capabilities")
def harness_capabilities() -> dict[str, Any]:
    config = get_harness_config()
    personal = getattr(config, "personal_search", {}) or {}
    hard = HardCeiling.from_config(config)
    return {
        "version": config.version,
        "product": "research-agent-harness",
        "not_a_search_engine": True,
        "control_model": {
            "slogan": "Global deterministic control, local agentic autonomy",
            "planner": "semantic WHAT (objective DAG)",
            "worker": "local HOW (search/fetch in isolated context)",
            "harness": "WHETHER / HOW MUCH (hard ceiling + adaptive effort)",
            "progress": "evidence-driven ENOUGH / GAP / PlanPatch",
            "retry_vs_replan": {
                "retry": "same goal, format/transient recovery; JSON-only forbids re-search",
                "replan": "strategy change via constrained PlanPatch",
            },
        },
        "experiment_modes": ["agent", "direct"],
        "default_mode": "agent",
        "environment_tools": ["internet_search", "fetch_url", "read_file_content"],
        "enabled_sources": personal.get("enabled_sources", {"web": True, "file": True}),
        "identity": {"tenant_id": "local", "user_id": "me"},
        "loop": [
            "agent: brief → effort → plan → dispatch → progress / replan → synthesize",
            "direct (baseline only): single worker + search tool",
        ],
        "control_plane": {
            "domain": "app.research",
            "runtime": "langgraph",
            "worker_runtime": "WorkerRuntime",
            "leaf": "langchain.create_agent",
            "graph_runtime_enabled": bool(getattr(config, "graph_runtime_enabled", True)),
            "progress_eval_enabled": bool(getattr(config, "progress_eval_enabled", True)),
        },
        "hard_ceiling": hard.to_dict(),
        "guardrails": {
            "max_tool_calls": config.max_tool_calls,
            "max_total_tokens": config.max_total_tokens,
            "max_run_sec": config.max_run_sec,
            "max_replan_count": config.max_replan_count,
            "max_plan_steps": config.max_plan_steps,
            "max_step_tool_calls": config.max_step_tool_calls,
            "max_parallel_workers": config.max_parallel_workers,
            "note": "Hard safety ceiling. Adaptive Effort can only clamp below these values.",
        },
        "adaptive_effort": {
            "enabled": True,
            "module": "app.research.planning.effort",
            "estimator": "deterministic ComplexityEstimator from Research Brief",
            "clamp": "min(effort_request, hard_ceiling)",
            "gap_grant": "PlanPatch tasks + reserved step retrieval; never raises session ceiling",
        },
        "memory_enabled": bool(getattr(config, "memory_enabled", False)),
        "developer_mode": {
            "eval": True,
            "trace": True,
            "metrics": config.metrics_enabled,
        },
    }
