"""
【Phase 13】Harness 能力清单 API — 个人搜索助手 defaults
"""

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
        "product": "personal-search-assistant",
        "search_modes": ["auto", "answer", "search", "research"],
        "search_mode_aliases": {"quick": "search", "deep": "research", "direct": "answer"},
        "default_mode": personal.get("default_mode", "auto"),
        "default_deliverable": personal.get("default_deliverable", "chat"),
        "enabled_sources": personal.get("enabled_sources", {"web": True, "file": True}),
        "projects": ["Inbox", "C++", "Agent", "KRX"],
        "identity": {"tenant_id": "local", "user_id": "me"},
        "loop": [
            "task_router (answer | search | research)",
            "research: brief → plan → dispatch → progress / replan",
            "finalize",
        ],
        "control_plane": {
            "domain_harness": "app.research",
            "runtime": "langgraph",
            "leaf": "langchain.create_agent",
        },
        "guardrails": {
            "max_tool_calls": config.max_tool_calls,
            "max_total_tokens": config.max_total_tokens,
            "max_run_sec": config.max_run_sec,
            "max_replan_count": config.max_replan_count,
            "max_plan_steps": config.max_plan_steps,
        },
        "personal_search": personal,
        "developer_mode": {
            "eval": True,
            "trace": True,
            "metrics": config.metrics_enabled,
        },
    }
