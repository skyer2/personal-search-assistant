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


async def _check_llm(timeout: float = 8.0) -> DependencyStatus:
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
            try:
                client.models.list()
                return True
            except Exception:
                # 部分网关不实现 GET /v1/models，改打一条极短 chat
                model = (os.getenv("LLM_QWEN_MAX") or "").strip()
                if not model:
                    return False
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
                return True
        except Exception:
            return False

    return _status_from_bool(await asyncio.to_thread(_probe))


def _check_tavily() -> DependencyStatus:
    provider = (os.getenv("SEARCH_PROVIDER") or "tavily").strip().lower()
    key_name = "BOCHA_API_KEY" if provider == "bocha" else "TAVILY_API_KEY"
    return _status_from_bool(bool(os.getenv(key_name)))


def _check_langfuse() -> DependencyStatus:
    config = get_harness_config()
    if not config.langfuse_enabled or not is_langfuse_enabled():
        return "disabled"
    return "ok"


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
        "product": "research-agent-harness",
        "enabled_sources": {"web": True, "file": True},
        "deployment": {
            "invariant": "single_backend_process",
            "note": "active_tasks、HITL Future、WebSocket fanout、RunJournal 都是进程内 cache。不要用 uvicorn --workers > 1。",
        },
    }
