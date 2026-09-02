"""RunStore：session/run projection、bootstrap、startup recovery、HITL、uploads。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.observability.events import AgentEvent, EventType, utc_now
from app.run_store import (
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETED,
    STATUS_RECOVERABLE,
    STATUS_RUNNING,
    RunStore,
    reset_run_store,
)
from app.run_store.files import list_output_files, resolve_output_file


def test_create_run_and_bootstrap(tmp_path):
    store = RunStore(tmp_path / "run.sqlite")
    store.create_run(run_id="r1", session_id="s1", query="比较 durability")
    store.mark_running("r1", session_workspace="session_s1")
    store.complete_run("r1", result="结论 A", status=STATUS_COMPLETED)
    store.create_run(run_id="r2", session_id="s1", query="继续比较")
    store.mark_running("r2")

    boot = store.bootstrap("s1", output_root=tmp_path / "output")
    assert boot.found is True
    assert [item.run_id for item in boot.runs] == ["r1", "r2"]
    assert boot.runs[0].final_result == "结论 A"
    assert boot.current_run is not None
    assert boot.current_run.run_id == "r2"
    assert boot.current_run.status == STATUS_RUNNING
    assert boot.current_run.query == "继续比较"

    missing = store.bootstrap("no-such")
    assert missing.found is False
    assert missing.notice == "session_not_found"
    print("[OK] run store bootstrap")


def test_hitl_and_elapsed(tmp_path):
    store = RunStore(tmp_path / "run.sqlite")
    store.create_run(run_id="r1", session_id="s1", query="q")
    store.mark_running("r1")
    store.set_hitl("r1", {"action_requests": [{"name": "tool", "args": {}}]})
    run = store.get_run("r1")
    assert run is not None
    assert run.status == STATUS_AWAITING_APPROVAL
    assert run.hitl_payload is not None
    assert run.pause_started_at
    store.clear_hitl("r1")
    run = store.get_run("r1")
    assert run is not None
    assert run.status == STATUS_RUNNING
    assert run.hitl_payload is None
    assert run.paused_total_ms >= 0
    print("[OK] run store HITL pause")


def test_on_event_updates_seq_and_counters(tmp_path):
    store = RunStore(tmp_path / "run.sqlite")
    store.create_run(run_id="r1", session_id="s1", query="q")
    event = AgentEvent(
        event_id="e1",
        trace_id="t1",
        span_id="sp1",
        run_id="r1",
        session_id="s1",
        seq=9,
        timestamp=utc_now(),
        type=EventType.TOOL_STARTED,
        attributes={"tool_name": "search"},
    )
    store.on_event(event)
    run = store.get_run("r1")
    assert run is not None
    assert run.last_event_seq == 9
    assert run.tool_calls == 1
    print("[OK] run store event projection")


def test_recover_stale_running(tmp_path):
    store = RunStore(tmp_path / "run.sqlite")
    store.create_run(run_id="r1", session_id="s1", query="q")
    store.mark_running("r1")
    recovered = store.recover_stale_runs(set())
    assert len(recovered) == 1
    assert recovered[0].status == STATUS_RECOVERABLE
    store.create_run(run_id="r2", session_id="s2", query="live")
    store.mark_running("r2")
    none = store.recover_stale_runs({"s2"})
    assert none == []
    assert store.get_run("r2").status == STATUS_RUNNING
    print("[OK] startup recovery")


def test_uploads_and_artifacts(tmp_path):
    store = RunStore(tmp_path / "run.sqlite")
    store.add_upload("s1", "paper.pdf", 123, server_path="paper.pdf")
    files = store.list_uploads("s1")
    assert files[0].name == "paper.pdf"
    assert files[0].size == 123

    output = tmp_path / "output" / "session_s1"
    output.mkdir(parents=True)
    (output / "report.md").write_text("# hi", encoding="utf-8")
    listed = list_output_files(tmp_path / "output", "s1")
    assert listed[0]["name"] == "report.md"
    assert listed[0]["path"] == "report.md"
    (output / "working_notes.md").write_text("# notes", encoding="utf-8")
    (output / "report.pdf").write_bytes(b"%PDF-1.4\n")
    ordered = [item["name"] for item in list_output_files(tmp_path / "output", "s1")]
    assert ordered[0] == "report.pdf"
    assert ordered.index("report.md") < ordered.index("working_notes.md")
    target = resolve_output_file(tmp_path / "output", "s1", "report.md")
    assert target.exists()
    try:
        resolve_output_file(tmp_path / "output", "s1", "../secret")
        raise AssertionError("path escape should fail")
    except ValueError:
        pass
    print("[OK] uploads and artifacts")
    reset_run_store()


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        test_create_run_and_bootstrap(path)
        test_hitl_and_elapsed(path / "hitl")
        test_on_event_updates_seq_and_counters(path / "evt")
        test_recover_stale_running(path / "rec")
        test_uploads_and_artifacts(path / "files")
    print("\n=== RunStore tests passed ===")
