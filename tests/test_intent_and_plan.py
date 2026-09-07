"""个人搜索助手 Intent / Plan / Brief-driven Progress（无需 LLM）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.planner import build_plan, understand_task
from app.agent.harness.research_brief import compile_research_brief
from app.research.planning.compose import compose_execution_plan_sync
from app.research.planning.policy import select_planning_mode
from app.research.runtime.scheduler import task_status_map


def test_brief_default_chat_and_web():
    intent = understand_task("2026 年 AI 电商趋势有哪些？附来源")
    assert intent.deliverable == "text"
    assert intent.needs_network is True
    assert intent.brief.deliverable == "text"
    assert "web" in intent.brief.source_policy
    assert intent.brief.prefer_primary is False
    assert select_planning_mode(intent) == "direct"
    plan = build_plan(intent)
    assert [s.step_type for s in plan.steps] == ["network_search", "summarize"]
    print("[OK] brief default chat + web")


def test_brief_compare_entities_and_dynamic_plan():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度")
    assert "Tesla" in intent.brief.entities
    assert "Figure" in intent.brief.entities
    assert intent.brief.depth == "thorough"
    assert intent.brief.time_range == "2026"
    assert "商业化" in intent.brief.dimensions or "横向比较" in intent.brief.dimensions
    assert select_planning_mode(intent) == "dynamic"
    plan, issues = compose_execution_plan_sync(intent)
    assert plan.planning_mode == "dynamic"
    research = [s for s in plan.steps if s.step_type == "research" and not s.depends_on]
    assert len(research) >= 3
    assert any("Tesla" in (s.objective or s.description) for s in research)
    assert all(step.step_type in {"research", "network_search", "file_read"} for step in plan.steps)
    print("[OK] brief compare → dynamic DAG")


def test_brief_prefer_primary_and_markdown():
    intent = understand_task("搜索 Tesla 官方白皮书并生成 Markdown 报告")
    assert intent.deliverable == "md"
    assert intent.brief.prefer_primary is True
    assert intent.brief.depth == "thorough"
    plan = build_plan(intent)
    assert plan.steps[-1].step_type == "generate_markdown"
    print("[OK] prefer_primary + markdown")


def test_brief_forbids_web():
    intent = understand_task("不要联网，只读我上传的文件", has_uploaded_files=True)
    assert intent.needs_network is False
    assert "web" in intent.forbidden_sources
    assert "file" in intent.brief.source_policy
    plan, _ = compose_execution_plan_sync(intent)
    assert all(s.step_type != "network_search" for s in plan.steps)
    print("[OK] brief respects no-web policy")


def test_intent_roundtrip_keeps_brief():
    intent = understand_task("比较 Tesla / Figure 2026 监管进展")
    restored = intent.from_dict(intent.to_dict())
    assert restored.brief.entities
    assert restored.brief.prefer_primary is False
    prompt = compile_research_brief(task_query=intent.raw_query, intent=intent).to_prompt()
    assert "Research Brief" in prompt
    assert "深度" in prompt
    print("[OK] intent brief roundtrip")


if __name__ == "__main__":
    test_brief_default_chat_and_web()
    test_brief_compare_entities_and_dynamic_plan()
    test_brief_prefer_primary_and_markdown()
    test_brief_forbids_web()
    test_intent_roundtrip_keeps_brief()
    print("\n=== Intent & Plan tests passed ===")
