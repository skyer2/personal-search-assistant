"""Regression tests for user-visible delivery and post-run latency contracts."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from pathlib import Path

from app.agent.harness.loop import AgentHarness
from app.agent.harness.state import LoopState
from app.agent.memory.identity import MemoryIdentity
from app.config.loader import HarnessConfig
from app.research.runtime.background import schedule_post_run
from app.research.runtime.latency import (
    critical_path_summary,
    note_stage_duration,
    note_substep_duration,
    note_worker_durations,
)
from app.observability.journal import summarize_trace
from app.research.brief.models import StructuredResearchBrief
from app.research.supervisor.agent import SupervisorAgent


class _Extractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract_writes(self, *args, **kwargs):
        self.calls += 1
        return []


class _Memory:
    def __init__(self) -> None:
        self.calls = 0

    async def remember_writes(self, *args, **kwargs):
        self.calls += 1
        return 0


def test_latency_summary_exposes_stage_substeps_and_parallel_savings() -> None:
    state = SimpleNamespace(
        metadata={"run_started_monotonic": 0.0},
    )
    note_stage_duration(state, "delivery", 120)
    note_substep_duration(state, "delivery", "persist_result", 30, input_size=10)
    note_worker_durations(
        state,
        [
            SimpleNamespace(task_id="a", duration_ms=100, queue_ms=5, execution_ms=95, metrics={}),
            SimpleNamespace(
                task_id="b",
                duration_ms=80,
                queue_ms=4,
                execution_ms=76,
                metrics={"llm_ms": 50, "tool_ms": 20, "idle_ms": 10, "llm_calls": 2, "tool_calls": 1},
            ),
        ],
    )

    summary = critical_path_summary(state.metadata)
    assert summary["schema_version"] == "latency.v2"
    assert summary["stage_ms"]["delivery"] == 120
    assert summary["stage_ms"]["research"] == 100
    assert summary["stages"]["research"]["duration_ms"] == 100
    assert summary["substeps"]["delivery.persist_result"]["duration_ms"] == 30
    assert summary["research_worker_sum_ms"] == 180
    assert summary["research_parallel_saved_ms"] == 80
    assert summary["worker_llm_ratio"] > 0
    assert summary["worker_tool_ratio"] > 0
    assert summary["llm_calls"] == 2

    # Checkpoint replay of the same worker attempt replaces the projection;
    # it must not double-count the critical path.
    replay = SimpleNamespace(
        task_id="b",
        duration_ms=80,
        queue_ms=4,
        execution_ms=76,
        metrics={
            "dispatch_wave_id": 0,
            "worker_attempt": 1,
            "llm_ms": 50,
            "tool_ms": 20,
            "idle_ms": 10,
            "llm_calls": 2,
            "tool_calls": 1,
        },
    )
    note_worker_durations(state, [replay])
    replayed = critical_path_summary(state.metadata)
    assert replayed["research_worker_sum_ms"] == 180
    assert len(replayed["worker_metrics"]) == 2

    state2 = SimpleNamespace(metadata={})
    note_worker_durations(
        state2,
        [
            SimpleNamespace(task_id="wave1", duration_ms=100, queue_ms=0, execution_ms=100, metrics={"dispatch_wave_id": 1}),
            SimpleNamespace(task_id="wave2", duration_ms=80, queue_ms=0, execution_ms=80, metrics={"dispatch_wave_id": 2}),
        ],
    )
    assert critical_path_summary(state2.metadata)["research_wall_ms"] == 180


def test_repeated_projection_measurements_keep_provider_latency_visible() -> None:
    state = SimpleNamespace(metadata={})
    note_stage_duration(state, "understand", 79_973)
    note_stage_duration(state, "understand", 4)
    note_substep_duration(state, "understand", "llm_request", 79_973)
    note_substep_duration(state, "understand", "llm_request", 4)

    summary = critical_path_summary(state.metadata)
    assert summary["stage_ms"]["understand"] == 79_973
    assert summary["substeps"]["understand.llm_request"]["duration_ms"] == 79_973
    assert summary["substeps"]["understand.llm_request"]["total_ms"] == 79_977
    assert summary["substeps"]["understand.llm_request"]["attempts"] == 2


def test_critical_path_projects_synthesis_substeps() -> None:
    state = SimpleNamespace(metadata={})
    note_substep_duration(state, "synthesis", "generation", 120, tokens=40)
    note_substep_duration(state, "synthesis", "citation_binding", 3)
    note_substep_duration(state, "synthesis", "validation", 2)

    summary = critical_path_summary(state.metadata)
    assert summary["synthesis_generation_ms"] == 120
    assert summary["synthesis_citation_ms"] == 3
    assert summary["synthesis_validation_ms"] == 2


def test_critical_path_reads_persisted_worker_metrics_alias() -> None:
    summary = critical_path_summary(
        {
            "latency": {
                "worker_metrics": [
                    {"worker_id": "a", "wave_id": 1, "wall_ms": 120},
                    {"worker_id": "b", "wave_id": 1, "wall_ms": 90},
                ]
            }
        }
    )
    assert summary["research_wall_ms"] == 120
    assert summary["research_worker_sum_ms"] == 210
    assert summary["research_parallel_saved_ms"] == 90
    assert summary["stages"]["research"]["duration_ms"] == 120


def test_critical_path_reconstructs_workers_from_compatibility_arrays() -> None:
    summary = critical_path_summary(
        {
            "latency": {
                "worker_queue_ms": [5, 4],
                "worker_execution_ms": [100, 80],
            }
        }
    )
    assert summary["research_worker_sum_ms"] == 189
    assert summary["research_wall_ms"] == 105
    assert summary["research_parallel_saved_ms"] == 84
    assert len(summary["worker_metrics"]) == 2


def test_supervisor_terminal_budget_decision_skips_provider() -> None:
    class _NeverModel:
        async def ainvoke(self, *args, **kwargs):
            raise AssertionError("provider must not be called after budget stop")

    brief = StructuredResearchBrief("b", 1, "research", "research", key_questions=("q",))
    action = asyncio.run(
        SupervisorAgent(_NeverModel()).decide(
            brief,
            [],
            None,
            {"exhausted": True},
        )
    )
    assert action.action == "COMPLETE"


def test_disabled_memory_never_calls_model_or_store() -> None:
    extractor = _Extractor()
    memory = _Memory()
    harness = AgentHarness(
        agent=None,
        project_root=None,
        memory=memory,
        memory_extractor=extractor,
        harness_config=HarnessConfig(),
    )
    state = LoopState(session_id="s", run_id="r", final_content="answer")
    state.intent = SimpleNamespace(raw_query="q", summary="topic")
    policy = SimpleNamespace(
        enabled=False,
        remember_on_partial=False,
        consolidation_enabled=False,
    )
    asyncio.run(
        harness._post_run_memory(
            state,
            success=True,
            content="answer",
            identity=MemoryIdentity(user_id="u", session_id="s"),
            policy=policy,
        )
    )
    assert extractor.calls == 0
    assert memory.calls == 0
    assert state.metadata["latency"]["substeps"]["post_run.memory_extract"]["status"] == "skipped"


def test_post_run_task_is_bounded_and_fail_open() -> None:
    async def main() -> None:
        async def slow() -> None:
            await asyncio.sleep(1)

        task = schedule_post_run(slow(), name="test", timeout_sec=0.01)
        assert task is not None
        await asyncio.sleep(0.2)
        assert task.done()
        assert not task.cancelled()

    asyncio.run(main())


def test_trace_summary_projects_latency_metadata() -> None:
    summary = summarize_trace(
        [
            {
                "type": "run.completed",
                "status": "success",
                "session_id": "s",
                "run_id": "r",
                "trace_id": "t",
                "attributes": {
                    "metadata": {
                        "latency": {
                            "schema_version": "latency.v2",
                            "delivery_ms": 42,
                        }
                    }
                },
            }
        ],
        include_lineage=False,
        include_tree_integrity=False,
    )
    assert summary["latency"]["schema_version"] == "latency.v2"
    assert summary["latency"]["delivery_ms"] == 42


def test_finalize_delivery_does_not_wait_for_disabled_memory(tmp_path: Path, monkeypatch) -> None:
    extractor = _Extractor()
    memory = _Memory()
    harness = AgentHarness(
        agent=None,
        project_root=tmp_path,
        memory=memory,
        memory_extractor=extractor,
        harness_config=HarnessConfig(),
    )
    monkeypatch.setattr(
        "app.agent.harness.loop.get_memory_policy",
        lambda: SimpleNamespace(
            enabled=False,
            remember_on_partial=False,
            consolidation_enabled=False,
        ),
    )
    state = LoopState(session_id="s", final_content="answer")
    state.intent = SimpleNamespace(raw_query="q", summary="topic", deliverable="text")
    state.metadata = {
        "run_started_monotonic": time.perf_counter(),
        "termination": {"outcome": "success"},
    }
    started = time.perf_counter()
    result = asyncio.run(
        harness._phase_finalize(
            state,
            tmp_path,
            success=True,
            started_at=started,
            deliverable_dir=tmp_path / "deliverables",
        )
    )
    assert (time.perf_counter() - started) < 1.0
    assert extractor.calls == 0
    assert memory.calls == 0
    assert result.metadata["post_run_status"] == "skipped"
    assert result.metadata["latency"]["substeps"]["post_run.memory_extract"]["status"] == "skipped"
    assert any(event.phase == "delivery" and event.status == "done" for event in result.trace)
