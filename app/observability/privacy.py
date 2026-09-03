"""Trace content policy: redacted | reference (default) | full."""

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
    "answer",
    "final_content",
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
    "finding_id",
    "claim_id",
    "brief_id",
    "plan_id",
    "answer_id",
    "patch_id",
    "gap_id",
    "progress_id",
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
    "finish_reason",
    "failure.stage",
    "failure.type",
    "failure.origin_stage",
    "failure.detected_stage",
    "failure.cause_event_id",
    "failure.cause_artifact_id",
    "fail_reason",
    "objective",
    "entities",
    "dimensions",
    "depth",
    "freshness",
    "deliverable",
    "prefer_primary",
    "planner_source",
    "intent_confidence",
    "brief_ref",
    "brief_hash",
    "plan_ref",
    "plan_hash",
    "answer_ref",
    "answer_hash",
    "prompt_ref",
    "output_ref",
    "prompt_template_id",
    "prompt_template_version",
    "input_hash",
    "output_hash",
    "temperature",
    "response_format",
    "task_ids",
    "task_count",
    "planning_mode",
    "parallel_groups",
    "brief_coverage",
    "finding_ids",
    "evidence_ids",
    "claim_ids",
    "citation_ids",
    "conflicts",
    "confidence",
    "tool_calls",
    "search_calls",
    "tokens",
    "latency",
    "result_ref",
    "result_count",
    "result_bytes",
    "target_gap_ids",
    "resolved_gap_ids",
    "open_gap_ids",
    "triggered_by",
    "target_span_id",
    "target_artifact_id",
    "target_type",
    "grader",
    "grader_version",
    "metric",
    "score",
    "label",
    "passed",
    "word_count",
    "support_type",
    "source_quality",
    "source_kind",
    "locator",
    "input_refs",
    "output_refs",
    "missing_dimensions",
    "conflict_count",
    "metadata",
    "issues",
}


def content_mode() -> str:
    raw = (os.getenv("OBS_CONTENT_MODE") or os.getenv("HARNESS_OBS_CONTENT_MODE") or "").strip().lower()
    if raw in {"full", "redacted", "reference"}:
        return raw
    try:
        from app.config.loader import get_harness_config

        mode = str(getattr(get_harness_config(), "obs_content_mode", "reference") or "reference")
        return mode if mode in {"full", "redacted", "reference"} else "reference"
    except Exception:
        return "reference"


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
        if lowered in {"input_refs", "output_refs"} and isinstance(value, list):
            cleaned[key] = [
                {str(sk): _truncate(sv, 120) for sk, sv in item.items()}
                for item in value
                if isinstance(item, dict)
            ][:24]
            continue
        if (
            lowered in _KEEP_KEYS
            or lowered.endswith("_id")
            or lowered.endswith("_ids")
            or lowered.endswith("_count")
            or lowered.endswith("_ref")
            or lowered.endswith("_hash")
            or lowered.endswith("_rate")
        ):
            cleaned[key] = _truncate(value, 500)
            continue
        if lowered in _REDACT_KEYS or any(part in lowered for part in ("prompt", "html", "content", "body")):
            if isinstance(value, (str, list, dict)):
                cleaned[f"{key}_bytes"] = len(str(value))
            else:
                cleaned[key] = value
            continue
        if chosen == "reference":
            # Prefer metadata + refs; drop bulky free-form blobs.
            if isinstance(value, (dict, list)) and lowered not in {"gaps", "brief_coverage", "remaining_budget", "dimensions", "entities"}:
                cleaned[f"{key}_count"] = len(value)
                continue
            cleaned[key] = _truncate(value, 240)
            continue
        cleaned[key] = _truncate(value, 240)
    return cleaned
