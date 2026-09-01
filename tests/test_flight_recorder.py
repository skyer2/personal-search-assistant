"""Agent Flight Recorder：统一事件、JSONL schema、并行 span、metrics 口径。"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.observability_metrics import aggregate_metrics, collect_run_summaries, render_prometheus_text
from app.api.trace_logger import JsonlTraceLogger
from app.observability.events import EventType, span_identity
from app.observability.journal import build_span_tree
from app.observability.privacy import sanitize_attributes
from app.observability.recorder import AgentTelemetry


def test_span_identity_parallel_workers():
    a = span_identity("execute", task_id="t1", attempt=1)
    b = span_identity("execute", task_id="t2", attempt=1)
    assert a != b
    print("[OK] parallel span identity")


def test_jsonl_run_summary_dual_schema():
    logger = JsonlTraceLogger(log_dir=Path(tempfile.mkdtemp()) / "traces", enabled=True)
    logger.log_run_summary(
        trace_id="tr_dual",
        session_id="sess_dual",
        status="success",
        duration_ms=1200,
        metadata={"tool_calls_count": 3, "replan_count": 1},
    )
    events = logger.read_trace("sess_dual")
    assert len(events) == 1
    record = events[0]
    assert record["event"] == "run_summary"
    assert record["extra"]["event"] == "run_summary"
    assert record["metadata"]["tool_calls_count"] == 3
    summaries = collect_run_summaries(logger.log_dir, window_hours=24)
    assert len(summaries) == 1
    assert summaries[0]["metadata"]["tool_calls_count"] == 3
    print("[OK] jsonl run_summary dual schema")


def test_collect_legacy_flat_and_nested():
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        nested = {
            "trace_id": "t1",
            "session_id": "a",
            "phase": "run",
            "status": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 100,
            "extra": {"event": "run_summary", "metadata": {"tool_calls_count": 2}},
        }
        flat = {
            "trace_id": "t2",
            "session_id": "b",
            "phase": "run",
            "status": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 200,
            "event": "run_summary",
            "metadata": {"tool_calls_count": 4, "replan_count": 1},
        }
        (log_dir / "a.jsonl").write_text(json.dumps(nested) + "\n", encoding="utf-8")
        (log_dir / "b.jsonl").write_text(json.dumps(flat) + "\n", encoding="utf-8")
        metrics = aggregate_metrics(log_dir, window_hours=24)
        assert metrics.runs_total == 2
        assert metrics.latency_p50_ms > 0
        assert metrics.replan_trigger_rate == 0.5
        prom = render_prometheus_text(metrics)
        assert "harness_runs_total 2" in prom
        assert "# TYPE harness_runs_total gauge" in prom
        print("[OK] collect both JSONL schemas + percentiles")


def test_recorder_emits_once_and_builds_tree():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    with tempfile.TemporaryDirectory() as tmp:
        tel.configure_jsonl(Path(tmp), enabled=True)
        tel.start_run(session_id="s_tree", run_id="s_tree", trace_id="trace-tree", query_preview="q")
        tel.emit_phase("execute", "start", task_id="t1", attempt=1)
        tel.emit(EventType.TOOL_STARTED, phase="execute", task_id="t1", attributes={"tool_name": "internet_search"})
        tel.emit(
            EventType.TOOL_COMPLETED,
            phase="execute",
            task_id="t1",
            duration_ms=40,
            attributes={"tool_name": "internet_search"},
        )
        tel.emit_phase("execute", "done", task_id="t1", attempt=1, duration_ms=80)
        tel.emit(
            EventType.REPLAN_APPLIED,
            phase="recover",
            attributes={"from_plan_version": 1, "to_plan_version": 2, "reason": "missing_dimension", "added_tasks": ["t4"]},
        )
        tel.finish_run(status="success", duration_ms=500, metadata={"tool_calls_count": 1, "replan_count": 1})
        events = tel._jsonl.read("s_tree") if tel._jsonl else []
        types = [item.get("type") or item.get("event") for item in events]
        assert "run.started" in types
        assert "run_summary" in types
        assert any(item.get("event") == "run_summary" and item.get("extra", {}).get("event") == "run_summary" for item in events)
        tree = build_span_tree(events)
        assert tree["event_count"] == len(events)
        assert tree["span_count"] >= 1
        snap = tel.metrics.snapshot()
        assert snap["counters"].get("harness.replan.applied", 0) >= 1
        assert snap["histograms"]["harness.run.duration_ms"]["count"] >= 1
        print("[OK] recorder journal + tree + live metrics")


def test_privacy_redacts_prompt_by_default():
    cleaned = sanitize_attributes({"prompt": "SECRET_PROMPT_TEXT", "tool_name": "search", "model": "qwen"}, mode="redacted")
    assert "SECRET_PROMPT_TEXT" not in json.dumps(cleaned)
    assert cleaned["tool_name"] == "search"
    full = sanitize_attributes({"prompt": "SECRET_PROMPT_TEXT"}, mode="full")
    assert "SECRET_PROMPT_TEXT" in str(full.get("prompt"))
    print("[OK] privacy redaction")


def test_parallel_phase_spans_and_intermediate_status():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_par", run_id="s_par", trace_id="trace-par")
    tel.emit_phase("execute", "start", task_id="t1", attempt=1)
    tel.emit_phase("execute", "start", task_id="t2", attempt=1)
    tel.emit_phase("execute", "context_built", task_id="t1", attempt=1)
    alias_t1 = span_identity("execute", task_id="t1", attempt=1)
    alias_t2 = span_identity("execute", task_id="t2", attempt=1)
    assert alias_t1 in tel._spans
    assert alias_t2 in tel._spans
    assert tel._spans[alias_t1].span_id != tel._spans[alias_t2].span_id
    tel.emit_phase("execute", "done", task_id="t1", attempt=1, duration_ms=12)
    tel.emit_phase("execute", "done", task_id="t2", attempt=1, duration_ms=18)
    assert alias_t1 not in tel._spans
    assert alias_t2 not in tel._spans
    tel.finish_run(status="success", duration_ms=40, metadata={"replan_count": 0})
    print("[OK] parallel execute spans survive context_built")


def test_recorder_run_summary_is_scanned():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        tel.configure_jsonl(log_dir, enabled=True)
        tel.start_run(session_id="s_sum", run_id="s_sum")
        tel.finish_run(
            status="success",
            duration_ms=900,
            metadata={"tool_calls_count": 5, "replan_count": 1},
        )
        summaries = collect_run_summaries(log_dir, window_hours=24)
        assert len(summaries) == 1
        assert summaries[0]["metadata"]["tool_calls_count"] == 5
        print("[OK] recorder run_summary collected")


def test_eval_score_attaches_to_existing_trace():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    event = tel.emit(
        EventType.EVAL_SCORED,
        phase="eval",
        status="pass",
        session_id="sess-eval",
        run_id="run-eval",
        trace_id="trace-eval",
        attributes={"case_id": "037", "variant": "full_harness", "accuracy": 1.0},
    )
    assert event.trace_id == "trace-eval"
    assert event.session_id == "sess-eval"
    assert event.run_id == "run-eval"
    print("[OK] eval score keeps trace identity")


if __name__ == "__main__":
    test_span_identity_parallel_workers()
    test_jsonl_run_summary_dual_schema()
    test_collect_legacy_flat_and_nested()
    test_recorder_emits_once_and_builds_tree()
    test_privacy_redacts_prompt_by_default()
    test_parallel_phase_spans_and_intermediate_status()
    test_recorder_run_summary_is_scanned()
    test_eval_score_attaches_to_existing_trace()
    print("\n=== Flight recorder tests passed ===")
