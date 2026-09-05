"""Replay AgentEvents from the in-memory journal merged with durable JSONL."""

from __future__ import annotations

from typing import Any

from app.observability.events import AgentEvent
from app.observability.exporters.websocket import wire_payload
from app.observability.journal import build_span_tree, summarize_trace
from app.observability.paths import traces_log_dir
from app.observability.recorder import get_recorder


def events_from_jsonl(session_id: str, run_id: str | None = None) -> list[AgentEvent]:
    try:
        from app.observability.exporters.jsonl import JsonlExporter

        exporter = JsonlExporter(log_dir=traces_log_dir(), enabled=True)
        records = exporter.read(session_id, run_id=run_id)
    except Exception:
        return []
    events: list[AgentEvent] = []
    for raw in records:
        try:
            events.append(AgentEvent.from_dict(raw))
        except Exception:
            continue
    return events


def merge_events(*groups: list[AgentEvent]) -> list[AgentEvent]:
    """Dedupe by event_id (prefer first seen), then sort by (run_id, seq, timestamp)."""
    merged: list[AgentEvent] = []
    seen: set[str] = set()
    for group in groups:
        for event in group:
            event_id = str(event.event_id or "").strip()
            key = f"{event.run_id}:{event.seq}:{event_id}" if event_id else ""
            if key:
                if key in seen:
                    continue
                seen.add(key)
            merged.append(event)
    merged.sort(
        key=lambda event: (
            str(event.run_id or ""),
            int(event.seq or 0),
            event.timestamp or "",
        )
    )
    return merged


def load_events(
    session_id: str,
    *,
    run_id: str | None = None,
    after_seq: int = 0,
    before_seq: int | None = None,
    limit: int | None = None,
) -> list[AgentEvent]:
    recorder = get_recorder()
    memory_events = (
        recorder.journal.events_for_run(session_id, run_id)
        if run_id
        else recorder.journal.replay(session_id)
    )
    durable_events: list[AgentEvent] = []
    if run_id:
        try:
            from app.observability.projection_store import get_projection_store

            durable_events = [
                AgentEvent.from_dict(row)
                for row in get_projection_store().events(
                    run_id,
                    after_seq=after_seq,
                    before_seq=before_seq,
                    limit=limit,
                )
                if str(row.get("session_id") or "") == session_id
            ]
        except Exception:
            durable_events = []
    if not durable_events:
        durable_events = events_from_jsonl(session_id, run_id=run_id)
    # Durable first so in-memory updates with same event_id win? Prefer memory for live.
    # Spec: merge durable + memory, dedupe by event_id. Memory usually has fresher copy —
    # put durable first then memory so memory overwrites when we change to last-wins.
    # Current merge keeps first; put memory after durable would drop memory duplicates.
    # Better: last wins for same event_id.
    events = _merge_last_wins(durable_events, memory_events)
    if run_id:
        events = [event for event in events if event.run_id == run_id]
    if after_seq:
        events = [event for event in events if int(event.seq or 0) > after_seq]
    if before_seq is not None:
        events = [event for event in events if int(event.seq or 0) < before_seq]
    events.sort(
        key=lambda event: (
            str(event.run_id or ""),
            int(event.seq or 0),
            event.timestamp or "",
        )
    )
    if limit is not None and limit >= 0:
        if before_seq is not None or not after_seq:
            events = events[-limit:]
        else:
            events = events[:limit]
    return events


def _merge_last_wins(*groups: list[AgentEvent]) -> list[AgentEvent]:
    by_id: dict[str, AgentEvent] = {}
    anonymous: list[AgentEvent] = []
    for group in groups:
        for event in group:
            event_id = str(event.event_id or "").strip()
            key = f"{event.run_id}:{event.seq}:{event_id}" if event_id else ""
            if not key:
                anonymous.append(event)
                continue
            by_id[key] = event
    merged = list(by_id.values()) + anonymous
    merged.sort(
        key=lambda event: (
            str(event.run_id or ""),
            int(event.seq or 0),
            event.timestamp or "",
        )
    )
    return merged


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


def load_trace_payload(
    session_id: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    events = load_events(session_id, run_id=run_id)
    records = [event.to_jsonl_record() for event in events]
    identity_run = run_id or (events[0].run_id if events else None)
    return {
        "session_id": session_id,
        "run_id": identity_run,
        "events": records,
        "total": len(records),
        "source": "agent_event.v1",
        "tree": build_span_tree(records),
        "summary": summarize_trace(records),
        "scope": "run" if run_id else "session",
    }


def load_summary_projection(session_id: str, *, run_id: str) -> dict[str, Any]:
    """Build only the overview projection; deliberately skips tree construction."""
    from app.observability.projection_store import get_projection_store

    store = get_projection_store()
    cached = store.get_projection(run_id, "summary")
    if cached is not None:
        return cached
    events = load_events(session_id, run_id=run_id)
    records = [event.to_jsonl_record() for event in events]
    summary = summarize_trace(
        records, include_lineage=False, include_tree_integrity=False
    )
    payload = {
        "session_id": session_id,
        "run_id": run_id,
        "summary": summary,
        "total": len(records),
    }
    store.put_projection(run_id, "summary", payload)
    return payload


def load_lineage_projection(session_id: str, *, run_id: str) -> list[dict[str, Any]]:
    from app.observability.semantic import build_lineage_edges
    from app.observability.projection_store import get_projection_store

    store = get_projection_store()
    cached = store.get_projection(run_id, "lineage")
    if cached is not None:
        return list(cached.get("items") or [])
    rows = build_lineage_edges(
        [event.to_jsonl_record() for event in load_events(session_id, run_id=run_id)]
    )
    store.put_projection(run_id, "lineage", {"items": rows})
    return rows


def load_tree_projection(session_id: str, *, run_id: str) -> dict[str, Any]:
    from app.observability.projection_store import get_projection_store

    store = get_projection_store()
    cached = store.get_projection(run_id, "tree")
    if cached is not None:
        return cached
    tree = build_span_tree(
        [event.to_jsonl_record() for event in load_events(session_id, run_id=run_id)]
    )
    store.put_projection(run_id, "tree", tree)
    return tree
