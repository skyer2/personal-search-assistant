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
    apply_grant_to_run_budget,
    brief_payload_for_lead_planner,
    estimate_complexity,
    grant_on_gap,
    resolve_effective_budget,
    retrieval_budget_for_effort_hint,
    stamp_effort_on_plan,
)
from app.research.planning.lead_planner import LEAD_PLANNER_PROMPT
from app.research.planning.progress import assess_progress


def test_simple_query_gets_narrow_effort():
    intent = understand_task("特斯拉 CEO 是谁")
    effort = estimate_complexity(intent)
    hard = HardCeiling.from_config(get_harness_config())
    effective = apply_effort_to_hard_ceiling(effort, hard)
    assert effort.complexity in {"narrow", "compound"}
    assert effective.session_tool_calls <= hard.max_tool_calls
    assert effective.step_tool_calls <= hard.max_step_tool_calls
    assert effective.parallel_workers <= hard.max_parallel_workers
    budget = effective.as_run_budget()
    assert "remaining_plan_patch_tasks" in budget
    assert "max_parallel_workers" in budget
    print("[OK] narrow/simple effort clamped")


def test_compare_query_breadth_heavy_still_under_ceiling():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度，生成 PDF")
    effective = resolve_effective_budget(intent, get_harness_config())
    hard = effective.hard
    assert effective.effort.complexity in {"breadth_heavy", "open_ended", "compound", "depth_heavy"}
    assert effective.research_tasks <= hard.max_research_tasks
    assert effective.session_tool_calls <= hard.max_tool_calls
    assert effective.as_run_budget()["max_tool_calls"] == effective.session_tool_calls
    grant = grant_on_gap(effective, assessment={"verdict": "gap", "coverage_gaps": ["a", "b"]})
    assert grant["max_new_tasks"] <= hard.max_plan_patch_tasks
    assert grant["gap_severity"] >= 1
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


def test_depth_heavy_without_compare_entities():
    """长链/深入单主题：不应只因缺少 compare 就当成 narrow。"""
    intent = understand_task(
        "完整研究 Linux io_uring 从用户态提交到内核 completion 的整个执行链，并分析性能瓶颈"
    )
    effort = estimate_complexity(intent)
    assert effort.complexity in {"depth_heavy", "open_ended", "compound", "breadth_heavy"}
    assert effort.suggested_workers >= 1
    assert "long_or_chain_query" in effort.signals or effort.complexity != "narrow"
    print("[OK] depth-heavy chain query", effort.complexity, effort.signals)


def test_lead_planner_brief_payload_includes_full_brief_fields():
    intent = understand_task("比较 Tesla 与 Figure 的融资与交付，优先官方来源")
    payload = brief_payload_for_lead_planner(intent)
    assert "entities" in payload or "objective" in payload or "raw_query" in payload
    assert "deliverable" in payload
    assert "slots" in payload
    assert "success_criteria" in payload or "constraints" in payload
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


def test_per_task_effort_hint_scales_retrieval():
    cfg = get_harness_config()
    intent = understand_task("比较 Tesla / Figure 2026")
    effective = resolve_effective_budget(intent, cfg)
    plan, _ = compose_execution_plan_sync(intent, config=cfg)
    research = [s for s in plan.steps if s.step_type == "research"]
    assert research
    research[0].metadata["effort"] = "low"
    research[1].metadata["effort"] = "high" if len(research) > 1 else "high"
    stamp_effort_on_plan(plan, effective)
    low = int(research[0].metadata["max_retrieval_calls"])
    high = int(research[1].metadata["max_retrieval_calls"]) if len(research) > 1 else low
    assert low <= high
    assert low <= effective.hard.max_step_tool_calls
    assert retrieval_budget_for_effort_hint(4, "low", hard_step=8) <= retrieval_budget_for_effort_hint(
        4, "high", hard_step=8
    )
    print("[OK] effort hint scales retrieval", low, high)


def test_grant_depletes_remaining_reserve():
    intent = understand_task("比较 Tesla / Figure / Unitree 商业化")
    effective = resolve_effective_budget(intent, get_harness_config())
    budget = effective.as_run_budget()
    g1 = grant_on_gap(
        effective,
        assessment={"coverage_gaps": ["a", "b", "c", "d"]},
        run_budget=budget,
    )
    assert g1["max_new_tasks"] >= 1
    budget = apply_grant_to_run_budget(budget, g1, tasks_granted=g1["max_new_tasks"])
    assert budget["remaining_plan_patch_tasks"] < effective.plan_patch_tasks or effective.plan_patch_tasks == 0
    g2 = grant_on_gap(effective, assessment={"coverage_gaps": ["e"]}, run_budget=budget)
    # 剩余额度应更紧
    assert g2["max_new_tasks"] <= budget["remaining_plan_patch_tasks"]
    print("[OK] grant depletes remaining reserve", budget)


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


def test_progress_scores_brief_success_criteria():
    intent = understand_task("比较 Tesla / Figure 2026 商业化，优先官方来源")
    plan, _ = compose_execution_plan_sync(intent, config=get_harness_config())
    for step in plan.steps:
        if step.step_type == "research":
            step.metadata["status"] = "done"
    research = [s for s in plan.steps if s.step_type == "research" and not s.depends_on]
    rows = [
        {
            "task_id": research[0].task_id,
            "ok": True,
            "summary": "Tesla 有一些二手媒体传闻",
            "payload": {
                "facts": ["Tesla 传闻量产"],
                "sources": ["https://random-blog.example/post"],
                "confidence": 0.6,
            },
        }
    ]
    for step in research[1:]:
        rows.append(
            {
                "task_id": step.task_id,
                "ok": True,
                "summary": "二手媒体传闻，无一手引用",
                "payload": {
                    "facts": ["传闻量产"],
                    "sources": ["https://blog.example/x"],
                    "confidence": 0.5,
                },
            }
        )
    assessment = assess_progress(
        plan,
        task_status={s.resolved_task_id(i): "done" for i, s in enumerate(plan.steps)},
        worker_results=rows,
        query=intent.raw_query,
        intent=intent,
        current_year=2026,
    )
    # 优先官方但来源无 primary hint → gap / unmet
    assert assessment.verdict == "gap"
    assert (
        assessment.coverage_gaps
        or assessment.unmet_success_criteria
        or assessment.unmet_constraints
        or assessment.missing_dimensions
    )
    print(
        "[OK] progress scores brief contract",
        "unmet_criteria=",
        assessment.unmet_success_criteria,
        "unmet_constraints=",
        assessment.unmet_constraints,
    )


def test_brief_is_ir_with_ambiguities_field():
    intent = understand_task("比较 Tesla 与 Waymo")
    assert intent.brief is not None
    d = intent.brief.to_dict()
    assert "success_criteria" in d
    assert "constraints" in d
    assert "ambiguities" in d
    assert "objective" in d
    print("[OK] ResearchBrief IR fields present")


if __name__ == "__main__":
    test_simple_query_gets_narrow_effort()
    test_compare_query_breadth_heavy_still_under_ceiling()
    test_effort_never_raises_hard_ceiling()
    test_depth_heavy_without_compare_entities()
    test_lead_planner_brief_payload_includes_full_brief_fields()
    test_compose_stamps_effort_metadata()
    test_per_task_effort_hint_scales_retrieval()
    test_grant_depletes_remaining_reserve()
    test_run_budget_guardrail_uses_metadata_ceiling()
    test_progress_scores_brief_success_criteria()
    test_brief_is_ir_with_ambiguities_field()
    print("all effort tests passed")
