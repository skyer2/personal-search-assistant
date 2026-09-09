"""Release gate for the deterministic simple-fact path."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]

from app.agent.harness.citations import SourceTier, classify_source_tier
from app.agent.harness.citations import CitationManager
from app.agent.harness.loop import AgentHarness
from app.agent.harness.state import HarnessResult, LoopState
from app.agent.harness.validator import ResultValidator
from app.config.loader import get_harness_config
from app.observability import get_recorder
from app.run_store import get_run_store, reset_run_store
from app.research.runtime import runner as runner_module
from app.research.routing.mode_router import route
from app.research.routing.task_shape import TaskShape, execution_profile_for_shape
from app.research.evidence.policy import registrable_domain
from app.research.runtime.simple_fact import render_simple_fact_answer


_CASES = [
    (
        "DeepSeek V3 发布时间？",
        "https://api-docs.deepseek.com/news/news251236",
        "DeepSeek V3 was officially released on 2024-12-26.",
        "2024年12月26日",
    ),
    (
        "Attention Is All You Need 哪年发表？",
        "https://arxiv.org/abs/1706.03762",
        "Attention Is All You Need was published in 2017.",
        "2017",
    ),
    (
        "LoRA 原论文是什么？",
        "https://arxiv.org/abs/2106.09685",
        "The original paper is LoRA: Low-Rank Adaptation of Large Language Models.",
        "LoRA: Low-Rank Adaptation of Large Language Models",
    ),
    (
        "DeepSeek-R1 发布时间？",
        "https://api-docs.deepseek.com/news/news250120",
        "DeepSeek-R1 was released on 2025-01-20.",
        "2025年1月20日",
    ),
    (
        "LangChain 首次发布是哪一年？",
        "https://docs.langchain.com/",
        "LangChain was first released in 2022.",
        "2022",
    ),
]


class FakeSearchTool:
    def __init__(self, url: str, content: str):
        self.url = url
        self.content = content

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "query": payload.get("query", ""),
            "answer": None,
            "results": [
                {
                    "title": "Trusted source",
                    "url": self.url,
                    "content": self.content,
                    "raw_content": self.content,
                }
            ],
            "provider": "fake",
        }


@pytest.mark.parametrize(("query", "url", "content", "expected"), _CASES)
def test_simple_fact_release_gate(query, url, content, expected, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.tools.tavily_tool.internet_search", FakeSearchTool(url, content)
    )
    config = get_harness_config()
    harness = AgentHarness(
        agent=None,
        project_root=ROOT,
        harness_config=config,
    )
    session_id = f"simple-fact-{tmp_path.name}"
    reset_run_store()
    get_recorder()._listeners = [get_run_store().on_event]
    try:
        result = asyncio.run(harness.run(query, session_id, mode="agent"))
    finally:
        reset_run_store()
        get_recorder()._listeners = []

    assert result.status == "success"
    assert expected in result.content
    assert "[1]" in result.content
    assert result.metadata["task_shape"] == "simple_fact"
    assert result.metadata["execution_path"] == "fast_path"
    assert result.metadata["planner_calls"] == 0
    assert result.metadata["replan_count"] == 0
    assert result.metadata["synthesis_calls"] == 0
    assert result.metadata["workers"] == 1
    assert result.metadata["tool_calls_count"] <= 3
    assert result.metadata["primary_sources"] >= 1
    assert result.metadata["budget_reservation_errors"] == 0
    assert result.metadata["partial_renderer_called"] is False
    assert result.metadata["outcome"] == "success"
    assert result.metadata["quality"] == "pass"


@pytest.mark.parametrize(
    ("query", "expected_shape"),
    [(case[0], "simple_fact") for case in _CASES],
)
def test_product_router_selects_fast_path(query: str, expected_shape: str):
    decision = route(query, user_mode="agent")
    assert decision.mode == "agent"
    assert decision.task_shape == expected_shape
    assert decision.execution_path == "fast_path"
    assert "task_shape:simple_fact" in decision.signals


def test_simple_fact_profile_has_deterministic_budget():
    profile = execution_profile_for_shape(TaskShape.SIMPLE_FACT)
    assert profile == {
        "parallel_workers": 1,
        "max_replan_count": 0,
        "max_tool_calls": 3,
        "max_search_queries": 2,
        "planner": False,
        "progress_eval": False,
        "compression": False,
        "synthesis_agent": False,
    }


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ("https://api-docs.deepseek.com/news", SourceTier.PRIMARY.value),
        ("https://arxiv.org/abs/1706.03762", SourceTier.PRIMARY.value),
        ("https://github.com/deepseek-ai/DeepSeek-V3", SourceTier.PRIMARY.value),
        ("https://www.reuters.com/article", SourceTier.HIGH_QUALITY_SECONDARY.value),
        ("https://blog.csdn.net/a", SourceTier.COMMUNITY.value),
        ("https://example.com/a", SourceTier.UNKNOWN.value),
        ("https://docs.random-blog.com/a", SourceTier.UNKNOWN.value),
        ("https://newsroom.anthropic.com/a", SourceTier.PRIMARY.value),
    ],
)
def test_source_tiers_are_not_url_equals_primary(locator: str, expected: str):
    assert classify_source_tier(locator) == expected


def test_high_quality_secondary_sources_must_be_independent():
    manager = CitationManager()
    manager.bind_worker_facts(
        0,
        "network_search",
        ["fact one", "fact two"],
        [
            "https://www.reuters.com/article-one",
            "https://reuters.com/article-two",
        ],
    )
    assert manager.source_counts_by_tier()[SourceTier.HIGH_QUALITY_SECONDARY.value] == 2
    assert manager.simple_fact_evidence_sufficient() is False

    manager.bind_worker_facts(
        0,
        "network_search",
        ["fact three"],
        ["https://www.bloomberg.com/article-three"],
    )
    assert manager.simple_fact_evidence_sufficient() is True


def test_registrable_domain_handles_multi_label_public_suffixes():
    assert registrable_domain("https://news.bbc.co.uk/story") == "bbc.co.uk"
    assert registrable_domain("https://www.economist.co.uk/story") == "economist.co.uk"


def test_renderer_cites_the_source_that_contains_the_extracted_fact():
    manager = CitationManager()
    manager.bind_worker_facts(
        0,
        "network_search",
        [
            "DeepSeek-V3 technical report describes the model.",
            "DeepSeek-V3 was released on 2024-12-26.",
        ],
        [
            "https://arxiv.org/abs/2412.19437",
            "https://www.reuters.com/technology/deepseek-v3",
        ],
    )
    worker_result = SimpleNamespace(
        facts=[source.bound_fact for source in manager.sources], summary="DeepSeek-V3"
    )

    answer = render_simple_fact_answer(
        query="DeepSeek V3 发布时间？",
        worker_result=worker_result,
        citation_manager=manager,
    )

    assert "2024年12月26日" in answer.content
    assert answer.source_id == manager.sources[1].source_id
    assert answer.supporting_fact == manager.sources[1].bound_fact
    assert "[2]" in answer.content


def test_synthesis_failure_cannot_quality_pass():
    class Harness:
        harness_config = get_harness_config()
        validator = ResultValidator()

        async def _phase_validate(self, *args: Any, **kwargs: Any) -> None:
            return None

    state = LoopState(session_id="terminal-quality")
    state.final_content = "已有答案 [1]"
    state.metadata["synthesis_failed"] = True
    session = SimpleNamespace(
        state=state,
        ctx=SimpleNamespace(
            session_dir=ROOT,
            citation_manager=None,
            deliverable_dir=None,
            run_dir=None,
        ),
    )
    graph_runner = runner_module.ResearchGraphRunner(Harness())
    runner_module._SESSIONS["terminal-quality"] = session
    try:
        update = asyncio.run(
            graph_runner.node_quality_gate(
                {
                    "run_id": "terminal-quality",
                    "phase": "synthesis",
                    "final_content": "已有答案 [1]",
                    "synthesis_failed": True,
                    "quality_attempts": 0,
                    "budget": {"max_replan_count": 0},
                }
            )
        )
    finally:
        runner_module._SESSIONS.pop("terminal-quality", None)
    assert update["quality_assessment"]["verdict"] == "fail"
    assert "synthesis_failed" in update["quality_assessment"]["issues"]


def test_workflow_termination_is_not_task_success_when_partial():
    class Harness:
        async def _phase_finalize(self, *args: Any, success: bool, **kwargs: Any):
            return HarnessResult(
                session_id="terminal-finalize",
                status="success" if success else "partial",
                content="partial content",
                trace=[],
                metadata={},
            )

    state = LoopState(session_id="terminal-finalize")
    state.final_content = "partial content"
    state.metadata["synthesis_failed"] = True
    state.metadata["quality_attempted"] = True
    session = SimpleNamespace(
        state=state,
        ctx=SimpleNamespace(
            session_dir=ROOT,
            run_started=time.perf_counter(),
            deliverable_dir=None,
        ),
    )
    graph_runner = runner_module.ResearchGraphRunner(Harness())
    runner_module._SESSIONS["terminal-finalize"] = session
    try:
        update = asyncio.run(
            graph_runner.node_finalize(
                {
                    "run_id": "terminal-finalize",
                    "phase": "quality",
                    "final_content": "partial content",
                    "quality_assessment": {"verdict": "fail"},
                    "evidence_assessment": {"status": "partial"},
                    "quality_attempts": 1,
                }
            )
        )
    finally:
        runner_module._SESSIONS.pop("terminal-finalize", None)
    assert update["lifecycle"]["status"] == "terminated"
    assert update["termination"]["outcome"] == "partial"
