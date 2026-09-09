"""End-to-end control-plane regression with deterministic leaf executors."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.agent.harness.loop import AgentHarness
from app.config.loader import get_harness_config
from app.observability import get_recorder
import app.research.execution.synthesis_executor as synthesis_executor_module
import app.research.execution.worker_executor as worker_executor_module
from app.research.execution.synthesis_executor import SynthesisRequest
from app.research.runtime.worker import ResearchContext, ResearchTask, WorkerResult


class DeterministicWorkerExecutor:
    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    async def execute(self, task: ResearchTask, context: ResearchContext) -> WorkerResult:
        facts = [
            "DeepSeek focuses on frontier open models and has strong engineer adoption.",
            "Moonshot AI provides the Kimi assistant and long-context model products.",
        ]
        locators = ["https://www.deepseek.com/", "https://www.moonshot.ai/"]
        manager = self.session.ctx.citation_manager
        sources = manager.bind_worker_facts(task.step_index, task.step_type, facts, locators)
        evidence_refs = [source.source_id for source in sources]
        if not evidence_refs:
            evidence_refs = [f"evidence:{task.task_id}"]
        return WorkerResult(
            ok=True,
            task_id=task.task_id,
            status="done",
            summary="Collected two primary company facts.",
            findings=[
                {
                    "task_id": task.task_id,
                    "summary": fact,
                    "claim": fact,
                    "evidence_ids": [evidence_refs[min(index, len(evidence_refs) - 1)]],
                }
                for index, fact in enumerate(facts)
            ],
            evidence_refs=evidence_refs,
            facts=facts,
            sources=locators,
            candidates=[
                {
                    "name": "DeepSeek",
                    "confidence": 0.9,
                    "evidence_ids": evidence_refs,
                },
                {
                    "name": "Moonshot AI",
                    "confidence": 0.88,
                    "evidence_ids": evidence_refs,
                },
            ],
        )


class DeterministicSynthesisExecutor:
    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    async def execute(self, request: SynthesisRequest, context: ResearchContext) -> WorkerResult:
        content = (
            "# 国内 AI 初创公司评估\n\n"
            "- DeepSeek 的模型技术声誉和开发者采用率使其值得评估。[1]\n"
            "- Moonshot AI 的 Kimi 产品和长上下文路线提供了差异化机会。[2]\n"
        )
        return WorkerResult(
            ok=True,
            task_id="synthesis",
            status="done",
            summary=content,
            evidence_refs=request.evidence_refs,
        )


def test_ai_startup_pdf_end_to_end_reaches_success(tmp_path: Path, monkeypatch):
    get_recorder()._listeners = []
    monkeypatch.setattr(
        worker_executor_module,
        "WorkerExecutorV2",
        DeterministicWorkerExecutor,
    )
    monkeypatch.setattr(
        synthesis_executor_module,
        "SynthesisExecutor",
        DeterministicSynthesisExecutor,
    )
    harness = AgentHarness(
        agent=None,
        project_root=tmp_path,
        harness_config=get_harness_config(),
    )
    query = "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？输出结果为pdf"
    result = asyncio.run(harness.run(query, "e2e-control-plane-pdf", mode="agent"))

    assert result.status == "success"
    assert "DeepSeek" in result.content
    assert "Moonshot" in result.content
    assert any(path.lower().endswith(".pdf") for path in result.artifacts)
    assert result.metadata["termination"]["outcome"] == "success"
    assert result.metadata["termination"]["research_completed"] is True
    assert result.metadata["termination"]["synthesis_attempted"] is True
    assert result.metadata["termination"]["quality_attempted"] is True
    assert result.metadata["quality"]["verdict"] == "pass"

    run_id = str(result.metadata["run_id"])
    deliverables = (
        tmp_path
        / "output"
        / "session_e2e-control-plane-pdf"
        / "runs"
        / run_id
        / "deliverables"
    )
    pdfs = [path for path in deliverables.glob("*.pdf")]
    assert len(pdfs) == 1
    assert pdfs[0].read_bytes()[:4] == b"%PDF"
