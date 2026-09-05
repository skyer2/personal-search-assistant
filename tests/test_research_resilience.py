"""Regressions for planner granularity and worker partial-evidence resilience."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.artifacts import ArtifactStore, reset_artifact_store, set_artifact_store
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep, StepStatus
from app.research.planning.granularity import (
    analyze_task_granularity,
    normalize_plan_granularity,
    split_oversized_step,
)
from app.research.planning.progress import assess_progress
from app.research.runtime.worker import (
    LangChainWorkerRuntime,
    ResearchContext,
    ResearchTask,
    WorkerResult,
)
from app.research.runtime import runner as runner_module


@dataclass
class FakeConfig:
    step_timeout_sec: int = 10
    max_retries: int = 0


class FakeHarness:
    def __init__(self, step_error: Exception | None = None):
        self.harness_config = FakeConfig()
        self._step_error = step_error

    async def _run_single_step(self, *args: Any, **kwargs: Any) -> bool:
        if self._step_error is not None:
            raise self._step_error
        return True

    def _refresh_working_memory(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeBudget:
    def sync_from_usage(self, *args: Any, **kwargs: Any) -> None:
        return None

    def research_allowed(self) -> tuple[bool, str]:
        return True, ""

    def remaining_for_research_sec(self) -> float:
        return 60.0


class FakeContext:
    task_query = "research query"
    user_id = "me"
    tenant_id = "local"
    project_id = "Inbox"
    relative_session_dir = "sessions/s"
    uploaded_prompt = ""
    session_dir = Path("output/s")
    idempotency = None
    citation_manager = None
    checkpoint_store = None
    run_dir = None


class FakeSession:
    def __init__(self, state: LoopState, harness: FakeHarness):
        self.state = state
        self.ctx = FakeContext()
        self.session_id = state.session_id
        self.run_id = "r_resilience"
        self.lock = asyncio.Lock()
        self.worker_sem = asyncio.Semaphore(1)
        self.budget_manager = FakeBudget()
        self.harness = harness


def _plan_state() -> tuple[LoopState, FakeSession, FakeHarness]:
    state = LoopState(session_id="s_resilience")
    state.plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                description="collect evidence",
                task_id="t_resilience",
                metadata={"status": StepStatus.PENDING.value},
            )
        ],
        summary="resilience",
    )
    harness = FakeHarness()
    return state, FakeSession(state, harness), harness


def _task() -> ResearchTask:
    return ResearchTask(
        task_id="t_resilience",
        objective="collect evidence",
        step_type="research",
        step_index=0,
        plan_version=1,
    )


def _context() -> ResearchContext:
    return ResearchContext(
        run_id="r_resilience",
        query="research query",
        session_id="s_resilience",
    )


def test_oversized_task_is_split_into_worker_sized_tasks():
    entities = ["DeepSeek", "Moonshot", "智谱", "MiniMax", "StepFun"]
    dimensions = ["融资", "技术", "商业化", "团队", "招聘", "评测"]
    step = PlanStep(
        step_type="research",
        description="、".join(entities),
        task_id="t_llm_head",
        objective="、".join(entities),
        metadata={"coverage_keys": dimensions},
    )
    brief = {"entities": entities, "dimensions": dimensions}
    complexity = analyze_task_granularity(step, brief)
    assert complexity.oversized is True
    split = split_oversized_step(step, brief)
    assert len(split) == 6
    assert all(analyze_task_granularity(item, brief).oversized is False for item in split)


def test_normalize_granularity_rewrites_dependents_to_all_splits():
    oversized = PlanStep(
        step_type="research",
        description="DeepSeek、Moonshot、智谱",
        task_id="t_source",
        objective="DeepSeek、Moonshot、智谱",
        metadata={"coverage_keys": ["融资", "技术", "商业化", "团队", "招聘"]},
    )
    synthesis = PlanStep(
        step_type="summarize",
        description="summarize",
        task_id="t_summary",
        depends_on=["t_source"],
    )
    brief = {"entities": ["DeepSeek", "Moonshot", "智谱"], "dimensions": ["融资", "技术", "商业化", "团队", "招聘"]}
    normalized = normalize_plan_granularity([oversized, synthesis], brief, max_research_tasks=8)
    assert len([item for item in normalized if item.step_type == "research"]) == 4
    assert normalized[-1].depends_on == [
        "t_source_g1_1",
        "t_source_g1_2",
        "t_source_g2_1",
        "t_source_g2_2",
    ]


def test_worker_timeout_salvages_artifact_evidence(monkeypatch):
    from app.research.runtime import worker as worker_module

    state, session, _harness = _plan_state()
    runtime = LangChainWorkerRuntime(FakeHarness(), session)
    store = ArtifactStore()
    set_artifact_store(store)
    try:
        async def timeout_with_artifact(awaitable: Any, timeout: float) -> bool:
            if hasattr(awaitable, "close"):
                awaitable.close()
            store.put(
                "partial web evidence",
                kind="web",
                locator="https://example.com/partial",
                title="Partial evidence",
                step_index=0,
                step_type="research",
                metadata={"task_id": "t_resilience"},
            )
            raise asyncio.TimeoutError

        monkeypatch.setattr(worker_module.asyncio, "wait_for", timeout_with_artifact)
        result = asyncio.run(runtime.execute(_task(), _context()))
        assert result.status == "failed"
        assert result.fail_reason == "step_timeout"
        assert result.evidence_refs
        assert result.findings
        assert result.findings[0]["partial"] is True
    finally:
        reset_artifact_store()


def test_sensitive_content_failure_returns_worker_result_not_crash():
    state, session, _harness = _plan_state()
    error = RuntimeError(
        "Error code: 400 - SensitiveContentDetected: input may contain sensitive information"
    )
    harness = FakeHarness(error)
    runtime = LangChainWorkerRuntime(harness, session)
    store = ArtifactStore()
    set_artifact_store(store)
    try:
        store.put(
            "sensitive page artifact",
            kind="web",
            locator="https://example.com/sensitive",
            title="Sensitive page",
            step_index=0,
            step_type="research",
            metadata={"task_id": "t_resilience"},
        )
        result = asyncio.run(runtime.execute(_task(), _context()))
        assert isinstance(result, WorkerResult)
        assert result.status == "failed"
        assert result.fail_reason == "content_filter"
        assert result.summary == "provider_content_filter"
        assert result.evidence_refs
    finally:
        reset_artifact_store()


def test_failed_worker_with_partial_evidence_does_not_create_empty_gap():
    state = LoopState(session_id="s_progress")
    state.plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                description="research",
                task_id="t_research",
                metadata={"status": StepStatus.FAILED.value},
            ),
            PlanStep(
                step_type="summarize",
                description="summarize",
                task_id="t_summary",
                depends_on=["t_research"],
            ),
        ],
        summary="plan",
        planning_mode="dynamic",
    )
    assessment = assess_progress(
        state.plan,
        task_status={"t_research": "failed", "t_summary": "pending"},
        state=state,
        worker_results=[
            {
                "task_id": "t_research",
                "ok": False,
                "status": "failed",
                "payload": {
                    "findings": [{"task_id": "t_research", "summary": "partial"}],
                    "evidence_ids": ["art-web-1"],
                },
            }
        ],
        query="research query",
    )
    assert assessment.coverage_gaps == []
    assert assessment.verdict in {"enough", "gap"}


def test_all_workers_failed_with_evidence_still_delivers_partial_report():
    state = LoopState(session_id="s_partial")
    state.plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                description="research",
                task_id="t_research",
                metadata={"status": StepStatus.FAILED.value},
            ),
            PlanStep(
                step_type="summarize",
                description="summarize",
                task_id="t_summary",
                depends_on=["t_research"],
            ),
        ],
        summary="partial plan",
        planning_mode="dynamic",
    )
    harness = FakeHarness(
        RuntimeError(
            "Error code: 400 - SensitiveContentDetected: input may contain sensitive information"
        )
    )
    session = FakeSession(state, harness)
    runner_module.bind_session(session)
    try:
        graph_runner = runner_module.ResearchGraphRunner(harness)
        projected = asyncio.run(
            graph_runner.node_synthesize(
                {
                    "run_id": session.run_id,
                    "plan": state.plan.to_dict(),
                    "task_status": {"t_research": "failed", "t_summary": "pending"},
                    "evidence_refs": ["art-web-1"],
                    "findings": [
                        {
                            "task_id": "t_research",
                            "summary": "partial evidence summary",
                        }
                    ],
                }
            )
        )
        assert projected["status"] == "partial"
        assert state.abort_reason == "provider_content_filter"
        assert "partial evidence summary" in state.final_content
        assert state.metadata["partial_delivered"] is True
        assert state.metadata["synthesis_failed"] is True
    finally:
        runner_module.drop_session(session.run_id)


def test_worker_or_compress_failure_does_not_require_quality_event():
    from app.observability.integrity import check_trace_integrity

    events = [
        {"type": "run.started", "seq": 1, "attributes": {"search_mode": "agent"}},
        {"type": "brief.compiled", "seq": 2, "attributes": {"search_mode": "agent"}},
        {"type": "plan.created", "seq": 3, "attributes": {}},
        {"type": "worker.started", "seq": 4, "attributes": {}},
        {"type": "worker.failed", "seq": 5, "attributes": {"fail_reason": "step_timeout"}},
        {
            "type": "run.failed",
            "seq": 6,
            "attributes": {
                "failure.origin_stage": "compress",
                "failure.detected_stage": "runtime",
            },
        },
    ]
    result = check_trace_integrity(events, run_status="failed")
    assert result["passed"] is True
    assert "missing_quality_event" not in result["issues"]
