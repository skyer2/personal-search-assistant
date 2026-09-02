"""Replay AgentEvents from the in-memory journal, falling back to durable JSONL."""

from __future__ import annotations

from typing import Any

from app.observability.events import AgentEvent
from app.observability.exporters.websocket import wire_payload
from app.observability.paths import traces_log_dir
from app.observability.recorder import get_recorder


def events_from_jsonl(session_id: str) -> list[AgentEvent]:
    try:
        from app.observability.exporters.jsonl import JsonlExporter

        exporter = JsonlExporter(log_dir=traces_log_dir(), enabled=True)
        records = exporter.read(session_id)
    except Exception:
        return []
    events: list[AgentEvent] = []
    for raw in records:
        try:
            events.append(AgentEvent.from_dict(raw))
        except Exception:
            continue
    return events


def load_events(
    session_id: str,
    *,
    run_id: str | None = None,
    after_seq: int = 0,
    before_seq: int | None = None,
    limit: int | None = None,
) -> list[AgentEvent]:
    recorder = get_recorder()
    events = recorder.journal.replay(session_id)
    if not events:
        events = events_from_jsonl(session_id)
    if run_id:
        events = [event for event in events if event.run_id == run_id]
    if after_seq:
        events = [event for event in events if int(event.seq or 0) > after_seq]
    if before_seq is not None:
        events = [event for event in events if int(event.seq or 0) < before_seq]
    events.sort(key=lambda event: (int(event.seq or 0), event.timestamp or ""))
    if limit is not None and limit >= 0:
        if before_seq is not None or not after_seq:
            events = events[-limit:]
        else:
            events = events[:limit]
    return events


def load_wire_events(
    session_id: str,
    *,
    run_id: str | None = None,
    after_seq: int = 0,
    before_seq: int | None = None,
    limit: int | None = 120,
    replay: bool = True,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in load_events(
        session_id,
        run_id=run_id,
        after_seq=after_seq,
        before_seq=before_seq,
        limit=limit,
    ):
        payload = wire_payload(event, replay=replay)
        if payload is not None:
            payloads.append(payload)
    return payloads
