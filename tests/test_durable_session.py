"""Durable session projection：RUN_COMPLETED mapping、WS fanout、replay、bootstrap API。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.monitor import ConnectionManager
from app.observability.events import AgentEvent, EventType, utc_now
from app.observability.exporters.websocket import monitor_payload, wire_payload
from app.observability.replay import load_wire_events
from app.run_store import STATUS_COMPLETED, get_run_store, reset_run_store


def _event(event_type: str, **kwargs) -> AgentEvent:
    attrs = kwargs.pop("attributes", {})
    return AgentEvent(
        event_id=kwargs.get("event_id", "e1"),
        trace_id="t1",
        span_id="sp1",
        run_id=kwargs.get("run_id", "r1"),
        session_id=kwargs.get("session_id", "s1"),
        seq=kwargs.get("seq", 1),
        timestamp=utc_now(),
        type=event_type,
        attributes=attrs,
    )


def test_run_completed_requires_full_result():
    preview_only = _event(EventType.RUN_COMPLETED, attributes={"result_preview": "截断..."})
    assert monitor_payload(preview_only) is None
    full = _event(EventType.RUN_COMPLETED, attributes={"result": "完整答案"})
    payload = monitor_payload(full)
    assert payload is not None
    assert payload["monitor_event"] == "task_result"
    assert payload["data"]["result"] == "完整答案"
    wire = wire_payload(full, replay=True)
    assert wire["type"] == "monitor_event"
    assert wire["event"] == "task_result"
    assert wire["replay"] is True
    assert wire["seq"] == 1
    print("[OK] RUN_COMPLETED mapping")


def test_run_failed_still_maps():
    payload = monitor_payload(_event(EventType.RUN_FAILED, attributes={"error": "boom"}))
    assert payload is not None
    assert payload["monitor_event"] == "error"
    print("[OK] RUN_FAILED mapping")


def test_connection_manager_multitab():
    class FakeWS:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, message: dict) -> None:
            self.messages.append(message)

    manager = ConnectionManager()
    first = FakeWS()
    second = FakeWS()

    async def _run() -> None:
        await manager.connect(first, "abc")
        await manager.connect(second, "abc")
        assert "abc" in manager.active_connections
        assert len(manager.active_connections["abc"]) == 2
        await manager.send_to_thread({"type": "monitor_event", "event": "phase"}, "abc")
        manager.disconnect(first, "abc")
        assert len(manager.active_connections["abc"]) == 1
        await manager.send_to_thread({"type": "pong"}, "abc")

    asyncio.run(_run())
    assert first.messages == [{"type": "monitor_event", "event": "phase"}]
    assert second.messages == [
        {"type": "monitor_event", "event": "phase"},
        {"type": "pong"},
    ]
    print("[OK] multi-tab websocket fanout")


def test_replay_after_seq(tmp_path, monkeypatch):
    from app.observability.recorder import AgentTelemetry
    from app.observability import recorder as recorder_mod

    tel = AgentTelemetry()
    tel.configure_jsonl(tmp_path / "traces", enabled=True)
    monkeypatch.setattr(recorder_mod, "_RECORDER", tel)
    tel.journal.append(_event(EventType.TOOL_STARTED, seq=1, attributes={"tool_name": "a"}))
    tel.journal.append(_event(EventType.TOOL_STARTED, seq=2, attributes={"tool_name": "b"}))
    tel.journal.append(_event(EventType.TOOL_STARTED, seq=3, attributes={"tool_name": "c"}))
    replayed = load_wire_events("s1", run_id="r1", after_seq=1, limit=2000, replay=True)
    assert [item["seq"] for item in replayed] == [2, 3]
    assert all(item["replay"] for item in replayed)
    print("[OK] after_seq replay")


def test_bootstrap_api(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_STORE_PATH", str(tmp_path / "run.sqlite"))
    reset_run_store()
    store = get_run_store()
    store.create_run(run_id="r1", session_id="sess-ok", query="比较 LangGraph")
    store.complete_run("r1", result="完整答案", status=STATUS_COMPLETED)
    store.add_upload("sess-ok", "paper.pdf", 12)

    from fastapi.testclient import TestClient

    from app.api.server import app

    with TestClient(app) as client:
        ok = client.get("/api/sessions/sess-ok/bootstrap")
        assert ok.status_code == 200
        data = ok.json()
        assert data["found"] is True
        assert data["current_run"]["run_id"] == "r1"
        assert data["current_run"]["final_result"] == "完整答案"
        assert data["uploaded_files"][0]["name"] == "paper.pdf"

        missing = client.get("/api/sessions/stale-id/bootstrap")
        assert missing.status_code == 200
        assert missing.json()["found"] is False

        run = client.get("/api/runs/r1")
        assert run.json()["query"] == "比较 LangGraph"

    reset_run_store()
    print("[OK] bootstrap API")


def test_session_traces_and_run_trace(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_STORE_PATH", str(tmp_path / "run.sqlite"))
    reset_run_store()
    store = get_run_store()
    store.create_run(run_id="r-trace", session_id="sess-trace", query="观测一次 run")
    store.complete_run("r-trace", result="done", status=STATUS_COMPLETED)

    from app.observability.events import EventType
    from app.observability.recorder import get_recorder

    recorder = get_recorder()
    recorder._ws_enabled = False
    recorder._listeners = [store.on_event]
    recorder.start_run(session_id="sess-trace", run_id="r-trace")
    recorder.emit(EventType.WORKER_STARTED, task_id="t1", attributes={"objective": "obs"})
    recorder.finish_run(status="success", duration_ms=12)

    from fastapi.testclient import TestClient

    from app.api.server import app

    with TestClient(app) as client:
        listed = client.get("/api/sessions/sess-trace/traces")
        assert listed.status_code == 200
        body = listed.json()
        assert body["current_run_id"] == "r-trace"
        assert body["traces"][0]["run_id"] == "r-trace"
        trace = client.get("/api/runs/r-trace/trace")
        assert trace.status_code == 200
        payload = trace.json()
        assert payload["run_id"] == "r-trace"
        assert payload["scope"] == "run"
        assert payload["summary"]["identity"]["run_id"] == "r-trace"

    reset_run_store()
    print("[OK] run-centric trace API")


def test_startup_marks_running_recoverable(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_STORE_PATH", str(tmp_path / "run.sqlite"))
    reset_run_store()
    store = get_run_store()
    store.create_run(run_id="r-live", session_id="sess-live", query="还在跑")
    store.mark_running("r-live")

    from fastapi.testclient import TestClient

    from app.api.server import app

    with TestClient(app) as client:
        data = client.get("/api/sessions/sess-live/bootstrap").json()
        assert data["found"] is True
        assert data["current_run"]["status"] == "recoverable"

    reset_run_store()
    print("[OK] restart recovery via lifespan")


def test_download_pdf_inline_vs_attachment(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import session_routes
    from app.api.server import app

    monkeypatch.setattr(session_routes, "_OUTPUT_DIR", tmp_path)
    session_id = "sess-pdf"
    folder = tmp_path / f"session_{session_id}"
    folder.mkdir()
    (folder / "report.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (folder / "notes.md").write_text("# hi\n", encoding="utf-8")

    with TestClient(app) as client:
        opened = client.get(f"/api/sessions/{session_id}/download", params={"name": "report.pdf"})
        assert opened.status_code == 200
        assert opened.content.startswith(b"%PDF")
        assert "inline" in opened.headers.get("content-disposition", "").lower()
        saved = client.get(
            f"/api/sessions/{session_id}/download",
            params={"name": "report.pdf", "download": "1"},
        )
        assert saved.status_code == 200
        assert "attachment" in saved.headers.get("content-disposition", "").lower()
        listed = client.get(f"/api/sessions/{session_id}/artifacts")
        assert listed.status_code == 200
        names = [item["name"] for item in listed.json()["files"]]
        assert names[0] == "report.pdf"

    print("[OK] PDF download inline vs attachment")


if __name__ == "__main__":
    test_run_completed_requires_full_result()
    test_run_failed_still_maps()
    test_connection_manager_multitab()
    print("\n=== durable session tests (partial) passed ===")
