"""Full-stack contract regression for timeout salvage and partial delivery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from app.agent.harness.loop import AgentHarness
from app.agent.harness.tool_contract import apply_tool_output_contract
from app.config.loader import get_harness_config
from app.observability import get_recorder
from app.observability.journal import summarize_trace
from app.research.execution import worker_executor as worker_executor_module
from app.research.execution.tool_gateway import ToolGateway
from app.research.execution.worker_executor import WorkerExecutorV2


class CapturingToolGateway(ToolGateway):
    current: CapturingToolGateway | None = None

    def __init__(self, remaining_calls: int | None):
        super().__init__(remaining_calls)
        CapturingToolGateway.current = self


def deterministic_search(**kwargs: Any) -> dict[str, Any]:
    query = str(kwargs.get("query") or "AI startup landscape")
    return {
        "query": query,
        "results": [
            {
                "title": "AI startup landscape",
                "url": "https://example.com/ai-startup-landscape",
                "content": "Candidate AI startups and their recent funding milestones.",
                "raw_content": "Candidate AI startups and their recent funding milestones.",
            }
        ],
    }


class DeterministicAgent:
    async def astream(self, payload: dict[str, Any], config: dict[str, Any] | None = None):
        messages = list(payload.get("messages") or [])
        last_message = messages[-1]
        prompt = (
            str(last_message.get("content") or "")
            if isinstance(last_message, dict)
            else str(getattr(last_message, "content", "") or "")
        )
        if prompt.startswith("任务：") and "合成模式" in prompt:
            yield {
                "synthesis": {
                    "messages": [
                        AIMessage(
                            content=(
                                "# 国内 AI 初创公司部分评估\n\n"
                                "- 已恢复的检索证据显示若干候选公司具有近期融资与商业化信号。\n"
                                "- 本次 Worker 超时，结论为降级部分交付，未覆盖全部候选池。\n"
                            )
                        )
                    ]
                }
            }
            return

        gateway = CapturingToolGateway.current
        if gateway is None:
            raise RuntimeError("worker tool gateway is not active")
        raw = gateway.call(deterministic_search, query="国内 AI 初创公司 全景 融资")
        contracted = apply_tool_output_contract(
            raw,
            tool_name="internet_search",
            step_type="network_search",
        )
        card = json.loads(contracted)["results"][0]
        yield {
            "worker": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "internet_search",
                                "args": {"query": "国内 AI 初创公司 全景 融资"},
                                "id": "call-landscape",
                            }
                        ],
                    )
                ]
            }
        }
        await asyncio.sleep(0.2)
        yield {
            "worker": {
                "messages": [
                    AIMessage(
                        content=json.dumps(
                            {
                                "ok": True,
                                "summary": "Collected one landscape source.",
                                "facts": ["Candidate AI startups have recent funding signals."],
                                "sources": [card["url"]],
                                "evidence_ids": [card["artifact_id"]],
                                "artifact_ids": [card["artifact_id"]],
                            },
                            ensure_ascii=False,
                        )
                    )
                ]
            }
        }


def test_landscape_timeout_salvage_reaches_partial_pdf_and_run_download(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    monkeypatch.setattr(WorkerExecutorV2, "_timeout_for", lambda self, step: 0.05)

    config = get_harness_config()
    config.planner_llm_enabled = False
    config.max_replan_count = 0
    config.direct_worker_invoke = True
    agent = DeterministicAgent()
    harness = AgentHarness(
        agent=agent,
        project_root=tmp_path,
        harness_config=config,
        workers={
            "research": agent,
            "network_search": agent,
            "web": agent,
        },
    )
    session_id = "e2e-timeout-salvage"
    query = (
        "近1年国内AI初创公司全景扫描，收敛值得加入的候选公司。"
        "输出结果为pdf"
    )
    result = asyncio.run(harness.run(query, session_id, mode="agent"))

    assert result.status == "partial"
    assert "降级部分交付" in result.content
    assert any(path.lower().endswith(".pdf") for path in result.artifacts)
    assert result.metadata["termination"]["outcome"] == "partial"
    assert result.metadata["termination"]["research_completed"] is True
    assert result.metadata["termination"]["synthesis_attempted"] is True
    assert result.metadata["termination"]["quality_attempted"] is True

    run_id = str(result.metadata["run_id"])
    run_dir = tmp_path / "output" / f"session_{session_id}" / "runs" / run_id
    artifacts_dir = run_dir / "artifacts"
    artifact_files = list((artifacts_dir / ".harness" / "artifacts").glob("*.txt"))
    assert len(artifact_files) >= 1
    for artifact_file in artifact_files:
        assert artifact_file.is_relative_to(artifacts_dir)
        assert artifact_file.read_text(encoding="utf-8").strip()
    deliverable_files = list((run_dir / "deliverables").glob("*"))
    assert sum(path.suffix.lower() in {".md", ".pdf"} for path in deliverable_files) == 2

    events = [
        event.to_dict()
        for event in get_recorder().journal.events_for_run(session_id, run_id)
    ]
    summary = summarize_trace(events)
    assert summary["brief"]["objective"]
    assert summary["brief"]["dimensions"]
    assert summary["plans"] and summary["plans"][-1]["task_ids"]
    assert summary["worker_count"] == 2
    assert all(row["execution_status"] == "failed" for row in summary["workers"])
    assert all(row["result_status"] == "partial" for row in summary["workers"])
    assert all(row["evidence_ids"] for row in summary["workers"])
    assert all(row["fail_reason"] == "worker_timeout" for row in summary["workers"])
    assert {row["attempt"] for row in summary["workers"]} == {1, 2}

    latest_progress = summary["progress"][-1]
    assert latest_progress["status"] == "gap"
    assert latest_progress["coverage_gaps"]
    assert latest_progress["reason_codes"]
    control_events = [event for event in events if event["type"] == "control.decided"]
    assert any(event["attributes"]["action"] == "deliver_partial" for event in control_events)
    assert summary["termination"]["outcome"] == "partial"
    assert summary["trace_integrity"]["passed"] is True

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import session_routes
    from app.run_store import RunStore

    store = RunStore(tmp_path / "run-store.sqlite")
    store.create_run(run_id=run_id, session_id=session_id, query=query)
    monkeypatch.setattr(session_routes, "get_run_store", lambda: store)
    monkeypatch.setattr(session_routes, "_OUTPUT_DIR", tmp_path / "output")
    api = FastAPI()
    api.include_router(session_routes.router)

    with TestClient(api) as client:
        listed = client.get(f"/api/runs/{run_id}/artifacts")
        assert listed.status_code == 200
        paths = [row["path"] for row in listed.json()["files"]]
        assert any(path.endswith(".pdf") for path in paths)

        pdf_path = next(path for path in paths if path.endswith(".pdf"))
        downloaded = client.get(
            f"/api/runs/{run_id}/download",
            params={"name": pdf_path},
        )
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"%PDF")

    store.close()
