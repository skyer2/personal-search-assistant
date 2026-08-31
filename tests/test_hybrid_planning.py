"""Hybrid planning：个人版 web + file 来源策略。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.orchestration import check_unauthorized_tools
from app.agent.harness.planner import build_plan, understand_task, validate_plan_against_intent
from app.agent.harness.state import PlanStep
from app.research.planning.compose import compose_execution_plan_sync
from app.research.planning.policy import parse_source_policy, select_planning_mode, tools_for_sources
from app.research.planning.progress import evaluate_progress
from app.research.runtime.scheduler import ready_research_steps
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
    plan = build_plan(direct)
    assert [s.step_type for s in plan.steps] == ["network_search", "summarize"]
    ok, issues = validate_plan_against_intent(direct, plan)
    assert ok and not issues
    print("[OK] direct chat deliverable")


def test_explicit_markdown_request():
    intent = understand_task("搜索 Tesla 2026 动态，生成 Markdown 报告")
    assert intent.deliverable == "md"
    plan = build_plan(intent)
    assert plan.steps[-1].step_type == "generate_markdown"
    print("[OK] explicit markdown request")


def test_dynamic_compare_builds_objective_dag():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度")
    assert select_planning_mode(intent) == "dynamic"
    plan, issues = compose_execution_plan_sync(intent)
    assert plan.planning_mode == "dynamic"
    research = [s for s in plan.steps if s.step_type == "research"]
    assert len(research) >= 3
    assert all("internet_search" in (s.allowed_tools or []) for s in research)
    print("[OK] dynamic DAG")


def test_research_worker_allowlist_and_registry():
    step = PlanStep(
        step_type="research",
        description="Figure 订单",
        allowed_tools=tools_for_sources(["web"]),
    )
    ok, bad = check_unauthorized_tools(step, ["internet_search"], enforce=True)
    assert ok is True and not bad
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


def test_progress_abort():
    intent = understand_task("比较 A 和 B")
    plan, _ = compose_execution_plan_sync(intent)
    assert evaluate_progress(plan, aborted=True) == "abort"
    print("[OK] progress abort")


if __name__ == "__main__":
    test_source_policy_forbids_web()
    test_direct_chat_deliverable()
    test_explicit_markdown_request()
    test_dynamic_compare_builds_objective_dag()
    test_research_worker_allowlist_and_registry()
    test_progress_abort()
    print("\n=== Hybrid planning tests passed ===")
