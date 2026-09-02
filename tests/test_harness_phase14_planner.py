"""
【Phase 14】工业级 Planner 单测（无需 LLM API）

结构化槽位、置信度、歧义澄清、Plan 校验、HITL 触发条件。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.harness.planner import (
    apply_intent_clarification,
    auto_resolve_clarification,
    build_plan,
    should_request_plan_review,
    understand_task,
    validate_plan_against_intent,
)
from app.config.loader import load_harness_config, reset_harness_config


def test_slots_cross_border_list_citations():
    query = (
        "请使用网络搜索工具，检索 2026 年跨境电商 AI 客服趋势，"
        "列出 5 条关键变化，并附上来源链接。"
    )
    intent = understand_task(query)
    assert intent.slots.item_count == 5
    assert intent.slots.require_citations is True
    assert intent.deliverable == "md"
    plan = build_plan(intent)
    steps = [s.step_type for s in plan.steps]
    assert steps == ["network_search", "generate_markdown"]
    ok, issues = validate_plan_against_intent(intent, plan)
    assert ok and not issues
    print("[OK] cross-border slots -> md plan")


def test_text_only_explicit():
    intent = understand_task("只搜索AI新闻，不需要生成文件")
    assert intent.deliverable == "text"
    plan = build_plan(intent)
    assert [s.step_type for s in plan.steps] == ["network_search", "summarize"]
    print("[OK] explicit text deliverable")


def test_ambiguity_flags_and_clarification():
    query = "简要回答 AI 趋势并附来源"
    intent = understand_task(query)
    assert intent.needs_clarification
    assert intent.clarification_question
    resolved = auto_resolve_clarification(intent)
    assert resolved.clarification_resolved
    print("[OK] ambiguity + auto_resolve")


def test_hitl_clarification_edit_deliverable():
    intent = understand_task("列出 3 条趋势并附链接")
    patched = apply_intent_clarification(
        intent,
        {"deliverable": "text", "slots": {"output_preference": "chat"}},
    )
    assert patched.deliverable == "text"
    assert patched.clarification_resolved
    print("[OK] HITL clarification patch")


def test_plan_review_triggers():
    multi = understand_task("结合网络和数据库研究生成报告")
    assert should_request_plan_review(multi) is True
    low = understand_task("列出趋势并附来源")
    low.intent_confidence = 0.5
    assert should_request_plan_review(low, min_confidence=0.75) is True
    print("[OK] plan review triggers")


def test_planner_config_phase14():
    reset_harness_config()
    cfg = load_harness_config()
    assert cfg.planner_llm_enabled is True
    assert cfg.planner_clarification_enabled is True
    assert cfg.planner_clarification_auto_resolve is True
    print("[OK] planner config phase14")


def test_intent_tasks_golden_dry():
    from tests.eval.runners.component import run_planner_eval

    results = run_planner_eval()
    failed = [row.task_id for row in results if not row.success]
    assert not failed, f"planner component failed: {failed}"
    print(f"[OK] all {len(results)} planner_v2 cases")


if __name__ == "__main__":
    test_slots_cross_border_list_citations()
    test_text_only_explicit()
    test_ambiguity_flags_and_clarification()
    test_hitl_clarification_edit_deliverable()
    test_plan_review_triggers()
    test_planner_config_phase14()
    test_intent_tasks_golden_dry()
    print("\n=== Phase 14 planner tests passed ===")
