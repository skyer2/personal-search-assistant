"""Observability Correctness Hardening: span tree, failure attribution, integrity."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.observability.events import EventType
from app.observability.integrity import check_trace_integrity
from app.observability.journal import build_span_tree
from app.observability.recorder import AgentTelemetry
from app.observability.semantic import (
    build_lineage_edges,
    earliest_failure_origin,
    plan_brief_coverage,
)


def test_span_context_push_pop_no_stale_parent():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_pp", run_id="r_pp", trace_id="tr_pp")
    from app.observability.context import current_context

    root_span = current_context().span_id
    s1 = tel.start_span("worker.execute", phase="execute", task_id="t1")
    worker_span = current_context().span_id
    assert worker_span != root_span
    s2 = tel.start_span("tool.search", phase="execute")
    tool_span = current_context().span_id
    assert tool_span != worker_span
    tel.end_span(s2, status="ok")
    ctx_after_tool = current_context()
    assert ctx_after_tool.span_id == worker_span
    tel.end_span(s1, status="ok")
    ctx_after_worker = current_context()
    assert ctx_after_worker.span_id == root_span
    # Emit after spans closed should use root as parent, not stale worker
    event = tel.emit(EventType.PROGRESS_EVALUATED, phase="validate", status="enough")
    assert event.parent_span_id != worker_span or event.parent_span_id is None
    tel.finish_run(status="success", duration_ms=1, metadata={})
    records = [e.to_jsonl_record() for e in tel.journal.replay("s_pp")]
    tree = build_span_tree(records)
    assert tree["root_count"] >= 1
    assert tree["cycle_count"] == 0
    assert tree["valid"] is True
    print("[OK] span context push/pop: no stale parent")


def test_span_tree_detects_cycles_and_reports():
    events = [
        {"type": "run.started", "span_id": "A", "parent_span_id": "B"},
        {"type": "worker.started", "span_id": "B", "parent_span_id": "A"},
        {"type": "tool.started", "span_id": "C", "parent_span_id": "C"},
    ]
    tree = build_span_tree(events)
    assert tree["cycle_count"] >= 2
    assert tree["valid"] is False
    assert tree["root_count"] >= 1
    print("[OK] span tree detects cycles")


def test_warning_not_in_failure_attribution():
    events = [
        {
            "type": "phase",
            "phase": "validate",
            "status": "warning",
            "attributes": {"failure.stage": "evidence", "failure.type": "unknown"},
        },
        {
            "type": "run.completed",
            "status": "success",
            "attributes": {},
        },
    ]
    origin = earliest_failure_origin(events)
    assert origin is None
    print("[OK] warning skipped in failure attribution")


def test_trace_integrity_detects_missing_events():
    events = [
        {"type": "run.started", "span_id": "root", "seq": 1, "attributes": {"search_mode": "agent"}},
        {"type": "brief.compiled", "span_id": "s1", "parent_span_id": "root", "seq": 2, "attributes": {}},
        {"type": "plan.created", "span_id": "s2", "parent_span_id": "root", "seq": 3, "attributes": {}},
        {"type": "worker.started", "span_id": "s3", "parent_span_id": "root", "seq": 4, "task_id": "t1", "attributes": {}},
        {"type": "worker.completed", "span_id": "s3", "parent_span_id": "root", "seq": 5, "task_id": "t1", "attributes": {}},
        {"type": "run.completed", "span_id": "root", "seq": 6, "status": "success", "attributes": {}},
    ]
    result = check_trace_integrity(events, run_status="success")
    assert not result["passed"]
    assert "missing_progress_event" in result["issues"]
    assert "missing_synthesis_or_termination_event" in result["issues"]
    assert "missing_quality_event" in result["issues"]
    assert result["counts"]["brief"] == 1
    assert result["counts"]["plan"] == 1
    print("[OK] integrity detects missing progress/synthesis/quality")


def test_trace_integrity_passes_complete_run():
    events = [
        {"type": "run.started", "span_id": "root", "seq": 1, "attributes": {"search_mode": "agent"}},
        {"type": "brief.compiled", "span_id": "s1", "parent_span_id": "root", "seq": 2, "attributes": {}},
        {"type": "plan.created", "span_id": "s2", "parent_span_id": "root", "seq": 3, "attributes": {}},
        {"type": "worker.started", "span_id": "s3", "seq": 4, "attributes": {}},
        {"type": "worker.completed", "span_id": "s3", "seq": 5, "attributes": {}},
        {"type": "progress.evaluated", "span_id": "s4", "seq": 6, "attributes": {"verdict": "enough"}},
        {"type": "synthesis.completed", "span_id": "s5", "seq": 7, "attributes": {}},
        {"type": "quality.evaluated", "span_id": "s6", "seq": 8, "attributes": {"passed": True}},
        {"type": "run.completed", "span_id": "root", "seq": 9, "status": "success", "attributes": {}},
    ]
    result = check_trace_integrity(events, run_status="success")
    assert result["passed"]
    assert result["issues"] == []
    assert result["span_tree"]["valid"] is True
    print("[OK] integrity passes complete run")


def test_failed_run_records_origin_and_passes_stage_integrity():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_failed", run_id="r_failed", trace_id="tr_failed")
    tel.emit(EventType.BRIEF_COMPILED, phase="understand", status="ok")
    tel.finish_run(
        status="failed",
        duration_ms=43,
        metadata={
            "error": "planner exploded",
            "failure.origin_stage": "planning",
            "failure.detected_stage": "planning",
        },
        error="planner exploded",
    )
    events = [event.to_dict() for event in tel.journal.replay("s_failed")]
    failed = next(event for event in events if event["type"] == "run.failed")

    assert failed["attributes"]["failure.origin_stage"] == "planning"
    assert failed["attributes"]["failure.stage"] == "planning"

    result = check_trace_integrity(events, run_status="failed")
    assert result["passed"]
    assert result["issues"] == []
    assert result["failure_origin_stage"] == "planning"
    assert result["counts"]["plan"] == 0
    print("[OK] failed run records origin and stage-aware integrity")


def test_plan_phase_emits_created_and_validated(monkeypatch):
    from app.agent.harness.loop import AgentHarness
    from app.agent.harness.planner import build_plan, understand_task
    from app.agent.harness.state import LoopState, Phase
    tel = AgentTelemetry()
    tel._ws_enabled = False
    tel.start_run(session_id="s_plan", run_id="r_plan", trace_id="tr_plan")
    monkeypatch.setattr("app.observability.get_recorder", lambda: tel)
    harness = object.__new__(AgentHarness)
    harness._current_tracer = None
    harness.trace_logger = None
    state = LoopState(session_id="s_plan", phase=Phase.PLAN)
    state.intent = understand_task("搜索 Tesla 2026 动态，生成 Markdown 报告")
    state.plan = build_plan(state.intent)

    try:
        harness._report_phase(Phase.PLAN, "done", state=state)
    finally:
        tel.finish_run(status="success", duration_ms=1)

    event_types = [event.type for event in tel.journal.replay("s_plan")]
    assert EventType.PLAN_CREATED in event_types
    assert EventType.PLAN_VALIDATED in event_types
    print("[OK] plan phase emits created and validated")


def test_plan_coverage_fuzzy_keywords():
    class FakeStep:
        def __init__(self, objective, task_id="t1"):
            self.objective = objective
            self.description = ""
            self.task_id = task_id
            self.metadata = {}

    class FakePlan:
        def __init__(self, steps):
            self.steps = steps

    brief = {"dimensions": ["AI编程能力现状与进展", "预测可信度与正反方论据"]}
    plan = FakePlan([
        FakeStep("AI编程能力现状与最新进展实证", "t_ai_coding"),
        FakeStep("预测可信度分析与正反方论据收集", "t_credibility"),
    ])
    cov = plan_brief_coverage(brief, plan)
    assert cov["dimensions"]["AI编程能力现状与进展"] is True
    assert cov["dimensions"]["预测可信度与正反方论据"] is True
    assert cov["missing_dimensions"] == []
    print("[OK] plan coverage fuzzy keywords")


def test_lineage_deduped_and_no_self_edges():
    events = [
        {
            "type": "brief.compiled",
            "attributes": {"brief_id": "b1"},
            "input_refs": [{"type": "user_query", "id": "q"}],
            "output_refs": [{"type": "research_brief", "id": "b1"}],
        },
        {
            "type": "plan.created",
            "attributes": {"brief_id": "b1", "plan_id": "p1"},
            "input_refs": [{"type": "research_brief", "id": "b1"}],
            "output_refs": [{"type": "research_plan", "id": "p1"}],
        },
        # Duplicate edge attempt
        {
            "type": "plan.created",
            "attributes": {"brief_id": "b1", "plan_id": "p1"},
            "input_refs": [{"type": "research_brief", "id": "b1"}],
            "output_refs": [{"type": "research_plan", "id": "p1"}],
        },
        # Self-edge attempt
        {
            "type": "worker.completed",
            "attributes": {"plan_id": "p1"},
            "input_refs": [{"type": "research_plan", "id": "p1"}],
            "output_refs": [{"type": "research_plan", "id": "p1"}],
        },
        # Non-semantic event should be skipped (no explicit refs, not in whitelist)
        {
            "type": "tool.completed",
            "attributes": {"tool_name": "search", "plan_id": "p1"},
        },
    ]
    edges = build_lineage_edges(events)
    assert len(edges) == 2
    types = [(e["from_type"], e["to_type"]) for e in edges]
    assert ("user_query", "research_brief") in types
    assert ("research_brief", "research_plan") in types
    assert not any(e["from_id"] == e["to_id"] and e["from_type"] == e["to_type"] for e in edges)
    print("[OK] lineage deduped, no self edges, only semantic events")


if __name__ == "__main__":
    test_span_context_push_pop_no_stale_parent()
    test_span_tree_detects_cycles_and_reports()
    test_warning_not_in_failure_attribution()
    test_trace_integrity_detects_missing_events()
    test_trace_integrity_passes_complete_run()
    test_plan_coverage_fuzzy_keywords()
    test_lineage_deduped_and_no_self_edges()
    print("all correctness hardening tests passed")
