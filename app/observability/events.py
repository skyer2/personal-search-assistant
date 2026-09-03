"""Canonical Agent event vocabulary and envelope."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "agent_event.v1"

EVENT_VOCABULARY: tuple[str, ...] = (
    "run.started",
    "run.completed",
    "run.failed",
    "brief.compiled",
    "plan.created",
    "plan.validated",
    "worker.started",
    "worker.completed",
    "worker.failed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "gen_ai.chat",
    "retrieval.search",
    "evidence.registered",
    "progress.evaluated",
    "replan.proposed",
    "replan.applied",
    "replan.rejected",
    "synthesis.started",
    "synthesis.completed",
    "synthesis.failed",
    "recovery.decided",
    "recovery.completed",
    "context.built",
    "context.compressed",
    "checkpoint.saved",
    "checkpoint.resumed",
    "budget.decided",
    "budget.exhausted",
    "quality.evaluated",
    "eval.scored",
    "phase",
    "hitl.interrupt",
    "run_summary",
    "llm_usage",
)


class EventType:
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    BRIEF_COMPILED = "brief.compiled"
    PLAN_CREATED = "plan.created"
    PLAN_VALIDATED = "plan.validated"
    WORKER_STARTED = "worker.started"
    WORKER_COMPLETED = "worker.completed"
    WORKER_FAILED = "worker.failed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    GEN_AI_CHAT = "gen_ai.chat"
    RETRIEVAL_SEARCH = "retrieval.search"
    EVIDENCE_REGISTERED = "evidence.registered"
    PROGRESS_EVALUATED = "progress.evaluated"
    REPLAN_PROPOSED = "replan.proposed"
    REPLAN_APPLIED = "replan.applied"
    REPLAN_REJECTED = "replan.rejected"
    SYNTHESIS_STARTED = "synthesis.started"
    SYNTHESIS_COMPLETED = "synthesis.completed"
    SYNTHESIS_FAILED = "synthesis.failed"
    RECOVERY_DECIDED = "recovery.decided"
    RECOVERY_COMPLETED = "recovery.completed"
    CONTEXT_BUILT = "context.built"
    CONTEXT_COMPRESSED = "context.compressed"
    CHECKPOINT_SAVED = "checkpoint.saved"
    CHECKPOINT_RESUMED = "checkpoint.resumed"
    BUDGET_DECIDED = "budget.decided"
    BUDGET_EXHAUSTED = "budget.exhausted"
    QUALITY_EVALUATED = "quality.evaluated"
    EVAL_SCORED = "eval.scored"
    PHASE = "phase"
    HITL_INTERRUPT = "hitl.interrupt"
    RUN_SUMMARY = "run_summary"
    LLM_USAGE = "llm_usage"


def new_id(size: int = 16) -> str:
    return uuid.uuid4().hex[:size]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def span_identity(
    phase: str,
    *,
    task_id: str | None = None,
    attempt: int | None = None,
    span_id: str | None = None,
) -> str:
    """并行 Worker 不能只用 phase 当 span key。"""
    if span_id:
        return span_id
    return f"{phase}:{task_id or '-'}:{int(attempt or 0)}"


@dataclass
class AgentEvent:
    event_id: str
    trace_id: str
    span_id: str
    run_id: str
    session_id: str
    seq: int
    timestamp: str
    type: str
    parent_span_id: str | None = None
    plan_version: int | None = None
    task_id: str | None = None
    attempt: int | None = None
    phase: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    input_refs: list[dict[str, Any]] = field(default_factory=list)
    output_refs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = SCHEMA_VERSION
        payload["event"] = self.type
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentEvent":
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        event_type = str(raw.get("type") or raw.get("event") or "")
        try:
            seq = int(raw.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        try:
            duration = raw.get("duration_ms")
            duration_ms = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        input_refs = raw.get("input_refs")
        if not isinstance(input_refs, list):
            input_refs = attrs.get("input_refs") if isinstance(attrs.get("input_refs"), list) else []
        output_refs = raw.get("output_refs")
        if not isinstance(output_refs, list):
            output_refs = attrs.get("output_refs") if isinstance(attrs.get("output_refs"), list) else []
        return cls(
            event_id=str(raw.get("event_id") or ""),
            trace_id=str(raw.get("trace_id") or ""),
            span_id=str(raw.get("span_id") or raw.get("event_id") or ""),
            parent_span_id=raw.get("parent_span_id"),
            run_id=str(raw.get("run_id") or ""),
            session_id=str(raw.get("session_id") or ""),
            seq=seq,
            timestamp=str(raw.get("timestamp") or utc_now()),
            type=event_type,
            plan_version=raw.get("plan_version"),
            task_id=raw.get("task_id"),
            attempt=raw.get("attempt"),
            phase=raw.get("phase"),
            status=raw.get("status"),
            duration_ms=duration_ms,
            attributes=dict(attrs),
            input_refs=[dict(x) for x in input_refs if isinstance(x, dict)],
            output_refs=[dict(x) for x in output_refs if isinstance(x, dict)],
        )

    def to_jsonl_record(self) -> dict[str, Any]:
        """双写：新 envelope + 旧 JSONL 顶层字段，兼容 analyze_usage / TraceViewer。"""
        record = self.to_dict()
        attrs = dict(self.attributes or {})
        if self.phase:
            record["phase"] = self.phase
        if self.status:
            record["status"] = self.status
        if self.duration_ms is not None:
            record["duration_ms"] = self.duration_ms
        for key in ("step_index", "step_type", "tool_calls", "tokens_used", "tool_name"):
            if key in attrs and attrs[key] is not None:
                record[key] = attrs[key]
        if self.type == EventType.RUN_SUMMARY or attrs.get("event") == "run_summary":
            metadata = attrs.get("metadata") or {}
            record["event"] = "run_summary"
            record["phase"] = record.get("phase") or "run"
            record["metadata"] = metadata
            record["extra"] = {"event": "run_summary", "metadata": metadata}
            if "tool_calls_count" in metadata and "tool_calls" not in record:
                record["tool_calls"] = metadata.get("tool_calls_count")
        if self.type in {EventType.GEN_AI_CHAT, EventType.LLM_USAGE}:
            record["event"] = "llm_usage"
            for key in (
                "model",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cost_usd",
            ):
                if key in attrs:
                    record[key] = attrs[key]
        return record
