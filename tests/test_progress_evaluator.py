"""Progress Evaluator：语义缺口 / 冲突 / 过时证据（无需 LLM）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.planner import understand_task
from app.research.planning.compose import compose_execution_plan_sync
from app.research.planning.plan_patch import apply_plan_patch, build_progress_patch
from app.research.planning.progress import assess_progress, evaluate_progress
from app.research.runtime.graph import route_dispatch, route_progress
from app.research.runtime.scheduler import annotate_plan_tasks, task_status_map
from app.research.runtime.state import empty_research_state


def _dynamic_plan():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度")
    plan, _issues = compose_execution_plan_sync(intent)
    plan = annotate_plan_tasks(plan)
    for step in plan.steps:
        if step.step_type == "research":
            step.metadata["status"] = "done"
    return intent, plan


def _done_status(plan):
    return task_status_map(plan)


def test_empty_and_gap_signals_block_synthesis():
    intent, plan = _dynamic_plan()
    research = [s for s in plan.steps if s.step_type == "research" and not s.depends_on]
    tesla, figure, unitree = research[0], research[1], research[2]
    rows = [
        {
            "task_id": tesla.task_id,
            "ok": True,
            "summary": "Tesla 未披露收入，暂无数据",
            "payload": {
                "facts": [],
                "sources": ["https://example.com/tesla"],
                "gaps": ["收入数据缺失"],
                "confidence": 0.4,
            },
        },
        {
            "task_id": figure.task_id,
            "ok": True,
            "summary": "Figure 2024 小规模试产",
            "payload": {
                "facts": ["Figure 2024 订单约 100 台"],
                "sources": ["https://example.com/figure"],
                "confidence": 0.7,
            },
        },
        {
            "task_id": unitree.task_id,
            "ok": True,
            "summary": "Unitree 收入口径冲突",
            "payload": {
                "facts": ["Unitree 2026 收入 10亿美元", "Unitree 2026 收入 50亿美元"],
                "sources": ["https://a.example", "https://b.example"],
                "conflicts": ["收入口径不一致"],
                "confidence": 0.6,
            },
        },
    ]
    assessment = assess_progress(
        plan,
        task_status=_done_status(plan),
        worker_results=rows,
        query=intent.raw_query,
        current_year=2026,
    )
    assert assessment.verdict == "gap"
    assert assessment.coverage_gaps
    assert assessment.conflicts
    print(
        f"[OK] semantic gap coverage={len(assessment.coverage_gaps)} "
        f"conflicts={len(assessment.conflicts)} stale={assessment.stale_evidence}"
    )


def test_enough_when_coverage_is_solid():
    intent, plan = _dynamic_plan()
    research = [s for s in plan.steps if s.step_type == "research" and not s.depends_on]
    rows = []
    for step in research:
        rows.append(
            {
                "task_id": step.task_id,
                "ok": True,
                "summary": f"{step.objective} 2026 已量产，收入与订单均有来源",
                "payload": {
                    "facts": [
                        f"{step.objective} 2026 收入 12亿美元",
                        f"{step.objective} 2026 订单 8000 台，已量产交付客户",
                    ],
                    "sources": [f"https://example.com/{step.task_id}"],
                    "confidence": 0.9,
                },
            }
        )
    assessment = assess_progress(
        plan,
        task_status=_done_status(plan),
        worker_results=rows,
        query=intent.raw_query,
        current_year=2026,
    )
    assert assessment.verdict == "enough"
    assert not assessment.coverage_gaps
    assert not assessment.conflicts
    print("[OK] solid coverage is enough")


def test_dispatch_routes_to_progress_not_synthesize():
    intent, plan = _dynamic_plan()
    state = empty_research_state(
        run_id="r-gap",
        session_id="s-gap",
        task_query=intent.raw_query,
    )
    state["plan"] = plan.to_dict()
    state["task_status"] = _done_status(plan)
    assert route_dispatch(state) == "progress"
    state["progress_assessment"] = assess_progress(
        plan,
        task_status=state["task_status"],
        worker_results=[
            {
                "task_id": plan.steps[0].task_id,
                "ok": True,
                "summary": "Tesla 未披露收入",
                "payload": {"facts": [], "gaps": ["收入缺失"], "sources": ["https://x"]},
            }
        ],
        query=intent.raw_query,
        current_year=2026,
    ).to_dict()
    assert route_progress(state) == "replan"
    state["replan_exhausted"] = True
    assert route_progress(state) == "prepare_synthesis"
    print("[OK] dispatch→progress→replan/exhausted")


def test_progress_patch_is_constrained_and_policy_safe():
    intent = understand_task("比较 Tesla / Figure，不要联网，只根据内部数据库")
    plan, _ = compose_execution_plan_sync(intent)
    plan = annotate_plan_tasks(plan)
    for step in plan.steps:
        if step.step_type == "research":
            step.metadata["status"] = "done"
    assessment = {
        "verdict": "gap",
        "coverage_gaps": ["empty:t_tesla:Tesla 商业化收入"],
        "conflicts": ["收入:亿: t1=10; t2=50"],
        "stale_evidence": [],
        "missing_dimensions": [],
        "low_confidence_claims": [],
        "reason": "semantic_gap",
    }
    patch = build_progress_patch(plan, intent, assessment=assessment, max_new_tasks=2)
    assert patch["add_tasks"]
    assert len(patch["add_tasks"]) <= 2
    updated, issues = apply_plan_patch(plan, patch, intent, max_new_tasks=2)
    assert not issues
    for step in updated.steps:
        if step.task_id.startswith("t_gap"):
            assert "internet_search" not in (step.allowed_tools or [])
    assert evaluate_progress(plan, aborted=True) == "abort"
    print("[OK] constrained progress patch")


if __name__ == "__main__":
    test_empty_and_gap_signals_block_synthesis()
    test_enough_when_coverage_is_solid()
    test_dispatch_routes_to_progress_not_synthesize()
    test_progress_patch_is_constrained_and_policy_safe()
    print("\n=== Progress evaluator tests passed ===")
