"""Progress stop policy + budget reserve + partial report + gap binding."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.partial_report import render_partial_report
from app.agent.harness.planner import understand_task
from app.agent.harness.run_budget import PhaseBudgetPlan, RunBudgetManager
from app.agent.harness.state import LoopState, PlanStep, ExecutionPlan, StepResult
from app.observability.semantic import plan_brief_coverage
from app.research.planning.compose import compose_execution_plan_sync
from app.research.planning.plan_patch import build_progress_patch
from app.research.planning.progress import assess_progress, _classify_conflict
from app.config.loader import get_harness_config


def test_benchmark_conflict_is_expected_disagreement_not_gap():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度")
    plan, _ = compose_execution_plan_sync(intent, config=get_harness_config())
    assert plan.planning_mode == "dynamic"
    for step in plan.steps:
        if step.step_type == "research":
            step.metadata["status"] = "done"
    research = [s for s in plan.steps if s.step_type == "research" and not s.depends_on]
    assert research
    rows = []
    for step in research:
        rows.append(
            {
                "task_id": step.task_id,
                "ok": True,
                "summary": (
                    f"{step.objective} 2026 已量产，收入与订单均有来源。"
                    " SWE-bench Verified 76.8% 与 SWE-bench Pro 59.1% "
                    "差异来自脚手架与数据划分不同口径"
                ),
                "payload": {
                    "facts": [
                        f"{step.objective} 2026 收入 12亿美元",
                        f"{step.objective} 2026 订单 8000 台，已量产交付客户",
                        "SWE-bench Verified 76.8%",
                        "SWE-bench Pro 59.1%",
                    ],
                    "sources": [
                        f"https://example.com/{step.task_id}",
                        "https://example.com/swe-verified",
                        "https://example.com/swe-pro",
                    ],
                    "conflicts": ["SWE-bench Verified 76.8% vs Pro 59.1%（脚手架不同）"],
                    "confidence": 0.85,
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
    assert assessment.expected_disagreements, assessment.to_dict()
    assert assessment.verdict == "enough", assessment.to_dict()
    assert not assessment.unresolved_conflicts
    print("[OK] expected disagreement does not force GAP")


def test_thin_conflict_still_unresolved():
    kind = _classify_conflict(
        "收入数字对不上",
        sources=[],
        confidence=0.2,
        facts=[],
    )
    assert kind == "unresolved_conflict"
    print("[OK] thin conflict unresolved")


def test_plan_patch_binds_only_selected_gaps():
    intent = understand_task("比较 Tesla / Figure 商业化缺口")
    plan, _ = compose_execution_plan_sync(intent, config=get_harness_config())
    assessment = {
        "verdict": "gap",
        "reason": "semantic_gap",
        "coverage_gaps": ["empty:t1:缺少订单", "empty:t2:缺少收入", "empty:t3:缺少客户"],
        "unresolved_conflicts": ["t1:冲突A", "t2:冲突B"],
        "expected_disagreements": ["benchmark A vs B"],
        "gaps": [
            {"gap_id": "gap_a", "type": "coverage", "description": "empty:t1:缺少订单", "actionable": True},
            {"gap_id": "gap_b", "type": "coverage", "description": "empty:t2:缺少收入", "actionable": True},
            {"gap_id": "gap_c", "type": "coverage", "description": "empty:t3:缺少客户", "actionable": True},
            {"gap_id": "gap_d", "type": "unresolved_conflict", "description": "t1:冲突A", "actionable": True},
            {
                "gap_id": "gap_e",
                "type": "expected_disagreement",
                "description": "benchmark A vs B",
                "actionable": False,
            },
        ],
        "open_gap_ids": ["gap_a", "gap_b", "gap_c", "gap_d"],
        "progress_id": "progress_test",
    }
    patch = build_progress_patch(plan, intent, assessment=assessment, max_new_tasks=2)
    assert len(patch.get("add_tasks") or []) <= 2
    targets = patch.get("target_gap_ids") or []
    assert len(targets) == len(patch.get("add_tasks") or [])
    assert "gap_e" not in targets
    for task in patch.get("add_tasks") or []:
        assert (task.get("metadata") or {}).get("resolves_gap_ids")
    print("[OK] patch targets only selected gaps", targets)


def test_run_budget_protects_synthesis_reserve():
    mgr = RunBudgetManager(token_limit=100_000, llm_call_limit=30, tool_call_limit=40)
    assert mgr.phase_plan.synthesis_reserve_tokens(100_000) == 25_000
    mgr.commit_tokens(70_000)
    assert mgr.force_synthesis() is True
    allowed, why = mgr.research_allowed()
    assert allowed is False
    assert why in {"synthesis_reserve_protect", "research_token_cap"}
    print("[OK] research blocked to protect synthesis reserve", why)


def test_partial_report_is_readable_not_raw_dump():
    state = LoopState(session_id="s_partial")
    state.step_results = [
        StepResult(
            step_type="research",
            content="很长的原始搜索日志" * 50,
            metadata={
                "task_id": "t_tech",
                "worker_payload": {
                    "summary": "技术可行性：实验室基准高，真实任务可靠性仍有差距。",
                    "facts": ["SWE-bench Verified ~75%"],
                    "sources": ["https://a.example"],
                },
            },
        )
    ]
    text = render_partial_report(state=state, abort_reason="deadline_exceeded", synthesis_failed=True)
    assert "本次研究未完整完成" in text
    assert "技术可行性" in text
    assert "很长的原始搜索日志" not in text
    print("[OK] partial report readable")


def test_limits_forecast_covers_局限与未来趋势():
    brief = {
        "dimensions": ["技术可行性", "软件工程本质与复杂度", "产业进展与商业化", "局限与未来趋势"],
    }
    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                description="tech",
                task_id="t_ai_coding_tech",
                objective="AI coding 技术能力现状",
            ),
            PlanStep(
                step_type="research",
                description="swe",
                task_id="t_swe_essence",
                objective="软件工程不可约复杂性",
            ),
            PlanStep(
                step_type="research",
                description="industry",
                task_id="t_industry_progress",
                objective="产业落地与商业化",
            ),
            PlanStep(
                step_type="research",
                description="limits",
                task_id="t_limits_forecast",
                objective="AI coding 已知失效模式与未来预测",
            ),
        ]
    )
    cov = plan_brief_coverage(brief, plan)
    assert "局限与未来趋势" not in (cov.get("missing_dimensions") or []), cov
    print("[OK] limits_forecast covers 局限与未来趋势")


if __name__ == "__main__":
    test_benchmark_conflict_is_expected_disagreement_not_gap()
    test_thin_conflict_still_unresolved()
    test_plan_patch_binds_only_selected_gaps()
    test_run_budget_protects_synthesis_reserve()
    test_partial_report_is_readable_not_raw_dump()
    test_limits_forecast_covers_局限与未来趋势()
    print("all research-stop tests passed")
