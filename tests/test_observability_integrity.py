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
            "attributes": {"status": "gap", "verdict": "gap"},
        },
        {
            "type": "synthesis.completed",
            "span_id": "synthesis",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 7,
        },
        {
            "type": "quality.assessed",
            "span_id": "quality",
            "parent_span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 8,
            "attributes": {"passed": True},
        },
        {
            "type": "run.completed",
            "span_id": "root",
            "run_id": "r1",
            "session_id": "s1",
            "seq": 9,
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

