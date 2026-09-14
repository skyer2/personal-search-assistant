from __future__ import annotations

import pytest

from app.agent.harness.artifacts import ArtifactStore, set_artifact_store
from app.agent.harness.state import PlanStep, StepResult
from app.research.execution.worker_executor import WorkerExecutorV2
from app.research.runtime.worker import ResearchContext, ResearchTask


def _reset_store() -> ArtifactStore:
    store = ArtifactStore()
    set_artifact_store(store)
    return store


def _task() -> ResearchTask:
    return ResearchTask(
        task_id="t_timeout",
        objective="collect evidence",
        step_type="research",
        step_index=2,
    )


def _step() -> PlanStep:
    return PlanStep(
        step_type="research",
        task_id="t_timeout",
        description="collect evidence",
        objective="collect evidence",
    )


def test_timeout_without_evidence_is_failed_without_result():
    _reset_store()
    result = WorkerExecutorV2(None, None)._salvage_or_fail(
        _task(),
        ResearchContext(run_id="run-worker", query="collect evidence"),
        _step(),
        2,
        0.0,
        cause="worker_timeout",
        fail_reason="worker_timeout",
        status="failed",
        ok=False,
    )
    assert result.ok is False
    assert result.status == "failed"
    assert result.fail_reason == "worker_timeout"
    assert result.evidence_refs == []
    assert result.findings == []


def test_timeout_with_evidence_is_failed_partial_and_keeps_failure():
    store = _reset_store()
    artifact = store.put(
        "DeepSeek was founded by Liang Wenfeng.",
        kind="web",
        locator="https://example.com/deepseek",
        title="DeepSeek profile",
        metadata={"run_id": "run-worker", "task_id": "t_timeout"},
        step_index=2,
        step_type="research",
    )
    result = WorkerExecutorV2(None, None)._salvage_or_fail(
        _task(),
        ResearchContext(run_id="run-worker", query="collect evidence"),
        _step(),
        2,
        0.0,
        cause="worker_timeout",
        fail_reason="worker_timeout",
        status="failed",
        ok=False,
    )
    assert result.ok is False
    assert result.status == "partial"
    assert result.fail_reason == "worker_timeout"
    assert result.evidence_refs == [artifact.artifact_id]
    assert result.findings
    assert result.sources == ["https://example.com/deepseek"]


def test_soft_finalization_with_evidence_is_normal_done():
    store = _reset_store()
    artifact = store.put(
        "Company A traction is growing.",
        kind="web",
        locator="https://example.com/company-a",
        title="Company A profile",
        metadata={"run_id": "run-worker", "task_id": "t_timeout"},
        step_index=2,
        step_type="research",
    )
    result = WorkerExecutorV2(None, None)._salvage_or_fail(
        _task(),
        ResearchContext(run_id="run-worker", query="collect evidence"),
        _step(),
        2,
        0.0,
        cause="budget_blocked:worker_token_cap",
        fail_reason="worker_token_cap",
        status="blocked",
        ok=False,
        stop_reason="soft_budget_finalize",
    )

    assert result.ok is True
    assert result.status == "done"
    assert result.fail_reason == "worker_token_cap"
    assert result.metrics["stop_reason"] == "soft_budget_finalize"
    assert result.evidence_refs == [artifact.artifact_id]
    assert result.findings


