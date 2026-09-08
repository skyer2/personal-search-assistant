from __future__ import annotations

from app.observability.integrity import check_trace_integrity


def _events(run_status: str) -> list[dict]:
    return [
        {
            "type": "run.started",
            "span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 1,
            "attributes": {"search_mode": "agent"},
        },
        {
            "type": "brief.compiled",
            "span_id": "brief",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 2,
        },
        {
            "type": "plan.created",
            "span_id": "plan",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 3,
        },
        {
            "type": "worker.started",
            "span_id": "worker",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 4,
            "task_id": "t_landscape",
        },
        {
            "type": "worker.failed",
            "span_id": "worker",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 5,
            "task_id": "t_landscape",
        },
        {
            "type": "progress.assessed",
            "span_id": "progress",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 6,
            "attributes": {
                "status": "gap",
                "reason_codes": ["required_research_failed"],
                "coverage_gaps": ["required_task:t_landscape"],
            },
        },
        {
            "type": "control.decided",
            "span_id": "progress",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 7,
            "status": "deliver_partial",
            "attributes": {"action": "deliver_partial"},
        },
        {
            "type": "synthesis.completed",
            "span_id": "synthesis",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 8,
        },
        {
            "type": "quality.assessed",
            "span_id": "quality",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 9,
            "attributes": {"passed": True},
        },
        {
            "type": "run.terminated",
            "span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 10,
            "status": "partial",
            "attributes": {"termination": {"outcome": "partial", "reason": "degraded_delivery"}},
        },
        {
            "type": "run.completed",
            "span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 11,
            "status": run_status,
            "attributes": {
                "termination": {
                    "reason": "degraded_delivery",
                    "stage": "quality",
                    "quality_attempted": True,
                }
            },
        },
    ]


def test_assessed_events_are_required_and_span_tree_has_root():
    result = check_trace_integrity(_events("partial"), run_status="partial")
    assert result["passed"] is True
    assert result["issues"] == []
    assert result["counts"]["brief"] == 1
    assert result["counts"]["plan"] == 1
    assert result["counts"]["progress"] == 1
    assert result["counts"]["quality"] == 1
    assert result["span_tree"]["span_count"] > 0
    assert result["span_tree"]["root_count"] >= 1
    assert result["span_tree"]["cycle_count"] == 0


def test_integrity_rejects_zero_worker_started():
    events = [item for item in _events("partial") if item["type"] != "worker.started"]
    result = check_trace_integrity(events, run_status="partial")
    assert "worker_lifecycle_mismatch:started=0,done=1" in result["issues"]


def test_integrity_rejects_zero_root_span():
    events = [{"type": "llm_usage", "attributes": {"search_mode": "agent"}}]
    result = check_trace_integrity(events, run_status="partial")
    assert "missing_root_span" in result["issues"]


def test_integrity_rejects_missing_lineage_and_synthesis_span():
    events = _events("partial")
    for synthesis in (item for item in events if str(item["type"]).startswith("synthesis.")):
        synthesis["span_id"] = "root"
        synthesis["attributes"] = {"evidence_ids": []}
    worker_failed = next(item for item in events if item["type"] == "worker.failed")
    worker_failed["attributes"] = {"evidence_ids": ["ev-1"]}
    events.append(
        {
            "type": "synthesis.started",
            "span_id": "root",
            "parent_span_id": "root",
            "seq": 13,
            "attributes": {"evidence_ids": []},
        }
    )
    events.append(
        {
            "type": "evidence.assessed",
            "span_id": "progress",
            "parent_span_id": "root",
            "seq": 12,
            "attributes": {"status": "partial"},
        }
    )
    result = check_trace_integrity(events, run_status="partial")
    assert "missing_synthesis_span" in result["issues"]
    assert "missing_evidence_lineage" in result["issues"]


def test_integrity_rejects_synthesis_failure_without_reason_and_empty_delivery():
    events = _events("failed")
    synthesis = next(item for item in events if item["type"] == "synthesis.completed")
    synthesis["type"] = "synthesis.failed"
    synthesis["status"] = "failed"
    synthesis["attributes"] = {"evidence_ids": ["ev-1"], "fail_reason": ""}
    events.append(
        {
            "type": "evidence.assessed",
            "span_id": "progress",
            "parent_span_id": "root",
            "seq": 12,
            "attributes": {"status": "partial"},
        }
    )
    terminated = next(item for item in reversed(events) if item["type"] == "run.terminated")
    terminated["attributes"]["final_content_chars"] = 0
    result = check_trace_integrity(events, run_status="failed")
    assert "synthesis_failure_reason_missing" in result["issues"]
    assert "usable_evidence_with_empty_final_content" in result["issues"]
