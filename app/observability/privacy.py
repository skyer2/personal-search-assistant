"""Trace content policy: default redacted; full payloads are opt-in."""

from __future__ import annotations

import os
from typing import Any

_REDACT_KEYS = {
    "args",
    "prompt",
    "messages",
    "content",
    "html",
    "text",
    "body",
    "query",
    "result",
    "output",
    "input",
    "tool_output",
    "page",
}

_KEEP_KEYS = {
    "tool_name",
    "tool_call_id",
    "model",
    "phase",
    "status",
    "duration_ms",
    "step_index",
    "step_type",
    "task_id",
    "attempt",
    "plan_version",
    "artifact_id",
    "evidence_id",
    "source_id",
    "verdict",
    "reason",
    "gaps",
    "added_tasks",
    "removed_tasks",
    "from_plan_version",
    "to_plan_version",
    "remaining_budget",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cost_usd",
    "usage_missing",
    "worker_runtime",
    "variant",
    "case_id",
    "benchmark",
    "git_sha",
    "config_hash",
}


def content_mode() -> str:
    raw = (os.getenv("OBS_CONTENT_MODE") or os.getenv("HARNESS_OBS_CONTENT_MODE") or "").strip().lower()
    if raw in {"full", "redacted"}:
        return raw
    try:
        from app.config.loader import get_harness_config

        mode = str(getattr(get_harness_config(), "obs_content_mode", "redacted") or "redacted")
        return mode if mode in {"full", "redacted"} else "redacted"
    except Exception:
        return "redacted"


def _truncate(value: Any, limit: int = 400) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, dict):
        return {str(k): _truncate(v, limit) for k, v in list(value.items())[:24]}
    if isinstance(value, list):
        return [_truncate(item, limit) for item in value[:12]]
    return value


def sanitize_attributes(attributes: dict[str, Any] | None, *, mode: str | None = None) -> dict[str, Any]:
    payload = dict(attributes or {})
    chosen = (mode or content_mode()).lower()
    if chosen == "full":
        return {k: _truncate(v, 4000) for k, v in payload.items()}

    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = str(key).lower()
        if lowered in _KEEP_KEYS or lowered.endswith("_id") or lowered.endswith("_count"):
            cleaned[key] = _truncate(value, 500)
            continue
        if lowered in _REDACT_KEYS or any(part in lowered for part in ("prompt", "html", "content", "body")):
            if isinstance(value, (str, list, dict)):
                cleaned[f"{key}_bytes"] = len(str(value))
            else:
                cleaned[key] = value
            continue
        cleaned[key] = _truncate(value, 240)
    return cleaned
