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
        self.emit(
            event_type,
            phase="run",
            status=status,
            duration_ms=duration_ms,
            attributes={"metadata": metadata or {}, "result_preview": result_preview[:240], "error": error},
        )
        self.emit(
            EventType.RUN_SUMMARY,
            phase="run",
            status=status,
            duration_ms=duration_ms,
            attributes={"event": "run_summary", "metadata": metadata or {}},
        )
        replan_count = int((metadata or {}).get("replan_count") or 0)
        if replan_count > 0:
            if event_type == EventType.RUN_COMPLETED:
                self.metrics.inc("harness.replan.recovered")
            else:
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
            ctx.parent_span_id = parent
            ctx.span_id = span_id
            if task_id:
                ctx.task_id = task_id
            if attempt is not None:
                ctx.attempt = attempt
            set_context(ctx)
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
            ctx.span_id = handle.parent_span_id
            set_context(ctx)

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
        )
        self.journal.append(event)
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
            # 保存 phase+task+attempt → unique key，便于 end 时找回
            with self._lock:
                self._spans[key] = self._spans[unique_key]
            if ctx is not None:
                ctx.parent_span_id = parent
                ctx.span_id = span_id
                ctx.task_id = task_id or ctx.task_id
                ctx.attempt = attempt if attempt is not None else ctx.attempt
                set_context(ctx)
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
        otel_span = otel_start_span(
            name,
            {
                "span.id": span_id,
                "parent.span.id": parent_span_id or "",
                **{f"agent.{k}": v for k, v in (attributes or {}).items() if isinstance(v, (str, int, float, bool))},
            },
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
