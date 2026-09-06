"""Architecture and behavior gates for control-plane convergence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.harness.run_budget import BudgetReservationError
from app.agent.harness.state import ExecutionPlan, PlanStep, StepResult
from app.research.control.policy import decide_after_quality, decide_progress
from app.research.control.transitions import (
    InvalidTransition,
    assert_transition,
    transition_allowed,
)
from app.research.domain.contracts import (
    OutcomeStatus,
    TaskStatus,
    TerminationReason,
    WorkflowPhase,
    initialize_tasks,
    merge_task_state,
    task_status_projection,
)
from app.research.execution import worker_executor as worker_executor_module
from app.research.execution.worker_executor import WorkerExecutorV2
from app.research.runtime.worker import ResearchContext, ResearchTask


ROOT = Path(__file__).resolve().parents[1]


def test_task_runtime_state_is_workflow_truth() -> None:
    plan = ExecutionPlan(
        steps=[PlanStep(step_type="research", task_id="t1", description="research")],
        summary="plan",
    )
    tasks = initialize_tasks(plan)
    assert task_status_projection(tasks) == {"t1": TaskStatus.PENDING.value}
    tasks = merge_task_state(tasks, "t1", TaskStatus.DONE)
    assert task_status_projection(tasks) == {"t1": TaskStatus.DONE.value}


def test_transition_table_rejects_invalid_and_terminal_transitions() -> None:
    assert transition_allowed(WorkflowPhase.PROGRESS, WorkflowPhase.DISPATCH)
    assert not transition_allowed(WorkflowPhase.PROGRESS, WorkflowPhase.EXECUTE)
    assert not transition_allowed(WorkflowPhase.TERMINATED, WorkflowPhase.DISPATCH)
    with pytest.raises(InvalidTransition):
        assert_transition(WorkflowPhase.QUALITY, WorkflowPhase.DISPATCH)


def test_typed_outcome_and_termination_reasons_stay_canonical() -> None:
    assert OutcomeStatus.PARTIAL.value == "partial"
    assert TerminationReason.CONTROL_NO_PROGRESS.value == "control_plane_no_progress"


def test_quality_policy_never_replans_after_exhaustion() -> None:
    state = {
        "quality_passed": False,
        "quality_repair_action": "replan",
        "replan_attempts": 3,
        "budget": {"max_replan_count": 3},
    }
    assert decide_after_quality(state).value == "finalize"


def test_progress_policy_never_replans_after_no_progress() -> None:
    state = {
        "progress_assessment": {"verdict": "gap"},
        "control_no_progress": True,
        "budget": {"max_replan_count": 3},
    }
    assert decide_progress(state).value == "quality_gate"


def test_worker_executor_v2_uses_gateways_and_never_legacy_loop() -> None:
    source = (ROOT / "app/research/execution/worker_executor.py").read_text(
        encoding="utf-8"
    )
    assert "LLMGateway" in source
    assert "ToolGateway" in source
    assert "_run_single_step" not in source
    assert "snapshot_worker_loop_state" not in source
    assert "LoopState" not in source


def test_main_runtime_no_longer_projects_graph_to_loop() -> None:
    source = (ROOT / "app/research/runtime/runner.py").read_text(encoding="utf-8")
    assert "apply_graph_to_loop" not in source
    scheduler = (ROOT / "app/research/runtime/scheduler.py").read_text(encoding="utf-8")
    assert 'metadata["status"]' not in scheduler
    assert "metadata['status']" not in scheduler


@dataclass
class FakeConfig:
    step_timeout_sec: int = 10
    synthesis_step_timeout_sec: int = 777
    direct_worker_invoke: bool = True
    enforce_subagent_binding: bool = False
    synthesis_use_evidence_digest: bool = False


class FakeAgent:
    async def astream(self, payload, config=None):
        content = json.dumps(
            {
                "ok": True,
                "summary": "evidence found",
                "facts": ["fact"],
                "sources": ["https://example.com"],
                "evidence_ids": ["e1"],
            },
            ensure_ascii=False,
        )
        yield {"agent": {"messages": [SimpleNamespace(content=content, tool_calls=[])]}}


class SalvageAgent:
    async def astream(self, payload, config=None):
        yield {
            "agent": {
                "messages": [
                    SimpleNamespace(
                        content="",
                        tool_calls=[{"id": "call-1", "name": "internet_search"}],
                    )
                ]
            }
        }


class BudgetInterruptedAgent:
    async def astream(self, payload, config=None):
        yield {
            "agent": {
                "messages": [
                    SimpleNamespace(
                        content="",
                        tool_calls=[{"id": "call-1", "name": "internet_search"}],
                    )
                ]
            }
        }
        raise BudgetReservationError("budget_tokens")


class FakeBuilder:
    def build_step_message(self, *args, **kwargs):
        return "worker prompt"


class FakeHarness:
    harness_config = FakeConfig()
    context_builder = FakeBuilder()
    workers = {"research": FakeAgent()}

    def _enrich_worker_result(self, step, result, state):
        return result


class SalvageHarness(FakeHarness):
    workers = {"research": SalvageAgent()}

    def _enrich_worker_result(self, step, result, state):
        result.metadata["worker_payload"] = {
            "ok": True,
            "summary": "salvaged evidence",
            "facts": ["salvaged fact"],
            "sources": ["https://example.com/source"],
            "findings": [],
            "artifact_ids": ["art-web-1"],
        }
        result.metadata["tool_calls"] = len(result.metadata.get("tools_invoked") or [])
        return result


class BudgetInterruptedHarness(FakeHarness):
    workers = {"research": BudgetInterruptedAgent()}


class FakeBudget:
    def __init__(self):
        self.released = []

    def reserve_worker_lease(self, task_id, **kwargs):
        return f"lease:{task_id}", ""

    def release_worker_lease(self, lease_id):
        self.released.append(lease_id)


def test_worker_executor_v2_returns_typed_result_and_releases_lease() -> None:
    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t1",
                description="research",
                subagent="research",
            )
        ],
        summary="plan",
    )
    state = SimpleNamespace(plan=plan, tool_calls_count=0, assistants_called=[])
    ctx = SimpleNamespace(
        task_query="query",
        user_id="me",
        tenant_id="local",
        project_id="Inbox",
        relative_session_dir="sessions/s",
        uploaded_prompt="",
        citation_manager=None,
    )
    budget = FakeBudget()
    session = SimpleNamespace(
        state=state, ctx=ctx, session_id="s", run_id="r", budget_manager=budget
    )
    executor = WorkerExecutorV2(FakeHarness(), session)
    assert executor._timeout_for(SimpleNamespace(step_type="generate_markdown")) == 777
    assert executor._timeout_for(SimpleNamespace(step_type="research")) == 10
    result = asyncio.run(
        executor.execute(
            ResearchTask(task_id="t1", objective="research", step_type="research"),
            ResearchContext(run_id="r", query="query", session_id="s"),
        )
    )
    assert result.ok is True
    assert result.status == "done"
    assert result.facts == ["fact"]
    assert result.evidence_refs == ["e1"]
    assert isinstance(result.raw, StepResult)
    assert budget.released == ["lease:t1"]
    assert state.assistants_called == ["research"]


def test_worker_executor_v2_consumes_enriched_payload_and_syncs_tool_usage() -> None:
    plan = ExecutionPlan(
        steps=[PlanStep(step_type="research", task_id="t1", description="research")],
        summary="plan",
    )
    state = SimpleNamespace(plan=plan, tool_calls_count=0)
    ctx = SimpleNamespace(
        task_query="query",
        user_id="me",
        tenant_id="local",
        project_id="Inbox",
        relative_session_dir="sessions/s",
        uploaded_prompt="",
        citation_manager=None,
    )
    session = SimpleNamespace(
        state=state,
        ctx=ctx,
        session_id="s",
        run_id="r",
        budget_manager=FakeBudget(),
    )
    executor = WorkerExecutorV2(SalvageHarness(), session)
    result = asyncio.run(
        executor.execute(
            ResearchTask(task_id="t1", objective="research", step_type="research"),
            ResearchContext(run_id="r", query="query", session_id="s"),
        )
    )
    assert result.ok is True
    assert result.status == "done"
    assert result.summary == "salvaged evidence"
    assert result.facts == ["salvaged fact"]
    assert result.sources == ["https://example.com/source"]
    assert result.evidence_refs == ["art-web-1"]
    assert state.tool_calls_count == 1


def test_worker_executor_v2_recovers_evidence_and_tool_usage_after_budget_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_executor_module,
        "salvage_worker_evidence",
        lambda **kwargs: {
            "findings": [{"finding_id": "salvage_1", "summary": "recovered"}],
            "evidence_refs": ["art-web-1"],
            "sources": ["https://example.com/recovered"],
            "facts": ["recovered fact"],
        },
    )
    plan = ExecutionPlan(
        steps=[PlanStep(step_type="research", task_id="t1", description="research")],
        summary="plan",
    )
    state = SimpleNamespace(plan=plan, tool_calls_count=0)
    ctx = SimpleNamespace(
        task_query="query",
        user_id="me",
        tenant_id="local",
        project_id="Inbox",
        relative_session_dir="sessions/s",
        uploaded_prompt="",
        citation_manager=None,
    )
    session = SimpleNamespace(
        state=state,
        ctx=ctx,
        session_id="s",
        run_id="r",
        budget_manager=FakeBudget(),
    )
    result = asyncio.run(
        WorkerExecutorV2(BudgetInterruptedHarness(), session).execute(
            ResearchTask(task_id="t1", objective="research", step_type="research"),
            ResearchContext(run_id="r", query="query", session_id="s"),
        )
    )
    assert result.ok is True
    assert result.status == "done"
    assert result.evidence_refs == ["art-web-1"]
    assert result.sources == ["https://example.com/recovered"]
    assert state.tool_calls_count == 1
