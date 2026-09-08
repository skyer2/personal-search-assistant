"""Canonical attribute builders for research semantic events.

Event producers use these helpers instead of hand-writing schema fields. The
journal, integrity checker, and frontend all consume the same flat contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _digest(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        text = str(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def brief_event_attributes(
    brief: dict[str, Any] | None,
    *,
    run_id: str = "",
    planner_source: str = "",
    intent_confidence: Any = None,
) -> dict[str, Any]:
    value = dict(brief or {})
    brief_id = str(value.get("brief_id") or f"brief:{run_id}")
    return {
        "brief_id": brief_id,
        "brief_version": _optional_int(value.get("brief_version")) or 1,
        "objective": str(value.get("objective") or value.get("raw_query") or ""),
        "entities": _strings(value.get("entities")),
        "dimensions": _strings(value.get("dimensions")),
        "depth": str(value.get("depth") or ""),
        "freshness": str(value.get("freshness") or ""),
        "deliverable": str(value.get("deliverable") or ""),
        "prefer_primary": bool(value.get("prefer_primary")),
        "planner_source": str(planner_source or value.get("planner_source") or ""),
        "intent_confidence": _optional_int(intent_confidence),
        "brief_ref": str(value.get("brief_ref") or brief_id),
        "brief_hash": _digest(value),
    }


def plan_event_attributes(
    plan: Any,
    brief: dict[str, Any] | None,
    *,
    run_id: str = "",
    planner_source: str = "",
) -> dict[str, Any]:
    from app.observability.semantic import plan_brief_coverage

    plan_version = int(getattr(plan, "plan_version", 1) or 1)
    task_ids = [
        str(step.resolved_task_id(index))
        for index, step in enumerate(getattr(plan, "steps", None) or [])
    ]
    plan_id = f"plan:{run_id}:{plan_version}"
    payload = {
        "plan_id": plan_id,
        "plan_version": plan_version,
        "brief_id": str((brief or {}).get("brief_id") or f"brief:{run_id}"),
        "task_count": len(task_ids),
        "task_ids": task_ids,
        "planning_mode": str(getattr(plan, "planning_mode", "") or ""),
        "planner_source": str(planner_source or ""),
        "brief_coverage": plan_brief_coverage(dict(brief or {}), plan),
        "plan_ref": plan_id,
    }
    payload["plan_hash"] = _digest(payload)
    return payload


def worker_event_attributes(
    *,
    objective: str,
    step_type: str = "",
    worker_runtime: str = "",
    search_mode: str = "",
    task_shape: str = "",
    execution_path: str = "",
    dispatch_wave_id: int = 0,
    execution_status: str = "",
    result_status: str = "",
    fail_reason: str = "",
    evidence_ids: list[str] | None = None,
    finding_ids: list[str] | None = None,
    tool_calls: int | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "objective": str(objective or ""),
        "step_type": str(step_type or ""),
        "worker_runtime": str(worker_runtime or ""),
        "search_mode": str(search_mode or ""),
        "task_shape": str(task_shape or ""),
        "execution_path": str(execution_path or ""),
        "dispatch_wave_id": _optional_int(dispatch_wave_id) or 0,
        "execution_status": str(execution_status or ""),
        "result_status": str(result_status or ""),
        "fail_reason": str(fail_reason or ""),
        "evidence_ids": _strings(evidence_ids),
        "finding_ids": _strings(finding_ids),
        "tool_calls": _optional_int(tool_calls),
        "duration_ms": _optional_int(duration_ms),
    }


def progress_event_attributes(
    assessment: dict[str, Any] | None,
    *,
    plan_version: int = 1,
    dispatch_wave_id: int = 0,
) -> dict[str, Any]:
    value = dict(assessment or {})
    identity = {
        "status": str(value.get("status") or "unknown"),
        "reason_codes": _strings(value.get("reason_codes")),
        "coverage_gaps": _strings(value.get("coverage_gaps")),
        "gap_ids": _strings(value.get("gap_ids")),
        "missing_dimensions": _strings(value.get("missing_dimensions")),
        "unresolved_conflicts": _strings(value.get("unresolved_conflicts")),
    }
    return {
        **identity,
        "low_confidence_claims": _strings(value.get("low_confidence_claims")),
        "stale_evidence": _strings(value.get("stale_evidence")),
        "unmet_success_criteria": _strings(value.get("unmet_success_criteria")),
        "resolved_gap_ids": _strings(value.get("resolved_gap_ids")),
        "unresolved_gap_count": len(_strings(value.get("gap_ids"))),
        "dispatch_wave_id": _optional_int(dispatch_wave_id) or 0,
        "plan_version": _optional_int(value.get("plan_version")) or int(plan_version or 1),
        "progress_id": f"progress:{_digest(identity)}",
    }


def evidence_event_attributes(assessment: dict[str, Any] | None) -> dict[str, Any]:
    value = dict(assessment or {})
    payload = {
        key: value.get(key)
        for key in (
            "status",
            "evidence_count",
            "trusted_evidence_count",
            "primary_source_count",
            "independent_source_count",
            "supported_claims",
            "unsupported_claims",
        )
    }
    payload.update(
        {
            "unresolved_conflicts": _strings(value.get("unresolved_conflicts")),
            "stale_sources": _strings(value.get("stale_sources")),
            "reason_codes": _strings(value.get("reason_codes")),
        }
    )
    return payload


def execution_health_event_attributes(assessment: dict[str, Any] | None) -> dict[str, Any]:
    value = dict(assessment or {})
    return {
        "status": str(value.get("status") or "unknown"),
        "active_tasks": _strings(value.get("active_tasks")),
        "succeeded_tasks": _optional_int(value.get("succeeded_tasks")) or 0,
        "failed_tasks": _optional_int(value.get("failed_tasks")) or 0,
        "retryable_tasks": _strings(value.get("retryable_tasks")),
        "stalled_cycles": _optional_int(value.get("stalled_cycles")) or 0,
        "failures": [dict(item) for item in value.get("failures") or [] if isinstance(item, dict)],
    }


def delivery_event_attributes(assessment: dict[str, Any] | None) -> dict[str, Any]:
    value = dict(assessment or {})
    return {
        "status": str(value.get("status") or "unknown"),
        "mode": str(value.get("mode") or "none"),
        "blockers": _strings(value.get("blockers")),
        "limitations": _strings(value.get("limitations")),
        "reason_codes": _strings(value.get("reason_codes")),
    }


def control_decision_event_attributes(decision: dict[str, Any] | None) -> dict[str, Any]:
    value = dict(decision or {})
    return {
        "decision_id": str(value.get("decision_id") or ""),
        "action": str(value.get("action") or "unknown"),
        "mode": str(value.get("mode") or ""),
        "reasons": _strings(value.get("reason_codes") or value.get("reasons")),
        "task_ids": _strings(value.get("task_ids")),
        "policy_version": str(value.get("policy_version") or ""),
        "plan_version": _optional_int(value.get("plan_version")),
    }


def quality_event_attributes(assessment: dict[str, Any] | None) -> dict[str, Any]:
    value = dict(assessment or {})
    citation_metrics = value.get("citation_metrics")
    return {
        "verdict": str(value.get("verdict") or "unknown"),
        "issues": _strings(value.get("issues")),
        "repairable": bool(value.get("repairable")),
        "suggested_action": str(value.get("suggested_action") or ""),
        "grounding": bool(value.get("grounding")),
        "citation_metrics": dict(citation_metrics) if isinstance(citation_metrics, dict) else {},
    }


def termination_event_attributes(termination: dict[str, Any] | None) -> dict[str, Any]:
    value = dict(termination or {})
    return {
        "termination": value,
        "termination_status": str(value.get("status") or ""),
        "termination_reason": str(value.get("reason") or ""),
        "termination_stage": str(value.get("stage") or ""),
        "research_completed": bool(value.get("research_completed")),
        "synthesis_attempted": bool(value.get("synthesis_attempted")),
        "quality_attempted": bool(value.get("quality_attempted")),
    }


__all__ = [
    "brief_event_attributes",
    "control_decision_event_attributes",
    "delivery_event_attributes",
    "evidence_event_attributes",
    "execution_health_event_attributes",
    "plan_event_attributes",
    "progress_event_attributes",
    "quality_event_attributes",
    "termination_event_attributes",
    "worker_event_attributes",
]
