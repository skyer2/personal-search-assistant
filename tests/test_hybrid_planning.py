"""Hybrid planning：个人版 web + file 来源策略。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.orchestration import check_unauthorized_tools
from app.agent.harness.planner import understand_task
from app.agent.harness.state import PlanStep
from app.research.planning.compose import compose_execution_plan_sync
from app.research.planning.policy import parse_source_policy, select_planning_mode, tools_for_sources
from app.research.workers.registry import resolve_execute_target


def test_source_policy_forbids_web():
    query = "不要联网，只读我上传的文件"
    intent = understand_task(query, has_uploaded_files=True)
    assert intent.needs_network is False
    assert intent.needs_file_read is True
    assert "web" in intent.forbidden_sources
    policy = parse_source_policy(query)
    assert not policy.allows("web")
    plan, issues = compose_execution_plan_sync(intent)
    assert all(step.step_type != "network_search" for step in plan.steps)
    print("[OK] source policy forbids web")


def test_direct_chat_deliverable():
    direct = understand_task("2026 年 AI 电商趋势有哪些？附来源")
    assert select_planning_mode(direct) == "direct"
    assert direct.deliverable == "text"
    plan, issues = compose_execution_plan_sync(direct)
    assert not issues
    assert [s.step_type for s in plan.steps] == ["network_search"]
    print("[OK] direct chat deliverable")


def test_explicit_markdown_request():
    intent = understand_task("搜索 Tesla 2026 动态，生成 Markdown 报告")
    assert intent.deliverable == "md"
    plan, issues = compose_execution_plan_sync(intent)
    assert not issues
    assert all(s.step_type in {"research", "network_search", "file_read"} for s in plan.steps)
    print("[OK] explicit markdown request")


def test_dynamic_compare_builds_objective_dag():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度")
    assert select_planning_mode(intent) == "dynamic"
    plan, issues = compose_execution_plan_sync(intent)
    assert plan.planning_mode == "dynamic"
    research = [s for s in plan.steps if s.step_type == "research"]
    assert len(research) >= 3
    assert all("internet_search" in (s.allowed_tools or []) for s in research)
    assert all("read_artifact" in (s.allowed_tools or []) for s in research)
    print("[OK] dynamic DAG")


def test_planner_defect_falls_back_to_template(monkeypatch):
    from app.research.planning import compose as compose_module

    intent = understand_task("搜索 Tesla 2026 动态，生成 Markdown 报告")

    def strict_failure(*args, **kwargs):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(compose_module, "_compose_execution_plan_strict", strict_failure)
    plan, issues = compose_execution_plan_sync(intent)

    assert plan.planning_mode == "fallback_template"
    assert plan.steps
    assert issues[0] == "planning_fallback:RuntimeError"
    assert all(step.step_type != "generate_markdown" for step in plan.steps)
    print("[OK] planner defect falls back to template")


def test_research_worker_allowlist_and_registry():
    step = PlanStep(
        step_type="research",
        description="Figure 订单",
        allowed_tools=tools_for_sources(["web"]),
    )
    ok, bad = check_unauthorized_tools(step, ["internet_search"], enforce=True)
    assert ok is True and not bad
    ok_ctx, bad_ctx = check_unauthorized_tools(
        step, ["read_artifact", "read_evidence"], enforce=True
    )
    assert ok_ctx is True and not bad_ctx
    ok2, bad2 = check_unauthorized_tools(step, ["generate_markdown"], enforce=True)
    assert ok2 is False and "generate_markdown" in bad2
    worker = object()
    agent, mode = resolve_execute_target(
        "research",
        workers={"research": worker},
        main_agent=object(),
        direct_invoke=True,
    )
    assert agent is worker and mode == "direct"
    print("[OK] research allowlist + registry")


if __name__ == "__main__":
    test_source_policy_forbids_web()
    test_direct_chat_deliverable()
    test_explicit_markdown_request()
    test_dynamic_compare_builds_objective_dag()
    test_research_worker_allowlist_and_registry()
    print("\n=== Hybrid planning tests passed ===")
