"""Single emit path: business code → AgentTelemetry → exporters."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.observability.context import (
    ObservabilityContext,
    bind_run,
    current_context,
    reset_run,
    set_context,
)
from app.observability.events import AgentEvent, EventType, new_id, span_identity, utc_now
from app.observability.exporters.jsonl import JsonlExporter
from app.observability.exporters.otel import end_span as otel_end_span
from app.observability.exporters.otel import flush_otel, init_otel, start_span as otel_start_span
from app.observability.exporters.websocket import WebSocketExporter
from app.observability.failure import enrich_failure_attributes
from app.observability.journal import RunJournal
from app.observability.metrics import get_metrics
from app.observability.paths import REPO_ROOT, traces_log_dir
from app.observability.privacy import sanitize_attributes

_SPAN_END_STATUSES = frozenset(
    {
        "done",
        "ok",
        "failed",
        "error",
        "cancelled",
        "rejected",
        "budget_exceeded",
        "warning",
        "budget_tool_calls",
        "budget_tokens",
        "deadline_exceeded",
    }
)


def _git_sha() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=REPO_ROOT,
                timeout=2,
                stderr=subprocess.DEVNULL,
            )
            .decode("utf-8")
            .strip()
        )
    except Exception:
        return ""


def _config_hash() -> str:
    try:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        raw = (
            f"{cfg.max_replan_count}|{cfg.max_tool_calls}|{cfg.max_run_sec}|"
            f"{cfg.progress_eval_enabled}|{cfg.jsonl_log_enabled}"
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return ""


@dataclass
class OpenSpan:
    key: str
    span_id: str
    parent_span_id: str | None
    name: str
    started_at: float
    otel_span: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)


class AgentTelemetry:
    def __init__(self) -> None:
        self.journal = RunJournal()
        self.metrics = get_metrics()
        self._lock = threading.Lock()
        self._spans: dict[str, OpenSpan] = {}
        self._jsonl: JsonlExporter | None = None
        self._ws = WebSocketExporter()
        self._ws_enabled = True
        self._listeners: list[Callable[[AgentEvent], None]] = []
        self._emitting = threading.local()
        self._tool_spans: dict[str, str] = {}

    @property
    def is_active(self) -> bool:
        return current_context() is not None

    def configure_jsonl(self, log_dir: Path, enabled: bool = True) -> None:
        self._jsonl = JsonlExporter(log_dir=log_dir, enabled=enabled)

    def add_listener(self, fn: Callable[[AgentEvent], None]) -> None:
        self._listeners.append(fn)

    def start_run(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        trace_id: str | None = None,
        variant: str = "",
        query_preview: str = "",
    ) -> ObservabilityContext:
        init_otel()
        ctx, _token = bind_run(
            session_id=session_id,
            run_id=run_id or session_id,
            trace_id=trace_id,
            git_sha=_git_sha(),
            config_hash=_config_hash(),
            variant=variant,
        )
        ctx.span_id = new_id()
        ctx.root_span_id = ctx.span_id
        set_context(ctx)
        root_key = span_identity("research.run", span_id=ctx.root_span_id)
        self._open_span(root_key, name="research.run", span_id=ctx.root_span_id, parent_span_id=None)
        self.emit(
            EventType.RUN_STARTED,
            phase="run",
            status="start",
            attributes={"query_preview": query_preview[:240], "git_sha": ctx.git_sha, "config_hash": ctx.config_hash, "variant": variant},
        )
        self.metrics.inc("harness.runs.started")
        return ctx

    def finish_run(
        self,
        *,
        status: str,
        duration_ms: int,
        metadata: dict[str, Any] | None = None,
        result_preview: str = "",
        error: str = "",
    ) -> None:
        ctx = current_context()
        event_type = EventType.RUN_COMPLETED if status in {"success", "partial", "ok"} else EventType.RUN_FAILED
        attributes = {
            "metadata": metadata or {},
            "result_preview": result_preview[:240],
            "error": error,
        }
        origin_stage = str((metadata or {}).get("failure.origin_stage") or "")
        detected_stage = str(
            (metadata or {}).get("failure.detected_stage") or origin_stage
        )
        if origin_stage:
            attributes["failure.origin_stage"] = origin_stage
            attributes["failure.detected_stage"] = detected_stage
            attributes["failure.stage"] = origin_stage
        self.emit(
            event_type,
            phase="run",
            status=status,
            duration_ms=duration_ms,
            attributes=attributes,
        )
        self.emit(
            EventType.RUN_SUMMARY,
            phase="run",
            status=status,
            duration_ms=duration_ms,
            attributes={
                "event": "run_summary",
                "metadata": metadata or {},
                **(
                    {
                        "failure.origin_stage": origin_stage,
                        "failure.detected_stage": detected_stage,
                        "failure.stage": origin_stage,
                    }
                    if origin_stage
                    else {}
                ),
            },
        )
        replan_count = int((metadata or {}).get("replan_count") or 0)
        if replan_count > 0:
            try:
                from app.observability.semantic import compute_replan_gap_closure

                run_events = []
                if ctx is not None:
                    run_events = [
                        event.to_dict()
                        for event in self.journal.events_for_run(ctx.session_id, ctx.run_id)
                    ]
                closure = compute_replan_gap_closure(run_events)
                if closure.get("replan_useful"):
                    self.metrics.inc("harness.replan.recovered")
                elif event_type != EventType.RUN_COMPLETED:
                    self.metrics.inc("harness.replan.waste")
                if closure.get("gap_closure_rate") is not None:
                    self.metrics.observe(
                        "harness.replan.gap_closure_rate",
                        float(closure["gap_closure_rate"]),
                    )
            except Exception:
                if event_type != EventType.RUN_COMPLETED:
                    self.metrics.inc("harness.replan.waste")
        if ctx and ctx.root_span_id:
            self.end_span(
                span_identity("research.run", span_id=ctx.root_span_id),
                status=status,
                duration_ms=duration_ms,
            )
        self.metrics.observe("harness.run.duration_ms", float(duration_ms))
        if event_type == EventType.RUN_COMPLETED:
            self.metrics.inc("harness.runs.completed")
        else:
            self.metrics.inc("harness.runs.failed")
        flush_otel()
        reset_run(None)

    def start_span(
        self,
        name: str,
        *,
        phase: str | None = None,
        task_id: str | None = None,
        attempt: int | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        ctx = current_context()
        span_id = new_id()
        parent = ctx.span_id if ctx else None
        key = span_identity(phase or name, task_id=task_id, attempt=attempt, span_id=span_id)
        self._open_span(key, name=name, span_id=span_id, parent_span_id=parent, attributes=attributes)
        if ctx is not None:
            child = ctx.child(
                span_id=span_id,
                parent_span_id=parent,
                task_id=task_id or ctx.task_id,
                attempt=attempt if attempt is not None else ctx.attempt,
            )
            set_context(child)
        return key

    def end_span(
        self,
        key: str,
        *,
        status: str = "ok",
        duration_ms: int | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            handle = self._spans.pop(key, None)
            if handle is not None and handle.key != key:
                self._spans.pop(handle.key, None)
        if handle is None:
            return
        elapsed = duration_ms
        if elapsed is None:
            elapsed = int((time.perf_counter() - handle.started_at) * 1000)
        otel_end_span(handle.otel_span, status=status, attributes=attributes)
        ctx = current_context()
        if ctx is not None and ctx.span_id == handle.span_id:
            # Walk the open span stack to find grandparent
            grandparent = None
            if handle.parent_span_id:
                with self._lock:
                    for other in self._spans.values():
                        if other.span_id == handle.parent_span_id:
                            grandparent = other.parent_span_id
                            break
            restored = ctx.child(
                span_id=handle.parent_span_id,
                parent_span_id=grandparent,
            )
            set_context(restored)

    def emit(
        self,
        event_type: str,
        *,
        phase: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        plan_version: int | None = None,
        task_id: str | None = None,
        attempt: int | None = None,
        attributes: dict[str, Any] | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        input_refs: list[dict[str, Any]] | None = None,
        output_refs: list[dict[str, Any]] | None = None,
        to_ws: bool = True,
        session_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> AgentEvent:
        if getattr(self._emitting, "busy", False):
            # 防止 exporter → monitor → emit 递归
            ctx = current_context()
            return AgentEvent(
                event_id=new_id(),
                trace_id=trace_id or (ctx.trace_id if ctx else ""),
                span_id=span_id or (ctx.span_id if ctx else new_id()),
                run_id=run_id or (ctx.run_id if ctx else ""),
                session_id=session_id or (ctx.session_id if ctx else ""),
                seq=0,
                timestamp=utc_now(),
                type=event_type,
            )
        ctx = current_context()
        session_id = session_id or (ctx.session_id if ctx else str((attributes or {}).get("session_id") or "unknown"))
        run_id = run_id or (ctx.run_id if ctx else str((attributes or {}).get("run_id") or session_id))
        trace_id = trace_id or (ctx.trace_id if ctx else str((attributes or {}).get("trace_id") or new_id(32)))
        seq = ctx.next_seq() if ctx else 0
        attrs = dict(attributes or {})
        refs_in = list(input_refs or attrs.pop("input_refs", None) or [])
        refs_out = list(output_refs or attrs.pop("output_refs", None) or [])
        if (
            event_type.endswith(".failed")
            or status in {"failed", "error", "fail"}
            or attrs.get("fail_reason")
        ) and str(status or "").lower() != "warning":
            attrs = enrich_failure_attributes(
                attrs,
                reason=str(attrs.get("fail_reason") or attrs.get("error") or status or ""),
                phase=phase,
                event_type=event_type,
            )
        attributes = attrs
        event = AgentEvent(
            event_id=new_id(20),
            trace_id=trace_id,
            span_id=span_id or (ctx.span_id if ctx else new_id()),
            parent_span_id=parent_span_id if parent_span_id is not None else (ctx.parent_span_id if ctx else None),
            run_id=run_id,
            session_id=session_id,
            seq=seq,
            timestamp=utc_now(),
            type=event_type,
            plan_version=plan_version if plan_version is not None else (ctx.plan_version if ctx else None),
            task_id=task_id if task_id is not None else (ctx.task_id if ctx else None),
            attempt=attempt if attempt is not None else (ctx.attempt if ctx else None),
            phase=phase,
            status=status,
            duration_ms=duration_ms,
            attributes=sanitize_attributes(attributes),
            input_refs=[dict(x) for x in refs_in if isinstance(x, dict)],
            output_refs=[dict(x) for x in refs_out if isinstance(x, dict)],
        )
        self.journal.append(event)
        try:
            from app.observability.projection_store import get_projection_store
            get_projection_store().append(event)
        except Exception:
            # JSONL remains the audit fallback if the query store is unavailable.
            pass
        self._record_metrics(event)
        self._emitting.busy = True
        try:
            if self._jsonl is not None:
                self._jsonl.export(event)
            if self._ws_enabled and to_ws:
                self._ws.export(event)
            for listener in self._listeners:
                listener(event)
        finally:
            self._emitting.busy = False
        return event

    def emit_phase(
        self,
        phase: str,
        status: str,
        *,
        task_id: str | None = None,
        attempt: int | None = None,
        plan_version: int | None = None,
        duration_ms: int | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> AgentEvent:
        key = span_identity(phase, task_id=task_id, attempt=attempt)
        if status == "start":
            ctx = current_context()
            parent = ctx.span_id if ctx else None
            span_id = new_id()
            unique_key = span_identity(phase, task_id=task_id, attempt=attempt, span_id=span_id)
            self._open_span(unique_key, name=phase, span_id=span_id, parent_span_id=parent, attributes=attributes)
            with self._lock:
                self._spans[key] = self._spans[unique_key]
            if ctx is not None:
                child = ctx.child(
                    span_id=span_id,
                    parent_span_id=parent,
                    task_id=task_id or ctx.task_id,
                    attempt=attempt if attempt is not None else ctx.attempt,
                )
                set_context(child)
            span_id_for_event = span_id
            parent_for_event = parent
        else:
            with self._lock:
                handle = self._spans.get(key)
            span_id_for_event = handle.span_id if handle else None
            parent_for_event = handle.parent_span_id if handle else None
            # context_built / awaiting_approval 等中间状态不能关掉并行 Worker span
            if status in _SPAN_END_STATUSES:
                self.end_span(key, status=status, duration_ms=duration_ms, attributes=attributes)
        return self.emit(
            EventType.PHASE,
            phase=phase,
            status=status,
            duration_ms=duration_ms,
            task_id=task_id,
            attempt=attempt,
            plan_version=plan_version,
            attributes=attributes,
            span_id=span_id_for_event,
            parent_span_id=parent_for_event,
        )

    def _open_span(
        self,
        key: str,
        *,
        name: str,
        span_id: str,
        parent_span_id: str | None,
        attributes: dict[str, Any] | None = None,
    ) -> OpenSpan:
        otel_parent = self._otel_parent(parent_span_id)
        otel_span = otel_start_span(
            name,
            {
                "agent.span_id": span_id,
                "agent.parent_span_id": parent_span_id or "",
                **(attributes or {}),
            },
            parent=otel_parent,
        )
        handle = OpenSpan(
            key=key,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=name,
            started_at=time.perf_counter(),
            otel_span=otel_span,
            attributes=dict(attributes or {}),
        )
        with self._lock:
            self._spans[key] = handle
        return handle

    def _otel_parent(self, parent_span_id: str | None) -> Any:
        if not parent_span_id:
            return None
        with self._lock:
            for handle in self._spans.values():
                if handle.span_id == parent_span_id and handle.otel_span is not None:
                    return handle.otel_span
        return None

    def _handle(self, key: str) -> OpenSpan | None:
        with self._lock:
            return self._spans.get(key)

    def begin_tool(
        self,
        tool_name: str,
        *,
        tool_call_id: str = "",
        args: dict[str, Any] | None = None,
    ) -> str:
        call_id = tool_call_id or new_id()
        key = self.start_span(
            f"tool.{tool_name}",
            phase="execute",
            attributes={"tool_name": tool_name, "tool_call_id": call_id},
        )
        with self._lock:
            self._tool_spans[call_id] = key
        handle = self._handle(key)
        self.emit(
            EventType.TOOL_STARTED,
            phase="execute",
            status="start",
            span_id=handle.span_id if handle else None,
            parent_span_id=handle.parent_span_id if handle else None,
            attributes={"tool_name": tool_name, "tool_call_id": call_id, "args": args or {}},
        )
        return call_id

    def finish_tool(
        self,
        tool_name: str,
        *,
        tool_call_id: str = "",
        duration_ms: int | None = None,
        status: str = "ok",
        error: str = "",
        result_ref: str = "",
        result_count: int = 0,
        result_bytes: int = 0,
        artifact_ids: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        call_id = tool_call_id or ""
        with self._lock:
            key = self._tool_spans.pop(call_id, "") if call_id else ""
            if not key and not call_id and self._tool_spans:
                call_id, key = self._tool_spans.popitem()
        handle = self._handle(key) if key else None
        event_type = EventType.TOOL_COMPLETED if status == "ok" else EventType.TOOL_FAILED
        attrs = {
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "error": error,
            "fail_reason": error if status != "ok" else "",
            "result_ref": result_ref,
            "result_count": int(result_count or 0),
            "result_bytes": int(result_bytes or 0),
            "artifact_ids": list(artifact_ids or []),
            **(extra or {}),
        }
        is_search = str(tool_name or "").lower() in {
            "internet_search",
            "web_search",
            "tavily_search",
            "search",
        }
        if is_search and status == "ok":
            self.emit(
                EventType.RETRIEVAL_SEARCH,
                phase="execute",
                status=status,
                duration_ms=duration_ms,
                span_id=handle.span_id if handle else None,
                parent_span_id=handle.parent_span_id if handle else None,
                attributes={
                    **attrs,
                    "document_ids": list((extra or {}).get("document_ids") or []),
                    "domains": list((extra or {}).get("domains") or []),
                    "top_k": (extra or {}).get("top_k"),
                },
            )
        self.emit(
            event_type,
            phase="execute",
            status=status,
            duration_ms=duration_ms,
            span_id=handle.span_id if handle else None,
            parent_span_id=handle.parent_span_id if handle else None,
            attributes=attrs,
        )
        if key:
            self.end_span(key, status=status, duration_ms=duration_ms)

    def record_generation(
        self,
        *,
        model: str,
        phase: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_ms: int | None = None,
        finish_reason: str = "",
        usage_missing: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> AgentEvent:
        attributes = {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cost_usd": cost_usd,
            "finish_reason": finish_reason,
            "usage_missing": usage_missing,
            **(extra or {}),
        }
        key = self.start_span("gen_ai.chat", phase=phase, attributes=attributes)
        handle = self._handle(key)
        event = self.emit(
            EventType.GEN_AI_CHAT,
            phase=phase,
            status="ok",
            duration_ms=duration_ms,
            span_id=handle.span_id if handle else None,
            parent_span_id=handle.parent_span_id if handle else None,
            attributes=attributes,
        )
        self.end_span(key, status="ok", duration_ms=duration_ms, attributes={"model": model})
        return event

    def _record_metrics(self, event: AgentEvent) -> None:
        if event.type == EventType.TOOL_STARTED:
            self.metrics.inc("harness.tool.calls")
        if event.type in {EventType.TOOL_COMPLETED, EventType.TOOL_FAILED} and event.duration_ms is not None:
            self.metrics.observe("harness.tool.duration_ms", float(event.duration_ms))
        if event.type == EventType.WORKER_FAILED:
            self.metrics.inc("harness.worker.failed")
        if event.type in {EventType.WORKER_COMPLETED, EventType.WORKER_FAILED} and event.duration_ms is not None:
            self.metrics.observe("harness.worker.duration_ms", float(event.duration_ms))
        if event.type == EventType.WORKER_STARTED and int(event.attempt or 0) > 1:
            self.metrics.inc("harness.worker.retry")
        if event.type == EventType.PLAN_VALIDATED and event.status == "issues":
            self.metrics.inc("harness.plan.validation_failed")
        if event.type == EventType.QUALITY_EVALUATED and event.status == "fail":
            self.metrics.inc("harness.quality.failed")
        if event.type == EventType.REPLAN_APPLIED:
            self.metrics.inc("harness.replan.applied")
        if event.type == EventType.REPLAN_REJECTED:
            self.metrics.inc("harness.replan.rejected")
        if event.type == EventType.PROGRESS_EVALUATED and str((event.attributes or {}).get("verdict") or "") == "gap":
            self.metrics.inc("harness.progress.gap")
        if event.type == EventType.GEN_AI_CHAT:
            tokens = float((event.attributes or {}).get("total_tokens") or 0)
            cost = float((event.attributes or {}).get("cost_usd") or 0)
            self.metrics.inc("harness.llm.calls")
            if tokens:
                self.metrics.observe("harness.llm.tokens", tokens)
            if cost:
                self.metrics.observe("harness.llm.cost_usd", cost)
        if event.status in {"budget_exceeded", "budget_tool_calls", "budget_tokens", "deadline_exceeded"}:
            self.metrics.inc("harness.budget.exhausted")


_RECORDER: AgentTelemetry | None = None


def get_recorder() -> AgentTelemetry:
    global _RECORDER
    if _RECORDER is None:
        _RECORDER = AgentTelemetry()
        try:
            from app.config.loader import get_harness_config

            cfg = get_harness_config()
            _RECORDER.configure_jsonl(traces_log_dir(), enabled=cfg.jsonl_log_enabled)
        except Exception:
            pass
    return _RECORDER
