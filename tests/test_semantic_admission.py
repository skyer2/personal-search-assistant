"""Regression tests for semantic correctness and evidence admission."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.citations import CitationManager
from app.agent.harness.planner import understand_task
from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.planning.progress import assess_progress
from app.research.planning.validator import validate_required_research_contract
from app.research.runtime.graph import GraphInvariantViolation, prepare_synthesis_node
from app.research.runtime.synthesis_admission import evaluate_synthesis_admission
from app.research.runtime.state import empty_research_state


QUERY = "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？"


def _intent_and_plan(*, required: bool) -> tuple[Any, ExecutionPlan]:
    intent = understand_task(QUERY)
    dimensions = list(intent.brief.dimensions)
    research = PlanStep(
        step_type="research",
        task_id="t_research",
        description="覆盖核心维度",
        objective="覆盖核心维度",
        metadata={
            "coverage_keys": dimensions,
            "required": required,
            "optional": not required,
        },
    )
    summary = PlanStep(
        step_type="summarize",
        task_id="t_summary",
        description="summary",
        depends_on=["t_research"],
    )
    return intent, ExecutionPlan(
        steps=[research, summary],
        planning_mode="dynamic",
    )


def _state(plan: ExecutionPlan, **overrides: Any) -> dict[str, Any]:
    state = empty_research_state(
        run_id="run-semantic",
        session_id="session-semantic",
        task_query=QUERY,
    )
    state.update(
        {
            "plan": plan.to_dict(),
            "plan_version": plan.plan_version,
            "task_status": {
                step.resolved_task_id(index): str(
                    step.metadata.get("status") or "pending"
                )
                for index, step in enumerate(plan.steps)
            },
        }
    )
    state.update(overrides)
    return state


def test_zero_workers_with_missing_coverage_must_be_gap() -> None:
    intent, plan = _intent_and_plan(required=False)
    assessment = assess_progress(
        plan,
        task_status={"t_research": "pending", "t_summary": "pending"},
        worker_results=[],
        query=QUERY,
        intent=intent,
    )

    assert assessment.verdict == "gap"
    assert assessment.gaps
    assert all(item["type"] == "coverage_gap" for item in assessment.gaps)
    assert assessment.coverage_gaps


def test_coverage_gaps_materialize_into_open_gap_ids() -> None:
    intent, plan = _intent_and_plan(required=False)
    assessment = assess_progress(
        plan,
        task_status={"t_research": "pending", "t_summary": "pending"},
        worker_results=[],
        query=QUERY,
        intent=intent,
    )

    assert assessment.open_gap_ids
    assert len(assessment.open_gap_ids) == len(
        [item for item in assessment.gaps if item["type"] == "coverage_gap"]
    )


def test_all_optional_research_plan_is_rejected() -> None:
    intent, plan = _intent_and_plan(required=False)

    assert "no_required_research_task" in validate_required_research_contract(
        intent, plan
    )


def test_core_dimension_only_optional_is_rejected() -> None:
    intent = understand_task(QUERY)
    dimensions = list(intent.brief.dimensions)
    required = PlanStep(
        step_type="research",
        task_id="t_required",
        description=dimensions[0],
        objective=dimensions[0],
        metadata={"coverage_keys": [dimensions[0]], "required": True},
    )
    optional = PlanStep(
        step_type="research",
        task_id="t_optional",
        description="、".join(dimensions[1:]),
        objective="、".join(dimensions[1:]),
        metadata={
            "coverage_keys": dimensions[1:],
            "required": False,
            "optional": True,
        },
    )
    plan = ExecutionPlan(steps=[required, optional], planning_mode="dynamic")
    issues = validate_required_research_contract(intent, plan)

    assert issues
    assert all(
        issue.startswith("core_dimension_only_optional:") for issue in issues
    )


def test_normal_enough_never_skips_required_research() -> None:
    _, plan = _intent_and_plan(required=True)
    state = _state(
        plan,
        progress_assessment={"verdict": "enough", "reason": "coverage_ok", "gaps": []},
        evidence_refs=["external:preexisting"],
    )

    with pytest.raises(GraphInvariantViolation):
        prepare_synthesis_node(state)


def test_zero_trusted_evidence_blocks_normal_synthesis() -> None:
    _, plan = _intent_and_plan(required=True)
    state = _state(
        plan,
        task_status={"t_research": "done", "t_summary": "pending"},
        progress_assessment={"verdict": "enough", "reason": "coverage_ok", "gaps": []},
    )
    admission = evaluate_synthesis_admission(
        state,
        plan,
        dict(state["task_status"]),
    )

    assert admission.allowed is False
    assert admission.reason == "no_trusted_evidence"
    assert admission.trusted_evidence_count == 0


def test_emergency_without_evidence_returns_deterministic_partial() -> None:
    _, plan = _intent_and_plan(required=True)
    state = _state(
        plan,
        task_status={"t_research": "pending", "t_summary": "pending"},
        progress_assessment={
            "verdict": "enough",
            "reason": "force_synthesis_budget",
            "gaps": [],
        },
        replan_exhausted=True,
    )
    update = prepare_synthesis_node(state)

    assert update["status"] == "partial"
    assert update["progress"] == "no_evidence_partial"
    assert update["synthesis_mode"] == "no_evidence_partial"
    assert update["task_status"]["t_research"] == "skipped"
    assert update["task_status"]["t_summary"] == "skipped"
    assert "没有可核实的外部证据" in update["final_content"]


def test_synthesis_output_never_becomes_evidence() -> None:
    manager = CitationManager()
    content = "这是一段超过八十个字符的合成输出。" * 10

    assert manager.register_from_step(1, "summarize", content) == []
    assert manager.register_from_step(1, "generate_markdown", content) == []
    assert manager.register_from_step(1, "convert_pdf", content) == []
    assert manager.register_from_step(1, "finalize", content) == []
    assert manager.sources == []
    assert manager.bind_worker_facts(1, "summarize", [content], []) == []


def test_preexisting_external_evidence_is_legal_with_zero_workers() -> None:
    _, plan = _intent_and_plan(required=True)
    state = _state(
        plan,
        task_status={"t_research": "done", "t_summary": "pending"},
        progress_assessment={"verdict": "enough", "reason": "coverage_ok", "gaps": []},
        evidence_refs=["preexisting:external"],
    )
    admission = evaluate_synthesis_admission(
        state,
        plan,
        dict(state["task_status"]),
    )

    assert admission.allowed is True
    assert admission.mode == "normal"
    assert admission.trusted_evidence_count == 1
    assert admission.worker_terminal_count == 1


def test_partial_terminal_event_keeps_termination_reason_after_metadata_truncation() -> None:
    from app.observability import EventType, get_recorder
    from app.observability.integrity import check_trace_integrity
    from app.observability.recorder import AgentTelemetry

    telemetry = AgentTelemetry()
    telemetry._ws_enabled = False
    telemetry.start_run(
        session_id="s-semantic-terminal",
        run_id="r-semantic-terminal",
        trace_id="trace-semantic-terminal",
    )
    metadata = {f"field_{index}": index for index in range(30)}
    metadata["termination"] = {
        "status": "partial",
        "reason": "incomplete",
        "stage": "quality",
        "quality_attempted": True,
    }
    telemetry.finish_run(
        status="partial",
        duration_ms=12,
        metadata=metadata,
    )
    events = telemetry.journal.replay("s-semantic-terminal")
    terminal = next(
        event
        for event in reversed(events)
        if event.type == EventType.RUN_COMPLETED
    )

    assert terminal.attributes["termination_reason"] == "incomplete"
    assert terminal.attributes["termination_stage"] == "quality"
    records = [event.to_jsonl_record() for event in events]
    integrity = check_trace_integrity(records, run_status="partial")
    assert "partial_without_termination_reason" not in integrity["issues"]


def test_tree_projection_rebuilds_when_cached_span_count_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.observability import projection_store
    from app.observability import replay as replay_module

    class FakeRecord:
        def to_jsonl_record(self) -> dict[str, Any]:
            return {"type": "worker.started", "span_id": "span-semantic"}

    class FakeProjectionStore:
        def __init__(self) -> None:
            self.saved: list[dict[str, Any]] = []

        def get_projection(
            self, run_id: str, kind: str
        ) -> dict[str, Any] | None:
            return {"span_count": 99, "event_count": 1}

        def put_projection(
            self, run_id: str, kind: str, payload: dict[str, Any]
        ) -> None:
            self.saved.append(payload)

    store = FakeProjectionStore()
    monkeypatch.setattr(
        replay_module,
        "load_events",
        lambda *args, **kwargs: [FakeRecord()],
    )
    monkeypatch.setattr(
        replay_module,
        "build_span_tree",
        lambda records: {
            "span_count": 1,
            "root_count": 1,
            "cycle_count": 0,
            "valid": True,
        },
    )
    monkeypatch.setattr(
        projection_store,
        "get_projection_store",
        lambda: store,
    )

    tree = replay_module.load_tree_projection(
        "s-semantic-tree", run_id="r-semantic-tree"
    )
    assert tree["span_count"] == 1
    assert store.saved
