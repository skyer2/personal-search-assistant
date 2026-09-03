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
from app.observability.journal import build_span_tree, summarize_trace
from app.observability.paths import APP_ROOT, traces_log_dir
from app.observability.replay import load_trace_payload
from app.run_store import get_run_store

router = APIRouter(prefix="/api/traces", tags=["traces"])


def _jsonl_path(session_id: str) -> Path:
    log_dir = traces_log_dir()
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return log_dir / f"{safe_id}.jsonl"


def _latest_run_id(session_id: str) -> str | None:
    try:
        run = get_run_store().latest_run(session_id)
    except Exception:
        return None
    return run.run_id if run else None


@router.get("/langfuse/config")
def langfuse_viewer_config() -> dict[str, Any]:
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    enabled = is_langfuse_enabled() and get_harness_config().langfuse_enabled
    return {
        "enabled": enabled,
        "host": host,
        "session_filter_hint": "按 session_id 列出 traces；单条 trace 用 run_id",
        "ui_url": f"{host}/" if enabled else None,
    }


@router.get("/jsonl/{session_id}")
def get_jsonl_trace(session_id: str, run_id: str | None = None) -> dict[str, Any]:
    resolved = run_id if isinstance(run_id, str) and run_id else _latest_run_id(session_id)
    payload = load_trace_payload(session_id, run_id=resolved)
    path = _jsonl_path(session_id)
    payload["path"] = str(path)
    if payload["total"] == 0 and path.exists():
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if resolved:
            events = [item for item in events if str(item.get("run_id") or "") in {resolved, ""}]
        payload = {
            "session_id": session_id,
            "run_id": resolved,
            "events": events,
            "total": len(events),
            "source": "jsonl",
            "path": str(path),
            "tree": build_span_tree(events),
            "summary": summarize_trace(events),
            "scope": "run" if resolved else "session",
        }
    if payload["total"] == 0:
        payload["message"] = "暂无 JSONL trace，请先完成一次 Harness run"
        payload["summary"] = payload.get("summary") or summarize_trace([])
        payload["tree"] = payload.get("tree") or build_span_tree([])
        return payload
    if resolved:
        payload["message"] = f"run-centric trace ({resolved})"
    return payload


@router.get("/tree/{session_id}")
def get_trace_tree(session_id: str) -> dict[str, Any]:
    payload = get_jsonl_trace(session_id)
    return {
        "session_id": session_id,
        "source": "agent_event.v1",
        "tree": payload.get("tree") or build_span_tree(payload.get("events") or []),
        "summary": payload.get("summary") or summarize_trace(payload.get("events") or []),
        "total": payload.get("total") or 0,
    }


@router.get("/langfuse/{session_id}")
def get_langfuse_traces(session_id: str) -> dict[str, Any]:
    """未配置 Langfuse 时只返回本地因果树，不访问外部 API。"""
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    enabled = is_langfuse_enabled() and get_harness_config().langfuse_enabled
    tree = get_trace_tree(session_id)
    return {
        "session_id": session_id,
        "enabled": enabled,
        "export": "otlp" if enabled else "disabled",
        "traces": [],
        "tree": tree.get("tree"),
        "message": (
            "Langfuse 通过 OpenTelemetry OTLP 导出（不再使用 /api/public/traces）。"
            if enabled
            else "Langfuse 未配置，已跳过；下方是本地 Agent span tree。"
        ),
        "ui_url": f"{host}/" if enabled else None,
    }


@router.get("/payloads/{run_id}/{name}")
def get_semantic_payload(run_id: str, name: str) -> dict[str, Any]:
    """Load a sanitized semantic artifact from the payload store by run + filename."""
    from app.observability.payload_store import get_payload_store

    store = get_payload_store()
    ref = f"payloads/{run_id}/{name}"
    data = store.get(ref)
    if data is None:
        data = store.get(name)
    if data is None:
        return {"run_id": run_id, "name": name, "found": False, "message": "payload not found"}
    return {"run_id": run_id, "name": name, "found": True, "payload": data}


@router.get("/citations/{session_id}")
def get_citations(session_id: str) -> dict[str, Any]:
    """【Phase 6】读取 session 证据链 evidence.json。"""
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    path = APP_ROOT / "output" / f"session_{safe_id}" / "evidence.json"
    if not path.exists():
        return {
            "session_id": session_id,
            "sources": [],
            "total": 0,
            "message": "暂无证据链，请完成带 Citation-First 的 Harness run",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "session_id": session_id,
            "sources": [],
            "total": 0,
            "message": "证据链文件无法解析，已忽略",
        }
    sources = data.get("sources", []) if isinstance(data, dict) else []
    return {
        "session_id": session_id,
        "sources": sources,
        "total": len(sources),
        "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
    }
