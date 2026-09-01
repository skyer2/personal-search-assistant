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
        if event.get("duration_ms") is not None:
            node["duration_ms"] = event.get("duration_ms")
        if event.get("status"):
            node["status"] = event.get("status")
        if event.get("parent_span_id") and not node.get("parent_span_id"):
            node["parent_span_id"] = event.get("parent_span_id")
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
