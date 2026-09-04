"""Claim-level cross-worker conflict reconciliation + disclosure gate."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.guardrails import can_replan
from app.agent.harness.state import LoopState, TaskIntent
from app.agent.harness.validator import ResultValidator
from app.config.loader import get_harness_config
from app.research.claims import reconcile_worker_results
from app.research.claims.extract import extract_claims_from_worker_results
from app.research.planning.progress import assess_progress
from app.agent.harness.state import ExecutionPlan, PlanStep


def _rows_cross_worker_revenue_conflict():
    return [
        {
            "task_id": "t_worker_a",
            "ok": True,
            "summary": "Tesla FY2026 total revenue 10B USD from official IR",
            "payload": {
                "facts": ["Tesla FY2026 total revenue 10亿美元"],
                "sources": ["https://ir.tesla.com/10-k"],
                "confidence": 0.9,
                "findings": [
                    {
                        "claim": "Tesla FY2026 total revenue 10亿美元",
                        "confidence": 0.9,
                        "source_quality": "primary",
                        "evidence_ids": ["e1"],
                    }
                ],
            },
        },
        {
            "task_id": "t_worker_b",
            "ok": True,
            "summary": "Tesla FY2026 total revenue 8B USD from Reuters",
            "payload": {
                "facts": ["Tesla FY2026 total revenue 8亿美元"],
                "sources": ["https://www.reuters.com/tesla-revenue"],
                "confidence": 0.7,
                "findings": [
                    {
                        "claim": "Tesla FY2026 total revenue 8亿美元",
                        "confidence": 0.7,
                        "source_quality": "secondary",
                        "evidence_ids": ["e2"],
                    }
                ],
            },
        },
    ]


def test_extract_and_detect_cross_worker_conflict():
    rows = _rows_cross_worker_revenue_conflict()
    claims = extract_claims_from_worker_results(rows)
    assert len(claims) >= 2
    result = reconcile_worker_results(rows)
    assert result.edges, result.to_dict()
    # same subject/metric/period/scope → unresolved (or authority resolve)
    assert result.unresolved_labels or result.resolved_labels, result.to_dict()
    print("[OK] cross-worker claims detected", result.to_dict())


def test_scope_mismatch_is_expected_disagreement():
    rows = [
        {
            "task_id": "t_a",
            "ok": True,
            "summary": "ok",
            "payload": {
                "facts": ["Tesla FY2026 automotive revenue 8亿美元"],
                "sources": ["https://ir.tesla.com/a"],
                "confidence": 0.9,
            },
        },
        {
            "task_id": "t_b",
            "ok": True,
            "summary": "ok",
            "payload": {
                "facts": ["Tesla FY2026 total revenue 10亿美元"],
                "sources": ["https://ir.tesla.com/b"],
                "confidence": 0.9,
            },
        },
    ]
    result = reconcile_worker_results(rows)
    assert result.disclosed_labels or not result.unresolved_labels, result.to_dict()
    print("[OK] scope mismatch disclosed", result.disclosed_labels)


def test_progress_merges_global_reconciliation():
    plan = ExecutionPlan(
        steps=[
            PlanStep(step_type="research", description="a", task_id="t_worker_a", objective="A"),
            PlanStep(step_type="research", description="b", task_id="t_worker_b", objective="B"),
            PlanStep(step_type="summarize", description="s", task_id="t_synth", objective="s"),
        ],
        planning_mode="dynamic",
    )
    for step in plan.steps:
        if step.step_type == "research":
            step.metadata["status"] = "done"
    # 同等权威二级来源 → 应留下 unresolved，供 Progress 合并
    rows = [
        {
            "task_id": "t_worker_a",
            "ok": True,
            "summary": "Tesla FY2026 total revenue 10B",
            "payload": {
                "facts": ["Tesla FY2026 total revenue 10亿美元"],
                "sources": ["https://www.bloomberg.com/a"],
                "confidence": 0.7,
                "findings": [
                    {
                        "claim": "Tesla FY2026 total revenue 10亿美元",
                        "confidence": 0.7,
                        "source_quality": "secondary",
                        "evidence_ids": ["e1"],
                    }
                ],
            },
        },
        {
            "task_id": "t_worker_b",
            "ok": True,
            "summary": "Tesla FY2026 total revenue 8B",
            "payload": {
                "facts": ["Tesla FY2026 total revenue 8亿美元"],
                "sources": ["https://www.reuters.com/b"],
                "confidence": 0.7,
                "findings": [
                    {
                        "claim": "Tesla FY2026 total revenue 8亿美元",
                        "confidence": 0.7,
                        "source_quality": "secondary",
                        "evidence_ids": ["e2"],
                    }
                ],
            },
        },
    ]
    for row in rows:
        row["payload"]["facts"].append("2026 已量产交付客户订单充足")
        row["payload"]["summary"] = row["summary"] + " 商业化进展明确"
    recon = reconcile_worker_results(rows)
    assert recon.unresolved_labels, recon.to_dict()
    assessment = assess_progress(
        plan,
        task_status={"t_worker_a": "done", "t_worker_b": "done", "t_synth": "pending"},
        worker_results=rows,
        query="比较 Tesla 收入",
        reconciliation=recon,
    )
    assert assessment.unresolved_conflicts, assessment.to_dict()
    assert assessment.verdict in {"gap", "enough", "run"}
    print("[OK] progress merges reconciliation", assessment.verdict, assessment.unresolved_conflicts[:2])


def test_conflict_disclosure_gate():
    state = LoopState(session_id="s_gate")
    state.intent = TaskIntent(raw_query="q", deliverable="text", summary="s")
    state.metadata["claim_reconciliation"] = {
        "unresolved_labels": ["Tesla revenue 8 vs 10"],
        "disclosed_labels": [],
    }
    state.final_content = "Tesla 收入很好，综合约为 9。"
    outcome = ResultValidator().validate_conflict_disclosure(state)
    assert outcome.passed is False
    assert outcome.reason == "unsupported_reconciled_value"

    state.final_content = "Tesla 2026 收入表现强劲。"
    outcome2 = ResultValidator().validate_conflict_disclosure(state)
    assert outcome2.passed is False
    assert outcome2.reason == "conflict_not_disclosed"

    state.final_content = (
        "关于 Tesla 2026 revenue 存在未解决冲突：来源 A 报告 8B，来源 B 报告 10B，"
        "口径未能确认，因此当前无法可靠判断哪个数字正确。"
    )
    ok = ResultValidator().validate_conflict_disclosure(state)
    assert ok.passed is True

    # expected_disagreement  alone 不硬失败
    state.metadata["claim_reconciliation"] = {
        "unresolved_labels": [],
        "disclosed_labels": ["automotive vs total"],
    }
    state.final_content = "汽车业务收入与总收入口径不同，分别引用如下。"
    assert ResultValidator().validate_conflict_disclosure(state).passed is True
    print("[OK] conflict disclosure gate")


def test_authority_prefers_primary_over_secondary():
    rows = _rows_cross_worker_revenue_conflict()
    result = reconcile_worker_results(rows)
    assert result.resolved_labels, result.to_dict()
    assert not result.unresolved_labels, result.to_dict()
    print("[OK] authority resolve", result.resolved_labels[:2])


def test_can_replan_independent_of_retry_budget():
    cfg = get_harness_config()
    state = LoopState(session_id="s_replan")
    state.plan = ExecutionPlan(steps=[PlanStep(step_type="research", description="x", task_id="t1")])
    state.retry_count = state.max_retries  # format retries exhausted
    state.replan_count = 0
    state.metadata["run_budget"] = {"max_replan_count": 2, "max_plan_steps": 8}
    assert can_replan(state, cfg) is True
    state.replan_count = 2
    assert can_replan(state, cfg) is False
    print("[OK] retry vs replan budgets decoupled")


if __name__ == "__main__":
    test_extract_and_detect_cross_worker_conflict()
    test_scope_mismatch_is_expected_disagreement()
    test_progress_merges_global_reconciliation()
    test_conflict_disclosure_gate()
    test_can_replan_independent_of_retry_budget()
    print("all claim conflict tests passed")
