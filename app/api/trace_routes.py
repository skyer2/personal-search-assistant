"""
Trace API：JSONL 本地 trace + Langfuse 代理，供前端 Trace 查看器使用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.api.tracing import is_langfuse_enabled
from app.config.loader import get_harness_config
from app.observability.journal import build_span_tree
from app.observability.paths import traces_log_dir

router = APIRouter(prefix="/api/traces", tags=["traces"])


def _jsonl_path(session_id: str) -> Path:
    log_dir = traces_log_dir()
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return log_dir / f"{safe_id}.jsonl"


@router.get("/langfuse/config")
def langfuse_viewer_config() -> dict[str, Any]:
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    enabled = is_langfuse_enabled() and get_harness_config().langfuse_enabled
    return {
        "enabled": enabled,
        "host": host,
        "session_filter_hint": "按 session_id (= thread_id) 过滤",
        "ui_url": f"{host}/" if enabled else None,
    }


@router.get("/jsonl/{session_id}")
def get_jsonl_trace(session_id: str) -> dict[str, Any]:
    path = _jsonl_path(session_id)
    if not path.exists():
        return {
            "session_id": session_id,
            "events": [],
            "total": 0,
            "source": "jsonl",
            "path": str(path),
            "message": "暂无 JSONL trace，请先完成一次 Harness run",
        }

    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    return {
        "session_id": session_id,
        "events": events,
        "total": len(events),
        "source": "jsonl",
        "path": str(path),
        "tree": build_span_tree(events),
    }


@router.get("/tree/{session_id}")
def get_trace_tree(session_id: str) -> dict[str, Any]:
    payload = get_jsonl_trace(session_id)
    return {
        "session_id": session_id,
        "source": "agent_event.v1",
        "tree": payload.get("tree") or build_span_tree(payload.get("events") or []),
        "total": payload.get("total") or 0,
    }


@router.get("/langfuse/{session_id}")
def get_langfuse_traces(session_id: str) -> dict[str, Any]:
    """不再调用已弃用的 GET /api/public/traces。本地因果树 + Langfuse UI 链接。"""
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    enabled = is_langfuse_enabled() and get_harness_config().langfuse_enabled
    tree = get_trace_tree(session_id)
    return {
        "session_id": session_id,
        "enabled": enabled,
        "export": "otlp",
        "traces": [],
        "tree": tree.get("tree"),
        "message": (
            "Langfuse 通过 OpenTelemetry OTLP 导出（不再使用 /api/public/traces）。"
            if enabled
            else "Langfuse 未配置；下方是本地 Agent span tree。"
        ),
        "ui_url": f"{host}/" if enabled else None,
    }


@router.get("/citations/{session_id}")
def get_citations(session_id: str) -> dict[str, Any]:
    """【Phase 6】读取 session 证据链 evidence.json。"""
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    path = ROOT / "output" / f"session_{safe_id}" / "evidence.json"
    if not path.exists():
        return {
            "session_id": session_id,
            "sources": [],
            "total": 0,
            "message": "暂无证据链，请完成带 Citation-First 的 Harness run",
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("sources", [])
    return {
        "session_id": session_id,
        "sources": sources,
        "total": len(sources),
        "generated_at": data.get("generated_at"),
    }
