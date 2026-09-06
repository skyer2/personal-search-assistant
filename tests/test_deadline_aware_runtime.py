"""Deadline-aware runtime regressions."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.planner import understand_task
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep, StepStatus
from app.agent.harness.usage_tracker import UsageTrackingCallback
from app.observability.integrity import check_trace_integrity
from app.research.planning.candidate import (
    annotate_candidate_dependencies,
    build_candidate_set,
)
from app.research.planning.compose import compose_execution_plan
from app.research.planning.granularity import (
    analyze_task_granularity,
    normalize_plan_granularity,
)
from app.research.planning.progress import assess_progress
from app.research.runtime import runner as runner_module
from app.research.runtime.fast_synthesis import build_minimal_evidence_pack
from app.research.runtime.worker import (
    WorkerIdleTimeoutError,
    _run_worker_step_with_lease,
)
from app.research.runtime.activity import tracked_worker_operation
from app.research.runtime.activity import (
    WorkerActivityTracker,
    reset_current_worker_activity,
    set_current_worker_activity,
)
from app.research.runtime.scheduler import ready_steps


@dataclass
class DeadlineConfig:
    step_timeout_sec: int = 10
    max_retries: int = 0
    worker_idle_timeout_sec: float = 75
    fast_synthesis_threshold_sec: float = 45
    emergency_context_budget_sec: float = 10
    replan_context_overhead_sec: float = 30
    replan_checkpoint_overhead_sec: float = 2
    max_replan_count: int = 2
    max_plan_steps: int = 20
    hitl_allow_replan: bool = True
    citations_min_coverage_rate: float = 0.0


class FakeHarness:
    def __init__(self):
        self.harness_config = DeadlineConfig()

    async def _run_single_step(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def _refresh_working_memory(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeBudget:
    def __init__(self, remaining_run_sec: float = 60.0):
        self._remaining_run_sec = remaining_run_sec

    def sync_from_usage(self, *args: Any, **kwargs: Any) -> None:
        return None

    def research_allowed(self) -> tuple[bool, str]:
        return True, ""

    def remaining_for_research_sec(self) -> float:
        return self._remaining_run_sec

    def remaining_run_sec(self) -> float:
        return self._remaining_run_sec


class FakeContext:
    task_query = "research query"
    user_id = "me"
    tenant_id = "local"
    project_id = "Inbox"
    relative_session_dir = "sessions/deadline"
    uploaded_prompt = ""
    session_dir = Path("output/sessions/deadline")
    idempotency = None
    citation_manager = None
    checkpoint_store = None
    run_dir = None
    deliverable_dir = None


class FakeSession:
    def __init__(
        self,
        state: LoopState,
        *,
        harness: FakeHarness | None = None,
        budget: FakeBudget | None = None,
    ):
        self.state = state
        self.ctx = FakeContext()
        self.session_id = state.session_id
        self.run_id = "r_deadline"
        self.lock = asyncio.Lock()
        self.worker_sem = asyncio.Semaphore(1)
        self.budget_manager = budget or FakeBudget()
        self.harness = harness or FakeHarness()


class FakeChild:
    task_id = "t_activity"
    tool_calls_count = 0
    trace: list[Any] = []
    step_results: list[Any] = []


def test_llm_in_flight_operation_does_not_trigger_idle_timeout():
    async def slow_llm() -> bool:
        with tracked_worker_operation("llm.request"):
            await asyncio.sleep(0.16)
        return True

    async def scenario() -> bool:
        return await _run_worker_step_with_lease(
            slow_llm(),
            child=FakeChild(),
            wall_timeout_sec=1.0,
            idle_timeout_sec=0.05,
        )

    assert asyncio.run(scenario()) is True


def test_worker_without_operation_or_heartbeat_triggers_idle_timeout():
    async def idle_worker() -> bool:
        await asyncio.sleep(0.16)
        return True

    async def scenario() -> bool:
        return await _run_worker_step_with_lease(
            idle_worker(),
            child=FakeChild(),
            wall_timeout_sec=1.0,
            idle_timeout_sec=0.05,
        )

    with pytest.raises(WorkerIdleTimeoutError):
        asyncio.run(scenario())


def test_llm_error_closes_in_flight_activity_operation():
    tracker = WorkerActivityTracker(worker_id="t_activity_error")
    token = set_current_worker_activity(tracker)
    callback = UsageTrackingCallback(session_id="s_activity", phase="worker")
    try:
        callback.on_llm_start({}, ["prompt"], run_id="llm-error")
        assert tracker.has_in_flight_operations() is True
        callback.on_llm_error(RuntimeError("provider failed"), run_id="llm-error")
        assert tracker.has_in_flight_operations() is False
    finally:
        reset_current_worker_activity(token)


def test_discovery_target_items_use_item_dimension_cost_model():
    step = PlanStep(
        step_type="research",
        task_id="t_landscape",
        description="发现 8 家候选公司",
        objective="发现 8 家候选公司",
        metadata={
            "task_kind": "discovery",
            "target_items": 8,
            "coverage_keys": ["赛道", "融资", "商业化", "技术", "团队", "前景"],
        },
    )
    complexity = analyze_task_granularity(step, {})
    assert complexity.entity_count == 8
    assert complexity.estimated_cells == 48
    assert complexity.oversized is True


def test_oversized_discovery_is_normalized_into_coverage_lanes():
    discovery = PlanStep(
        step_type="research",
        task_id="t_landscape",
        description="发现 8 家候选公司",
        objective="发现 8 家候选公司",
        metadata={
            "task_kind": "discovery",
            "target_items": 8,
            "coverage_keys": ["赛道", "融资", "商业化", "技术", "团队", "前景"],
        },
    )
    summary = PlanStep(
        step_type="summarize",
        task_id="t_summary",
        description="summarize",
        depends_on=["t_landscape"],
    )
    normalized = normalize_plan_granularity(
        [discovery, summary], {}, max_research_tasks=12
    )
    lanes = [step for step in normalized if step.task_id.startswith("t_landscape_d")]
    assert len(lanes) == 6
    assert all(analyze_task_granularity(step, {}).oversized is False for step in lanes)
    assert normalized[-1].depends_on == [step.task_id for step in lanes]


def test_candidate_artifact_replaces_discovery_hard_dependency():
    discovery = PlanStep(
        step_type="research",
        task_id="t_landscape",
        description="发现候选公司",
        metadata={"task_kind": "discovery"},
    )
    deep_dive = PlanStep(
        step_type="research",
        task_id="t_deep_dive",
        description="DeepSeek 深挖",
        depends_on=["t_landscape"],
    )
    plan = annotate_candidate_dependencies(
        ExecutionPlan(steps=[discovery, deep_dive], summary="candidate plan")
    )
    assert discovery.metadata["produces_artifact"] == "candidate_set"
    assert deep_dive.depends_on == []
    assert deep_dive.metadata["requires_artifacts"] == ["candidate_set"]
    ready = ready_steps(
        plan,
        {"t_landscape": "failed", "artifact:candidate_set": "available"},
        include_types={"research"},
    )
    assert [step.task_id for _, step in ready] == ["t_deep_dive"]


def test_failed_discovery_materializes_fallback_candidate_set():
    discovery = PlanStep(
        step_type="research",
        task_id="t_landscape",
        description="发现候选公司",
        metadata={"task_kind": "discovery"},
    )
    deep_dive = PlanStep(
        step_type="research",
        task_id="t_deep_dive",
        description="DeepSeek 深挖",
        depends_on=["t_landscape"],
    )
    plan = annotate_candidate_dependencies(
        ExecutionPlan(steps=[discovery, deep_dive], summary="candidate plan")
    )
    candidate_set = build_candidate_set(
        plan,
        worker_rows=[],
        task_status={"t_landscape": "failed"},
        brief={"entities": ["DeepSeek"]},
    )
    assert candidate_set["available"] is True
    assert candidate_set["fallback"] is True
    assert candidate_set["items"] == ["DeepSeek"]


def test_planner_budget_exceeded_falls_back_to_heuristic_plan(monkeypatch):
    from app.config.loader import get_harness_config
    from app.research.planning import compose as compose_module

    async def slow_lead_planner(*args: Any, **kwargs: Any):
        await asyncio.sleep(0.08)
        return None

    config = get_harness_config()
    monkeypatch.setattr(config, "planner_wall_budget_sec", 0.01)
    monkeypatch.setattr(compose_module, "lead_plan_with_llm", slow_lead_planner)
    intent = understand_task("比较 DeepSeek、Moonshot、智谱 2026 商业化进展")
    plan, issues = asyncio.run(
        compose_execution_plan(
            intent,
            model=object(),
            llm_enabled=True,
            config=config,
        )
    )
    assert plan.planning_mode == "dynamic"
    assert plan.steps
    assert all(not issue.startswith("planning_fallback") for issue in issues)


def test_emergency_synthesis_uses_minimal_evidence_pack_fast_path():
    state = LoopState(session_id="s_emergency")
    state.intent = understand_task("研究国内 AI 初创公司")
    state.plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_research",
                description="research",
                metadata={"status": StepStatus.DONE.value},
            ),
            PlanStep(
                step_type="summarize",
                task_id="t_summary",
                description="summarize",
                metadata={"status": StepStatus.PENDING.value},
            ),
        ],
        summary="emergency",
    )
    session = FakeSession(state, budget=FakeBudget(remaining_run_sec=1.0))
    runner_module.bind_session(session)
    try:
        graph_runner = runner_module.ResearchGraphRunner(FakeHarness())
        projected = asyncio.run(
            graph_runner.node_synthesize(
                {
                    "run_id": session.run_id,
                    "task_status": {"t_research": "done", "t_summary": "pending"},
                    "evidence_refs": ["art-web-1"],
                    "findings": [
                        {"task_id": "t_research", "summary": "partial evidence summary"}
                    ],
                }
            )
        )
    finally:
        runner_module.drop_session(session.run_id)

    assert projected["status"] == "partial"
    assert "partial evidence summary" in state.final_content
    termination = state.metadata["termination"]
    assert termination["status"] == "partial"
    assert termination["reason"] == "synthesis_time_reserve"
    assert termination["stage"] == "synthesis"
    assert termination["research_completed"] is False
    assert termination["synthesis_attempted"] is True
    assert termination["synthesis_status"] == "partial_fast_path"
    assert termination["quality_attempted"] is False
    assert termination["origin_stage"] == "research"
    assert termination["detected_stage"] == "dispatch"
    assert "force_synthesis" in termination["causal_chain"]


def test_emergency_zero_evidence_skips_llm_and_returns_partial():
    class NoLLMHarness(FakeHarness):
        async def _run_single_step(self, *args: Any, **kwargs: Any) -> bool:
            raise AssertionError("zero-evidence emergency synthesis must not call LLM")

    state = LoopState(session_id="s_emergency_no_evidence")
    state.intent = understand_task("研究国内 AI 初创公司")
    state.plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_research",
                description="research",
                metadata={"status": StepStatus.PENDING.value},
            ),
            PlanStep(
                step_type="summarize",
                task_id="t_summary",
                description="summarize",
                depends_on=["t_research"],
            ),
        ],
        summary="emergency no evidence",
    )
    harness = NoLLMHarness()
    session = FakeSession(state, harness=harness, budget=FakeBudget(remaining_run_sec=1.0))
    runner_module.bind_session(session)
    try:
        graph_runner = runner_module.ResearchGraphRunner(harness)
        projected = asyncio.run(
            graph_runner.node_synthesize(
                {
                    "run_id": session.run_id,
                    "task_status": {"t_research": "pending", "t_summary": "pending"},
                }
            )
        )
    finally:
        runner_module.drop_session(session.run_id)

    assert projected["status"] == "partial"
    assert projected["progress"] == "no_evidence_partial"
    assert projected["synthesis_mode"] == "no_evidence_partial"
    assert projected["trusted_evidence_count"] == 0


def test_minimal_evidence_pack_builds_under_emergency_budget():
    state = LoopState(session_id="s_minimal")
    findings = [
        {"task_id": f"t_{index}", "summary": f"finding {index}", "facts": [f"fact {index}"]}
        for index in range(2000)
    ]
    started = time.perf_counter()
    pack = build_minimal_evidence_pack(
        state=state,
        graph_state={"findings": findings},
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0
    assert len(pack["findings"]) == 16
    assert pack["findings"][0]["summary"] == "finding 0"


def test_emergency_context_budget_timeout_degrades_to_fallback_pack(monkeypatch):
    from app.research.runtime import fast_synthesis as fast_synthesis_module

    def slow_pack(*, state: Any, graph_state: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.08)
        raise AssertionError("slow pack should be cancelled")

    monkeypatch.setattr(fast_synthesis_module, "build_minimal_evidence_pack", slow_pack)
    harness = FakeHarness()
    harness.harness_config.emergency_context_budget_sec = 0.01
    state = LoopState(session_id="s_emergency_timeout")
    state.intent = understand_task("研究国内 AI 初创公司")
    state.plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_research",
                description="research",
                metadata={"status": StepStatus.DONE.value},
            ),
            PlanStep(
                step_type="summarize",
                task_id="t_summary",
                description="summarize",
                metadata={"status": StepStatus.PENDING.value},
            ),
        ],
        summary="emergency timeout",
    )
    session = FakeSession(state, harness=harness, budget=FakeBudget(remaining_run_sec=1.0))
    runner_module.bind_session(session)
    try:
        graph_runner = runner_module.ResearchGraphRunner(harness)
        projected = asyncio.run(
            graph_runner.node_synthesize(
                {
                    "run_id": session.run_id,
                    "task_status": {"t_research": "done", "t_summary": "pending"},
                    "evidence_refs": ["art-web-1"],
                    "findings": [
                        {"task_id": "t_research", "summary": "fallback evidence"}
                    ],
                }
            )
        )
    finally:
        runner_module.drop_session(session.run_id)

    assert projected["status"] == "partial"
    assert state.metadata["emergency_context_timeout"] is True
    assert "fallback evidence" in state.final_content


def test_quality_gate_marks_attempt_and_updates_termination_stage():
    class Outcome:
        passed = True
        severity = "warning"
        reason = ""

    class Validator:
        def validate_finalize(self, *args: Any, **kwargs: Any) -> Outcome:
            return Outcome()

    class QualityHarness(FakeHarness):
        def __init__(self):
            super().__init__()
            self.validator = Validator()

        async def _phase_validate(self, *args: Any, **kwargs: Any) -> None:
            return None

    state = LoopState(session_id="s_quality")
    state.final_content = "answer"
    state.metadata["termination"] = {
        "status": "partial",
        "reason": "deadline_exceeded",
        "stage": "synthesis",
        "quality_attempted": False,
    }
    session = FakeSession(state, harness=QualityHarness())
    runner_module.bind_session(session)
    try:
        graph_runner = runner_module.ResearchGraphRunner(session.harness)
        asyncio.run(graph_runner.node_quality_gate({"run_id": session.run_id}))
    finally:
        runner_module.drop_session(session.run_id)

    assert state.metadata["quality_attempted"] is True
    assert state.metadata["termination"]["quality_attempted"] is True
    assert state.metadata["termination"]["stage"] == "quality"


def test_blocked_research_is_execution_health_not_semantic_gap():
    discovery = PlanStep(
        step_type="research",
        task_id="t_landscape",
        description="发现候选公司",
        metadata={"status": StepStatus.FAILED.value, "task_kind": "discovery"},
    )
    deep_dive = PlanStep(
        step_type="research",
        task_id="t_deep_dive",
        description="DeepSeek 融资深挖",
        metadata={"status": StepStatus.PENDING.value},
    )
    plan = annotate_candidate_dependencies(
        ExecutionPlan(steps=[discovery, deep_dive], summary="blocked plan")
    )
    assessment = assess_progress(
        plan,
        task_status={"t_landscape": "failed", "t_deep_dive": "pending"},
        worker_results=[],
        query="DeepSeek 融资",
    )
    assert assessment.execution_blocked_tasks == ["t_deep_dive"]
    assert assessment.reason == "blocked_pending_research"
    assert not any("blocked:" in gap for gap in assessment.coverage_gaps)
    assert any(item.get("type") == "execution_blocker" for item in assessment.gaps)


def test_failed_worker_with_partial_evidence_is_not_empty_coverage_gap():
    step = PlanStep(
        step_type="research",
        task_id="t_failed",
        description="DeepSeek 融资深挖",
        metadata={"status": StepStatus.FAILED.value},
    )
    plan = ExecutionPlan(steps=[step], summary="partial evidence")
    assessment = assess_progress(
        plan,
        task_status={"t_failed": "failed"},
        worker_results=[
            {
                "task_id": "t_failed",
                "ok": False,
                "fail_reason": "idle_timeout",
                "payload": {
                    "findings": [{"summary": "partial finding"}],
                    "evidence_ids": ["art-web-1"],
                },
            }
        ],
        query="DeepSeek 融资",
    )
    assert assessment.execution_failed_tasks == ["t_failed"]
    assert assessment.execution_failure_reasons == ["idle_timeout"]
    assert "empty:t_failed" not in assessment.coverage_gaps


def test_replan_affordability_includes_complete_wave_overhead():
    state = LoopState(session_id="s_replan")
    state.intent = understand_task("DeepSeek 融资与技术路线")
    state.plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_failed",
                description="DeepSeek 融资",
                metadata={"status": StepStatus.FAILED.value},
            ),
            PlanStep(
                step_type="summarize",
                task_id="t_summary",
                description="summarize",
                metadata={"status": StepStatus.PENDING.value},
            ),
        ],
        summary="replan plan",
    )
    session = FakeSession(state, budget=FakeBudget(remaining_run_sec=1.0))
    runner_module.bind_session(session)
    try:
        graph_runner = runner_module.ResearchGraphRunner(FakeHarness())
        projected = asyncio.run(
            graph_runner.node_replan(
                {
                    "run_id": session.run_id,
                    "progress_assessment": {
                        "verdict": "gap",
                        "reason": "execution_failure",
                        "gaps": [
                            {
                                "type": "execution_failure",
                                "task_id": "t_failed",
                                "description": "worker failed",
                            }
                        ],
                    },
                    "worker_results": [
                        {
                            "task_id": "t_failed",
                            "execution_ms": 3000,
                            "queue_ms": 1000,
                        }
                    ],
                }
            )
        )
    finally:
        runner_module.drop_session(session.run_id)

    assert projected["progress_assessment"]["reason"] == "replan_unaffordable_force_synthesis"
    assert state.metadata["force_synthesis"] is True
    assert state.metadata["estimated_wave_cost_sec"] >= 50


def test_deadline_partial_with_unreached_quality_passes_trace_integrity():
    events = [
        {"type": "run.started", "seq": 1, "attributes": {"search_mode": "agent"}},
        {"type": "brief.compiled", "seq": 2, "attributes": {"search_mode": "agent"}},
        {"type": "plan.created", "seq": 3, "attributes": {}},
        {"type": "worker.started", "seq": 4, "attributes": {}},
        {"type": "worker.failed", "seq": 5, "attributes": {"fail_reason": "idle_timeout"}},
        {"type": "progress.evaluated", "seq": 6, "attributes": {}},
        {
            "type": "synthesis.completed",
            "seq": 7,
            "attributes": {"fast_path": True, "synthesis_status": "partial_fast_path"},
        },
        {
            "type": "run.failed",
            "seq": 8,
            "attributes": {
                "metadata": {
                    "termination": {
                        "status": "partial",
                        "reason": "deadline_exceeded",
                        "stage": "synthesis",
                        "quality_attempted": False,
                    }
                }
            },
        },
    ]
    result = check_trace_integrity(events, run_status="partial")
    assert result["passed"] is True
    assert "missing_quality_event" not in result["issues"]
    assert "missing_progress_event" not in result["issues"]


def test_pre_research_partial_does_not_require_worker_or_progress_event():
    events = [
        {"type": "run.started", "seq": 1, "attributes": {"search_mode": "agent"}},
        {"type": "brief.compiled", "seq": 2, "attributes": {"search_mode": "agent"}},
        {
            "type": "run.failed",
            "seq": 3,
            "attributes": {
                "metadata": {
                    "termination": {
                        "status": "partial",
                        "reason": "provider_usage_limit",
                        "stage": "understand",
                        "quality_attempted": False,
                    }
                }
            },
        },
    ]
    result = check_trace_integrity(events, run_status="partial")
    assert result["passed"] is True
    assert "missing_worker_terminal_event" not in result["issues"]
    assert "missing_progress_event" not in result["issues"]


def test_quality_attempted_partial_requires_quality_event():
    events = [
        {"type": "run.started", "seq": 1, "attributes": {"search_mode": "agent"}},
        {"type": "brief.compiled", "seq": 2, "attributes": {"search_mode": "agent"}},
        {"type": "plan.created", "seq": 3, "attributes": {}},
        {"type": "worker.started", "seq": 4, "attributes": {}},
        {"type": "worker.completed", "seq": 5, "attributes": {}},
        {"type": "progress.evaluated", "seq": 6, "attributes": {}},
        {"type": "synthesis.completed", "seq": 7, "attributes": {}},
        {
            "type": "run.failed",
            "seq": 8,
            "attributes": {
                "metadata": {
                    "termination": {
                        "status": "partial",
                        "reason": "quality_failed",
                        "stage": "quality",
                        "quality_attempted": True,
                    }
                }
            },
        },
    ]
    result = check_trace_integrity(events, run_status="partial")
    assert result["passed"] is False
    assert "missing_quality_event" in result["issues"]


def test_partial_without_termination_reason_fails_trace_integrity():
    events = [
        {"type": "run.started", "seq": 1, "attributes": {"search_mode": "agent"}},
        {"type": "brief.compiled", "seq": 2, "attributes": {"search_mode": "agent"}},
        {"type": "run.failed", "seq": 3, "attributes": {"metadata": {}}},
    ]
    result = check_trace_integrity(events, run_status="partial")
    assert "partial_without_termination_reason" in result["issues"]


def test_stale_zero_span_projection_is_rebuilt(monkeypatch):
    from app.observability import replay as replay_module
    from app.observability import projection_store

    class FakeRecord:
        def to_jsonl_record(self) -> dict[str, Any]:
            return {"type": "worker.started", "span_id": "span-1"}

    class FakeProjectionStore:
        def __init__(self, cached: dict[str, Any] | None):
            self.cached = cached
            self.saved: list[dict[str, Any]] = []

        def get_projection(self, run_id: str, kind: str) -> dict[str, Any] | None:
            return self.cached

        def put_projection(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
            self.saved.append(payload)

    store = FakeProjectionStore({"span_count": 0, "event_count": 1})
    monkeypatch.setattr(replay_module, "load_events", lambda *args, **kwargs: [FakeRecord()])
    monkeypatch.setattr(
        replay_module,
        "build_span_tree",
        lambda records: {"span_count": 1, "root_count": 1, "cycle_count": 0, "valid": True},
    )
    monkeypatch.setattr(projection_store, "get_projection_store", lambda: store)

    tree = replay_module.load_tree_projection("s_projection", run_id="r_projection")
    assert tree["span_count"] == 1
    assert tree["event_count"] == 1
    assert store.saved
