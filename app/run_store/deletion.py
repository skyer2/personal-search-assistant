"""Run/session deletion cascade.

RunStore owns the UI projection. This module extends deletion to durable trace,
graph checkpoints, semantic payloads, and long-term memory derived from a run.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from app.config.loader import get_harness_config
from app.observability.paths import APP_ROOT
from app.observability.projection_store import ProjectionStore
from app.run_store.service import RunStore


def _safe_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or ""))
    return cleaned[:120] or "unknown"


def _trace_root(traces_root: Path | None) -> Path:
    return traces_root or (APP_ROOT / get_harness_config().jsonl_log_dir)


def delete_trace_run(
    *,
    session_id: str,
    run_id: str,
    traces_root: Path | None = None,
) -> dict[str, int | bool]:
    root = _trace_root(traces_root)
    projection = ProjectionStore(root / "trace-projections.sqlite3")
    event_count = projection.delete_run(run_id)
    run_path = root / _safe_segment(session_id) / f"{_safe_segment(run_id)}.jsonl"
    run_file_removed = run_path.exists()
    run_path.unlink(missing_ok=True)
    payload_dir = root / "payloads" / _safe_segment(run_id)
    payload_removed = payload_dir.exists()
    if payload_removed:
        shutil.rmtree(payload_dir, ignore_errors=True)
    return {
        "trace_events_deleted": event_count,
        "trace_file_removed": run_file_removed,
        "payload_dir_removed": payload_removed,
    }


def delete_trace_session(
    *,
    session_id: str,
    run_ids: list[str],
    traces_root: Path | None = None,
) -> dict[str, int | bool]:
    root = _trace_root(traces_root)
    projection = ProjectionStore(root / "trace-projections.sqlite3")
    event_count = projection.delete_session(session_id)
    for run_id in run_ids:
        payload_dir = root / "payloads" / _safe_segment(run_id)
        if payload_dir.exists():
            shutil.rmtree(payload_dir, ignore_errors=True)
    session_dir = root / _safe_segment(session_id)
    session_dir_removed = session_dir.exists()
    if session_dir_removed:
        shutil.rmtree(session_dir, ignore_errors=True)
    return {
        "trace_events_deleted": event_count,
        "trace_dir_removed": session_dir_removed,
    }


def _checkpoint_path(path: Path | None) -> Path:
    if path is not None:
        return path
    override = os.getenv("HARNESS_GRAPH_CHECKPOINT")
    if override:
        return Path(override)
    configured = Path(get_harness_config().graph_checkpoint_path)
    return configured if configured.is_absolute() else APP_ROOT / configured


def delete_checkpoint_run(run_id: str, *, path: Path | None = None) -> dict[str, int]:
    target = _checkpoint_path(path)
    if not target.exists():
        return {"checkpoints_deleted": 0, "writes_deleted": 0}
    try:
        with sqlite3.connect(target, timeout=30) as conn:
            checkpoint_cursor = conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (run_id,))
            write_cursor = conn.execute("DELETE FROM writes WHERE thread_id=?", (run_id,))
            return {
                "checkpoints_deleted": max(0, checkpoint_cursor.rowcount),
                "writes_deleted": max(0, write_cursor.rowcount),
            }
    except sqlite3.Error:
        return {"checkpoints_deleted": 0, "writes_deleted": 0, "error": "checkpoint_delete_failed"}


async def delete_run_cascade(
    store: RunStore,
    run_id: str,
    *,
    tenant_id: str = "local",
    output_root: Path | None = None,
    traces_root: Path | None = None,
    checkpoint_path: Path | None = None,
    memory_store: Any | None = None,
) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        return {"deleted": False, "reason": "not_found"}
    if run.tenant_id != tenant_id:
        return {"deleted": False, "reason": "forbidden"}
    if memory_store is not None:
        memory_deleted = await memory_store.forget_run(
            run_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
        )
    else:
        memory_deleted = 0
    result = store.delete_run(
        run_id,
        tenant_id=tenant_id,
        output_root=output_root,
    )
    result["memory_deleted"] = memory_deleted
    result["trace"] = delete_trace_run(
        session_id=run.session_id,
        run_id=run_id,
        traces_root=traces_root,
    )
    result["checkpoint"] = delete_checkpoint_run(run_id, path=checkpoint_path)
    return result


async def delete_session_cascade(
    store: RunStore,
    session_id: str,
    *,
    tenant_id: str = "local",
    output_root: Path | None = None,
    updated_root: Path | None = None,
    traces_root: Path | None = None,
    checkpoint_path: Path | None = None,
    memory_store: Any | None = None,
) -> dict[str, Any]:
    session = store.get_session(session_id)
    if session is None:
        return {"deleted": False, "reason": "not_found"}
    if session.tenant_id != tenant_id:
        return {"deleted": False, "reason": "forbidden"}
    runs = store.list_runs(session_id)
    run_ids = [run.run_id for run in runs]
    if memory_store is not None:
        memory_deleted = await memory_store.forget_session(
            session_id,
            user_id=session.user_id,
            tenant_id=session.tenant_id,
        )
    else:
        memory_deleted = 0
    result = store.delete_session(
        session_id,
        tenant_id=tenant_id,
        output_root=output_root,
        updated_root=updated_root,
    )
    result["memory_deleted"] = memory_deleted
    result["trace"] = delete_trace_session(
        session_id=session_id,
        run_ids=run_ids,
        traces_root=traces_root,
    )
    checkpoints_deleted = 0
    writes_deleted = 0
    for run_id in run_ids:
        cleanup = delete_checkpoint_run(run_id, path=checkpoint_path)
        checkpoints_deleted += int(cleanup.get("checkpoints_deleted") or 0)
        writes_deleted += int(cleanup.get("writes_deleted") or 0)
    result["checkpoint"] = {
        "checkpoints_deleted": checkpoints_deleted,
        "writes_deleted": writes_deleted,
    }
    return result


__all__ = [
    "delete_checkpoint_run",
    "delete_run_cascade",
    "delete_session_cascade",
    "delete_trace_run",
    "delete_trace_session",
]
