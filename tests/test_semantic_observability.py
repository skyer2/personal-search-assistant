"""Semantic observability contract: lineage, gap_id, synthesis, replay merge."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.observability.events import EventType
from app.observability.failure import classify_failure
from app.observability.journal import summarize_trace
from app.observability.payload_store import SemanticPayloadStore, reset_payload_store
from app.observability.privacy import sanitize_attributes
from app.observability.recorder import AgentTelemetry
from app.observability.replay import load_events, merge_events
from app.observability.events import EVENT_VOCABULARY
from app.observability.semantic_events import control_decision_event_attributes, progress_event_attributes
from app.observability.semantic import (
    compute_supervisor_gap_closure,
    earliest_failure_origin,
    materialize_gap_items,
    stable_gap_id,
)


def test_brief_event_and_payload_ref():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    with tempfile.TemporaryDirectory() as tmp:
        store = SemanticPayloadStore(root=Path(tmp) / "payloads")
        reset_payload_store(store)
        tel.configure_jsonl(Path(tmp), enabled=True)
        tel.start_run(session_id="s_brief", run_id="r_brief")
        ref = store.put(
            run_id="r_brief",
            artifact_type="research_brief",
            artifact_id="brief_01",
            payload={"objective": "compare agents", "dimensions": ["memory", "eval"]},
        )
        event = tel.emit(
            EventType.BRIEF_COMPILED,
            phase="understand",
            status="ok",
            attributes={
                "brief_id": "brief_01",
                "objective": "compare agents",
                "dimensions": ["memory", "eval"],
                "brief_ref": ref.ref,
                "brief_hash": ref.sha256,
            },
            input_refs=[{"type": "user_query", "id": "query"}],
            output_refs=[ref.to_dict()],
        )
        tel.finish_run(status="success", duration_ms=10, metadata={})
        assert event.type == EventType.BRIEF_COMPILED
        assert event.output_refs[0]["id"] == "brief_01"
        loaded = store.get(ref.ref)
        assert loaded is not None
        assert loaded["payload"]["dimensions"] == ["memory", "eval"]
        print("[OK] brief event + payload ref")


def test_spec_event_summarizes_as_research_brief():
    events = [
        {
            "type": "spec.compiled",
            "span_id": "spec",
            "attributes": {
                "spec_id": "spec_01",
                "objective": "find promising AI startups",
                "entities": ["DeepSeek", "Moonshot AI"],
                "dimensions": ["commercialization", "technology"],
                "deliverable": "pdf",
                "prefer_primary": True,
            },
        }
    ]
    summary = summarize_trace(events)
    assert summary["brief"]["brief_id"] == "spec_01"
    assert summary["brief"]["objective"] == "find promising AI startups"
    assert summary["brief"]["entities"] == ["DeepSeek", "Moonshot AI"]
    assert summary["brief"]["dimensions"] == ["commercialization", "technology"]
    assert summary["brief"]["deliverable"] == "pdf"
    print("[OK] spec event summarized as brief")


def test_progress_event_projects_semantic_gap_ids():
    attributes = progress_event_attributes(
        {
            "status": "gap",
            "semantic_gap_ids": ["gap_01"],
            "missing_ids": ["coverage_01"],
        }
    )
    assert attributes["gap_ids"] == ["gap_01"]
    assert attributes["unresolved_gap_count"] == 1
    assert attributes["missing_dimensions"] == ["coverage_01"]


def test_semantic_event_vocabulary_and_control_lineage():
    required = {
        "spec.compiled",
        "spec.validated",
        "coverage.compiled",
        "coverage.assessed",
        "candidate_set.materialized",
        "claim.extracted",
        "claim.conflict_detected",
        "claim.conflict_resolved",
        "semantic_gain.assessed",
        "control.decided",
        "supervisor.started",
        "supervisor.decided",
    }
    assert required <= set(EVENT_VOCABULARY)
    attributes = control_decision_event_attributes(
        {
            "decision_id": "decision_01",
            "action": "gap_fill",
            "reason_codes": ["semantic_gap"],
            "task_ids": ["task_01"],
            "gap_ids": ["gap_01"],
            "candidate_set_id": "candidate_set_01",
            "strategy_fingerprint": "strategy_01",
            "state_version": 2,
            "assessment_refs": ["coverage_01"],
        }
    )
    assert attributes["gap_ids"] == ["gap_01"]
    assert attributes["candidate_set_id"] == "candidate_set_01"
    assert attributes["strategy_fingerprint"] == "strategy_01"
    assert attributes["state_version"] == 2
    assert attributes["assessment_refs"] == ["coverage_01"]


def test_plan_links_to_brief_and_worker_lineage():
    events = [
        {
            "type": "brief.compiled",
            "span_id": "su",
            "attributes": {"brief_id": "brief_01", "dimensions": ["memory", "eval", "multi-agent"]},
            "output_refs": [{"type": "research_brief", "id": "brief_01"}],
        },
        {
            "type": "plan.created",
            "span_id": "sp",
            "plan_version": 1,
            "attributes": {
                "plan_id": "plan_v1",
                "brief_id": "brief_01",
                "task_ids": ["t_memory", "t_multi"],
                "brief_coverage": {"missing_dimensions": ["eval"], "coverage_rate": 0.66},
            },
            "input_refs": [{"type": "research_brief", "id": "brief_01"}],
            "output_refs": [{"type": "research_plan", "id": "plan_v1"}],
        },
        {
            "type": "worker.completed",
            "span_id": "sw",
            "task_id": "t_memory",
            "attempt": 1,
            "status": "ok",
            "attributes": {
                "objective": "memory",
                "brief_id": "brief_01",
                "plan_id": "plan_v1",
                "finding_ids": ["f1"],
                "evidence_ids": ["e1"],
            },
            "input_refs": [{"type": "research_plan", "id": "plan_v1"}, {"type": "task", "id": "t_memory"}],
            "output_refs": [{"type": "finding", "id": "f1"}, {"type": "evidence", "id": "e1"}],
        },
        {
            "type": "evidence.registered",
            "attributes": {
                "finding_id": "f1",
                "claim_id": "c1",
                "evidence_id": "e1",
                "artifact_id": "a1",
                "source_id": "e1",
                "support_type": "direct",
            },
        },
        {
            "type": "synthesis.completed",
            "attributes": {
                "answer_id": "answer_01",
                "brief_id": "brief_01",
                "plan_id": "plan_v1",
                "evidence_ids": ["e1"],
                "claim_ids": ["c1"],
            },
            "input_refs": [{"type": "research_brief", "id": "brief_01"}],
            "output_refs": [{"type": "answer", "id": "answer_01"}],
        },
    ]
    summary = summarize_trace(events)
    assert summary["brief"]["brief_id"] == "brief_01"
    assert summary["plans"][0]["brief_id"] == "brief_01"
    assert summary["workers"][0]["evidence_ids"] == ["e1"]
    assert summary["evidence"][0]["claim_id"] == "c1"
    assert summary["synthesis"][0]["answer_id"] == "answer_01"
    assert any(edge["from_id"] == "brief_01" and edge["to_id"] == "plan_v1" for edge in summary["lineage"])
    print("[OK] plan/worker/evidence/synthesis lineage")


def test_progress_gap_has_stable_gap_id_and_supervisor_targets():
    gaps = materialize_gap_items(
        missing_dimensions=["regulation"],
        coverage_gaps=["empty:t1:obj"],
    )
    open_gap_ids = [str(item["gap_id"]) for item in gaps]
    assert gaps
    assert all(item["gap_id"].startswith("gap_") for item in gaps)
    gid = stable_gap_id("missing_dimension", "regulation")
    assert gid in open_gap_ids

    events = [
        {
            "type": "progress.assessed",
            "seq": 1,
            "attributes": {
                "progress_id": "progress_01",
                "status": "gap",
                "gaps": gaps,
                "open_gap_ids": open_gap_ids,
                "resolved_gap_ids": [],
            },
        },
        {
            "type": "coverage.assessed",
            "seq": 2,
            "attributes": {
                "sufficient": False,
                "missing": [gid],
            },
        },
        {
            "type": "progress.assessed",
            "seq": 3,
            "attributes": {
                "progress_id": "progress_02",
                "status": "sufficient",
                "gaps": [],
                "open_gap_ids": [],
                "resolved_gap_ids": [gid],
            },
        },
    ]
    events.append(
        {
            "type": "supervisor.decided",
            "seq": 3,
            "attributes": {"action": "CONDUCT_RESEARCH", "task_count": 1},
        }
    )
    events.append(
        {
            "type": "coverage.assessed",
            "seq": 4,
            "attributes": {"sufficient": True, "missing": []},
        }
    )
    closure = compute_supervisor_gap_closure(events)
    assert closure["supervisor_recovery"] is True
    assert closure["gap_closure_rate"] == 1.0
    assert gid in closure["closed_missing"]
    summary = summarize_trace(events)
    assert summary["supervisor_recovery"] is True
    assert summary["supervisor_iterations"] == 1
    print("[OK] gap_id + supervisor gap closure")


def test_eval_targets_span_and_failure_origin_earliest():
    events = [
        {
            "type": "eval.scored",
            "seq": 1,
            "phase": "understand",
            "attributes": {
                "target_type": "research_brief",
                "target_span_id": "span_understand",
                "target_artifact_id": "brief_01",
                "grader": "brief_coverage",
                "grader_version": "v2",
                "metric": "dimension_recall",
                "score": 0.5,
                "passed": False,
                "failure.origin_stage": "understand",
                "failure.detected_stage": "eval",
                "failure.cause_artifact_id": "brief_01",
            },
        },
        {
            "type": "worker.failed",
            "seq": 9,
            "phase": "execute",
            "attributes": {
                "fail_reason": "timeout",
                "failure.stage": "worker",
                "failure.type": "timeout",
                "failure.origin_stage": "worker",
                "failure.detected_stage": "worker",
            },
        },
    ]
    summary = summarize_trace(events)
    assert summary["evals"][0]["target_span_id"] == "span_understand"
    assert summary["failure_origin"]["origin_stage"] == "understand"
    classified = classify_failure("missing_dimension", phase="understand")
    assert classified["failure.origin_stage"] == "understand"
    print("[OK] eval target + earliest failure origin")


def test_replay_merges_memory_and_jsonl_dedupe():
    tel = AgentTelemetry()
    tel._ws_enabled = False
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        tel.configure_jsonl(log_dir, enabled=True)
        # Patch traces_log_dir via exporter already pointing at tmp; load_events uses traces_log_dir().
        # Write durable events directly and inject memory via journal.
        from app.observability.events import AgentEvent, new_id, utc_now
        from app.observability.exporters.jsonl import JsonlExporter

        exporter = JsonlExporter(log_dir=log_dir, enabled=True)
        durable = AgentEvent(
            event_id="evt_durable",
            trace_id="tr",
            span_id="s1",
            run_id="r1",
            session_id="sess_merge",
            seq=1,
            timestamp=utc_now(),
            type=EventType.RUN_STARTED,
            attributes={},
        )
        memory_only = AgentEvent(
            event_id="evt_memory",
            trace_id="tr",
            span_id="s2",
            run_id="r1",
            session_id="sess_merge",
            seq=2,
            timestamp=utc_now(),
            type=EventType.BRIEF_COMPILED,
            attributes={"brief_id": "b1"},
        )
        duplicate = AgentEvent(
            event_id="evt_durable",
            trace_id="tr",
            span_id="s1b",
            run_id="r1",
            session_id="sess_merge",
            seq=1,
            timestamp=utc_now(),
            type=EventType.RUN_STARTED,
            attributes={"from": "memory"},
        )
        exporter.export(durable)
        tel.journal.append(memory_only)
        tel.journal.append(duplicate)

        # Monkeypatch events_from_jsonl path by reading via merge helper directly.
        merged = merge_events([durable], [memory_only, duplicate])
        assert len(merged) == 2
        assert {e.event_id for e in merged} == {"evt_durable", "evt_memory"}

        # Last-wins path used by load_events
        from app.observability.replay import _merge_last_wins

        last = _merge_last_wins([durable], [duplicate, memory_only])
        assert len(last) == 2
        durable_copy = next(e for e in last if e.event_id == "evt_durable")
        assert durable_copy.attributes.get("from") == "memory"
        print("[OK] replay merge/dedupe")


def test_reference_mode_emits_payload_ref_not_prompt():
    cleaned = sanitize_attributes(
        {
            "prompt": "SECRET_PROMPT",
            "brief_ref": "payloads/r1/brief_001.json",
            "brief_hash": "abc",
            "brief_id": "brief_01",
            "objective": "ok",
        },
        mode="reference",
    )
    assert "SECRET_PROMPT" not in json.dumps(cleaned)
    assert cleaned["brief_ref"].endswith("brief_001.json")
    assert cleaned["brief_id"] == "brief_01"
    redacted = sanitize_attributes({"prompt": "SECRET_PROMPT", "tool_name": "search"}, mode="redacted")
    assert "SECRET_PROMPT" not in json.dumps(redacted)
    print("[OK] reference/redacted privacy")


def test_gap_items_deterministic():
    a = materialize_gap_items(missing_dimensions=["eval", "eval"], coverage_gaps=["empty:t1:x"])
    assert len(a) == 2
    assert a[0]["gap_id"] == stable_gap_id(a[0]["type"], a[0]["description"])
    print("[OK] deterministic gap ids")


def test_failure_origin_prefers_understand_over_late_worker():
    origin = earliest_failure_origin(
        [
            {
                "seq": 1,
                "type": "eval.scored",
                "attributes": {
                    "passed": False,
                    "target_type": "research_brief",
                    "failure.origin_stage": "understand",
                    "failure.cause_artifact_id": "brief_01",
                },
            },
            {
                "seq": 8,
                "type": "run.failed",
                "attributes": {"failure.origin_stage": "runtime", "failure.type": "timeout"},
            },
        ]
    )
    assert origin is not None
    assert origin["origin_stage"] == "understand"
    print("[OK] earliest failure stage")


if __name__ == "__main__":
    test_brief_event_and_payload_ref()
    test_plan_links_to_brief_and_worker_lineage()
    test_progress_gap_has_stable_gap_id_and_supervisor_targets()
    test_eval_targets_span_and_failure_origin_earliest()
    test_replay_merges_memory_and_jsonl_dedupe()
    test_reference_mode_emits_payload_ref_not_prompt()
    test_gap_items_deterministic()
    test_failure_origin_prefers_understand_over_late_worker()
    print("all semantic observability contracts passed")
