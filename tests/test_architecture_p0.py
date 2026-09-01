"""P0 架构收敛：Task Router 三档、单一 ResearchState、WorkerRuntime。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.research.routing.mode_router import (
    budget_for_mode,
    canonicalize_mode,
    classify_auto,
    graph_branch_for_mode,
    route,
)
from app.research.runtime.direct import compose_direct_answer
from app.research.runtime.project import apply_graph_to_loop, findings_from_worker_row
from app.research.runtime.state import empty_research_state
from app.research.runtime.worker import (
    LangChainWorkerRuntime,
    PlaceholderWorkerRuntime,
    ResearchContext,
    ResearchTask,
    WorkerRuntime,
)
from app.agent.harness.state import LoopState


def test_canonicalize_aliases():
    assert canonicalize_mode("quick") == "search"
    assert canonicalize_mode("deep") == "research"
    assert canonicalize_mode("direct") == "answer"
    assert canonicalize_mode("ANSWER") == "answer"
    print("[OK] mode aliases")


def test_three_tier_auto():
    direct = classify_auto("std::apply是什么")
    assert direct.mode == "answer"
    search = classify_auto("glibc 2.42 release notes里改了什么")
    assert search.mode == "search"
    research = classify_auto("对比 LangGraph、Temporal 和 DeepSeek Harness 在 durable workflow 上的设计")
    assert research.mode == "research"
    print("[OK] auto answer/search/research")


def test_user_override_canonical():
    assert route("比较 A 和 B", user_mode="quick").mode == "search"
    assert route("今天休市吗", user_mode="deep").mode == "research"
    assert route("需要搜索", user_mode="answer").mode == "answer"
    print("[OK] user override canonical")


def test_budget_answer_has_no_tools():
    a = budget_for_mode("answer")
    s = budget_for_mode("quick")
    r = budget_for_mode("deep")
    assert a["max_tool_calls"] == 0
    assert s["max_replan_count"] == 0
    assert r["max_replan_count"] == 2
    print("[OK] budgets")


def test_graph_branch_for_mode():
    assert graph_branch_for_mode("answer") == "direct_answer"
    assert graph_branch_for_mode("quick") == "quick_search"
    assert graph_branch_for_mode("search") == "quick_search"
    assert graph_branch_for_mode("deep") == "intent"
    assert graph_branch_for_mode("research") == "intent"
    print("[OK] graph branches")


def test_research_state_has_brief_and_findings():
    state = empty_research_state(run_id="r", session_id="s", task_query="q")
    assert "brief" in state and state["brief"] == {}
    assert "findings" in state and state["findings"] == []
    print("[OK] ResearchState brief/findings")


def test_apply_graph_to_loop_is_one_way():
    loop = LoopState(session_id="s")
    loop.replan_count = 9
    apply_graph_to_loop(
        loop,
        {
            "replan_count": 1,
            "final_content": "hi",
            "abort_reason": "",
            "progress": "planned",
        },
    )
    assert loop.replan_count == 1
    assert loop.final_content == "hi"
    assert loop.metadata["workflow_authority"] == "research_state"
    print("[OK] graph→loop projection")


def test_placeholder_worker_runtime_protocol():
    runtime: WorkerRuntime = PlaceholderWorkerRuntime()
    assert isinstance(runtime, WorkerRuntime)
    assert LangChainWorkerRuntime.__name__ == "LangChainWorkerRuntime"
    print("[OK] WorkerRuntime protocol")


async def _run_placeholder():
    runtime = PlaceholderWorkerRuntime()
    result = await runtime.execute(
        ResearchTask(task_id="t1", objective="确认 ARM64 指令", step_type="research"),
        ResearchContext(run_id="r", query="q"),
    )
    assert result.ok and result.findings
    rows = findings_from_worker_row(
        {
            "task_id": "t1",
            "ok": True,
            "summary": "ok",
            "payload": {"summary": "ok", "facts": ["x"], "sources": ["https://a"]},
        }
    )
    assert rows and rows[0]["task_id"] == "t1"


def test_placeholder_execute_and_findings():
    import asyncio

    asyncio.run(_run_placeholder())
    print("[OK] placeholder execute + findings")


def test_compose_direct_answer():
    text = compose_direct_answer("std::apply是什么")
    assert "std::apply" in text
    assert "直答" in text
    print("[OK] direct answer fallback")


def test_persist_loop_state_off_by_default():
    from app.config.loader import reload_harness_config

    cfg = reload_harness_config()
    assert cfg.persist_loop_state is False
    assert cfg.graph_runtime_enabled is True
    print("[OK] persist_loop_state default false")


def test_compile_answer_and_search_and_research_graphs():
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        from app.research.runtime.graph import compile_research_graph, initial_graph_state, route_after_mode
    except ModuleNotFoundError:
        print("[SKIP] graph compile (langgraph not installed)")
        return

    assert route_after_mode({"search_mode": "answer"}) == "direct_answer"
    assert route_after_mode({"search_mode": "search"}) == "quick_search"
    assert route_after_mode({"search_mode": "research"}) == "intent"

    graph = compile_research_graph(checkpointer=InMemorySaver())
    answer = graph.invoke(
        initial_graph_state(
            run_id="r-ans",
            session_id="s-ans",
            task_query="std::apply是什么",
            search_mode="auto",
        ),
        config={"configurable": {"thread_id": "s-ans"}, "recursion_limit": 20},
    )
    assert answer["search_mode"] == "answer"
    assert answer.get("plan") in (None, {})
    assert "直答" in (answer.get("final_content") or "")

    search = graph.invoke(
        initial_graph_state(
            run_id="r-search",
            session_id="s-search",
            task_query="今天纳斯达克休市了吗",
            search_mode="search",
        ),
        config={"configurable": {"thread_id": "s-search"}, "recursion_limit": 20},
    )
    assert search["search_mode"] == "search"
    assert search.get("plan") in (None, {})

    research = graph.invoke(
        initial_graph_state(
            run_id="r-res",
            session_id="s-res",
            task_query="比较 Tesla 和 Figure 的差异并生成 Markdown 报告",
            search_mode="research",
        ),
        config={"configurable": {"thread_id": "s-res"}, "recursion_limit": 50},
    )
    assert research["search_mode"] == "research"
    assert research.get("plan")
    print("[OK] three-tier graphs")


if __name__ == "__main__":
    test_canonicalize_aliases()
    test_three_tier_auto()
    test_user_override_canonical()
    test_budget_answer_has_no_tools()
    test_graph_branch_for_mode()
    test_research_state_has_brief_and_findings()
    test_apply_graph_to_loop_is_one_way()
    test_placeholder_worker_runtime_protocol()
    test_placeholder_execute_and_findings()
    test_compose_direct_answer()
    test_persist_loop_state_off_by_default()
    test_compile_answer_and_search_and_research_graphs()
    print("\n=== architecture P0 tests passed ===")