def test_soft_deadline_reserves_one_model_call(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SEC", "180")
    monkeypatch.setenv("LLM_WORKER_TIMEOUT_SEC", "60")

    assert WorkerExecutorV2._soft_deadline_delay(120) == 50.0


def test_worker_model_timeout_inherits_global_timeout(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SEC", "180")
    monkeypatch.delenv("LLM_WORKER_TIMEOUT_SEC", raising=False)

    assert WorkerExecutorV2._model_timeout_sec() == 180.0


@pytest.mark.asyncio
async def test_structured_soft_finalization_payload_is_normal_worker_done(monkeypatch):
    from types import SimpleNamespace
    import json

    from app.research.execution import worker_executor as worker_executor_module

    class FakeBudgetManager:
        def reserve_worker_lease(self, *args, **kwargs):
            return "lease", ""

        def release_worker_lease(self, *args, **kwargs):
            return None

    class FakeState:
        plan = SimpleNamespace(steps=[_step()])

    class FakeSession:
        state = FakeState()
        budget_manager = FakeBudgetManager()
        ctx = SimpleNamespace()
        active_wave_size = 1

    class FakeHarness:
        harness_config = SimpleNamespace(step_timeout_sec=10)

        def _enrich_worker_result(self, _step, result, _state):
            return result

    async def invoke(*args, **kwargs):
        kwargs["tool_usage"]["finalization_reason"] = "soft_budget_finalize"
        return StepResult(
            step_type="research",
            content=json.dumps(
                {
                    "ok": False,
                    "summary": "soft stop with evidence",
                    "evidence_ids": ["art-web-1"],
                    "facts": ["Company A traction is growing."],
                    "sources": ["https://example.com/company-a"],
                    "error_code": "worker_token_cap",
                }
            ),
            metadata={},
        )

    monkeypatch.setattr(
        worker_executor_module,
        "resolve_execute_target",
        lambda *args, **kwargs: (object(), "direct"),
    )
    monkeypatch.setattr(WorkerExecutorV2, "_invoke_leaf", invoke)

    result = await WorkerExecutorV2(FakeHarness(), FakeSession()).execute(
        ResearchTask(
            task_id="t_timeout",
            objective="collect evidence",
            step_type="research",
            step_index=0,
        ),
        ResearchContext(run_id="run-worker", query="collect evidence"),
    )

    assert result.ok is True
    assert result.status == "done"
    assert result.fail_reason == ""
    assert result.metrics["stop_reason"] == "soft_budget_finalize"


@pytest.mark.asyncio
async def test_partial_worker_result_maps_to_failed_partial_canonical_state(monkeypatch):
    from types import SimpleNamespace

    from app.agent.harness.state import ExecutionPlan
    from app.research.domain.task_state import initialize_tasks
    from app.research.execution import worker_executor as worker_executor_module
    from app.research.runtime import runner as runner_module
    from app.research.runtime.runner import ResearchGraphRunner
    from app.research.runtime.state import empty_research_state
    from app.research.runtime.worker import WorkerResult

    class FakeState:
        def __init__(self, plan):
            self.plan = plan
            self.step_results = []
            self.metadata = {}

    class FakeSession:
        def __init__(self, harness, plan):
            self.harness = harness
            self.ctx = SimpleNamespace(
                task_query="collect evidence",
                user_id="me",
                tenant_id="local",
                project_id="Inbox",
            )
            self.run_id = "run-worker"
            self.session_id = "session-worker"
            self.state = FakeState(plan)

    class FakeExecutor:
        def __init__(self, harness, session):
            self.harness = harness
            self.session = session

        async def execute(self, task, context):
            return WorkerResult(
                ok=False,
                task_id=task.task_id,
                status="partial",
                summary="worker_timeout; recovered evidence",
                findings=[
                    {
                        "task_id": task.task_id,
                        "summary": "partial evidence",
                        "sources": ["https://example.com/partial"],
                    }
                ],
                evidence_refs=["art-web-1"],
                fail_reason="worker_timeout",
            )

    plan = ExecutionPlan(steps=[_step()], summary="test")
    harness = SimpleNamespace(
        harness_config=SimpleNamespace(hitl_enabled=False, hitl_step_gate_types=[])
    )
    session = FakeSession(harness, plan)
    graph_runner = ResearchGraphRunner(harness)
    gstate = empty_research_state(
        run_id="run-worker",
        session_id="session-worker",
        task_query="collect evidence",
    )
    gstate.update(
        {
            "phase": "dispatch",
            "plan": plan.to_dict(),
            "tasks": initialize_tasks(plan),
            "step_index": 0,
            "task_id": "t_timeout",
        }
    )
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)
    monkeypatch.setattr(worker_executor_module, "WorkerExecutorV2", FakeExecutor)

    update = await graph_runner.node_research_worker(gstate)
    task = update["tasks"]["t_timeout"]
    assert task["execution_status"] == "stopped"
    assert task["result_status"] == "partial"
    assert task["failure"]["code"] == "worker_timeout"
    assert task["stop_reason"] == "timeout"
    assert task["failure"]["code"] == "worker_timeout"
    assert task["evidence_refs"] == ["art-web-1"]
    assert update["evidence_refs"] == ["art-web-1"]
