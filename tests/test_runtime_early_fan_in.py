"""Early fan-in and actual-wave resource lease regressions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.agent.harness.run_budget import RunBudgetManager
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep
from app.research.brief.compiler import compile_structured_brief
from app.research.domain.task_state import initialize_tasks
from app.research.execution import worker_executor as worker_executor_module
from app.research.execution.worker_executor import WorkerExecutorV2
from app.research.runtime import runner as runner_module
from app.research.runtime.ingestion import ingest_new_worker_results
from app.research.runtime.state import empty_research_state
from app.research.runtime.worker import ResearchContext, ResearchTask, WorkerResult


class FakeConfig:
    max_parallel_workers = 4
    max_llm_calls_per_worker = 8
    step_timeout_sec = 30
    max_retries = 0
    hitl_enabled = False
    hitl_step_gate_types = ()


class FakeHarness:
    harness_config = FakeConfig()


def _session() -> runner_module.RunSession:
    loop = LoopState(session_id="early-fan-in", run_id="early-fan-in")
    ctx = SimpleNamespace(
        state=loop,
        session_id=loop.session_id,
        run_id=loop.run_id,
        lock=SimpleNamespace(),
        budget_manager=RunBudgetManager(token_limit=100_000, llm_call_limit=100),
        citation_manager=None,
        user_id="me",
        tenant_id="local",
        project_id="Inbox",
        run_started=0.0,
        task_query="Evaluate Company A for an AI startup role.",
    )
    return runner_module.RunSession(FakeHarness(), ctx)


def _graph_state(session: runner_module.RunSession) -> dict[str, Any]:
    query = "Evaluate Company A for an AI startup role."
    brief = compile_structured_brief(query)
    plan = ExecutionPlan(
        summary="early fan-in test",
        planning_mode="supervisor_action",
        steps=[
            PlanStep(
                step_type="research",
                task_id="task_000_company_a",
                description="Collect Company A evidence",
                objective="Collect Company A evidence",
                allowed_tools=["internet_search", "fetch_url"],
                metadata={
                    "target_criteria": list(brief.success_criteria or brief.key_questions)[:1],
                    "max_llm_calls": 2,
                    "token_ceiling": 4_000,
                },
            )
        ],
    )
    session.state.plan = plan
    state = empty_research_state(
        run_id=session.run_id,
        session_id=session.session_id,
        task_query=query,
    )
    state.update(
        {
            "brief": brief.to_dict(),
            "plan": plan.to_dict(),
            "tasks": initialize_tasks(plan),
            "dispatch_wave_id": 1,
            "plan_version": 1,
            "task_id": "task_000_company_a",
            "step_index": 0,
            "step_type": "research",
        }
    )
    return state


async def test_worker_result_is_ingested_before_wave_barrier(monkeypatch):
    session = _session()
    state = _graph_state(session)
    result = WorkerResult(
        ok=True,
        task_id="task_000_company_a",
        status="done",
        summary="Company A has funding evidence",
        facts=["Company A has funding evidence"],
        sources=["https://example.com/company-a"],
    )

    class FakeExecutor:
        def __init__(self, harness: Any, run_session: Any):
            self.harness = harness
            self.session = run_session

        async def execute(self, task: ResearchTask, context: ResearchContext) -> WorkerResult:
            return result

    monkeypatch.setattr(worker_executor_module, "WorkerExecutorV2", FakeExecutor)
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)

    update = await runner_module.ResearchGraphRunner(FakeHarness()).node_research_worker(state)
    assert update["processed_worker_result_ids"]
    assert update["evidence_records"]
    assert update["findings"]

    merged = {**state, **update}
    assert ingest_new_worker_results(merged) == {}


async def test_optional_worker_skips_after_partial_wave_coverage():
    session = _session()
    plan = ExecutionPlan(
        summary="optional straggler test",
        planning_mode="supervisor_action",
        steps=[
            PlanStep(
                step_type="research",
                task_id="task_optional",
                description="Optional enrichment",
                objective="Optional enrichment",
                metadata={"optional": True},
            )
        ],
    )
    session.state.plan = plan
    session.wave_early_stop = True

    result = await WorkerExecutorV2(FakeHarness(), session).execute(
        ResearchTask(
            task_id="task_optional",
            objective="Optional enrichment",
            step_type="research",
            step_index=0,
            description="Optional enrichment",
        ),
        ResearchContext(run_id=session.run_id, query="Evaluate Company A"),
    )
    assert result.ok is True
    assert result.status == "skipped"
    assert result.summary == "skipped_optional_wave_coverage_complete"
    assert result.fail_reason == "optional_wave_coverage_complete"


def test_task_limits_are_bound_to_actual_wave_lease():
    manager = RunBudgetManager(token_limit=100_000, llm_call_limit=100, max_parallel_workers=4)
    lease_id, reason = manager.reserve_worker_lease(
        "task_limited",
        parallel_workers=2,
        max_llm_calls=2,
        token_ceiling=4_000,
    )
    assert lease_id and not reason
    lease = manager._worker_leases[lease_id]
    assert lease.max_llm_calls == 2
    assert lease.token_ceiling == 4_000
