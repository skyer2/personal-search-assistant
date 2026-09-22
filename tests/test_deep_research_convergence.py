from __future__ import annotations

import time
import asyncio
from datetime import datetime, timedelta, timezone

from app.agent.harness.step_budget import (
    FINALIZE_JSON_MESSAGE,
    STOP_JSON_MESSAGE,
    arm_worker_soft_finalization,
    consume_search_queries_or_block,
    worker_retrieval_budget,
)
from app.agent.harness.usage_tracker import (
    bind_budget_manager,
    bind_worker_budget_scope,
    wrap_model_with_budget,
)
from app.research.brief.models import (
    FreshnessRequirements,
    SourceRequirements,
    StructuredResearchBrief,
)
from app.research.claims.models import ClaimRecord
from app.research.claims.reconcile import detect_conflict_edges
from app.research.claims.resolve import resolve_edges
from app.research.coverage.judge import CoverageGap, CoverageJudgement, judge_coverage
from app.research.execution.synthesis_executor import SynthesisExecutor, SynthesisRequest
from app.research.findings.models import ResearchFinding
from app.research.runtime.worker import ResearchContext
from app.research.supervisor.agent import SupervisorAgent


def _brief(
    *,
    primary_required: bool = False,
    fresh_required: bool = False,
) -> StructuredResearchBrief:
    return StructuredResearchBrief(
        brief_id="brief-convergence",
        version=1,
        objective="Evaluate Company A",
        user_intent="research",
        key_questions=("What is Company A's commercial traction?",),
        success_criteria=("The answer directly aligns with the user goal.",),
        source_requirements=SourceRequirements(
            min_independent_sources=2,
            primary_required=primary_required,
        ),
        freshness_requirements=FreshnessRequirements(
            required=fresh_required,
            time_horizon="recent",
        ),
    )


def _finding() -> ResearchFinding:
    return ResearchFinding(
        finding_id="finding-1",
        task_id="task-1",
        summary="Company A commercial traction",
        claims=("Company A commercial traction",),
        evidence_ids=("evidence-1", "evidence-2"),
        supported_criteria=("What is Company A's commercial traction?",),
    )


def _validated_claim() -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "text": "Company A commercial traction",
        "criterion_id": "What is Company A's commercial traction?",
        "validated": True,
        "evidence_ids": ["evidence-1", "evidence-2"],
    }


def _evidence(
    *,
    primary: bool = True,
    published: str | None = None,
) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": "evidence-1",
            "source_id": "company-a.com",
            "source_tier": "PRIMARY" if primary else "SECONDARY",
            "published_at": published or "",
        },
        {
            "evidence_id": "evidence-2",
            "source_id": "reuters.com",
            "source_tier": "PRIMARY" if primary else "SECONDARY",
            "published_at": published or "",
        },
    ]


def test_coverage_uses_key_questions_and_lexical_only_is_partial() -> None:
    finding = ResearchFinding(
        finding_id="finding-lexical",
        task_id="task-1",
        summary="Unrelated wording but commercial traction appears here",
        claims=("Unrelated claim",),
        evidence_ids=("evidence-1", "evidence-2"),
        supported_criteria=(),
    )
    judgement = judge_coverage(_brief(), [finding], evidence=_evidence())

    assert len(judgement.criteria) == 1
    assert judgement.criteria[0].status == "unsupported"
    assert "direct_criterion_binding" in judgement.criteria[0].missing_evidence_types
    assert judgement.sufficient is False


def test_primary_and_freshness_requirements_gate_supported() -> None:
    primary_missing = judge_coverage(
        _brief(primary_required=True),
        [_finding()],
        claims=[_validated_claim()],
        evidence=_evidence(primary=False, published=_recent_date()),
    )
    assert primary_missing.criteria[0].status == "partial"
    assert "primary_source" in primary_missing.criteria[0].missing_evidence_types

    stale = judge_coverage(
        _brief(fresh_required=True),
        [_finding()],
        claims=[_validated_claim()],
        evidence=_evidence(published="2020-01-01T00:00:00+00:00"),
    )
    assert stale.criteria[0].status == "partial"
    assert "fresh_evidence" in stale.criteria[0].missing_evidence_types

    supported = judge_coverage(
        _brief(primary_required=True, fresh_required=True),
        [_finding()],
        claims=[_validated_claim()],
        evidence=_evidence(published=_recent_date()),
    )
    assert supported.criteria[0].status == "supported"
    assert supported.sufficient is True
    assert supported.gaps == ()


def test_dangling_evidence_reference_is_not_counted_and_does_not_crash() -> None:
    judgement = judge_coverage(
        _brief(primary_required=True),
        [_finding()],
        claims=[_validated_claim()],
        evidence=_evidence(primary=False, published=_recent_date())[:1],
    )

    assert judgement.criteria[0].status == "partial"
    assert "primary_source" in judgement.criteria[0].missing_evidence_types


