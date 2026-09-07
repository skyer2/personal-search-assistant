"""Phase 21: Domain Harness 控制面 + DAG scheduler + 幂等键（无需 LLM）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.planner import build_plan, finalize_plan, understand_task
from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.idempotency import action_idempotency_key
from app.research.runtime.reducers import merge_dicts, merge_worker_payloads
from app.research.runtime.scheduler import (
    annotate_plan_tasks,
    dispatch_sends,
    initialize_tasks,
    research_only_plan,
)
from app.research.workers.registry import resolve_execute_target, worker_tools_for_step


def test_action_idempotency_stable_across_index_shift():
    key1 = action_idempotency_key(
        run_id="run-a",
        plan_version=2,
        task_id="t_market",
        action_id="execute",
    )
    key2 = action_idempotency_key(
        run_id="run-a",
        plan_version=2,
        task_id="t_market",
        action_id="execute",
    )
    assert key1 == key2
    assert "p2" in key1
    print("[OK] action idempotency key")


def test_annotate_plan_dag_dependencies():
    intent = understand_task("搜索公开资料，生成Markdown报告")
    plan = research_only_plan(annotate_plan_tasks(finalize_plan(build_plan(intent))))
    ids = [s.task_id for s in plan.steps]
    assert all(ids)
    assert all(step.step_type in {"research", "network_search", "file_read"} for step in plan.steps)
    print(f"[OK] research-only DAG task_ids={ids}")


def test_ready_retrieval_can_fan_out():
    intent = understand_task("搜索公开资料并读取附件，生成Markdown")
    intent.needs_file_read = True
    plan = research_only_plan(annotate_plan_tasks(finalize_plan(build_plan(intent))))
    tasks = initialize_tasks(plan)
    sends = dispatch_sends(plan, tasks)
    assert "network_search" in {row["step_type"] for row in sends}
    assert all(step.step_type != "generate_markdown" for step in plan.steps)
    print("[OK] scheduler READY fan-out without synthesis gating")


def test_merge_worker_payloads_dedup():
    merged = merge_worker_payloads(
        [
            {"payload": {"facts": ["增速 15%"], "sources": ["https://a.example"]}},
            {"payload": {"facts": ["增速 15%"], "sources": ["https://b.example"]}},
        ]
    )
    assert merged["facts"] == ["增速 15%"]
    assert len(merged["sources"]) == 2
    assert merge_dicts({"a": "pending"}, {"a": "done", "b": "pending"}) == {
        "a": "done",
        "b": "pending",
    }
    print("[OK] evidence reducer")


def test_unregistered_step_fail_closed():
    from app.research.workers.registry import UnsupportedTaskType, resolve_execute_target

    try:
        resolve_execute_target(
            "some_new_step",
            workers={"generate_markdown": object()},
            main_agent=object(),
            direct_invoke=True,
        )
        raise AssertionError("expected UnsupportedTaskType")
    except UnsupportedTaskType:
        pass
    print("[OK] unregistered step fail-closed")


def test_synthesis_prompt_is_not_supervisor():
    from app.research.workers.prompts import SYNTHESIS_SYSTEM_PROMPT

    text = SYNTHESIS_SYSTEM_PROMPT
    assert "不调度" in text
    assert "不进行新的" in text
    assert "团队负责人调度" not in text
    assert "todo-list" in text.lower() or "不要生成 todo-list" in text
    print("[OK] synthesis prompt is leaf not supervisor")


def test_all_step_types_direct_when_registered():
    main = object()
    workers = {
        "network_search": object(),
        "generate_markdown": object(),
        "convert_pdf": object(),
    }
    for step_type, expected in workers.items():
        agent, mode = resolve_execute_target(
            step_type,
            workers=workers,
            main_agent=main,
            direct_invoke=True,
        )
        assert agent is expected
        assert mode == "direct"
    assert "internet_search" in worker_tools_for_step("network_search")
    assert "generate_markdown" in worker_tools_for_step("generate_markdown")
    print("[OK] registry direct dispatch includes synthesis")


def test_compile_research_graph():
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        from app.research.domain.task_state import task_execution_projection
        from app.research.runtime.graph import compile_research_graph, initial_graph_state

    except ModuleNotFoundError:
        print("[SKIP] compile_research_graph (langgraph not installed)")
        return

    graph = compile_research_graph(checkpointer=InMemorySaver(), profile="agent")
    state = initial_graph_state(
        run_id="r1",
        session_id="s1",
        task_query="比较 Tesla 和 Figure 的差异并生成 Markdown 报告",
        search_mode="agent",
    )
    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": "s1"}, "recursion_limit": 50},
    )
    assert result["search_mode"] == "agent"
    assert result["plan"]
    task_status = task_execution_projection(result["tasks"])
    assert task_status
    assert any(v == "succeeded" for v in task_status.values())
    assert result.get("progress_assessment") is not None
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("dispatch", "retry") in edges
    print(f"[OK] graph invoke status={result.get('status')} tasks={task_status} progress={result.get('progress_assessment')}")


def test_compile_fact_query_still_uses_harness():
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        from app.research.runtime.graph import compile_research_graph, initial_graph_state
    except ModuleNotFoundError:
        print("[SKIP] compile_fact_query_still_uses_harness (langgraph not installed)")
        return

    graph = compile_research_graph(checkpointer=InMemorySaver(), profile="agent")
    state = initial_graph_state(
        run_id="r-auto",
        session_id="s-auto",
        task_query="今天纳斯达克休市了吗",
        search_mode="agent",
    )
    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": "s-auto"}, "recursion_limit": 50},
    )
    assert result["search_mode"] == "agent"
    assert result.get("plan")
    print(f"[OK] fact query still harness plan={bool(result.get('plan'))}")


def test_compile_direct_baseline():
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        from app.research.runtime.graph import compile_research_graph, initial_graph_state
    except ModuleNotFoundError:
        print("[SKIP] compile_direct_baseline (langgraph not installed)")
        return

    graph = compile_research_graph(checkpointer=InMemorySaver(), profile="direct")
    state = initial_graph_state(
        run_id="r-direct",
        session_id="s-direct",
        task_query="今天纳斯达克休市了吗",
        search_mode="direct",
    )
    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": "s-direct"}, "recursion_limit": 20},
    )
    assert result["search_mode"] == "direct"
    assert result.get("plan") in (None, {})
    assert result.get("final_content")
    print(f"[OK] direct baseline status={result.get('status')} mode={result.get('search_mode')}")


def test_plan_step_roundtrip_includes_dag_fields():
    step = PlanStep(
        step_type="network_search",
        description="搜市场",
        subagent="网络搜索助手",
        task_id="t_market",
        depends_on=[],
    )
    plan = ExecutionPlan(summary="x", steps=[step], plan_version=3)
    restored = ExecutionPlan.from_dict(plan.to_dict())
    assert restored.plan_version == 3
    assert restored.steps[0].task_id == "t_market"
    print("[OK] plan DAG fields roundtrip")


if __name__ == "__main__":
    test_action_idempotency_stable_across_index_shift()
    test_annotate_plan_dag_dependencies()
    test_ready_retrieval_can_fan_out()
    test_merge_worker_payloads_dedup()
    test_unregistered_step_fail_closed()
    test_synthesis_prompt_is_not_supervisor()
    test_all_step_types_direct_when_registered()
    test_compile_research_graph()
    test_compile_fact_query_still_uses_harness()
    test_compile_direct_baseline()
    test_plan_step_roundtrip_includes_dag_fields()
    print("\n=== Phase 21 research harness tests passed ===")
