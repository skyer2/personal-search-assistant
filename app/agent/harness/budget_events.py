"""Canonical budget-denial telemetry shared by runtime budget boundaries."""

from __future__ import annotations

from typing import Any


def emit_budget_denied(
    *,
    scope: str,
    resource: str,
    reason: str,
    task_id: str = "",
    worker_lease_id: str = "",
    used: int = 0,
    reserved: int = 0,
    limit: int = 0,
    budget_manager: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one self-describing denial event without exposing prompts or tool args."""
    try:
        from app.observability import EventType, get_recorder

        recorder = get_recorder()
        if not recorder.is_active:
            return

        worker: dict[str, int] = {}
        if budget_manager is not None and task_id:
            lease_snapshot = getattr(budget_manager, "worker_lease_snapshot", None)
            if callable(lease_snapshot):
                worker = dict(lease_snapshot(task_id) or {})

        run: dict[str, int] = {}
        remaining_run_sec = 0.0
        snapshot_method = getattr(budget_manager, "snapshot", None)
        if callable(snapshot_method):
            snapshot = snapshot_method()
            run = {
                "used_tokens": int(getattr(snapshot, "used_tokens", 0) or 0),
                "reserved_tokens": int(getattr(snapshot, "reserved_tokens", 0) or 0),
                "token_limit": int(getattr(snapshot, "token_limit", 0) or 0),
                "used_llm_calls": int(getattr(snapshot, "llm_calls", 0) or 0),
                "reserved_llm_calls": int(getattr(snapshot, "reserved_llm_calls", 0) or 0),
                "llm_call_limit": int(getattr(snapshot, "llm_call_limit", 0) or 0),
                "used_tool_calls": int(getattr(snapshot, "tool_calls", 0) or 0),
                "tool_call_limit": int(getattr(snapshot, "tool_call_limit", 0) or 0),
            }
            remaining_run_sec = max(0.0, float(getattr(snapshot, "remaining_run_sec", 0.0) or 0.0))

        # Call sites which only know the stop reason still need to report the
        # actual rejecting counter, rather than a misleading 0/0 placeholder.
        if limit <= 0 and budget_manager is not None:
            if scope == "worker" and resource == "token":
                used = int(worker.get("tokens_used", 0) or 0)
                limit = int(worker.get("token_limit", 0) or 0)
            elif scope == "worker" and resource == "llm_call":
                used = int(worker.get("llm_calls_used", 0) or 0)
                limit = int(worker.get("llm_calls_limit", 0) or 0)
            elif scope == "research_phase" and resource == "token":
                used = int(run.get("used_tokens", 0) or 0)
                reserved = int(run.get("reserved_tokens", 0) or 0)
                limit = int(getattr(snapshot, "research_cap_tokens", 0) or 0)
            elif scope == "run" and resource == "token":
                used = int(run.get("used_tokens", 0) or 0)
                reserved = int(run.get("reserved_tokens", 0) or 0)
                limit = int(run.get("token_limit", 0) or 0)
            elif scope == "run" and resource == "llm_call":
                used = int(run.get("used_llm_calls", 0) or 0)
                reserved = int(run.get("reserved_llm_calls", 0) or 0)
                limit = int(run.get("llm_call_limit", 0) or 0)
            elif scope == "run" and resource == "tool_call":
                used = int(run.get("used_tool_calls", 0) or 0)
                limit = int(run.get("tool_call_limit", 0) or 0)
            elif resource == "time":
                used = int(getattr(snapshot, "elapsed_sec", 0) or 0)
                deadline = int(getattr(snapshot, "deadline_sec", 0) or 0)
                reserve = int(getattr(snapshot, "synthesis_reserve_sec", 0) or 0)
                limit = max(0, deadline - reserve) if scope == "research_phase" else deadline

        attributes = {
            "scope": scope,
            "resource": resource,
            "reason": reason,
            "task_id": task_id,
            "worker_lease_id": worker_lease_id,
            "used": int(used or 0),
            "reserved": int(reserved or 0),
            "limit": int(limit or 0),
            "worker_tokens_used": int(worker.get("tokens_used", 0) or 0),
            "worker_token_limit": int(worker.get("token_limit", 0) or 0),
            "worker_llm_calls_used": int(worker.get("llm_calls_used", 0) or 0),
            "worker_llm_calls_limit": int(worker.get("llm_calls_limit", 0) or 0),
            "research_used_tokens": int(run.get("used_tokens", 0) or 0),
            "research_token_limit": int(run.get("token_limit", 0) or 0),
            "run_used_tokens": int(run.get("used_tokens", 0) or 0),
            "run_token_limit": int(run.get("token_limit", 0) or 0),
            "run_used_llm_calls": int(run.get("used_llm_calls", 0) or 0),
            "run_llm_call_limit": int(run.get("llm_call_limit", 0) or 0),
            "run_used_tool_calls": int(run.get("used_tool_calls", 0) or 0),
            "run_tool_call_limit": int(run.get("tool_call_limit", 0) or 0),
            "remaining_run_sec": round(remaining_run_sec, 3),
            **(extra or {}),
        }
        recorder.emit(
            EventType.BUDGET_DENIED,
            phase="execute",
            status="denied",
            task_id=task_id or None,
            attributes=attributes,
        )
    except Exception:
        return


def emit_budget_decided(
    *,
    scope: str,
    resource: str,
    reason: str,
    task_id: str = "",
    worker_lease_id: str = "",
    used: int = 0,
    reserved: int = 0,
    limit: int = 0,
    budget_manager: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a non-denial budget decision such as worker finalization."""
    try:
        from app.observability import EventType, get_recorder

        recorder = get_recorder()
        if not recorder.is_active:
            return
        worker: dict[str, int] = {}
        if budget_manager is not None and task_id:
            lease_snapshot = getattr(budget_manager, "worker_lease_snapshot", None)
            if callable(lease_snapshot):
                worker = dict(lease_snapshot(task_id) or {})
        snapshot_method = getattr(budget_manager, "snapshot", None)
        snapshot = snapshot_method() if callable(snapshot_method) else None
        recorder.emit(
            EventType.BUDGET_DECIDED,
            phase="execute",
            status="finalize",
            task_id=task_id or None,
            attributes={
                "scope": scope,
                "resource": resource,
                "reason": reason,
                "task_id": task_id,
                "worker_lease_id": worker_lease_id,
                "used": int(used or 0),
                "reserved": int(reserved or 0),
                "limit": int(limit or 0),
                "worker_tokens_used": int(worker.get("tokens_used", 0) or 0),
                "worker_token_limit": int(worker.get("token_limit", 0) or 0),
                "worker_llm_calls_used": int(worker.get("llm_calls_used", 0) or 0),
                "worker_llm_calls_limit": int(worker.get("llm_calls_limit", 0) or 0),
                "run_used_tokens": int(getattr(snapshot, "used_tokens", 0) or 0),
                "run_token_limit": int(getattr(snapshot, "token_limit", 0) or 0),
                **(extra or {}),
            },
        )
    except Exception:
        return


__all__ = ["emit_budget_decided", "emit_budget_denied"]
