"""Harness 健康检查 — 个人版依赖探针。"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from dotenv import find_dotenv, load_dotenv

from app.api.tracing import is_langfuse_enabled
from app.config.loader import get_harness_config

load_dotenv(find_dotenv())

DependencyStatus = Literal["ok", "degraded", "down", "disabled"]


def _status_from_bool(ok: bool, configured: bool = True) -> DependencyStatus:
    if not configured:
        return "disabled"
    return "ok" if ok else "down"


async def _check_llm(timeout: float = 3.0) -> DependencyStatus:
    if not os.getenv("OPENAI_API_KEY"):
        return "down"

    def _probe() -> bool:
        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
                base_url=os.getenv("OPENAI_BASE_URL"),
                timeout=timeout,
            )
            client.models.list()
            return True
        except Exception:
            return False

    return _status_from_bool(await asyncio.to_thread(_probe))


def _check_tavily() -> DependencyStatus:
    return _status_from_bool(bool(os.getenv("TAVILY_API_KEY")))


def _check_langfuse() -> DependencyStatus:
    config = get_harness_config()
    if not config.langfuse_enabled:
        return "disabled"
    return _status_from_bool(is_langfuse_enabled())


async def collect_health() -> dict[str, Any]:
    config = get_harness_config()
    llm = await _check_llm()
    dependencies = {
        "llm": llm,
        "tavily": _check_tavily(),
        "langfuse": _check_langfuse(),
    }
    overall = "ok" if llm == "ok" else "degraded"
    return {
        "status": overall,
        "dependencies": dependencies,
        "version": config.version,
        "product": "personal-search-assistant",
        "enabled_sources": {"web": True, "file": True},
    }
