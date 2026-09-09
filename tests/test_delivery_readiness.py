from __future__ import annotations

from app.research.assessment.delivery import DeliveryMode, DeliveryStatus, assess_delivery
from app.research.coverage.compiler import compile_coverage_contract
from app.research.spec.compiler import compile_research_spec


def _semantic_state(*, partial: bool = False, semantic_stall: int = 0) -> dict:
    spec = compile_research_spec("What is the research capability of the product?")
    contract = compile_coverage_contract(spec)
    coverage_ids = [unit.coverage_id for unit in contract.units]
    return {
        "research_spec": spec.to_dict(),
        "coverage_contract": contract.to_dict(),
        "coverage_state": {
            "coverage_ratio": 1.0,
            "covered_ids": coverage_ids,
            "partial_ids": coverage_ids if partial else [],
            "missing_ids": [],
            "conflicted_ids": [],
            "stale_ids": [],
        },
        "evidence_records": [
            {
                "evidence_id": "e0",
                "source_id": "example.com",
                "source_kind": "web",
                "locator": "https://example.com",
                "authority_score": 0.9,
            }
        ],
        "claims": [
            {"claim_id": "c0", "text": "The product supports research.", "evidence_ids": ["e0"]}
        ],
        "semantic_stall": semantic_stall,
    }


def test_unknown_facts_block_delivery():
    readiness = assess_delivery({"evidence_assessment": {"status": "unknown"}})
    assert readiness["status"] == DeliveryStatus.NOT_READY.value
    assert readiness["mode"] == DeliveryMode.NONE.value
    assert set(readiness["blockers"]) == {"progress_unknown", "evidence_unknown"}


def test_partial_evidence_produces_degraded_delivery():
    readiness = assess_delivery(_semantic_state(partial=True))
    assert readiness["status"] == DeliveryStatus.READY.value
    assert readiness["mode"] == DeliveryMode.DEGRADED.value
    assert "evidence_partial" in readiness["limitations"]


def test_stalled_execution_is_a_limitation_not_a_progress_fact():
    readiness = assess_delivery(_semantic_state(semantic_stall=2))
    assert readiness["status"] == DeliveryStatus.READY.value
    assert readiness["mode"] == DeliveryMode.DEGRADED.value
    assert "execution_stalled" in readiness["limitations"]