def test_blocking_conflict_prevents_sufficient_and_creates_gap() -> None:
    judgement = judge_coverage(
        _brief(),
        [_finding()],
        claims=[_validated_claim()],
        evidence=_evidence(),
        claim_conflicts=[{"edge_id": "edge-1"}],
        claim_resolutions=[
            {
                "edge_id": "edge-1",
                "status": "unresolved",
                "blocking": True,
                "criterion_id": "What is Company A's commercial traction?",
            }
        ],
    )

    assert judgement.criteria[0].status == "conflicted"
    assert judgement.sufficient is False
    assert judgement.gaps[0].blocking_conflict_ids == ("edge-1",)
    assert "conflict_resolution" in judgement.gaps[0].missing_evidence_type


def test_supervisor_consumes_exact_coverage_gap() -> None:
    judgement = CoverageJudgement(
        sufficient=False,
        status="gap",
        gaps=(
            CoverageGap(
                gap_id="gap-r3",
                criterion_id="coverage-r3",
                description="R3 needs a primary source",
                current_evidence_ids=("evidence-1",),
                missing_evidence_type=("primary_source",),
                priority="high",
            ),
        ),
    )
    action = SupervisorAgent(agent=None).fallback_action(
        _brief(),
        judgement,
        {},
    )

    assert action.action == "CONDUCT_RESEARCH"
    assert len(action.research_tasks) == 1
    task = action.research_tasks[0]
    assert task.criterion_id == "coverage-r3"
    assert task.gap_id == "gap-r3"
    assert task.target_criteria == ("coverage-r3",)
    assert "primary_source" in task.missing_evidence_types


def test_soft_deadline_allows_first_retrieval_then_blocks_new_search() -> None:
    with worker_retrieval_budget(
        search_queries=10,
        fetch_sources=10,
        tool_invocations=10,
        soft_deadline_at=time.monotonic() - 0.001,
    ) as budget:
        first = consume_search_queries_or_block(1)
        blocked = consume_search_queries_or_block(1)

    assert first is None
    assert blocked == FINALIZE_JSON_MESSAGE
    assert getattr(blocked, "reason") == "soft_deadline_finalize"
    assert budget.search_queries_used == 1
    assert budget.tool_invocations_used == 1


def test_existing_worker_artifact_allows_soft_finalization_before_search() -> None:
    from app.agent.harness.artifacts import ArtifactStore, set_artifact_store
    from app.agent.harness.usage_tracker import bind_worker_execution_scope

    store = ArtifactStore()
    set_artifact_store(store)
    store.put(
        "Previously collected evidence.",
        kind="web",
        locator="https://example.com/source",
        metadata={"run_id": "run-existing", "task_id": "task-existing"},
    )
    with worker_retrieval_budget(
        search_queries=10,
        fetch_sources=10,
        tool_invocations=10,
        soft_deadline_at=time.monotonic() - 0.001,
    ) as budget:
        with bind_worker_execution_scope(
            "task-existing", step_index=0, run_id="run-existing", session_id="session-existing"
        ):
            blocked = consume_search_queries_or_block(1)

    assert getattr(blocked, "reason") == "soft_deadline_finalize"
    assert budget.admitted_evidence_count >= 1
    assert budget.search_queries_used == 0


def test_soft_finalization_tool_result_is_not_budget_denied() -> None:
    from app.agent.harness.step_budget import BudgetBlock
    from app.tools.tavily_tool import _denied as search_result
    from app.tools.fetch_url import _denied as fetch_result
    from app.tools.batch_retrieval import _denied as batch_result

    soft = BudgetBlock(
        FINALIZE_JSON_MESSAGE,
        resource="search_query",
        reason="soft_deadline_finalize",
        used=1,
        limit=10,
    )
    hard = BudgetBlock(
        STOP_JSON_MESSAGE,
        resource="search_query",
        reason="search_query_cap",
        used=10,
        limit=10,
    )
    for serialize in (search_result, fetch_result, batch_result):
        assert serialize(soft)["error"] == "finalization_requested"
        hard_result = serialize(hard)
        assert hard_result["error"] == "budget_denied"
        assert hard_result["scope"] == "worker"
        assert hard_result["reserved"] == 0


def test_committed_llm_usage_arms_soft_token_finalization() -> None:
    class LeaseManager:
        def worker_lease_snapshot(self, task_id: str) -> dict[str, int]:
            assert task_id == "task-soft-finalize"
            return {
                "llm_calls_used": 1,
                "llm_calls_limit": 8,
                "tokens_used": 8_100,
                "token_limit": 10_000,
            }

    with (
        worker_retrieval_budget(
            search_queries=10,
            fetch_sources=10,
            tool_invocations=10,
        ) as budget,
        bind_budget_manager(LeaseManager()),
        bind_worker_budget_scope("task-soft-finalize"),
    ):
        budget.admitted_evidence_count = 1
        reason = arm_worker_soft_finalization()
        blocked = consume_search_queries_or_block(1)

    assert reason == "soft_budget_finalize"
    assert blocked == FINALIZE_JSON_MESSAGE
    assert budget.finalization_reason == "soft_budget_finalize"
    assert budget.search_queries_used == 0


