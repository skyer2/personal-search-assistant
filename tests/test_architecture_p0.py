"""Harness 范围框死：agent 主路径、direct 仅 baseline、WorkerRuntime、search 只是 tool。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.state import LoopState
from app.research.routing.mode_router import budget_for_mode, canonicalize_mode, graph_branch_for_mode, route
from app.research.runtime.project import sync_execution_projection
from app.research.runtime.state import empty_research_state


def test_canonicalize_experiment_modes():
    assert canonicalize_mode("direct") == "direct"
    assert canonicalize_mode("vanilla") == "direct"
    assert canonicalize_mode("baseline") == "direct"
    assert canonicalize_mode("agent") == "agent"
    assert canonicalize_mode("auto") == "agent"
    assert canonicalize_mode("search") == "agent"
    assert canonicalize_mode("answer") == "agent"
    assert canonicalize_mode("research") == "agent"
    print("[OK] experiment mode aliases")


def test_no_product_search_path():
    fact = route("std::apply是什么")
    assert fact.mode == "agent"
    searchish = route("glibc 2.42 release notes里改了什么")
    assert searchish.mode == "agent"
    compare = route("对比 LangGraph 和 Temporal")
    assert compare.mode == "agent"
    baseline = route("任意问题", user_mode="direct")
    assert baseline.mode == "direct" and baseline.user_override
    print("[OK] product ANSWER/SEARCH removed; direct is baseline only")


def test_budget_direct_has_no_replan():
    d = budget_for_mode("direct")
    a = budget_for_mode("agent")
    assert d["max_replan_count"] == 0
    assert d["progress_eval"] is False
    assert a["max_replan_count"] == 2
    assert a["progress_eval"] is True
    print("[OK] experiment budgets")


def test_graph_branch_for_mode():
    assert graph_branch_for_mode("direct") == "vanilla"
    assert graph_branch_for_mode("agent") == "intent"
    assert graph_branch_for_mode("search") == "intent"
    print("[OK] graph branches")


def test_research_state_has_brief_and_findings():
    state = empty_research_state(run_id="r", session_id="s", task_query="q")
    assert state["brief"] == {}
    assert state["findings"] == []
    assert state["search_mode"] == "agent"
    print("[OK] ResearchState brief/findings")


def test_sync_execution_projection_is_one_way():
    loop = LoopState(session_id="s")
    loop.replan_count = 9
    sync_execution_projection(
        loop,
        {"replan_budget": {"attempted": 1, "applied": 1, "max_attempts": 2}, "final_content": "hi"},
    )
    assert loop.replan_count == 1
    assert loop.final_content == "hi"
    assert loop.metadata["workflow_authority"] == "research_state"
    print("[OK] graph→loop projection")


def test_memory_off_by_default():
    from app.config.loader import reload_harness_config

    cfg = reload_harness_config()
    assert cfg.memory_enabled is False
    print("[OK] memory off by default")


def test_compile_agent_and_direct_graphs():
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        from app.research.runtime.graph import compile_research_graph, initial_graph_state
    except ModuleNotFoundError:
        print("[SKIP] graph compile (langgraph not installed)")
        return

    agent = compile_research_graph(checkpointer=InMemorySaver(), profile="agent")
    result = agent.invoke(
        initial_graph_state(
            run_id="r-agent",
            session_id="s-agent",
            task_query="对比 LangGraph 和 Temporal 的 durable workflow",
            search_mode="agent",
        ),
        config={"configurable": {"thread_id": "s-agent"}, "recursion_limit": 50},
    )
    assert result["search_mode"] == "agent"
    assert result.get("plan")
    assert result.get("brief") is not None

    direct = compile_research_graph(checkpointer=InMemorySaver(), profile="direct")
    baseline = direct.invoke(
        initial_graph_state(
            run_id="r-direct",
            session_id="s-direct",
            task_query="std::apply是什么",
            search_mode="direct",
        ),
        config={"configurable": {"thread_id": "s-direct"}, "recursion_limit": 20},
    )
    assert baseline["search_mode"] == "direct"
    assert baseline.get("plan") in (None, {})
    assert "direct baseline" in (baseline.get("final_content") or "")
    print("[OK] agent harness graph + direct baseline")


if __name__ == "__main__":
    test_canonicalize_experiment_modes()
    test_no_product_search_path()
    test_budget_direct_has_no_replan()
    test_graph_branch_for_mode()
    test_research_state_has_brief_and_findings()
    test_sync_execution_projection_is_one_way()
    test_memory_off_by_default()
    test_compile_agent_and_direct_graphs()
    print("\n=== harness scope tests passed ===")
