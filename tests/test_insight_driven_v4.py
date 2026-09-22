"""Regression contract for the insight-driven v4 research path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.agent.harness.run_budget import RunBudgetManager
from app.research.brief.models import StructuredResearchBrief
from app.research.delivery.insights import build_insight_layer
from app.research.domain.completion import evaluate_completion
from app.research.execution.synthesis_executor import SynthesisExecutor, SynthesisRequest
from app.research.planning.brief_plan import execution_plan_from_brief, plan_coverage, validate_brief_plan
from app.research.runtime.worker import ResearchContext


def _brief() -> StructuredResearchBrief:
    return StructuredResearchBrief(
        brief_id="brief-v4",
        version=1,
        objective="研究 Agent 的当前热点、未来方向和不确定性",
        user_intent="trend_forecast",
        key_questions=("当前热点是什么？", "未来方向是什么？", "哪些判断仍不确定？"),
    )


def test_initial_plan_covers_every_key_question_with_counter_lane() -> None:
    brief = _brief()
    plan = execution_plan_from_brief(brief)
    coverage = plan_coverage(plan, brief)

    assert coverage.complete
    assert len(plan.steps) == 3
    assert not validate_brief_plan(plan, brief=brief)
    assert all("counter_evidence" in step.metadata["research_lanes"] for step in plan.steps)


def test_initial_research_cannot_borrow_targeted_repair_reserve() -> None:
    manager = RunBudgetManager(token_limit=1_000, llm_call_limit=20)
    assert manager.remaining_for_research_tokens() == 600
    assert manager.remaining_for_repair_tokens() == 750

    manager.commit_tokens(600)
    assert manager.research_allowed() == (False, "research_phase_token_cap")
    assert manager.repair_allowed() == (True, "")
    lease, reason = manager.reserve_worker_lease("repair-q1", stage="repair", token_ceiling=100)
    assert lease and not reason


def test_insight_layer_only_creates_mechanism_from_multiple_grounded_signals() -> None:
    layer = build_insight_layer([
        {"claim": "企业正在部署可观测 Agent runtime。", "evidence_ids": ["e1"], "criterion_id": "q1"},
        {"claim": "评测和权限控制成为生产落地的共同要求。", "evidence_ids": ["e2"], "criterion_id": "q1"},
        {"claim": "没有来源的观点不能形成信号。", "criterion_id": "q2"},
    ])
    assert len(layer["signals"]) == 2
    assert len(layer["mechanisms"]) == 1
    assert layer["mechanisms"][0]["evidence_refs"] == ["e1", "e2"]


def test_completion_requires_authoritative_evidence_for_each_question() -> None:
    result = evaluate_completion(
        brief={"key_questions": ["问题一", "问题二"]},
        answer_contract={"answers": [
            {"question_id": "q1", "direct_answer": "回答一", "evidence_refs": ["e1"]},
            {"question_id": "q2", "direct_answer": "回答二", "evidence_refs": ["e2"]},
        ]},
        evidence_records=[
            {"evidence_id": "e1", "source_tier": "PRIMARY", "authority_score": 0.9},
            {"evidence_id": "e2", "source_tier": "SECONDARY", "authority_score": 0.55},
        ],
        final_content="完整回答 [1] [2]",
        require_authoritative_per_question=True,
    )
    assert not result.passed
    assert "q2:authoritative_evidence_missing" in result.unresolved_blocking


class _EmptyModel:
    async def ainvoke(self, *args, **kwargs):
        return ""


def test_provider_empty_content_is_explicit_retryable_telemetry() -> None:
    executor = SynthesisExecutor(
        SimpleNamespace(synthesis_model=_EmptyModel(), harness_config=SimpleNamespace(synthesis_step_timeout_sec=5)),
        SimpleNamespace(budget_manager=RunBudgetManager(token_limit=10_000, llm_call_limit=10)),
    )
    result = asyncio.run(executor.execute(
        SynthesisRequest(mode="normal", evidence_refs=["e1"]),
        ResearchContext(run_id="r", query="q", session_id="s"),
    ))
    assert result.fail_reason == "provider_empty_content"
    assert result.metadata["provider_failure_class"] == "provider_empty_content"
    assert result.metadata["retryable"] is True
