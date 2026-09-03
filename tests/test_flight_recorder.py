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
from app.observability.journal import build_span_tree, summarize_trace
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


def test_summarize_trace_workers_and_replan():
    events = [
        {"type": "run.started", "session_id": "s1", "run_id": "r1", "trace_id": "t1", "attributes": {"git_sha": "abc"}},
        {
            "type": "worker.started",
            "task_id": "t_a",
            "status": "start",
            "attempt": 1,
            "attributes": {"objective": "下一跳预测"},
        },
        {"type": "worker.completed", "task_id": "t_a", "status": "ok", "duration_ms": 12, "attempt": 1},
        {
            "type": "progress.evaluated",
            "status": "enough",
            "plan_version": 1,
            "attributes": {"verdict": "enough", "reason": "ready_queue_empty", "gaps": []},
        },
        {
            "type": "replan.applied",
            "attributes": {
                "from_plan_version": 1,
                "to_plan_version": 2,
                "reason": "missing_dimension",
                "added_tasks": ["t4"],
            },
        },
        {"type": "gen_ai.chat", "attributes": {"total_tokens": 100, "cost_usd": 0.01}},
        {
            "type": "quality.evaluated",
            "status": "pass",
            "attributes": {"passed": True, "citation_coverage_rate": 0.9, "severity": "info"},
        },
        {"type": "eval.scored", "attributes": {"case_id": "037", "variant": "full_harness", "accuracy": 1}},
    ]
    summary = summarize_trace(events)
    assert summary["identity"]["run_id"] == "r1"
    assert summary["worker_count"] == 1
    assert len(summary["workers"]) == 1
    worker = summary["workers"][0]
    assert worker["type"] == "worker.completed"
    assert worker["status"] == "ok"
    assert worker["duration_ms"] == 12
    assert worker["objective"] == "下一跳预测"
    assert summary["progress_count"] == 1
    assert summary["progress"][0]["verdict"] == "enough"
    assert summary["replan_count"] == 1
    assert summary["usage"]["total_tokens"] == 100
    assert summary["evals"][0]["type"] == "quality.evaluated"
    assert summary["evals"][0]["passed"] is True
    assert summary["evals"][1]["case_id"] == "037"
    print("[OK] summarize_trace")


def test_summarize_trace_progress_without_replan():
    events = [
        {
            "type": "worker.started",
            "task_id": "t_next_hop",
            "status": "start",
            "attempt": 1,
            "attributes": {"objective": "下一跳"},
        },
        {"type": "worker.completed", "task_id": "t_next_hop", "status": "ok", "duration_ms": 115797, "attempt": 1},
        {
            "type": "worker.started",
            "task_id": "t_langgraph",
            "status": "start",
            "attempt": 1,
            "attributes": {"objective": "LangGraph"},
        },
        {"type": "worker.completed", "task_id": "t_langgraph", "status": "ok", "duration_ms": 230308, "attempt": 1},
        {
            "type": "progress.evaluated",
            "status": "enough",
            "attributes": {"verdict": "enough", "reason": "coverage_ok"},
        },
        {
            "type": "worker.started",
            "task_id": "t_running",
            "status": "start",
            "attempt": 1,
            "attributes": {"objective": "进行中"},
        },
    ]
    summary = summarize_trace(events)
    assert summary["worker_count"] == 3
    assert [row["task_id"] for row in summary["workers"]] == ["t_next_hop", "t_langgraph", "t_running"]
    assert summary["workers"][0]["objective"] == "下一跳"
    assert summary["workers"][0]["duration_ms"] == 115797
    assert summary["workers"][2]["status"] == "start"
    assert summary["workers"][2]["objective"] == "进行中"
    assert summary["replan_count"] == 0
    assert summary["progress_count"] == 1
    assert summary["evals"] == []
    print("[OK] summarize_trace progress without replan")


def test_span_tree_omits_llm_usage():
    events = [
        {"type": "run.started", "span_id": "root"},
        {"type": "llm_usage", "span_id": "llm-1", "event": "llm_usage"},
        {"type": "gen_ai.chat", "span_id": "llm-2", "parent_span_id": "root"},
        {"type": "worker.started", "span_id": "w1", "task_id": "t_next_hop", "parent_span_id": "root"},
        {"type": "phase", "span_id": "p1", "phase": "abort", "status": "budget_tool_calls"},
    ]
    tree = build_span_tree(events)
    assert tree["event_count"] == 5
    assert tree["omitted_count"] == 2
    names = {node["name"] for node in tree["roots"]}
    assert "llm_usage" not in names
    assert "gen_ai.chat" not in names
    assert tree["span_count"] == 3
    root = next(node for node in tree["roots"] if node["span_id"] == "root")
    child_names = {child["name"] for child in root["children"]}
    assert "gen_ai.chat" not in child_names
    print("[OK] span tree omits llm_usage and gen_ai.chat")


def test_bind_worker_isolates_span_context():
    from app.observability.context import bind_worker, current_context, reset_run, set_context

    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_iso", run_id="r_iso", trace_id="tr_iso")
    parent = current_context()
    assert parent is not None
    parent_span = parent.span_id
    restored, _token = bind_worker(task_id="t1", attempt=1)
    child = current_context()
    assert child is not None
    assert child.task_id == "t1"
    assert child.span_id == parent_span
    assert child.sequence is parent.sequence
    assert restored is parent
    tel.start_span("worker.execute", phase="execute", task_id="t1", attempt=1)
    worker_ctx = current_context()
    assert worker_ctx is not None
    assert worker_ctx.span_id != parent_span
    set_context(parent)
    assert current_context() is not None
    assert current_context().span_id == parent_span
    tel.finish_run(status="success", duration_ms=1)
    reset_run(None)
    print("[OK] bind_worker isolates context")


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


