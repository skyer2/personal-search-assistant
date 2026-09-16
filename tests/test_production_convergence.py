from __future__ import annotations

from pathlib import Path

from app.agent.harness.citations import CitationManager
from app.agent.harness.orchestration import GapSignal, parse_worker_payload
from app.agent.harness.state import PlanStep, StepResult
from app.observability.events import AgentEvent
from app.observability.projection_store import ProjectionStore
from app.research.runtime.isolation import worker_row


def test_gap_signal_remains_structured() -> None:
    payload = parse_worker_payload(
        '{"summary":"ok","facts":["fact"],"gaps":[{"type":"evidence_gap","dimension":"cost","severity":"critical","blocking":true,"description":"missing"}]}'
    )
    assert payload.gaps == [
        GapSignal("evidence_gap", "cost", "missing", "critical", True)
    ]


def test_worker_row_preserves_latency() -> None:
    step = PlanStep(
        step_type="research",
        description="x",
        metadata={"queue_ms": 12, "execution_ms": 345},
    )
    row = worker_row(
        "task", step, True, StepResult(step_type="research", content="done")
    )
    assert (row["queue_ms"], row["execution_ms"], row["duration_ms"]) == (12, 345, 345)


def test_evidence_admission_dedupes_and_caps_per_task() -> None:
    manager = CitationManager()
    content = " ".join(f"https://example.com/{i}?utm_source=test" for i in range(10))
    registered = manager.register_from_step(0, "network_search", content)
    assert len(registered) == 6
    assert (
        manager.register_from_step(0, "network_search", "https://example.com/0") == []
    )


def test_projection_store_pages_by_run(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path / "trace.sqlite3")
    for seq in range(1, 6):
        store.append(
            AgentEvent(
                str(seq), "trace", "span", "run-a", "session", seq, "now", "phase"
            )
        )
        store.append(
            AgentEvent(
                f"b{seq}", "trace", "span", "run-b", "session", seq, "now", "phase"
            )
        )
    assert [row["seq"] for row in store.events("run-a", limit=2)] == [4, 5]
    assert [row["seq"] for row in store.events("run-a", after_seq=2, limit=2)] == [3, 4]


def test_summary_projection_invalidates_old_contract_cache(monkeypatch) -> None:
    from app.observability import replay
    from app.observability import projection_store
    from tests.test_observability_integrity import _events

    events = [AgentEvent.from_dict(item) for item in _events("partial")]
    writes: list[dict] = []

    class Store:
        def get_projection(self, run_id: str, kind: str):
            return {
                "run_id": run_id,
                "kind": kind,
                "event_count": len(events),
                "projection_version": "trace-summary-v1",
                "summary": {"trace_integrity": {"passed": False, "issues": ["missing_root_span"]}},
            }

        def put_projection(self, run_id: str, kind: str, payload: dict) -> None:
            writes.append(payload)

    monkeypatch.setattr(projection_store, "get_projection_store", lambda: Store())
    monkeypatch.setattr(replay, "load_events", lambda session_id, run_id=None: events)

    payload = replay.load_summary_projection("session", run_id="run")

    assert payload["projection_version"] == replay.SUMMARY_PROJECTION_VERSION
    assert payload["summary"]["trace_integrity"]["passed"] is True
    assert len(writes) == 1
