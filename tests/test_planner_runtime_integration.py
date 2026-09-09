from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.research.runtime import runner as runner_module
from app.research.runtime.runner import ResearchGraphRunner
from app.research.runtime.state import empty_research_state
from app.research.spec.compiler import compile_research_spec


class FakeState:
    def __init__(self):
        self.intent = None
        self.plan = None
        self.replan_count = 0
        self.metadata = {}


class FakeSession:
    def __init__(self, harness, query):
        self.harness = harness
        self.ctx = SimpleNamespace(task_query=query)
        self.run_id = "run-planner"
        self.session_id = "session-planner"
        self.state = FakeState()


def _gstate(query: str) -> dict:
    spec = compile_research_spec(query)
    state = empty_research_state(
        run_id="run-planner",
        session_id="session-planner",
        task_query=query,
    )
    state.update(
        {
            "research_spec": spec.to_dict(),
            "task_query": query,
            "phase": "spec_gate",
        }
    )
    return state


async def _plan(query: str) -> tuple[dict, object]:
    harness = SimpleNamespace(
        harness_config=SimpleNamespace(
            planner_llm_enabled=True,
            planner_dynamic_lead_enabled=True,
            planner_max_research_tasks=6,
        )
    )
    session = FakeSession(harness, query)
    graph_runner = ResearchGraphRunner(harness)
    original_get_session = runner_module.get_session
    runner_module.get_session = lambda _run_id: session
    try:
        return await graph_runner.node_plan(_gstate(query)), session
    finally:
        runner_module.get_session = original_get_session


@pytest.mark.asyncio
async def test_dynamic_discovery_query_creates_discovery_task(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("spec-driven planner must not invoke the legacy lead planner")

    monkeypatch.setattr(
        "app.research.planning.lead_planner.lead_plan_with_llm",
        fail_if_called,
    )
    query = "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？"
    update, session = await _plan(query)
    task_ids = [step["task_id"] for step in update["plan"]["steps"]]
    assert task_ids == ["t_discovery"]
    assert session.state.metadata["planner_source"] == "spec_driven"


@pytest.mark.asyncio
async def test_landscape_query_does_not_collapse_to_generic_search():
    query = "当下国内 AI 初创公司有哪些值得加入？为什么？"
    update, _session = await _plan(query)
    steps = update["plan"]["steps"]
    assert steps
    assert all(step["step_type"] == "research" for step in steps)
    assert all(step["task_id"] != "t0:network_search" for step in steps)
