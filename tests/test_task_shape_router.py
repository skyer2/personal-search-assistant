"""TaskShape Router：按任务形态选择执行拓扑，且不覆盖已有预算字段。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.research.routing.task_shape import (
    TaskShape,
    classify_task_shape,
    execution_profile_for_shape,
)
from app.research.runtime.graph import intent_node
from app.research.runtime.state import empty_research_state


def test_simple_fact_routes_to_single_worker():
    decision = classify_task_shape("苹果的CEO是谁")
    assert decision.shape == TaskShape.SIMPLE_FACT
    profile = execution_profile_for_shape(decision.shape)
    assert profile["parallel_workers"] == 1
    assert profile["max_replan_count"] == 0
    print("[OK] simple fact routes to single worker")


def test_breadth_heavy_routes_to_parallel():
    decision = classify_task_shape(
        "对比 OpenAI、Anthropic 和 Google 的 deep research 架构",
        brief={"entities": ["OpenAI", "Anthropic", "Google"]},
    )
    assert decision.shape == TaskShape.BREADTH_HEAVY
    profile = execution_profile_for_shape(decision.shape)
    assert profile["parallel_workers"] >= 2
    print("[OK] breadth-heavy routes to parallel workers")


def test_conflict_routes_to_hybrid():
    decision = classify_task_shape("Claude Code 的 SWE-bench 分数存在哪些争议和矛盾说法")
    assert decision.shape == TaskShape.HYBRID_CONFLICT
    profile = execution_profile_for_shape(decision.shape)
    assert profile["max_replan_count"] >= 2
    print("[OK] conflict-heavy routes to hybrid with replan")


def test_intent_node_merges_budget_not_replaces():
    state = empty_research_state(
        run_id="r1",
        session_id="s1",
        task_query="苹果的CEO是谁",
        max_tool_calls=40,
        max_replan_count=3,
    )
    result = intent_node(state)
    budget = result["budget"]
    assert budget["max_tool_calls"] == 40
    assert budget["max_replan_count"] == 0
    assert budget["max_parallel_workers"] == 1
    assert any(s.startswith("task_shape:") for s in result["route_signals"])
    print("[OK] intent node merges budget with task shape")


def test_zero_replan_budget_is_preserved():
    state = empty_research_state(
        run_id="r2",
        session_id="s2",
        task_query="对比 Tesla 和 Figure 的技术路线",
        max_tool_calls=40,
        max_replan_count=0,
    )
    result = intent_node(state)
    # breadth_heavy 形态 replan=1，但已有预算 0 表示禁止 replan，不得被 or 语义回退抬高
    assert result["budget"]["max_replan_count"] == 0
    print("[OK] zero replan budget is preserved")


if __name__ == "__main__":
    test_simple_fact_routes_to_single_worker()
    test_breadth_heavy_routes_to_parallel()
    test_conflict_routes_to_hybrid()
    test_intent_node_merges_budget_not_replaces()
    test_zero_replan_budget_is_preserved()
    print("\n=== task shape router tests passed ===")
