"""Hard Ceiling + Adaptive Effort Allocation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.planner import understand_task
from app.agent.harness.state import LoopState
from app.agent.harness.guardrails import AbortReason, evaluate_run_guardrails
from app.config.loader import get_harness_config
from app.research.planning.compose import compose_execution_plan_sync
from app.research.planning.effort import (
    HardCeiling,
    apply_effort_to_hard_ceiling,
    brief_payload_for_lead_planner,
    estimate_complexity,
    grant_on_gap,
    resolve_effective_budget,
)
from app.research.planning.lead_planner import LEAD_PLANNER_PROMPT


def test_simple_query_gets_narrow_effort():
    intent = understand_task("特斯拉 CEO 是谁")
    effort = estimate_complexity(intent)
    hard = HardCeiling.from_config(get_harness_config())
    effective = apply_effort_to_hard_ceiling(effort, hard)
    assert effort.complexity in {"narrow", "compound"}
    assert effective.session_tool_calls <= hard.max_tool_calls
    assert effective.step_tool_calls <= hard.max_step_tool_calls
    assert effective.parallel_workers <= hard.max_parallel_workers
    print("[OK] narrow/simple effort clamped")


def test_compare_query_breadth_heavy_still_under_ceiling():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度，生成 PDF")
    effective = resolve_effective_budget(intent, get_harness_config())
    hard = effective.hard
    assert effective.effort.complexity in {"breadth_heavy", "open_ended", "compound", "depth_heavy"}
    assert effective.research_tasks <= hard.max_research_tasks
    assert effective.session_tool_calls <= hard.max_tool_calls
    assert effective.as_run_budget()["max_tool_calls"] == effective.session_tool_calls
    grant = grant_on_gap(effective, assessment={"verdict": "gap"})
    assert grant["max_new_tasks"] <= hard.max_plan_patch_tasks
    print("[OK] breadth effort under hard ceiling")


def test_effort_never_raises_hard_ceiling():
    intent = understand_task(
        "完整研究 Linux io_uring 从用户态提交到内核 completion 的整个执行链，并分析性能瓶颈，多维度深入"
    )
    hard = HardCeiling(
        max_tool_calls=10,
        max_step_tool_calls=3,
        max_parallel_workers=1,
        max_research_tasks=2,
        max_plan_patch_tasks=1,
        max_replan_count=1,
        max_plan_steps=6,
        max_run_sec=120,
    )
    effort = estimate_complexity(intent)
    # 估计会偏高
    assert effort.suggested_research_tasks >= 2 or effort.initial_session_tool_budget >= 8
    effective = apply_effort_to_hard_ceiling(effort, hard)
    assert effective.session_tool_calls <= 10
    assert effective.step_tool_calls <= 3
    assert effective.parallel_workers == 1
    assert effective.research_tasks <= 2
    print("[OK] effort cannot raise hard ceiling")


def test_lead_planner_brief_payload_includes_full_brief_fields():
    intent = understand_task("比较 Tesla 与 Figure 的融资与交付，优先官方来源")
    payload = brief_payload_for_lead_planner(intent)
    assert "entities" in payload or "objective" in payload or "raw_query" in payload
    assert "deliverable" in payload
    assert "slots" in payload
    # prompt 模板仍要求完整 Brief 占位
    assert "{brief}" in LEAD_PLANNER_PROMPT
    assert "完整 Research Brief" in LEAD_PLANNER_PROMPT
    print("[OK] lead planner brief payload enriched")


def test_compose_stamps_effort_metadata():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度")
    plan, issues = compose_execution_plan_sync(intent, config=get_harness_config())
    assert plan.planning_mode == "dynamic"
    meta = dict(getattr(plan, "metadata", None) or {})
    assert "effort_plan" in meta
    research = [s for s in plan.steps if s.step_type == "research"]
    assert research
    assert any(
        isinstance(getattr(s, "metadata", None), dict) and "max_retrieval_calls" in s.metadata
        for s in research
    )
    print("[OK] compose stamps effort on plan/steps", "issues=", issues)


def test_run_budget_guardrail_uses_metadata_ceiling():
    cfg = get_harness_config()
    state = LoopState(session_id="s_effort")
    state.metadata["run_budget"] = {
        "max_tool_calls": 2,
        "max_run_sec": 600,
        "max_replan_count": 2,
        "max_plan_steps": 8,
    }
    state.tool_calls_count = 2
    decision = evaluate_run_guardrails(state, cfg, elapsed_sec=1.0, estimated_tokens=10)
    assert decision.abort is True
    assert decision.reason == AbortReason.BUDGET_TOOL_CALLS
    print("[OK] run_budget overrides guardrail tool ceiling")


if __name__ == "__main__":
    test_simple_query_gets_narrow_effort()
    test_compare_query_breadth_heavy_still_under_ceiling()
    test_effort_never_raises_hard_ceiling()
    test_lead_planner_brief_payload_includes_full_brief_fields()
    test_compose_stamps_effort_metadata()
    test_run_budget_guardrail_uses_metadata_ceiling()
    print("all effort tests passed")
