"""
LoopState 编解码 — 任务进度的唯一落库形态。

LangGraph InMemorySaver 只覆盖单步 messages / interrupt。
进程重启后续跑认这份 JSON，不认图内 checkpoint。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any, Optional

from app.agent.harness.state import (
    ExecutionPlan,
    LoopState,
    Phase,
    PhaseEvent,
    StepResult,
    TaskIntent,
)
from app.agent.memory.models import MemoryRecord


LOOP_STATE_SCHEMA = "loop_state_v1"
_RUNTIME_METADATA_PREFIX = "_"

_OBS_INT_FIELDS = (
    "obs_structured_checks",
    "obs_structured_passes",
    "obs_structured_retries",
    "obs_parallel_batch_count",
    "obs_parallel_steps_executed",
    "obs_orchestration_violations",
    "obs_binding_violations",
    "obs_unauthorized_tool_hits",
    "obs_estimated_tokens_saved",
    "obs_step_message_tokens_peak",
    "obs_context_budget_trims",
    "obs_retention_patches",
    "obs_fresh_threads",
    "obs_tool_results_cleared",
    "obs_memory_recalled_count",
    "obs_memory_saved_count",
    "obs_memory_trust_filtered",
    "obs_memory_sources_recorded",
    "obs_evidence_retrieved_count",
    "obs_evidence_used_count",
    "obs_artifacts_stored",
    "obs_cache_read_tokens",
)


def _phase_event_to_dict(event: PhaseEvent) -> dict[str, Any]:
    return {
        "phase": event.phase,
        "status": event.status,
        "duration_ms": event.duration_ms,
        "data": dict(event.data or {}),
        "timestamp": event.timestamp,
    }


def _phase_event_from_dict(row: dict[str, Any]) -> PhaseEvent:
    return PhaseEvent(
        phase=str(row.get("phase", "")),
        status=str(row.get("status", "")),
        duration_ms=row.get("duration_ms"),
        data=dict(row.get("data") or {}),
        timestamp=str(row.get("timestamp") or datetime.now().isoformat()),
    )


def _step_result_to_dict(result: StepResult) -> dict[str, Any]:
    return {
        "step_type": result.step_type,
        "content": result.content,
        "compressed_content": result.compressed_content,
        "metadata": dict(result.metadata or {}),
    }


def _memory_to_dict(record: Any) -> Optional[dict[str, Any]]:
    if record is None:
        return None
    if hasattr(record, "to_dict"):
        payload = record.to_dict()
        payload.pop("embedding", None)
        return payload
    if isinstance(record, dict):
        data = dict(record)
        data.pop("embedding", None)
        return data
    if isinstance(record, str):
        return {"fact": record}
    return None


def _workflow_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    workflow_metadata: dict[str, Any] = {}
    for key, value in dict(metadata or {}).items():
        if key.startswith(_RUNTIME_METADATA_PREFIX):
            continue
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"non-serializable state: metadata.{key} -> {type(value).__name__}"
            ) from exc
        workflow_metadata[key] = copy.deepcopy(value)
    return workflow_metadata


def serialize_loop_state(state: LoopState) -> dict[str, Any]:
    started = state.started_at
    started_s = started.isoformat() if isinstance(started, datetime) else str(started)
    records = []
    for item in state.memory_records or []:
        encoded = _memory_to_dict(item)
        if encoded:
            records.append(encoded)
    ledger = []
    for item in state.memory_source_ledger or []:
        encoded = _memory_to_dict(item)
        if encoded:
            ledger.append(encoded)
    payload: dict[str, Any] = {
        "schema": LOOP_STATE_SCHEMA,
        "session_id": state.session_id,
        "phase": state.phase.value if isinstance(state.phase, Phase) else str(state.phase),
        "intent": state.intent.to_dict() if state.intent else None,
        "plan": state.plan.to_dict() if state.plan else None,
        "step_index": state.step_index,
        "step_results": [_step_result_to_dict(item) for item in state.step_results],
        "retry_count": state.retry_count,
        "max_retries": state.max_retries,
        "trace": [_phase_event_to_dict(item) for item in state.trace[-80:]],
        "final_content": state.final_content,
        "assistants_called": list(state.assistants_called),
        "recovery_hints": list(state.recovery_hints),
        "memory_facts": list(state.memory_facts),
        "memory_records": records,
        "memory_recalled": state.memory_recalled,
        "memory_user_id": state.memory_user_id,
        "memory_tenant_id": state.memory_tenant_id,
        "memory_wrap_untrusted": state.memory_wrap_untrusted,
        "memory_project_id": state.memory_project_id,
        "memory_identity_ephemeral": state.memory_identity_ephemeral,
        "memory_source_ledger": ledger,
        "tool_calls_count": state.tool_calls_count,
        "step_validation_results": list(state.step_validation_results),
        "compression_ratios": list(state.compression_ratios),
        "started_at": started_s,
        "metadata": _workflow_metadata(state.metadata),
        "replan_count": state.replan_count,
        "evidence_source_count": state.evidence_source_count,
        "citation_coverage_rate": state.citation_coverage_rate,
        "hallucination_rate": state.hallucination_rate,
        "completed_step_keys": list(state.completed_step_keys),
        "task_fingerprint": state.task_fingerprint,
        "resumed_from_checkpoint": True,
        "working_notes": state.working_notes,
        "evidence_lookup_block": state.evidence_lookup_block,
        "evidence_lookup": list(state.evidence_lookup or []),
        "research_brief": (
            state.research_brief_obj.to_dict()
            if getattr(state, "research_brief_obj", None) is not None
            and hasattr(state.research_brief_obj, "to_dict")
            else None
        ),
        "obs_entity_retention_rates": list(state.obs_entity_retention_rates),
        "graph_thread_ids": list(state.graph_thread_ids),
        "numeric_citation_coverage": state.numeric_citation_coverage,
        "obs_memory_recall_at_k": state.obs_memory_recall_at_k,
        "obs_memory_embedding_used": state.obs_memory_embedding_used,
        "abort_reason": state.abort_reason,
        "abort_message": state.abort_message,
        "citation_fact_bindings": list(
            (state.metadata or {}).get("citation_fact_bindings") or []
        ),
    }
    for name in _OBS_INT_FIELDS:
        payload[name] = int(getattr(state, name, 0) or 0)
    return payload


def deserialize_loop_state(
    payload: dict[str, Any],
    *,
    base: Optional[LoopState] = None,
) -> LoopState:
    state = base or LoopState(session_id=str(payload.get("session_id") or "unknown"))
    state.session_id = str(payload.get("session_id") or state.session_id)
    try:
        state.phase = Phase(str(payload.get("phase") or Phase.UNDERSTAND.value))
    except ValueError:
        state.phase = Phase.UNDERSTAND
    if payload.get("intent"):
        state.intent = TaskIntent.from_dict(payload["intent"])
    if payload.get("plan"):
        state.plan = ExecutionPlan.from_dict(payload["plan"])
    state.step_index = int(payload.get("step_index") or 0)
    state.step_results = [
        StepResult(
            step_type=str(row.get("step_type", "")),
            content=str(row.get("content", "")),
            compressed_content=row.get("compressed_content"),
            metadata=dict(row.get("metadata") or {}),
        )
        for row in (payload.get("step_results") or [])
        if isinstance(row, dict)
    ]
    state.retry_count = int(payload.get("retry_count") or 0)
    state.max_retries = int(payload.get("max_retries") or state.max_retries)
    state.trace = [
        _phase_event_from_dict(row)
        for row in (payload.get("trace") or [])
        if isinstance(row, dict)
    ]
    state.final_content = str(payload.get("final_content") or "")
    state.assistants_called = [str(x) for x in (payload.get("assistants_called") or [])]
    state.recovery_hints = [str(x) for x in (payload.get("recovery_hints") or [])]
    state.memory_facts = [str(x) for x in (payload.get("memory_facts") or [])]
    state.memory_records = [
        MemoryRecord.from_dict(row)
        for row in (payload.get("memory_records") or [])
        if isinstance(row, dict) and row.get("fact")
    ]
    state.memory_recalled = bool(payload.get("memory_recalled", False))
    state.memory_user_id = str(payload.get("memory_user_id") or state.memory_user_id)
    state.memory_tenant_id = str(payload.get("memory_tenant_id") or state.memory_tenant_id)
    state.memory_wrap_untrusted = bool(payload.get("memory_wrap_untrusted", False))
    state.memory_project_id = str(payload.get("memory_project_id") or state.memory_project_id)
    state.memory_identity_ephemeral = bool(payload.get("memory_identity_ephemeral", False))
    state.memory_source_ledger = [
        MemoryRecord.from_dict(row)
        for row in (payload.get("memory_source_ledger") or [])
        if isinstance(row, dict)
    ]
    state.tool_calls_count = int(payload.get("tool_calls_count") or 0)
    state.step_validation_results = list(payload.get("step_validation_results") or [])
    state.compression_ratios = [float(x) for x in (payload.get("compression_ratios") or [])]
    started_raw = payload.get("started_at")
    if started_raw:
        try:
            state.started_at = datetime.fromisoformat(str(started_raw))
        except ValueError:
            pass
    state.metadata = dict(payload.get("metadata") or {})
    bindings = payload.get("citation_fact_bindings")
    if bindings:
        state.metadata["citation_fact_bindings"] = list(bindings)
    state.replan_count = int(payload.get("replan_count") or 0)
    state.evidence_source_count = int(payload.get("evidence_source_count") or 0)
    state.citation_coverage_rate = float(payload.get("citation_coverage_rate") or 0.0)
    state.hallucination_rate = float(payload.get("hallucination_rate") or 0.0)
    state.completed_step_keys = [str(x) for x in (payload.get("completed_step_keys") or [])]
    state.task_fingerprint = str(payload.get("task_fingerprint") or state.task_fingerprint)
    state.resumed_from_checkpoint = True
    state.working_notes = str(payload.get("working_notes") or "")
    state.evidence_lookup_block = str(payload.get("evidence_lookup_block") or "")
    state.evidence_lookup = list(payload.get("evidence_lookup") or [])
    if payload.get("research_brief"):
        from app.agent.harness.research_brief import ResearchBrief

        state.research_brief_obj = ResearchBrief.from_dict(payload.get("research_brief"))
    state.obs_entity_retention_rates = [
        float(x) for x in (payload.get("obs_entity_retention_rates") or [])
    ]
    state.graph_thread_ids = [str(x) for x in (payload.get("graph_thread_ids") or [])]
    state.numeric_citation_coverage = float(payload.get("numeric_citation_coverage") or 0.0)
    state.obs_memory_recall_at_k = float(payload.get("obs_memory_recall_at_k") or 0.0)
    state.obs_memory_embedding_used = bool(payload.get("obs_memory_embedding_used", False))
    state.abort_reason = str(payload.get("abort_reason") or "")
    state.abort_message = str(payload.get("abort_message") or "")
    for name in _OBS_INT_FIELDS:
        setattr(state, name, int(payload.get(name) or 0))
    return state