def test_projected_llm_call_reserves_finalization_capacity() -> None:
    class LeaseManager:
        def worker_lease_snapshot(self, task_id: str) -> dict[str, int]:
            assert task_id == "task-dynamic-reserve"
            return {
                "llm_calls_used": 4,
                "llm_calls_limit": 16,
                "tokens_used": 50_000,
                "token_limit": 80_000,
            }

    with (
        worker_retrieval_budget(
            search_queries=10,
            fetch_sources=10,
            tool_invocations=10,
        ) as budget,
        bind_budget_manager(LeaseManager()),
        bind_worker_budget_scope("task-dynamic-reserve"),
    ):
        budget.admitted_evidence_count = 1
        reason = arm_worker_soft_finalization(
            projected_tokens=20_000,
            projected_llm_calls=1,
        )

    assert reason == "soft_budget_finalize"
    assert budget.finalization_reason == "soft_budget_finalize"


def test_budgeted_model_call_injects_finalization_instruction() -> None:
    class LeaseManager:
        def worker_lease_snapshot(self, task_id: str) -> dict[str, int]:
            assert task_id == "task-finalize-prompt"
            return {
                "llm_calls_used": 1,
                "llm_calls_limit": 8,
                "tokens_used": 9_200,
                "token_limit": 10_000,
            }

        def worker_output_limit(self, task_id: str) -> int:
            return 256

        def reserve_llm_call(self, **kwargs: int | str) -> tuple[str, str]:
            return "reservation", ""

        def commit_llm_usage(self, reservation_id: str, actual_tokens: int) -> None:
            assert reservation_id == "reservation"

    class CapturingModel:
        prompt: object = None

        async def ainvoke(self, prompt: object, config: object = None) -> str:
            self.prompt = prompt
            return "ok"

    model = CapturingModel()
    wrapped = wrap_model_with_budget(model)
    original_prompt = [{"role": "user", "content": "Summarize evidence."}]

    with (
        worker_retrieval_budget(
            search_queries=10,
            fetch_sources=10,
            tool_invocations=10,
        ) as budget,
        bind_budget_manager(LeaseManager()),
        bind_worker_budget_scope("task-finalize-prompt"),
    ):
        budget.admitted_evidence_count = 1
        result = asyncio.run(wrapped.ainvoke(original_prompt))

    assert result == "ok"
    assert len(model.prompt) == 2
    assert "Finalization Mode" in str(getattr(model.prompt[-1], "content", ""))
    assert budget.finalization_reason == "soft_budget_finalize"


def test_retrieval_hard_cap_remains_runaway_protection() -> None:
    with worker_retrieval_budget(
        search_queries=1,
        fetch_sources=1,
        tool_invocations=1,
    ) as budget:
        assert consume_search_queries_or_block(1) is None
        blocked = consume_search_queries_or_block(1)

    assert blocked == STOP_JSON_MESSAGE
    assert getattr(blocked, "reason") == "search_query_cap"
    assert budget.search_queries_used == 1


def test_bounded_semantic_conflict_binds_required_criterion() -> None:
    left = ClaimRecord(
        claim_id="claim-left",
        text="Company A is profitable",
        subject="Company A",
        criterion_id="coverage-r3",
    )
    right = ClaimRecord(
        claim_id="claim-right",
        text="Company A is loss-making",
        subject="Company A",
        criterion_id="coverage-r3",
    )
    edges = detect_conflict_edges([left, right])
    resolution = resolve_edges([left, right], edges).resolutions[0]

    assert edges[0].reason == "semantic_polarity_mismatch"
    assert resolution.status == "unresolved"
    assert resolution.criterion_id == "coverage-r3"
    assert resolution.blocking is True


def test_synthesis_prompt_consumes_structured_conflict_contract() -> None:
    request = SynthesisRequest(
        mode="degraded",
        unresolved_conflicts=["Company A traction conflicts"],
        conflict_resolutions=[
            {
                "edge_id": "edge-1",
                "status": "unresolved",
                "blocking": True,
                "criterion_id": "coverage-r3",
                "label": "Company A traction conflicts",
            }
        ],
    )
    prompt = SynthesisExecutor(harness=None, session=None)._prompt(
        request,
        ResearchContext(run_id="run", query="Evaluate Company A"),
    )

    assert "unresolved 只能披露不确定性" in prompt
    assert "edge-1" in prompt
    assert "blocking=True" in prompt


def _recent_date() -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
