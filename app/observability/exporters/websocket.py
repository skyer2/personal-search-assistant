"""WebSocket exporter — maps AgentEvent onto the existing monitor_event envelope."""

from __future__ import annotations

from typing import Any

from app.observability.events import AgentEvent, EventType


def monitor_payload(event: AgentEvent) -> dict[str, Any] | None:
    """把统一事件转成前端已认识的 monitor_event，避免再发明一套 UI schema。"""
    attrs = dict(event.attributes or {})
    data = {
        "span_id": event.span_id,
        "parent_span_id": event.parent_span_id,
        "trace_id": event.trace_id,
        "run_id": event.run_id,
        "session_id": event.session_id,
        "plan_version": event.plan_version,
        "task_id": event.task_id,
        "attempt": event.attempt,
        "duration_ms": event.duration_ms,
        **{k: v for k, v in attrs.items() if k not in {"metadata", "args"}},
    }
    mapping = {
        EventType.TOOL_STARTED: ("tool_start", f"开始执行工具: {attrs.get('tool_name') or ''}"),
        EventType.TOOL_COMPLETED: (
            "tool_end",
            f"工具完成: {attrs.get('tool_name') or ''}"
            + (f" ({event.duration_ms}ms)" if event.duration_ms is not None else ""),
        ),
        EventType.TOOL_FAILED: ("tool_error", f"工具失败: {attrs.get('tool_name') or ''}"),
        EventType.PHASE: (
            "phase",
            f"[{event.phase or attrs.get('phase')}] {event.status or ''}",
        ),
        EventType.WORKER_STARTED: (
            "worker",
            f"[worker] start task={event.task_id or '-'}",
        ),
        EventType.WORKER_COMPLETED: (
            "worker",
            f"[worker] done task={event.task_id or '-'}",
        ),
        EventType.WORKER_FAILED: (
            "worker",
            f"[worker] failed task={event.task_id or '-'}",
        ),
        EventType.PROGRESS_EVALUATED: (
            "progress",
            f"[progress] {attrs.get('verdict') or event.status or ''}",
        ),
        EventType.REPLAN_PROPOSED: (
            "replan",
            f"[replan] proposed {attrs.get('reason') or ''}".strip(),
        ),
        EventType.REPLAN_APPLIED: (
            "replan",
            f"[replan] {attrs.get('from_plan_version')}→{attrs.get('to_plan_version')}",
        ),
        EventType.REPLAN_REJECTED: ("replan", "[replan] rejected"),
        EventType.HITL_INTERRUPT: ("hitl_interrupt", "等待人工审批"),
        EventType.RUN_FAILED: ("error", str(attrs.get("error") or "任务失败")),
        EventType.RUN_COMPLETED: ("task_result", "任务执行完成"),
        EventType.PLAN_CREATED: ("plan", "[plan] created"),
        EventType.BRIEF_COMPILED: ("brief", "[brief] compiled"),
        EventType.SYNTHESIS_STARTED: ("synthesis", "[synthesis] start"),
        EventType.SYNTHESIS_COMPLETED: ("synthesis", "[synthesis] done"),
        EventType.SYNTHESIS_FAILED: ("synthesis", "[synthesis] failed"),
        EventType.RECOVERY_DECIDED: ("recovery", "[recovery] decided"),
        EventType.BUDGET_EXHAUSTED: ("budget", "[budget] exhausted"),
        EventType.CHECKPOINT_SAVED: ("checkpoint", "[checkpoint] saved"),
        EventType.CHECKPOINT_RESUMED: ("checkpoint", "[checkpoint] resumed"),
        EventType.CONTEXT_BUILT: ("context", "[context] built"),
        EventType.CONTEXT_COMPRESSED: ("context", "[context] compressed"),
        EventType.RETRIEVAL_SEARCH: ("retrieval", "[retrieval] search"),
        EventType.EVIDENCE_REGISTERED: (
            "evidence",
            f"[evidence] {attrs.get('evidence_id') or attrs.get('source_id') or ''}",
        ),
    }
    mapped = mapping.get(event.type)
    if mapped is None:
        return None
    event_type, message = mapped
    # finish_run 只带 result_preview；完整正文走 persist + report_task_result。
    # 只有显式 result 才能作为 UI 最终答案，避免 240 字预览覆盖完整结果。
    if event.type == EventType.RUN_COMPLETED and not attrs.get("result"):
        return None
    data["seq"] = event.seq
    data["event_id"] = event.event_id
    if event.type == EventType.PHASE:
        data.setdefault("phase", event.phase)
        data.setdefault("status", event.status)
    if event.type == EventType.HITL_INTERRUPT:
        data.update({k: v for k, v in attrs.items() if k in {"action_requests", "review_configs", "gate_type"}})
    if event.type == EventType.RUN_COMPLETED and attrs.get("result"):
        data["result"] = attrs.get("result")
    return {
        "monitor_event": event_type,
        "message": message.strip(),
        "data": {k: v for k, v in data.items() if v is not None},
    }


def wire_payload(event: AgentEvent, *, replay: bool = False) -> dict[str, Any] | None:
    """Canonical AgentEvent → 前端 monitor_event 线路格式，含 seq 以便去重 / replay."""
    mapped = monitor_payload(event)
    if mapped is None:
        return None
    data = dict(mapped["data"])
    if replay:
        data["replay"] = True
    return {
        "type": "monitor_event",
        "event": mapped["monitor_event"],
        "message": mapped["message"],
        "data": data,
        "timestamp": event.timestamp,
        "run_id": event.run_id,
        "session_id": event.session_id,
        "seq": event.seq,
        "event_id": event.event_id,
        "replay": replay,
    }


class WebSocketExporter:
    def export(self, event: AgentEvent) -> None:
        payload = monitor_payload(event)
        if payload is None:
            return
        try:
            from app.api.monitor import monitor

            monitor.forward_canonical_event(
                payload["monitor_event"],
                payload["message"],
                payload["data"],
            )
        except Exception as exc:
            print(f"[Observability] websocket export failed: {exc}")
