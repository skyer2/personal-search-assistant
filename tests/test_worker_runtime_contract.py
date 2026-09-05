"""WorkerRuntime return-contract regressions."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep
from app.research.runtime import runner as runner_module
from app.research.runtime import worker as worker_module
from app.research.runtime.isolation import IsolatedWorkerOutcome
from app.research.runtime.worker import (
    LangChainWorkerRuntime,
    ResearchContext,
    ResearchTask,
    WorkerResult,
)


@dataclass
class FakeConfig:
    step_timeout_sec: int = 10
    max_retries: int = 0
    hitl_enabled: bool = False


class FakeHarness:
    def __init__(self, step_result: bool = True):
        self.harness_config = FakeConfig()
        self._step_result = step_result

    async def _run_single_step(self, *args: Any, **kwargs: Any) -> bool:
        return self._step_result

    def _refresh_working_memory(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeBudget:
    def __init__(self, allowed: bool = True):
        self._allowed = allowed

    def sync_from_usage(self, *args: Any, **kwargs: Any) -> None:
        return None

    def research_allowed(self) -> tuple[bool, str]:
        return self._allowed, "" if self._allowed else "budget_tokens"

    def remaining_for_research_sec(self) -> float:
        return 60.0 if self._allowed else 0.0


class FakeContext:
    task_query = "research query"
    user_id = "me"
    tenant_id = "local"
    project_id = "Inbox"
    relative_session_dir = "session_s/updated"
    uploaded_prompt = ""
    session_dir = Path("output/session_s")
    idempotency = None
    citation_manager = None
    run_dir = None


class FakeSession:
    def __init__(self, state: LoopState, budget: FakeBudget):
        self.state = state
        self.ctx = FakeContext()
        self.session_id = state.session_id
        self.run_id = "r_contract"
        self.lock = asyncio.Lock()
        self.worker_sem = asyncio.Semaphore(1)
        self.budget_manager = budget


def _runtime(optional: bool = False, allowed: bool = True) -> tuple[LangChainWorkerRuntime, LoopState]:
    state = LoopState(session_id="s_contract")
    state.plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                description="collect evidence",
                task_id="t_contract",
                metadata={"optional": optional},
            )
        ],
        summary="contract",
    )
    session = FakeSession(state, FakeBudget(allowed))
    return LangChainWorkerRuntime(FakeHarness(), session), state


def _task() -> ResearchTask:
    return ResearchTask(
        task_id="t_contract",
        objective="collect evidence",
        step_type="research",
        step_index=0,
        plan_version=1,
    )


def _context() -> ResearchContext:
    return ResearchContext(run_id="r_contract", query="research query", session_id="s_contract")


def test_worker_success_returns_worker_result_done():
    runtime, _state = _runtime()
    result = asyncio.run(runtime.execute(_task(), _context()))
    assert isinstance(result, WorkerResult)
    assert result.status == "done"
    assert result.ok is True
    print("[OK] worker success contract")


def test_worker_timeout_returns_worker_result_failed(monkeypatch):
    runtime, _state = _runtime()

    async def timeout_immediately(*args: Any, **kwargs: Any):
        awaitable = args[0] if args else kwargs.get("aw")
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError

    async def lease_timeout(
        awaitable: Any,
        *,
        child: Any,
        wall_timeout_sec: float,
        idle_timeout_sec: float,
    ) -> bool:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(worker_module, "_run_worker_step_with_lease", lease_timeout)
    result = asyncio.run(runtime.execute(_task(), _context()))
    assert isinstance(result, WorkerResult)
    assert result.status == "failed"
    assert result.ok is False
    assert result.fail_reason == "step_timeout"
    assert isinstance(result.raw, IsolatedWorkerOutcome)
    print("[OK] worker timeout contract")


def test_worker_budget_blocked_returns_worker_result_blocked():
    runtime, state = _runtime(allowed=False)
    result = asyncio.run(runtime.execute(_task(), _context()))
    assert isinstance(result, WorkerResult)
    assert result.status == "blocked"
    assert result.ok is False
    assert result.fail_reason == "budget_tokens"
    assert state.metadata["force_synthesis"] is True
    print("[OK] worker budget blocked contract")


def test_worker_optional_early_stop_returns_worker_result_skipped():
    runtime, state = _runtime(optional=True)
    state.metadata["force_synthesis"] = True
    result = asyncio.run(runtime.execute(_task(), _context()))
    assert isinstance(result, WorkerResult)
    assert result.status == "skipped"
    assert result.ok is True
    assert result.summary == "skipped_optional_early_stop"
    print("[OK] worker optional skip contract")


def test_worker_missing_session_returns_worker_result_failed():
    runtime = LangChainWorkerRuntime(FakeHarness(), None)
    result = asyncio.run(runtime.execute(_task(), _context()))
    assert isinstance(result, WorkerResult)
    assert result.status == "failed"
    assert result.fail_reason == "missing_session"
    print("[OK] worker missing session contract")


def test_node_research_worker_handles_timeout_without_crashing(monkeypatch):
    state = LoopState(session_id="s_node_contract")
    state.plan = ExecutionPlan(
        steps=[PlanStep(step_type="research", description="collect", task_id="t_node")],
        summary="node contract",
    )
    session = FakeSession(state, FakeBudget(True))
    runner_module.bind_session(session)
    monkeypatch.setattr(
        worker_module,
        "LangChainWorkerRuntime",
        lambda harness, worker_session: _TimeoutRuntime(worker_session),
    )
    try:
        graph_runner = runner_module.ResearchGraphRunner(FakeHarness())
        projected = asyncio.run(
            graph_runner.node_research_worker(
                {
                    "run_id": session.run_id,
                    "step_index": 0,
                    "task_id": "t_node",
                    "step_type": "research",
                    "plan_version": 1,
                }
            )
        )
    finally:
        runner_module.drop_session(session.run_id)

    assert projected["task_status"]["t_node"] == "failed"
    assert projected["worker_results"][0]["status"] == "failed"
    assert projected["worker_results"][0]["fail_reason"] == "step_timeout"
    print("[OK] node handles timeout WorkerResult")


def test_node_research_worker_rejects_dict_contract(monkeypatch):
    state = LoopState(session_id="s_node_dict")
    state.plan = ExecutionPlan(
        steps=[PlanStep(step_type="research", description="collect", task_id="t_dict")],
        summary="dict contract",
    )
    session = FakeSession(state, FakeBudget(True))
    runner_module.bind_session(session)
    monkeypatch.setattr(
        worker_module,
        "LangChainWorkerRuntime",
        lambda harness, worker_session: _DictRuntime(worker_session),
    )
    try:
        graph_runner = runner_module.ResearchGraphRunner(FakeHarness())
        try:
            asyncio.run(
                graph_runner.node_research_worker(
                    {
                        "run_id": session.run_id,
                        "step_index": 0,
                        "task_id": "t_dict",
                        "step_type": "research",
                    }
                )
            )
        except TypeError as exc:
            assert "WorkerRuntime.execute must return WorkerResult" in str(exc)
        else:
            raise AssertionError("dict return must fail contract assertion")
    finally:
        runner_module.drop_session(session.run_id)
    print("[OK] node rejects broken WorkerRuntime contract")


class _TimeoutRuntime:
    def __init__(self, session: FakeSession):
        self.session = session

    async def execute(self, task: ResearchTask, context: ResearchContext) -> WorkerResult:
        child = self.session.state
        return WorkerResult(
            ok=False,
            task_id=task.task_id,
            status="failed",
            summary="step_timeout",
            raw=IsolatedWorkerOutcome(
                step_index=0,
                task_id=task.task_id,
                ok=False,
                result=None,
                child_state=child,
                fail_reason="step_timeout",
            ),
            fail_reason="step_timeout",
        )


class _DictRuntime:
    def __init__(self, session: FakeSession):
        self.session = session

    async def execute(self, task: ResearchTask, context: ResearchContext) -> WorkerResult:
        return {"task_status": {task.task_id: "failed"}}
