"""
【Phase 6】Citation-First + Trajectory Diff + HITL Edit 单元测试（无需 LLM API）
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.citations import CitationManager
from app.agent.harness.hitl import HitlCoordinator
from app.agent.harness.planner import (
    apply_plan_edits,
    build_plan,
    detect_multi_intent,
    dynamic_replan,
    understand_task,
)
from app.agent.harness.state import ExecutionPlan, PlanStep
from app.config.loader import reload_harness_config
from tests.eval.trajectory import compare_trajectories, extract_trajectory_dry


def test_citation_manager_registers_and_builds_report():
    mgr = CitationManager()
    content = "参考 https://example.com/ai 与行业数据，2026年趋势向好。"
    mgr.register_from_step(0, "network_search", content)
    cited = mgr.build_cited_report("## 摘要\nAI 电商持续增长。")
    assert "## 参考文献" in cited
    assert "[1]" in cited
    metrics = mgr.compute_metrics(cited)
    assert metrics["registered_sources"] >= 1
    assert metrics["citation_coverage_rate"] >= 0
    print("[OK] citation manager")


def test_citation_validate_finalize():
    mgr = CitationManager()
    mgr.register_from_step(0, "network_search", "https://a.com " * 5 + "content " * 20)
    report = mgr.build_cited_report("结论段落 [1]")
    ok, reason = mgr.validate_citations(report, min_coverage=0.1)
    assert ok or reason == "citation_coverage_low"
    print("[OK] citation validate")


def test_trajectory_diff():
    actual = ["network_search", "generate_markdown"]
    expected = ["network_search", "generate_markdown"]
    diff = compare_trajectories(actual, expected)
    assert diff["similarity"] == 1.0
    assert diff["missing_steps"] == []

    actual2 = extract_trajectory_dry(["network_search", "summarize"], ["网络搜索助手"])
    diff2 = compare_trajectories(actual2[:2], expected)
    assert diff2["similarity"] >= 0.5
    print("[OK] trajectory diff")


def test_planner_multi_intent_and_replan():
    intent = understand_task("结合公开资料和数据库，整理机器人行业报告并生成PDF")
    assert detect_multi_intent(intent) is True
    plan = build_plan(intent)
    replanned = dynamic_replan(plan, 1, "sql_empty")
    assert len(replanned.steps) > len(plan.steps)
    edited = apply_plan_edits(
        plan,
        [{"step_type": "network_search", "description": "用户编辑后的搜索", "subagent": "网络搜索助手"}],
    )
    assert edited.steps[0].description == "用户编辑后的搜索"
    print("[OK] planner replan + edit")


def test_hitl_edit_decision_flow():
    coordinator = HitlCoordinator()

    async def _run() -> list[dict]:
        payload = {
            "action_requests": [{"name": "execution_plan", "args": {"steps": []}}],
            "review_configs": [{"action_name": "execution_plan", "allowed_decisions": ["edit"]}],
            "gate_type": "plan_review",
        }

        async def worker() -> list[dict]:
            return await coordinator.wait_for_decisions("sess-edit", payload, timeout_sec=2)

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.05)
        ok = coordinator.submit_decisions(
            "sess-edit",
            [
                {
                    "type": "edit",
                    "edited_action": {
                        "steps": [
                            {
                                "step_type": "network_search",
                                "description": "编辑后计划",
                                "subagent": "网络搜索助手",
                            }
                        ],
                        "replan": True,
                    },
                }
            ],
        )
        assert ok is True
        return await task

    result = asyncio.run(_run())
    assert result[0]["type"] == "edit"
    print("[OK] hitl edit flow")


def test_phase6_config():
    config = reload_harness_config()
    assert config.citations_enabled is True
    assert config.hitl_plan_review_enabled is True
    assert config.hitl_allow_edit is True
    assert config.eval_trajectory_min_similarity == 0.6
    print("[OK] phase6 config")


def test_dry_eval_with_trajectory():
    from tests.eval.run_eval import load_tasks, run_dry_eval

    tasks = load_tasks(ROOT / "tests" / "eval" / "datasets" / "harness_scenarios_v1.jsonl")
    results = run_dry_eval(tasks[:3])
    assert len(results) == 3
    assert all(item.success for item in results)
    print("[OK] dry eval scenario subset")


if __name__ == "__main__":
    test_citation_manager_registers_and_builds_report()
    test_citation_validate_finalize()
    test_trajectory_diff()
    test_planner_multi_intent_and_replan()
    test_hitl_edit_decision_flow()
    test_phase6_config()
    test_dry_eval_with_trajectory()
    print("\n=== All Phase 6 tests passed ===")
