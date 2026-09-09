"""Full-stack contract regression for timeout salvage and partial delivery."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.agent.harness.loop import AgentHarness
from app.config.loader import get_harness_config
from app.observability import get_recorder
from app.observability.journal import summarize_trace
from app.research.execution import worker_executor as worker_executor_module
from app.research.execution.worker_executor import WorkerExecutorV2
from tests.e2e.deterministic_landscape import CapturingToolGateway, DeterministicAgent


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
    assert result.metadata["termination"]["research_completed"] is False
    assert result.metadata["termination"]["outcome"] == "partial"
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
    assert summary["worker_count"] == 6
    assert all(row["execution_status"] == "stopped" for row in summary["workers"])
    assert all(row["result_status"] == "partial" for row in summary["workers"])
    assert all(row["evidence_ids"] for row in summary["workers"])
    assert all(row["fail_reason"] == "worker_timeout" for row in summary["workers"])
    assert {row["attempt"] for row in summary["workers"]} == {1}

    latest_progress = summary["progress"][-1]
    assert latest_progress["status"] == "gap"
    assert latest_progress["gap_ids"]
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
