from __future__ import annotations

from app.research.assessment.delivery import DeliveryMode, DeliveryStatus, assess_delivery


def _state(*, partial: bool = False, semantic_stall: int = 0) -> dict:
    criteria = [
        {
            "criterion_id": "coverage_supported",
            "status": "partial" if partial else "supported",
            "evidence_ids": ["evidence_0"],
        }
    ]
    return {
        "coverage_judgement": {
            "sufficient": not partial,
            "status": "gap" if partial else "sufficient",
            "criteria": criteria,
            "delta": {},
        },
        "evidence_records": [
            {
                "evidence_id": "evidence_0",
                "source_id": "example.com",
                "source_kind": "web",
                "locator": "https://example.com",
                "authority_score": 0.9,
            }
        ],
        "claims": [
            {"claim_id": "claim_0", "text": "The product supports research.", "evidence_ids": ["evidence_0"]}
        ],
        "semantic_stall": semantic_stall,
    }


def test_unknown_facts_block_delivery():
    readiness = assess_delivery({"evidence_assessment": {"status": "unknown"}})
    assert readiness["status"] == DeliveryStatus.NOT_READY.value
    assert readiness["mode"] == DeliveryMode.NONE.value
    assert set(readiness["blockers"]) == {"progress_unknown", "evidence_unknown"}


def test_partial_evidence_produces_degraded_delivery():
    readiness = assess_delivery(_state(partial=True))
    assert readiness["status"] == DeliveryStatus.READY.value
    assert readiness["mode"] == DeliveryMode.DEGRADED.value
    assert "evidence_partial" in readiness["limitations"]


def test_stalled_execution_is_a_limitation_not_a_progress_fact():
    readiness = assess_delivery(_state(semantic_stall=2))
    assert readiness["status"] == DeliveryStatus.READY.value
    assert readiness["mode"] == DeliveryMode.DEGRADED.value
    assert "execution_stalled" in readiness["limitations"]
