from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import app.observability as observability
from app.agent.harness.state import StepResult
from app.research.execution.worker_executor import WorkerExecutorV2
from app.research.runtime.worker import ResearchContext, ResearchTask


class FakeRecorder:
    is_active = True

    def __init__(self):
        self.events: list[tuple[str, dict[str, Any]]] = []

    def start_span(self, *args: Any, **kwargs: Any) -> str:
        raise TypeError("plan_version is not supported")

    def emit(self, event_type: str, **kwargs: Any):
        self.events.append((event_type, kwargs))

    def end_span(self, *args: Any, **kwargs: Any):
        return None


class FakeBudgetManager:
    def reserve_worker_lease(self, *args: Any, **kwargs: Any):
        return "lease", ""

    def release_worker_lease(self, *args: Any, **kwargs: Any):
        return None


class FakeHarness:
    harness_config = SimpleNamespace(step_timeout_sec=10)

    def _enrich_worker_result(self, _step: Any, result: Any, _state: Any):
        return result


def test_worker_started_survives_start_span_failure(monkeypatch):
    recorder = FakeRecorder()
    monkeypatch.setattr(observability, "get_recorder", lambda: recorder)
    step = SimpleNamespace(
        step_type="network_search",
        description="search",
        objective="search",
        subagent="",
        allowed_tools=[],
        metadata={"simple_fact_fast_path": True},
    )
    state = SimpleNamespace(plan=SimpleNamespace(steps=[step]), tool_calls_count=0)
    session = SimpleNamespace(
        state=state,
        budget_manager=FakeBudgetManager(),
        ctx=SimpleNamespace(),
        _resolve_max_workers=lambda: 1,
    )
    harness = FakeHarness()

    async def invoke(*args: Any, **kwargs: Any) -> StepResult:
        return StepResult(
            step_type="network_search",
            content=json.dumps(
                {
                    "ok": True,
                    "summary": "found",
                    "findings": [],
                    "evidence_ids": ["ev-1"],
                }
            ),
            metadata={},
        )

    monkeypatch.setattr(WorkerExecutorV2, "_invoke_simple_fact", invoke)
    task = ResearchTask(task_id="t1", objective="q", step_type="network_search", step_index=0)
    context = ResearchContext(run_id="r", query="q", session_id="s")
    result = asyncio.run(WorkerExecutorV2(harness, session).execute(task, context))

    event_types = [item[0] for item in recorder.events]
    assert result.ok, result.summary
    assert "observability.internal_error" in event_types
    assert "worker.started" in event_types
    assert "worker.completed" in event_types
    internal = next(item for item in recorder.events if item[0] == "observability.internal_error")
    assert internal[1]["attributes"]["operation"] == "start_span"
