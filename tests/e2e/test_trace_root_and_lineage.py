from __future__ import annotations

import asyncio
from pathlib import Path

from app.agent.harness.loop import AgentHarness
from app.config.loader import get_harness_config
from app.observability import get_recorder
from app.observability.journal import summarize_trace
from app.research.execution import worker_executor as worker_executor_module
from tests.e2e.deterministic_landscape import CapturingToolGateway, DeterministicAgent


QUERY = "你觉得当下国内 AI 初创有潜力值得加入的公司有哪些？为什么？"


def test_trace_root_and_evidence_lineage_survive_full_stack(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    config = get_harness_config()
    config.planner_llm_enabled = False
    config.direct_worker_invoke = True
    agent = DeterministicAgent()
    harness = AgentHarness(
        agent=agent,
        project_root=tmp_path,
        harness_config=config,
        workers={"research": agent, "network_search": agent, "web": agent},
    )
    result = asyncio.run(harness.run(QUERY, "e2e-trace-lineage", mode="agent"))
    assert result.status in {"success", "partial"}
    assert result.content.strip()

    run_id = str(result.metadata["run_id"])
    events = [
        event.to_dict()
        for event in get_recorder().journal.events_for_run("e2e-trace-lineage", run_id)
    ]
    summary = summarize_trace(events)
    assert summary["identity"]["run_id"] == run_id
    assert summary["trace_integrity"]["passed"] is True, summary["trace_integrity"]
    assert summary["termination"]["outcome"] in {"success", "partial"}
    assert any(
        edge["from_type"] == "task" and edge["to_type"] == "evidence"
        for edge in summary["lineage"]
    )
    assert any(
        edge["from_type"] == "evidence" and edge["to_type"] == "synthesis"
        for edge in summary["lineage"]
    )
