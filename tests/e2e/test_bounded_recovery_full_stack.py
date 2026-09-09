"""Production-topology E2E for bounded adaptive research control."""

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


QUERY = "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？输出结果为pdf"


def _run_once(tmp_path: Path, session_id: str):
    config = get_harness_config()
    config.planner_llm_enabled = False
    config.max_replan_count = 2
    config.direct_worker_invoke = True
    agent = DeterministicAgent()
    harness = AgentHarness(
        agent=agent,
        project_root=tmp_path,
        harness_config=config,
        workers={"research": agent, "network_search": agent, "web": agent},
    )
    return asyncio.run(harness.run(QUERY, session_id, mode="agent"))


def test_ai_startup_query_adaptive_control_is_bounded_and_deterministic(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    monkeypatch.setattr(WorkerExecutorV2, "_timeout_for", lambda self, step: 0.05)

    signatures = []
    for run_index in range(10):
        session_id = f"e2e-bounded-adaptive-{run_index}"
        result = _run_once(tmp_path, session_id)
        run_id = str(result.metadata["run_id"])
        events = [
            event.to_dict()
            for event in get_recorder().journal.events_for_run(session_id, run_id)
        ]
        summary = summarize_trace(events)

        assert result.status in {"partial", "success"}, (run_index, result.status)
        assert result.content.strip()
        assert result.artifacts
        assert result.metadata["termination"]["outcome"] in {"partial", "success"}

        gap_fills = sum(event["type"] == "plan.gap_fill_applied" for event in events)
        expansions = sum(event["type"] == "plan.expanded" for event in events)
        replans = sum(event["type"] == "replan.applied" for event in events)
        assert gap_fills <= 4, (run_index, gap_fills)
        assert expansions <= 2, (run_index, expansions)
        assert replans <= 2, (run_index, replans)

        worker_keys = [
            (row["task_id"], row["plan_version"], row["attempt"])
            for row in summary["workers"]
            if row.get("plan_version") is not None and row.get("attempt") is not None
        ]
        assert len(worker_keys) == len(set(worker_keys))
        assert summary["trace_integrity"]["passed"] is True, summary["trace_integrity"]
        assert any(
            event["type"] == "control.decided"
            and event["attributes"]["action"] in {"synthesize", "deliver_partial", "finalize_success"}
            for event in events
        )
        assert not any("GraphRecursion" in str(event.get("error") or "") for event in events)
        signatures.append((result.status, gap_fills, expansions, replans, len(result.artifacts)))

    assert len(set(signatures)) == 1, signatures
