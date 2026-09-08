from __future__ import annotations

from app.research.assessment.delivery import DeliveryMode, DeliveryStatus, assess_delivery


def test_unknown_facts_block_delivery():
    readiness = assess_delivery({"evidence_assessment": {"status": "unknown"}})
    assert readiness["status"] == DeliveryStatus.NOT_READY.value
    assert readiness["mode"] == DeliveryMode.NONE.value
    assert set(readiness["blockers"]) == {"progress_unknown", "evidence_unknown"}


def test_partial_evidence_produces_degraded_delivery():
    readiness = assess_delivery(
        {
            "plan": {
                "steps": [
                    {
                        "step_type": "research",
                        "task_id": "t0",
                        "description": "collect evidence",
                        "objective": "collect evidence",
                    }
                ],
                "plan_version": 1,
            },
            "evidence_assessment": {"status": "partial", "trusted_evidence_count": 1},
            "tasks": {
                "t0": {
                    "task_id": "t0",
                    "execution_status": "succeeded",
                    "result_status": "complete",
                    "attempt": 1,
                }
            },
        }
    )
    assert readiness["status"] == DeliveryStatus.READY.value
    assert readiness["mode"] == DeliveryMode.DEGRADED.value
    assert "evidence_partial" in readiness["limitations"]


def test_stalled_execution_is_a_limitation_not_a_progress_fact():
    readiness = assess_delivery(
        {
            "plan": {
                "steps": [
                    {
                        "step_type": "research",
                        "task_id": "t0",
                        "description": "collect evidence",
                        "objective": "collect evidence",
                    }
                ],
                "plan_version": 1,
            },
            "evidence_assessment": {"status": "sufficient", "trusted_evidence_count": 2},
            "stalled_cycles": 2,
            "tasks": {
                "t0": {
                    "task_id": "t0",
                    "execution_status": "succeeded",
                    "result_status": "complete",
                    "attempt": 1,
                }
            },
        }
    )
    assert readiness["status"] == DeliveryStatus.READY.value
    assert readiness["mode"] == DeliveryMode.DEGRADED.value
    assert "execution_stalled" in readiness["limitations"]
