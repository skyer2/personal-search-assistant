from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage

from app.agent.harness.artifacts import reset_artifact_store, set_artifact_store, ArtifactStore
from app.agent.harness.run_budget import RunBudgetManager
from app.agent.harness.token_counter import estimate_tokens
from app.research.delivery.partial_renderer import render_partial_delivery
from app.research.delivery.synthesis_context import EvidenceDigest, SynthesisContextBuilder
from app.research.execution.synthesis_executor import SynthesisExecutor, SynthesisRequest
from app.research.runtime.worker import ResearchContext


class FakeConfig:
    step_timeout_sec = 10
    synthesis_step_timeout_sec = 10


class FakeHarness:
    harness_config = FakeConfig()


class FailingModel:
    def __init__(self, error: Exception | None = None):
        self.error = error

    async def ainvoke(self, *args: Any, **kwargs: Any):
        if self.error is not None:
            raise self.error
        return ""


class FakeSession:
    budget_manager = RunBudgetManager(token_limit=100_000, llm_call_limit=10)
    ctx = SimpleNamespace(citation_manager=None)


def _request(token_budget: int = 40_000) -> SynthesisRequest:
    return SynthesisRequest(
        mode="degraded",
        evidence_refs=["ev-1"],
        research_summary="x" * 20_000,
        token_budget=token_budget,
    )


def _context() -> ResearchContext:
    return ResearchContext(run_id="r", query="q", session_id="s")


def test_synthesis_context_deduplicates_and_uses_artifact_digest(tmp_path: Path):
    store = ArtifactStore(session_dir=tmp_path)
    set_artifact_store(store)
    try:
        artifact = store.put(
            "DeepSeek is an AI company.",
            kind="web",
            locator="https://example.com/deepseek",
            title="DeepSeek",
        )
        gstate = {
            "evidence_refs": [artifact.artifact_id, artifact.artifact_id],
            "findings": [
                {"finding_id": "f1", "claim": "DeepSeek is an AI company.", "evidence_ids": [artifact.artifact_id]},
                {"finding_id": "f2", "claim": "DeepSeek is an AI company.", "evidence_ids": [artifact.artifact_id]},
            ],
            "worker_results": [
                {"task_id": "t1", "summary": "Collected evidence."},
                {"task_id": "t1", "summary": "Collected evidence."},
            ],
        }
        context = SynthesisContextBuilder(FakeHarness(), FakeSession()).build(gstate)
        assert context.evidence_refs == (artifact.artifact_id,)
        assert len(context.findings) == 1
        assert len(context.worker_summaries) == 1
        assert context.evidence_digests[0].locator == "https://example.com/deepseek"
    finally:
        reset_artifact_store()


def test_synthesis_prompt_is_token_bounded():
    executor = SynthesisExecutor(FakeHarness(), FakeSession())
    prompt = executor._prompt(_request(token_budget=1_000), _context())
    assert estimate_tokens(prompt) <= 1_000


def test_synthesis_failure_taxonomy():
    cases = {
        "429 rate limit": "provider_rate_limit",
        "usage limit exceeded": "provider_usage_limit",
        "invalid auth": "provider_auth",
        "bad request": "provider_bad_request",
        "content_filter": "provider_content_filter",
        "context_length_exceeded": "context_length_exceeded",
        "budget_tokens": "budget_tokens",
        "budget_llm_calls": "budget_llm_calls",
        "provider unavailable": "provider_unavailable",
        "stream error": "stream_error",
    }
    for message, expected in cases.items():
        result = _run_failure(message)
        assert result.fail_reason == expected


def _run_failure(message: str):
    harness = FakeHarness()
    harness.synthesis_model = FailingModel(RuntimeError(message))
    return asyncio.run(
        SynthesisExecutor(harness, FakeSession()).execute(
            SynthesisRequest(mode="degraded", evidence_refs=["ev"]), _context()
        )
    )


def test_empty_synthesis_is_retryable_taxonomy():
    harness = FakeHarness()
    harness.synthesis_model = FailingModel(None)
    result = asyncio.run(
        SynthesisExecutor(harness, FakeSession()).execute(
            SynthesisRequest(mode="degraded", evidence_refs=["ev"]), _context()
        )
    )
    assert result.fail_reason == "empty_content"


def test_partial_renderer_discloses_limitations_and_never_claims_success():
    content = render_partial_delivery(
        objective="评估公司",
        findings=[{"claim": "Company A raised funding.", "evidence_ids": ["ev-1"]}],
        evidence_digests=[EvidenceDigest("ev-1", "Source", "https://example.com", "excerpt")],
        worker_summaries=[{"task_id": "t1", "summary": "Collected evidence."}],
        semantic_gaps=["gap_candidate_pool"],
        limitations=["research_token_cap"],
        unresolved_conflicts=[],
        worker_failure_reasons=["research_token_cap"],
        synthesis_failure_reason="synthesis_timeout",
    )
    assert content
    assert "部分研究结果" in content
    assert "执行限制" in content
    assert "不能视为完整成功" in content
    assert render_partial_delivery(
        objective="q",
        findings=[],
        evidence_digests=[],
        worker_summaries=[],
        semantic_gaps=[],
        limitations=[],
        unresolved_conflicts=[],
        worker_failure_reasons=[],
        synthesis_failure_reason="provider_auth",
    ) == ""


def test_research_cap_still_reserves_synthesis_tokens():
    manager = RunBudgetManager(token_limit=100_000, llm_call_limit=10)
    manager.commit_tokens(manager.phase_plan.research_cap_tokens(100_000))
    assert manager.research_allowed() == (False, "research_token_cap")
    reservation, reason = manager.reserve_llm_call(estimated_tokens=1_000, phase="synthesis")
    assert reservation
    assert reason == ""
