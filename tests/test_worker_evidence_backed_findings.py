"""Worker final-answer and evidence-backed finding contract regressions."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.harness.orchestration import (
    build_strict_json_retry_instruction,
    extract_final_ai_content,
    parse_worker_payload,
    validate_structured_worker_payload,
)
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


def test_tool_message_cannot_become_final_worker_answer():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "batch_search", "args": {"queries": ["a"]}, "id": "call-1"}],
        ),
        ToolMessage(content=json.dumps({"results": []}), tool_call_id="call-1"),
    ]
    assert extract_final_ai_content(messages) == ""


def test_finding_first_validation_rejects_summary_and_facts_only():
    step = PlanStep(step_type="research", description="research", objective="research")
    summary_only = parse_worker_payload(
        json.dumps({"ok": True, "summary": "done", "facts": ["fact"], "sources": ["https://a"]}),
        step_type="research",
    )
    valid, reason = validate_structured_worker_payload(summary_only, step, require_json=True)
    assert not valid
    assert reason == "invalid_structured_worker_result"

    finding_payload = parse_worker_payload(
        json.dumps(
            {
                "ok": True,
                "summary": "done",
                "findings": [{"claim": "fact", "artifact_ids": ["art-web-1"]}],
            }
        ),
        step_type="research",
    )
    assert validate_structured_worker_payload(finding_payload, step, require_json=True) == (True, "")


def test_finalize_retry_instruction_forbids_all_retrieval_tools():
    step = PlanStep(step_type="research", description="research", objective="research")
    instruction = build_strict_json_retry_instruction(step)
    for tool_name in ("internet_search", "fetch_url", "batch_search", "batch_fetch"):
        assert tool_name in instruction
    assert "禁止自造" in instruction
    assert "findings" in instruction


@pytest.mark.asyncio
async def test_worker_finalization_retry_recovers_structured_findings():
    from app.research.workers.registry import FINALIZE_ONLY_WORKER_KEY, RETRIEVAL_TOOLS, finalize_only_tool_names

    assert not (set(finalize_only_tool_names()) & RETRIEVAL_TOOLS)

    class FakeContextBuilder:
        def build_step_message(self, *args: Any, **kwargs: Any) -> str:
            return "research task"

    class FakeHarness:
        harness_config = SimpleNamespace(
            enforce_subagent_binding=False,
            synthesis_use_evidence_digest=False,
            max_step_tool_calls=10,
            max_tool_calls=20,
        )
        context_builder = FakeContextBuilder()

        def __init__(self, finalize_agent: Any):
            self.workers = {FINALIZE_ONLY_WORKER_KEY: finalize_agent}

    class FakeSession:
        state = SimpleNamespace(tool_calls_count=0, assistants_called=[])
        ctx = SimpleNamespace(relative_session_dir="output", uploaded_prompt="")
        budget_manager = RunBudgetManager(token_limit=100_000, llm_call_limit=100)

    class PrimaryAgent:
        def __init__(self):
            self.calls = 0

        async def astream(self, payload: dict[str, Any], config: dict[str, Any] | None = None):
            self.calls += 1
            last_message = list(payload.get("messages") or [])[-1]
            prompt = str(last_message.get("content") or "") if isinstance(last_message, dict) else ""
            yield {
                "worker": {
                    "messages": [AIMessage(content=json.dumps({"ok": True, "summary": "summary only"}))]
                }
            }

    class FinalizeAgent:
        def __init__(self):
            self.calls = 0

        async def astream(self, payload: dict[str, Any], config: dict[str, Any] | None = None):
            self.calls += 1
            last_message = list(payload.get("messages") or [])[-1]
            prompt = str(last_message.get("content") or "") if isinstance(last_message, dict) else ""
            assert "Finalization-only" in prompt
            yield {
                "worker": {
                    "messages": [
                        AIMessage(
                            content=json.dumps(
                                {
                                    "ok": True,
                                    "summary": "finalized",
                                    "findings": [
                                        {
                                            "claim": "Company A is backed by evidence.",
                                            "artifact_ids": ["art-web-1"],
                                            "confidence": 0.9,
                                        }
                                    ],
                                    "stop_reason": "local_evidence_sufficient",
                                }
                            )
                        )
                    ]
                }
            }

    step = PlanStep(step_type="research", description="research", objective="research")
    task = ResearchTask(task_id="task-retry", objective="research", step_type="research", step_index=0)
    context = ResearchContext(run_id="run-retry", query="research", session_id="session-retry")
    agent = PrimaryAgent()
    finalize_agent = FinalizeAgent()
    result = await WorkerExecutorV2(FakeHarness(finalize_agent), FakeSession())._invoke_leaf(
        task=task,
        context=context,
        step=step,
        step_index=0,
        execute_agent=agent,
        dispatch_mode="direct",
        tool_usage={},
        timeout_sec=10,
    )
    assert agent.calls == 1
    assert finalize_agent.calls == 1
    assert not any(name in RETRIEVAL_TOOLS for name in result.metadata["tools_invoked"])
    assert result.metadata["final_ai_found"] is True
    assert result.metadata["structured_output_valid"] is True
    assert result.metadata["finalization_retry_count"] == 1
    assert result.metadata["raw_finding_count"] == 1


def test_ingestion_resolves_artifact_refs_and_reports_rejections():
    state = {
        "dispatch_wave_id": 0,
        "plan": {"steps": [{"task_id": "task-a", "metadata": {"target_criteria": ["核心事实"]}}]},
        "worker_results": [
            {
                "task_id": "task-a",
                "dispatch_wave_id": 0,
                "summary": "two findings",
                "payload": {
                    "summary": "two findings",
                    "facts": ["Fact A", "Fact B"],
                    "sources": ["https://example.com/a"],
                    "evidence_ids": ["evidence-a"],
                    "artifact_ids": ["art-a"],
                    "findings": [
                        {"claim": "Fact A", "artifact_ids": ["art-a"]},
                        {"claim": "Fact B", "evidence_ids": ["E1"]},
                    ],
                },
            }
        ],
    }
    update = ingest_new_worker_results(state)
    assert update["findings"][0]["evidence_ids"] == ["evidence-a"]
    assert update["findings"][0]["partial"] is False
    assert update["research_value_signal"]["accepted_finding_count"] == 1
    assert update["research_value_signal"]["rejected_finding_count"] == 1
    assert update["research_value_signal"]["unresolved_evidence_ref_count"] == 1
    assert update["finding_diagnostics"][0]["reason"] == "unresolved_evidence_refs"


def test_fallback_requires_facts_and_admitted_evidence():
    state = {
        "dispatch_wave_id": 0,
        "worker_results": [
            {
                "task_id": "task-fallback",
                "dispatch_wave_id": 0,
                "summary": "facts with evidence",
                "payload": {
                    "summary": "facts with evidence",
                    "facts": ["Fact A"],
                    "sources": ["https://example.com/a"],
                    "findings": [{"claim": "Fact A", "evidence_ids": ["E1"]}],
                },
            }
        ],
    }
    update = ingest_new_worker_results(state)
    assert len(update["findings"]) == 1
    assert update["findings"][0]["partial"] is True
    assert update["findings"][0]["evidence_ids"]
    assert update["research_value_signal"]["partial_fallback_finding_count"] == 1

    no_evidence_state = {
        "dispatch_wave_id": 0,
        "worker_results": [
            {
                "task_id": "task-no-evidence",
                "dispatch_wave_id": 0,
                "summary": "facts without evidence",
                "payload": {
                    "summary": "facts without evidence",
                    "facts": ["Fact A"],
                    "findings": [{"claim": "Fact A", "evidence_ids": ["E1"]}],
                },
            }
        ],
    }
    no_evidence_update = ingest_new_worker_results(no_evidence_state)
    assert no_evidence_update["findings"] == []
    assert no_evidence_update["research_value_signal"]["partial_fallback_finding_count"] == 0


class RunnerConfig:
    max_parallel_workers = 1
    max_llm_calls_per_worker = 8
    step_timeout_sec = 30
    max_retries = 0
    hitl_enabled = False
    hitl_step_gate_types = ()


class RunnerHarness:
    harness_config = RunnerConfig()


def _runner_session() -> runner_module.RunSession:
    loop = LoopState(session_id="finding-lifecycle", run_id="finding-lifecycle")
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
        task_query="Evaluate Company A.",
    )
    return runner_module.RunSession(RunnerHarness(), ctx)


def _runner_state(session: runner_module.RunSession) -> dict[str, Any]:
    query = "Evaluate Company A."
    brief = compile_structured_brief(query)
    plan = ExecutionPlan(
        summary="finding lifecycle",
        planning_mode="supervisor_action",
        steps=[
            PlanStep(
                step_type="research",
                task_id="task-a",
                description="Collect evidence",
                objective="Collect evidence",
                metadata={"target_criteria": list(brief.key_questions or (brief.objective,))[:1]},
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
            "task_id": "task-a",
            "step_index": 0,
        }
    )
    return state


@pytest.mark.asyncio
async def test_task_cannot_complete_without_accepted_finding(monkeypatch):
    session = _runner_session()
    state = _runner_state(session)
    result = WorkerResult(
        ok=True,
        task_id="task-a",
        status="done",
        summary="evidence without model finding",
        facts=["Company A has evidence."],
        sources=["https://example.com/a"],
        evidence_refs=["evidence-a"],
        metrics={"raw_finding_count": 0},
    )

    class FakeExecutor:
        def __init__(self, harness: Any, run_session: Any):
            self.harness = harness
            self.session = run_session

        async def execute(self, task: ResearchTask, context: ResearchContext) -> WorkerResult:
            return result

    monkeypatch.setattr(worker_executor_module, "WorkerExecutorV2", FakeExecutor)
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)
    update = await runner_module.ResearchGraphRunner(RunnerHarness()).node_research_worker(state)
    task = update["tasks"]["task-a"]
    assert task["execution_status"] == "stopped"
    assert task["result_status"] == "partial"
    assert task["failure"]["code"] == "no_accepted_findings"
    assert update["findings"][0]["partial"] is True
    assert update["worker_results"][0]["finding_acceptance"]["accepted_finding_count"] == 0


if __name__ == "__main__":
    asyncio.run(test_worker_finalization_retry_recovers_structured_findings())
