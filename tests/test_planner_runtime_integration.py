from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.harness.planner import understand_task
from app.research.runtime import runner as runner_module
from app.research.runtime.runner import ResearchGraphRunner
from app.research.runtime.state import empty_research_state


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
    intent = understand_task(query)
    state = empty_research_state(
        run_id="run-planner",
        session_id="session-planner",
        task_query=query,
    )
    state["intent"] = intent.to_dict()
    state["task_query"] = query
    state["phase"] = "understand"
    return state


async def _plan(query: str, *, llm_enabled: bool) -> tuple[dict, object]:
    harness = SimpleNamespace(
        harness_config=SimpleNamespace(
            planner_llm_enabled=llm_enabled,
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
async def test_breadth_heavy_query_uses_lead_planner(monkeypatch):
    query = "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？"
    calls: list[str] = []

    async def fake_lead_plan(intent, policy, **kwargs):
        calls.append(intent.raw_query)
        from app.research.planning.lead_planner import heuristic_dynamic_plan

        return heuristic_dynamic_plan(intent, policy)

    monkeypatch.setattr(
        "app.research.planning.lead_planner.lead_plan_with_llm",
        fake_lead_plan,
    )
    update, session = await _plan(query, llm_enabled=True)
    assert calls == [query]
    assert session.state.metadata["planner_source"] == "lead_llm"
    task_ids = [step["task_id"] for step in update["plan"]["steps"]]
    assert "t_landscape" in task_ids
    assert task_ids != ["t0:network_search"]


@pytest.mark.asyncio
async def test_landscape_query_does_not_collapse_to_generic_search():
    query = "当下国内 AI 初创公司有哪些值得加入？为什么？"
    update, _session = await _plan(query, llm_enabled=False)
    steps = update["plan"]["steps"]
    task_ids = [step["task_id"] for step in steps]
    assert "t_landscape" in task_ids
    assert task_ids != ["t0:network_search"]
