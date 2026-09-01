"""Harness 能力清单：实验档 agent / direct，不是搜索产品。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.config.loader import get_harness_config

router = APIRouter(prefix="/api/harness", tags=["harness"])


@router.get("/capabilities")
def harness_capabilities() -> dict[str, Any]:
    config = get_harness_config()
    personal = getattr(config, "personal_search", {}) or {}
    return {
        "version": config.version,
        "product": "research-agent-harness",
        "not_a_search_engine": True,
        "experiment_modes": ["agent", "direct"],
        "default_mode": "agent",
        "environment_tools": ["internet_search", "fetch_url", "read_file_content"],
        "enabled_sources": personal.get("enabled_sources", {"web": True, "file": True}),
        "identity": {"tenant_id": "local", "user_id": "me"},
        "loop": [
            "agent: brief → plan → dispatch → progress / replan → synthesize",
            "direct (baseline only): single worker + search tool",
        ],
        "control_plane": {
            "domain": "app.research",
            "runtime": "langgraph",
            "worker_runtime": "WorkerRuntime",
            "leaf": "langchain.create_agent",
        },
        "guardrails": {
            "max_tool_calls": config.max_tool_calls,
            "max_total_tokens": config.max_total_tokens,
            "max_run_sec": config.max_run_sec,
            "max_replan_count": config.max_replan_count,
            "max_plan_steps": config.max_plan_steps,
        },
        "memory_enabled": bool(getattr(config, "memory_enabled", False)),
        "developer_mode": {
            "eval": True,
            "trace": True,
            "metrics": config.metrics_enabled,
        },
    }
