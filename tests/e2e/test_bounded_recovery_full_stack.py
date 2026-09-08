"""Production-topology E2E for bounded recovery and monotonic gap closure."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.agent.harness.loop import AgentHarness
from app.config.loader import get_harness_config
from app.observability import get_recorder
from app.observability.journal import summarize_trace
from app.research.runtime.runner import ResearchGraphRunner
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


def test_ai_startup_query_bounded_recovery_is_deterministic(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    monkeypatch.setattr(WorkerExecutorV2, "_timeout_for", lambda self, step: 0.05)

    for run_index in range(10):
        session_id = f"e2e-bounded-recovery-{run_index}"
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

        replans = [row for row in summary["replans"] if row["type"] == "replan.applied"]
        assert len(replans) == 2, (run_index, len(replans))
        assert all(row["recovery_generation"] <= 2 for row in replans)
        assert all(
            len(row["added_task_ids"]) == len(row["superseded_task_ids"])
            for row in replans
        )
        assert all("_recovery_recovery" not in task_id for task_id in summary["workers"] for task_id in [task_id["task_id"]])
        assert replans[0]["added_task_ids"]
        assert all("_recovery_1" in task_id for task_id in replans[0]["added_task_ids"])
        assert all("_recovery_2" in task_id for task_id in replans[1]["added_task_ids"])

        gap_counts = [int(row.get("unresolved_gap_count") or 0) for row in summary["progress"]]
        assert gap_counts
        assert all(right <= left for left, right in zip(gap_counts, gap_counts[1:]))

        worker_keys = [
            (row["task_id"], row["plan_version"], row["attempt"])
            for row in summary["workers"]
            if row.get("plan_version") is not None and row.get("attempt") is not None
        ]
        assert len(worker_keys) == len(set(worker_keys))
        assert summary["trace_integrity"]["passed"] is True, summary["trace_integrity"]
        assert any(
            event["type"] == "control.decided"
            and event["attributes"]["action"] == "deliver_partial"
            for event in events
        )
        assert not any("GraphRecursion" in str(event.get("error") or "") for event in events)

        graph_steps = (
            4
            + 3 * len(summary["progress"])
            + 3 * len(replans)
            + 3
        )
        max_active_required_tasks = max(
            [1, *(len(row["added_task_ids"]) for row in replans)]
        )
        max_recovery_generation = max(row["recovery_generation"] for row in replans)
        assert graph_steps < ResearchGraphRunner.RECURSION_LIMIT
        assert max_active_required_tasks == 1

        print(
            f"bounded-recovery run={run_index} status={result.status} "
            f"max_replan_count=2 max_recovery_generation={max_recovery_generation} "
            f"recursion_limit={ResearchGraphRunner.RECURSION_LIMIT} "
            f"graph_steps={graph_steps} replan_attempts={replans[-1]['attempted']} "
            f"max_active_required_tasks={max_active_required_tasks} "
            f"workers={len(summary['workers'])} gap_counts={gap_counts} "
            f"final_outcome={result.metadata['termination']['outcome']} "
            f"final_content_non_empty={bool(result.content.strip())} "
            f"integrity={summary['trace_integrity']['passed']}"
        )