def test_run_sequence_is_shared_and_unique():
    from concurrent.futures import ThreadPoolExecutor

    from app.observability.context import current_context, reset_run

    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_seq", run_id="r_seq", trace_id="tr_seq")
    parent = current_context()
    assert parent is not None

    def _worker(task_id: str) -> list[int]:
        from app.observability.context import set_context

        set_context(parent.child(task_id=task_id, attempt=1))
        return [tel.emit(EventType.WORKER_STARTED, task_id=task_id).seq for _ in range(8)]

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(_worker, ["a", "b", "c"]))
    flat = [item for row in results for item in row]
    assert len(flat) == 24
    assert set(flat) == set(range(2, 26))
    tel.finish_run(status="success", duration_ms=1)
    reset_run(None)
    print("[OK] run sequence unique under parallel workers")


def test_otel_parent_context_not_attribute():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.observability.context import bind_worker, reset_run
    from app.observability.exporters import otel as otel_mod

    exporter = InMemorySpanExporter()
    otel_mod.reset_for_tests()
    otel_mod.init_otel(exporter=exporter, force=True)
    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_otel", run_id="r_otel", trace_id="tr_otel")
    bind_worker(task_id="t1", attempt=1)
    key = tel.start_span("worker.execute", phase="execute", task_id="t1", attempt=1)
    tel.begin_tool("internet_search", tool_call_id="call-1")
    tel.finish_tool("internet_search", tool_call_id="call-1", duration_ms=12, status="ok")
    tel.end_span(key, status="ok", duration_ms=40)
    tel.finish_run(status="success", duration_ms=80)
    reset_run(None)
    otel_mod.flush_otel()
    spans = exporter.get_finished_spans()
    by_name = {span.name: span for span in spans}
    assert "research.run" in by_name
    assert "worker.execute" in by_name
    assert "tool.internet_search" in by_name
    root = by_name["research.run"]
    worker = by_name["worker.execute"]
    tool = by_name["tool.internet_search"]
    assert worker.parent is not None
    assert worker.parent.span_id == root.context.span_id
    assert tool.parent is not None
    assert tool.parent.span_id == worker.context.span_id
    assert worker.context.trace_id == root.context.trace_id
    assert "gen_ai.conversation.id" in dict(root.attributes or {})
    otel_mod.reset_for_tests()
    print("[OK] OTel parent/child context")


def test_tool_and_generation_are_child_spans():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_span", run_id="r_span")
    worker_key = tel.start_span("worker.execute", phase="execute", task_id="t1")
    tel.begin_tool("search", tool_call_id="c1")
    tel.record_generation(model="qwen-max", phase="execute", prompt_tokens=10, completion_tokens=4, total_tokens=14)
    tel.finish_tool("search", tool_call_id="c1", duration_ms=9, status="ok")
    tel.end_span(worker_key, status="ok")
    events = [item.to_dict() for item in tel.journal.replay("s_span")]
    types = [item["type"] for item in events]
    assert "tool.started" in types
    assert "tool.completed" in types
    assert "gen_ai.chat" in types
    tool_started = next(item for item in events if item["type"] == "tool.started")
    gen = next(item for item in events if item["type"] == "gen_ai.chat")
    worker_started_span = next(item for item in events if item["span_id"] and item["type"] == "run.started")
    assert tool_started["parent_span_id"]
    assert gen["parent_span_id"] == tool_started["span_id"]
    assert tool_started["span_id"] != worker_started_span["span_id"]
    tel.finish_run(status="success", duration_ms=1)
    print("[OK] tool and generation first-class spans")


def test_failure_attribution_and_eval_matrix():
    from app.observability.failure import classify_failure

    classified = classify_failure("budget_tool_calls", phase="abort")
    assert classified["failure.stage"] == "runtime"
    assert classified["failure.type"] == "budget_exhausted"
    events = [
        {
            "type": "worker.failed",
            "task_id": "t1",
            "status": "failed",
            "attributes": {"fail_reason": "step_timeout", "failure.stage": "worker", "failure.type": "timeout"},
        },
        {"type": "eval.scored", "attributes": {"case_id": "037", "variant": "vanilla", "accuracy": 0, "latency_ms": 10}},
        {"type": "eval.scored", "attributes": {"case_id": "037", "variant": "full_harness", "accuracy": 1, "latency_ms": 40}},
    ]
    summary = summarize_trace(events)
    assert summary["failure_counts"]["worker"] == 1
    matrix = summary["eval_matrix"][0]
    assert "vanilla" in matrix["variants"]
    assert matrix["cases"][0]["variants"]["full_harness"]["accuracy"] == 1
    print("[OK] failure attribution + eval matrix")


if __name__ == "__main__":
    test_span_identity_parallel_workers()
    test_jsonl_run_summary_dual_schema()
    test_collect_legacy_flat_and_nested()
    test_recorder_emits_once_and_builds_tree()
    test_privacy_redacts_prompt_by_default()
    test_parallel_phase_spans_and_intermediate_status()
    test_recorder_run_summary_is_scanned()
    test_eval_score_attaches_to_existing_trace()
    test_summarize_trace_workers_and_replan()
    test_summarize_trace_progress_without_replan()
    test_span_tree_omits_llm_usage()
    test_bind_worker_isolates_span_context()
    test_run_sequence_is_shared_and_unique()
    test_tool_and_generation_are_child_spans()
    test_failure_attribution_and_eval_matrix()
    print("\n=== Flight recorder tests passed ===")
