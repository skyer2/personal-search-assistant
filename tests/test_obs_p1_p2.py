"""P1/P2 observability contracts: run-centric JSONL, context/recovery/checkpoint, bus, retention."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.observability.event_bus import InProcessEventBus, reset_event_bus
from app.observability.events import EventType
from app.observability.exporters.jsonl import JsonlExporter
from app.observability.recorder import AgentTelemetry
from app.observability.retention import prune_trace_files, should_sample
from app.agent.harness.usage_tracker import prompt_template_for_phase


def test_run_centric_jsonl_layout_and_legacy_read():
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        exporter = JsonlExporter(log_dir=log_dir, enabled=True, run_centric=True)
        tel = AgentTelemetry()
        tel._ws_enabled = False
        tel.configure_jsonl(log_dir, enabled=True)
        # Force the configured exporter to use same run-centric instance
        tel._jsonl = exporter
        tel.start_run(session_id="sess_rc", run_id="run_rc_1")
        tel.emit(EventType.CONTEXT_BUILT, phase="execute", status="ok", attributes={"after_tokens": 120})
        tel.emit(EventType.RECOVERY_DECIDED, phase="recover", status="decided", attributes={"decision": "retry"})
        tel.emit(EventType.CHECKPOINT_SAVED, phase="execute", status="ok", attributes={"checkpoint_id": "ck1"})
        tel.finish_run(status="success", duration_ms=12, metadata={"replan_count": 0})
        run_path = log_dir / "sess_rc" / "run_rc_1.jsonl"
        assert run_path.exists()
        rows = exporter.read("sess_rc", run_id="run_rc_1")
        types = [row.get("type") or row.get("event") for row in rows]
        assert "context.built" in types
        assert "recovery.decided" in types
        assert "checkpoint.saved" in types
        assert "run_rc_1" in exporter.list_runs("sess_rc")
        print("[OK] run-centric jsonl")


def test_prompt_template_and_generation_refs():
    tid, ver = prompt_template_for_phase("plan")
    assert tid == "planner_prompt"
    assert ver.startswith("v")
    tel = AgentTelemetry()
    tel._ws_enabled = False
    event = tel.record_generation(
        model="qwen-max",
        phase="plan",
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        extra={
            "prompt_template_id": tid,
            "prompt_template_version": ver,
            "prompt_ref": "payloads/r/prompt_001.json",
            "output_ref": "payloads/r/output_001.json",
            "input_hash": "abc",
            "output_hash": "def",
            "temperature": 0.2,
        },
    )
    assert event.attributes.get("prompt_template_id") == "planner_prompt"
    assert event.attributes.get("prompt_ref")
    print("[OK] llm generation refs")


def test_finish_tool_result_metadata_and_retrieval():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_tool", run_id="r_tool")
    call_id = tel.begin_tool("internet_search", tool_call_id="tc1", args={"query": "x"})
    tel.finish_tool(
        "internet_search",
        tool_call_id=call_id,
        duration_ms=33,
        status="ok",
        result_count=3,
        result_bytes=1200,
        artifact_ids=["a1"],
        extra={"document_ids": ["https://a.example/1"], "domains": ["a.example"], "top_k": 3},
    )
    types = [e.type for e in tel.journal.replay("s_tool")]
    assert "retrieval.search" in types
    assert "tool.completed" in types
    completed = next(e for e in tel.journal.replay("s_tool") if e.type == "tool.completed")
    assert completed.attributes.get("result_count") == 3
    tel.finish_run(status="success", duration_ms=40, metadata={})
    print("[OK] tool result metadata + retrieval")


def test_event_bus_inprocess_fanout():
    bus = InProcessEventBus()
    reset_event_bus(bus)
    seen: list[dict] = []
    bus.subscribe("agent_events", lambda payload: seen.append(payload))
    bus.publish("agent_events", {"monitor_event": "worker", "message": "hi", "data": {"task_id": "t1"}})
    assert len(seen) == 1
    assert seen[0]["data"]["task_id"] == "t1"
    print("[OK] in-process event bus")


def test_retention_keeps_semantic_events():
    assert should_sample("brief.compiled") is True
    assert should_sample("run.completed") is True
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        old = log_dir / "old.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        import os

        old_mtime = 1.0
        os.utime(old, (old_mtime, old_mtime))
        removed = prune_trace_files(log_dir, now=old_mtime + 20 * 86400)
        assert removed >= 1
        assert not old.exists()
    print("[OK] retention prune")


def test_collect_run_summaries_nested_layout():
    from app.api.observability_metrics import collect_run_summaries
    from datetime import datetime, timezone

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        nested = log_dir / "sess_n" / "run_n.jsonl"
        nested.parent.mkdir(parents=True)
        record = {
            "phase": "run",
            "event": "run_summary",
            "status": "success",
            "session_id": "sess_n",
            "run_id": "run_n",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 10,
            "metadata": {"tool_calls_count": 2},
        }
        nested.write_text(json.dumps(record) + "\n", encoding="utf-8")
        rows = collect_run_summaries(log_dir, window_hours=24)
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run_n"
        print("[OK] nested run summary scan")


if __name__ == "__main__":
    test_run_centric_jsonl_layout_and_legacy_read()
    test_prompt_template_and_generation_refs()
    test_finish_tool_result_metadata_and_retrieval()
    test_event_bus_inprocess_fanout()
    test_retention_keeps_semantic_events()
    test_collect_run_summaries_nested_layout()
    print("all p1/p2 observability contracts passed")
