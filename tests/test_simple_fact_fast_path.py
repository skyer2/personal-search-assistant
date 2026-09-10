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
from app.agent.harness.run_budget import RunBudgetManager
from app.research.brief.compiler import compile_structured_brief
from app.research.brief.models import FastPathEligibility
from app.research.runtime import runner as runner_module
from app.research.evidence.policy import registrable_domain
from app.research.runtime.atomic_fact import (
    AtomicFactAnswer,
    render_atomic_fact_answer,
)


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


class FakeStructuredModel:
    def __init__(self, answer: str, answer_type: str, query: str = ""):
        self.answer = answer
        self.answer_type = answer_type
        self.query = query

    def with_structured_output(self, _schema: Any) -> Any:
        model = self
        properties = dict(_schema.get("properties") or {})

        class Runnable:
            async def ainvoke(
                self,
                _prompt: str,
                config: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                if "objective" in properties:
                    return {
                        "objective": model.query,
                        "user_intent": "atomic_fact",
                        "explicit_subjects": [model.query],
                        "key_questions": [model.query],
                        "source_requirements": {
                            "min_independent_sources": 1,
                            "primary_required": False,
                            "preferred": ["official", "primary"],
                        },
                        "freshness_requirements": {"required": False, "time_horizon": "any"},
                        "deliverable": {"format": "text", "depth": "brief"},
                        "success_criteria": ["直接回答用户的原子事实问题。"],
                        "clarification_needed": False,
                        "confidence": 0.95,
                    }
                return {
                    "answer": model.answer,
                    "answer_type": model.answer_type,
                    "supporting_source_ids": ["src-1"],
                    "confidence": 0.95,
                    "sufficient": True,
                    "reason": "test fixture",
                }

        return Runnable()


@pytest.mark.parametrize(("query", "url", "content", "expected"), _CASES)
def test_simple_fact_release_gate(query, url, content, expected, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.tools.tavily_tool.internet_search", FakeSearchTool(url, content)
    )
    config = get_harness_config()
    answer_type = "date" if "发布时间" in query or "哪年" in query else "definition"
    harness = AgentHarness(
        agent=None,
        project_root=tmp_path,
        harness_config=config,
        control_agent=FakeStructuredModel(expected, answer_type, query),
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
    assert result.metadata["task_shape"] == "atomic_fact"
    assert result.metadata["execution_path"] == "fast_path"
    assert result.metadata["planner_calls"] == 0
    assert result.metadata["supervisor_iterations"] == 0
    assert result.metadata["synthesis_attempts"] == 1
    assert result.metadata["synthesis_failed"] is False
    assert result.metadata["workers"] == 1
    assert result.metadata["tool_calls_count"] <= 3
    assert result.metadata["primary_sources"] >= 1
    assert result.metadata["partial_renderer_called"] is False
    assert result.metadata["termination"]["outcome"] == "success"
    assert result.metadata["quality"]["verdict"] == "pass"
    run_root = tmp_path / "output" / f"session_{session_id}"
    artifact_files = list(run_root.rglob("*.md")) + list(run_root.rglob("*.pdf"))
    assert artifact_files == []


@pytest.mark.parametrize("query", [case[0] for case in _CASES])
def test_brief_selects_unified_fast_path(query: str):
    brief = compile_structured_brief(query)
    eligibility = FastPathEligibility.from_brief(brief)
    assert brief.user_intent == "atomic_fact"
    assert eligibility.eligible is True
    assert eligibility.reasons == ()


def test_fast_path_budget_cap_only_lowers_limit():
    manager = RunBudgetManager(tool_call_limit=10)
    manager.cap_tool_calls(3)
    assert manager.tool_call_limit == 3
    manager.cap_tool_calls(8)
    assert manager.tool_call_limit == 3


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


def test_atomic_fact_renderer_uses_only_supporting_sources():
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
    answer = AtomicFactAnswer(
        answer="DeepSeek V3 发布于 2024年12月26日。",
        answer_type="date",
        supporting_source_ids=(manager.sources[0].source_id,),
        confidence=0.95,
        sufficient=True,
        reason="test",
    )
    rendered = render_atomic_fact_answer(answer, manager)

    assert "## 答案" in rendered
    assert "2024年12月26日" in rendered
    assert "[1]" in rendered
    assert manager.sources[0].locator in rendered
    assert manager.sources[1].locator not in rendered


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


def test_fast_path_insufficient_evidence_is_not_repairable():
    class Harness:
        harness_config = get_harness_config()
        validator = ResultValidator()

        async def _phase_validate(self, *args: Any, **kwargs: Any) -> None:
            return None

    state = LoopState(session_id="terminal-quality-fast-path")
    state.final_content = "## 当前无法可靠确认"
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
    runner_module._SESSIONS["terminal-quality-fast-path"] = session
    try:
        update = asyncio.run(
            graph_runner.node_quality_gate(
                {
                    "run_id": "terminal-quality-fast-path",
                    "phase": "synthesis",
                    "fast_path": True,
                    "final_content": "## 当前无法可靠确认",
                    "synthesis_failed": True,
                    "coverage_judgement": {"sufficient": False},
                    "evidence_records": [{"evidence_id": "evidence-1"}],
                    "synthesis_attempts": 1,
                    "quality_attempts": 0,
                    "budget": {"max_replan_count": 0},
                }
            )
        )
    finally:
        runner_module._SESSIONS.pop("terminal-quality-fast-path", None)
    assert update["quality_assessment"]["repairable"] is False
    from app.research.runtime.graph import route_after_quality

    assert route_after_quality(update) == "finalize"


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
