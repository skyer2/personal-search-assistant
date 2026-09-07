"""Control-plane invariants for budget, semantics, quality, and trace state."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.research_brief import attach_brief, compile_research_brief
from app.agent.harness.run_budget import BudgetReservationError, PhaseBudgetPlan, RunBudgetManager
from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent
from app.agent.harness.step_budget import consume_retrieval_or_block, retrieval_budget
from app.agent.harness.usage_tracker import (
    UsageTrackingCallback,
    bind_budget_manager,
    bind_worker_budget_scope,
)
from app.observability.journal import summarize_trace
from app.research.planning.lead_planner import heuristic_dynamic_plan
from app.research.planning.policy import parse_source_policy
from app.research.planning.validator import validate_hybrid_plan
from app.research.runtime.findings import normalize_findings
from app.research.runtime.graph import finalize_node, route_after_quality
from app.research.runtime.scheduler import research_only_plan


def test_parallel_worker_leases_cannot_overcommit_research_pool() -> None:
    manager = RunBudgetManager(
        token_limit=100,
        llm_call_limit=20,
        tool_call_limit=20,
        deadline_sec=600,
        phase_plan=PhaseBudgetPlan(
            understand_plan=0.0,
            research=0.6,
            replan=0.0,
            synthesis=0.4,
            quality=0.0,
        ),
        max_parallel_workers=2,
    )
    lease_one, reason_one = manager.reserve_worker_lease("t1")
    lease_two, reason_two = manager.reserve_worker_lease("t2")
    assert lease_one and lease_two
    assert not reason_one and not reason_two

    call_one, call_one_reason = manager.reserve_llm_call(
        estimated_tokens=25, worker_task_id="t1", phase="execute"
    )
    assert call_one and not call_one_reason
    manager.commit_llm_usage(call_one, 25)
    call_two, _ = manager.reserve_llm_call(
        estimated_tokens=25, worker_task_id="t2", phase="execute"
    )
    assert call_two
    manager.commit_llm_usage(call_two, 25)
    assert manager.snapshot().used_tokens == 50
    assert manager.snapshot().reserved_tokens == 10

    blocked, blocked_reason = manager.reserve_llm_call(
        estimated_tokens=10, worker_task_id="t2", phase="execute"
    )
    assert not blocked
    assert blocked_reason == "research_token_cap"

    lease_three, lease_three_reason = manager.reserve_worker_lease("t3")
    assert not lease_three
    assert lease_three_reason == "research_token_cap"


def test_exact_token_exhaustion_reason_is_not_wall_deadline() -> None:
    manager = RunBudgetManager(
        token_limit=100,
        deadline_sec=600,
        phase_plan=PhaseBudgetPlan(
            understand_plan=0.0,
            research=0.6,
            replan=0.0,
            synthesis=0.4,
            quality=0.0,
        ),
    )
    manager.commit_tokens(60)
    assert manager.exhaustion_reason() == "research_token_cap"
    assert manager.force_synthesis() is True
    assert manager.remaining_run_sec() > 0


def test_llm_callback_reserves_before_request_and_commits_actual_usage() -> None:
    manager = RunBudgetManager(token_limit=10_000, llm_call_limit=5, deadline_sec=600)
    callback = UsageTrackingCallback(session_id="s_budget", phase="execute")
    with bind_budget_manager(manager), bind_worker_budget_scope("t_worker"):
        lease_id, _ = manager.reserve_worker_lease("t_worker", token_ceiling=9_000)
        assert lease_id
        callback.on_llm_start({}, ["short prompt"], run_id="r1")
        response = SimpleNamespace(
            llm_output={"model_name": "test", "token_usage": {"total_tokens": 12}},
            generations=[],
        )
        callback.on_llm_end(response, run_id="r1")
        manager.release_worker_lease(lease_id)

    snapshot = manager.snapshot()
    assert snapshot.used_tokens == 12
    assert snapshot.llm_calls == 1
    assert snapshot.reserved_tokens == 0
    assert snapshot.active_worker_leases == 0


def test_llm_callback_blocks_request_before_provider_call() -> None:
    manager = RunBudgetManager(token_limit=10, llm_call_limit=1, deadline_sec=600)
    callback = UsageTrackingCallback(session_id="s_block", phase="plan")
    with bind_budget_manager(manager):
        with pytest.raises(BudgetReservationError) as exc_info:
            callback.on_llm_start({}, ["x" * 100], run_id="r_block")
    assert exc_info.value.reason == "budget_tokens"
    assert manager.snapshot().reserved_llm_calls == 0


def test_retrieval_tool_reserves_global_tool_quota() -> None:
    manager = RunBudgetManager(token_limit=100, tool_call_limit=1, deadline_sec=600)
    with retrieval_budget(2), bind_budget_manager(manager):
        assert consume_retrieval_or_block("internet_search") is None
        blocked = consume_retrieval_or_block("internet_search")
    assert blocked is not None
    assert manager.snapshot().tool_calls == 1


def test_category_aliases_use_one_discovery_worker() -> None:
    brief = compile_research_brief(
        task_query="国内有哪些值得加入的 AI 初创公司？"
    )
    assert brief.task_kind == "landscape_discovery"
    assert brief.subjects[0].subject_id == "china_ai_startups"

    intent = attach_brief(
        TaskIntent(raw_query="国内 AI 初创公司 / 国内AI初创公司", summary="候选发现")
    )
    assert len(intent.brief.subjects) == 1
    assert intent.brief.subjects[0].subject_id == "china_ai_startups"


def test_landscape_plan_is_discovery_and_comparison_is_synthesis() -> None:
    query = "国内有哪些值得加入的 AI 初创公司？"
    intent = attach_brief(TaskIntent(raw_query=query, summary=query))
    plan = research_only_plan(heuristic_dynamic_plan(intent, parse_source_policy(query)))
    research_steps = [step for step in plan.steps if step.step_type == "research"]
    assert len(research_steps) == 1
    assert research_steps[0].metadata["task_kind"] == "discovery"
    assert research_steps[0].metadata["subject_id"] == "china_ai_startups"
    assert research_steps[0].metadata["produces_artifact"] == "candidate_set"
    assert not any(step.task_id == "t_compare" for step in plan.steps)
    assert validate_hybrid_plan(intent, plan) == []

    comparison_query = "DeepSeek / Moonshot / MiniMax 谁更值得加入？"
    comparison_intent = attach_brief(
        TaskIntent(raw_query=comparison_query, summary=comparison_query)
    )
    comparison_plan = heuristic_dynamic_plan(
        comparison_intent, parse_source_policy(comparison_query)
    )
    assert comparison_intent.brief.task_kind == "comparison"
    assert {
        step.metadata["subject_id"] for step in comparison_plan.steps if step.step_type == "research"
    } == {"deepseek", "moonshot", "minimax"}
    assert not any(step.task_id == "t_compare" for step in comparison_plan.steps)


def test_validator_rejects_category_deep_dive_and_comparison_worker() -> None:
    query = "国内有哪些值得加入的 AI 初创公司？"
    intent = attach_brief(TaskIntent(raw_query=query, summary=query))
    deep_dive = PlanStep(
        step_type="research",
        task_id="t_deep_dive",
        description="deep dive",
        objective="deep dive",
        metadata={"task_kind": "deep_dive", "subject_id": "china_ai_startups", "required": True},
    )
    plan = ExecutionPlan(
        steps=[
            deep_dive,
        ]
    )
    issues = validate_hybrid_plan(intent, plan)
    assert "category_requires_discovery:t_deep_dive" in issues

    compare = PlanStep(
        step_type="research",
        task_id="t_compare",
        description="横向比较",
        objective="横向比较",
        depends_on=["t_a", "t_b"],
        metadata={"task_kind": "comparison", "required": True},
    )
    comparison_plan = ExecutionPlan(steps=[compare])
    comparison_issues = validate_hybrid_plan(intent, comparison_plan)
    assert "comparison_is_synthesis:t_compare" in comparison_issues


def test_finding_normalizer_rejects_invalid_and_normalizes_worker_shape() -> None:
    findings, rejected = normalize_findings(
        [
            {"task_id": "t1", "summary": "候选公司 A 完成 B 轮融资", "artifact_id": "art-1"},
            {"task_id": "t1"},
            "候选公司 B 已推出商用产品",
        ],
        task_id="t1",
        subject_id="china_ai_startups",
        dimension="融资与估值",
    )
    assert len(findings) == 2
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "missing_claim_or_task_id"
    for finding in findings:
        assert finding["task_id"] == "t1"
        assert finding["subject_id"] == "china_ai_startups"
        assert finding["dimension"] == "融资与估值"
        assert finding["claim"]
        assert finding["summary"]
        assert finding["status"] in {"supported", "partial", "conflicted"}


def test_quality_failure_routes_conditionally_and_partial_is_preserved() -> None:
    assert (
        route_after_quality(
            {
                "quality_assessment": {
                    "verdict": "fail",
                    "repairable": True,
                    "suggested_action": "repair",
                },
            }
        )
        == "repair_synthesis"
    )
    assert (
        route_after_quality(
            {
                "quality_assessment": {
                    "verdict": "fail",
                    "repairable": False,
                    "suggested_action": "",
                },
                "final_content": "partial answer",
            }
        )
        == "finalize"
    )
    assert (
        finalize_node(
            {
                "phase": "quality",
                "quality_assessment": {"verdict": "fail"},
                "evidence_assessment": {"status": "sufficient"},
                "final_content": "partial answer",
            }
        )["termination"]["outcome"]
        == "partial"
    )


def test_trace_summary_exposes_quality_and_termination_attribution() -> None:
    events = [
        {
            "type": "run.started",
            "event_id": "e1",
            "span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 1,
            "timestamp": "2026-09-06T00:00:00Z",
        },
        {
            "type": "quality.assessed",
            "event_id": "e2",
            "span_id": "quality",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 2,
            "status": "fail",
            "phase": "quality",
            "timestamp": "2026-09-06T00:00:01Z",
            "attributes": {
                "passed": False,
                "reason": "citation_coverage_low",
                "repairable": True,
                "repair_action": "partial",
                "metric": "quality_gate",
            },
        },
        {
            "type": "run.completed",
            "event_id": "e3",
            "span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 3,
            "status": "partial",
            "timestamp": "2026-09-06T00:00:02Z",
            "attributes": {
                "metadata": {
                    "termination": {
                        "status": "partial",
                        "reason": "research_token_cap",
                        "origin_stage": "research",
                        "detected_stage": "dispatch",
                        "cause_event_id": "budget-1",
                        "causal_chain": [
                            "worker wave overconsumed tokens",
                            "research_token_cap",
                            "force_synthesis",
                            "quality_failed",
                        ],
                    }
                }
            },
        },
    ]
    summary = summarize_trace(events)
    assert summary["event_count"] == 3
    assert summary["quality"]["reason"] == "citation_coverage_low"
    assert summary["quality"]["repairable"] is True
    assert summary["termination"]["reason"] == "research_token_cap"
    assert summary["failure_origin"]["origin_stage"] == "research"
    assert summary["failure_origin"]["cause_event_id"] == "budget-1"
