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
    "runtime": "runtime",
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
