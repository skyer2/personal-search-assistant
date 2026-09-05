"""Normalize free-form fail_reason into aggregatable failure.stage / failure.type.

Also supports semantic quality attribution via origin_stage / detected_stage.
"""

from __future__ import annotations

from typing import Any

FAILURE_STAGES = (
    "understand",
    "planning",
    "retrieval",
    "worker",
    "tool",
    "evidence",
    "progress",
    "replan",
    "synthesis",
    "runtime",
)

_TYPE_ALIASES: dict[str, str] = {
    "content_filter": "content_filter",
    "provider_content_filter": "content_filter",
    "sensitivecontentdetected": "content_filter",
    "rate_limit": "rate_limit",
    "context_length": "context_length",
    "context_length_exceeded": "context_length",
    "auth": "provider_auth",
    "provider_auth": "provider_auth",
    "bad_request": "provider_error",
    "provider_bad_request": "provider_error",
    "timeout": "timeout",
    "step_timeout": "timeout",
    "deadline_exceeded": "timeout",
    "budget_exceeded": "budget_exhausted",
    "budget_tool_calls": "budget_exhausted",
    "budget_tokens": "budget_exhausted",
    "invalid_output": "invalid_output",
    "invalid_structured_output": "invalid_output",
    "no_content": "invalid_output",
    "missing_evidence": "missing_evidence",
    "no_file_generated": "missing_evidence",
    "false_enough": "false_enough",
    "missing_dimension": "missing_dimension",
    "unauthorized_tool": "unauthorized_tool",
    "unauthorized_tools": "unauthorized_tool",
    "tool_gateway_denied": "unauthorized_tool",
    "dependency_failed": "dependency_failed",
    "parallel_step_error": "dependency_failed",
    "missing_session": "runtime",
    "missing_step": "runtime",
    "worker_failed": "worker_failed",
}

_TYPE_BY_PROVIDER_KIND: dict[str, str] = {
    "content_filter": "content_filter",
    "rate_limit": "rate_limit",
    "timeout": "timeout",
    "context_length": "context_length",
    "auth": "provider_auth",
    "bad_request": "provider_error",
}

_STAGE_BY_PHASE: dict[str, str] = {
    "understand": "understand",
    "plan": "planning",
    "planning": "planning",
    "build_context": "retrieval",
    "execute": "worker",
    "compress": "synthesis",
    "validate": "evidence",
    "recover": "replan",
    "finalize": "synthesis",
    "synthesis": "synthesis",
    "abort": "runtime",
    "run": "runtime",
    "eval": "runtime",
    "quality": "synthesis",
}

_STAGE_BY_TYPE: dict[str, str] = {
    "timeout": "runtime",
    "budget_exhausted": "runtime",
    "invalid_output": "worker",
    "missing_evidence": "evidence",
    "false_enough": "progress",
    "missing_dimension": "understand",
    "unauthorized_tool": "tool",
    "dependency_failed": "worker",
    "worker_failed": "worker",
    "content_filter": "worker",
    "rate_limit": "worker",
    "context_length": "worker",
    "provider_auth": "worker",
    "provider_error": "worker",
    "runtime": "runtime",
}


def record_failure(
    state: Any,
    *,
    origin_stage: str,
    detected_stage: str,
    failure_type: str = "",
    reason: str = "",
) -> dict[str, str]:
    """Record origin once; detected stage may move as termination progresses."""
    metadata = getattr(state, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        setattr(state, "metadata", metadata)
    origin = str(metadata.get("failure.origin_stage") or origin_stage or "runtime")
    detected = str(detected_stage or origin)
    metadata["failure.origin_stage"] = origin
    metadata["failure.detected_stage"] = detected
    if failure_type:
        metadata["failure.type"] = str(failure_type)
    if reason:
        metadata["failure.reason"] = str(reason)[:500]
    return {
        "failure.origin_stage": origin,
        "failure.detected_stage": detected,
        **({"failure.type": str(failure_type)} if failure_type else {}),
    }


def classify_failure(
    reason: str | None,
    *,
    phase: str | None = None,
    event_type: str | None = None,
    origin_stage: str | None = None,
    detected_stage: str | None = None,
) -> dict[str, str]:
    raw = str(reason or "").strip()
    lowered = raw.lower()
    failure_type = "unknown"
    for key, mapped in _TYPE_ALIASES.items():
        if key == lowered or key in lowered:
            failure_type = mapped
            break
    if failure_type == "unknown" and raw:
        failure_type = "worker_failed" if (event_type or "").startswith("worker.") else "unknown"

    stage = _STAGE_BY_PHASE.get(str(phase or "").lower()) or _STAGE_BY_TYPE.get(failure_type) or "runtime"
    if (event_type or "").startswith("tool."):
        stage = "tool"
    elif (event_type or "").startswith("replan."):
        stage = "replan"
    elif (event_type or "") == "progress.evaluated":
        stage = "progress"
    elif (event_type or "").startswith("synthesis."):
        stage = "synthesis"
    elif (event_type or "") == "brief.compiled":
        stage = "understand"

    origin = str(origin_stage or stage)
    detected = str(detected_stage or stage)
    payload = {
        "failure.stage": stage,
        "failure.type": failure_type,
        "failure.origin_stage": origin,
        "failure.detected_stage": detected,
    }
    if raw:
        payload["fail_reason"] = raw[:500]
    return payload


def enrich_failure_attributes(
    attributes: dict[str, Any] | None,
    *,
    reason: str | None = None,
    phase: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    attrs = dict(attributes or {})
    classified = classify_failure(
        reason or str(attrs.get("fail_reason") or attrs.get("error") or ""),
        phase=phase,
        event_type=event_type,
        origin_stage=str(attrs.get("failure.origin_stage") or "") or None,
        detected_stage=str(attrs.get("failure.detected_stage") or "") or None,
    )
    for key, value in classified.items():
        attrs.setdefault(key, value)
    return attrs
