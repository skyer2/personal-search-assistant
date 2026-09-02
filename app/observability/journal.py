"""In-memory append-only journal + replay helpers."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from app.observability.events import AgentEvent


class RunJournal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, list[AgentEvent]] = defaultdict(list)

    def append(self, event: AgentEvent) -> None:
        with self._lock:
            self._by_session[event.session_id].append(event)

    def replay(self, session_id: str) -> list[AgentEvent]:
        with self._lock:
            return list(self._by_session.get(session_id, []))

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._by_session.clear()
            else:
                self._by_session.pop(session_id, None)


def build_span_tree(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把扁平 event 列表收成 span 因果树，供 TraceViewer 使用。"""
    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        span_id = str(event.get("span_id") or event.get("event_id") or f"anon-{len(nodes)}")
        if span_id not in nodes:
            nodes[span_id] = {
                "span_id": span_id,
                "parent_span_id": event.get("parent_span_id"),
                "name": event.get("type") or event.get("event") or event.get("phase") or "event",
                "phase": event.get("phase"),
                "status": event.get("status"),
                "duration_ms": event.get("duration_ms"),
                "task_id": event.get("task_id"),
                "plan_version": event.get("plan_version"),
                "attempt": event.get("attempt"),
                "timestamp": event.get("timestamp"),
                "events": [],
                "children": [],
            }
            order.append(span_id)
        node = nodes[span_id]
        node["name"] = _preferred_span_name(node, event)
        if event.get("duration_ms") is not None:
            node["duration_ms"] = event.get("duration_ms")
        if event.get("status"):
            node["status"] = event.get("status")
        if event.get("parent_span_id") and not node.get("parent_span_id"):
            node["parent_span_id"] = event.get("parent_span_id")
        if event.get("task_id") and not node.get("task_id"):
            node["task_id"] = event.get("task_id")
        node["events"].append(
            {
                "type": event.get("type") or event.get("event"),
                "status": event.get("status"),
                "timestamp": event.get("timestamp"),
                "duration_ms": event.get("duration_ms"),
            }
        )

    roots: list[dict[str, Any]] = []
    for span_id in order:
        node = nodes[span_id]
        parent = node.get("parent_span_id")
        if parent and parent in nodes and parent != span_id:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
    return {"roots": roots, "span_count": len(nodes), "event_count": len(events)}


_SPAN_NAME_PRIORITY = (
    "research.run",
    "worker.execute",
    "worker.started",
    "worker.completed",
    "plan.created",
    "replan.applied",
    "progress.evaluated",
    "tool.started",
    "gen_ai.chat",
    "quality.evaluated",
    "eval.scored",
)


def _preferred_span_name(node: dict[str, Any], event: dict[str, Any]) -> str:
    current = str(node.get("name") or "")
    incoming = str(event.get("type") or event.get("event") or event.get("phase") or "")
    if current in _SPAN_NAME_PRIORITY and incoming not in _SPAN_NAME_PRIORITY:
        return current
    if incoming in _SPAN_NAME_PRIORITY:
        return incoming
    return current or incoming or "event"


def summarize_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把 journal 收成 Agent-native 视图：identity / worker / replan / evidence / eval / usage。"""
    identity: dict[str, Any] = {}
    workers: list[dict[str, Any]] = []
    replans: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    evals: list[dict[str, Any]] = []
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.0,
        "calls": 0,
    }
    for event in events:
        event_type = str(event.get("type") or event.get("event") or "")
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        if not identity.get("session_id"):
            identity = {
                "session_id": event.get("session_id"),
                "run_id": event.get("run_id"),
                "trace_id": event.get("trace_id"),
                "git_sha": attrs.get("git_sha") or event.get("git_sha"),
                "config_hash": attrs.get("config_hash") or event.get("config_hash"),
                "variant": attrs.get("variant") or event.get("variant"),
            }
        if event_type.startswith("worker."):
            workers.append(
                {
                    "type": event_type,
                    "task_id": event.get("task_id") or attrs.get("task_id"),
                    "status": event.get("status"),
                    "duration_ms": event.get("duration_ms"),
                    "attempt": event.get("attempt"),
                    "plan_version": event.get("plan_version"),
                    "objective": attrs.get("objective"),
                    "fail_reason": attrs.get("fail_reason") or event.get("fail_reason"),
                    "evidence_ids": attrs.get("evidence_ids") or [],
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type.startswith("replan."):
            replans.append(
                {
                    "type": event_type,
                    "from_plan_version": attrs.get("from_plan_version"),
                    "to_plan_version": attrs.get("to_plan_version") or event.get("plan_version"),
                    "reason": attrs.get("reason"),
                    "gaps": attrs.get("gaps") or [],
                    "added_tasks": attrs.get("added_tasks") or [],
                    "removed_tasks": attrs.get("removed_tasks") or [],
                    "remaining_budget": attrs.get("remaining_budget") or {},
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type == "evidence.registered":
            evidence.append(
                {
                    "evidence_id": attrs.get("evidence_id") or attrs.get("source_id"),
                    "artifact_id": attrs.get("artifact_id"),
                    "task_id": event.get("task_id"),
                    "locator": attrs.get("locator"),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type in {"eval.scored", "quality.evaluated"}:
            evals.append(
                {
                    "case_id": attrs.get("case_id"),
                    "variant": attrs.get("variant"),
                    "accuracy": attrs.get("accuracy"),
                    "citation_score": attrs.get("citation_score") or attrs.get("citation_coverage_rate"),
                    "replan_count": attrs.get("replan_count"),
                    "latency_ms": attrs.get("latency_ms"),
                    "status": event.get("status"),
                    "type": event_type,
                }
            )
        elif event_type in {"gen_ai.chat", "llm_usage"}:
            usage["calls"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens"):
                usage[key] += int(attrs.get(key) or event.get(key) or 0)
            usage["cost_usd"] += float(attrs.get("cost_usd") or event.get("cost_usd") or 0.0)
    return {
        "identity": identity,
        "workers": workers,
        "replans": replans,
        "evidence": evidence,
        "evals": evals,
        "usage": usage,
        "event_count": len(events),
        "worker_count": len({row.get("task_id") for row in workers if row.get("task_id")}),
        "replan_count": sum(1 for row in replans if row.get("type") == "replan.applied"),
    }
