from app.research.coverage.assessor import assess_coverage
from app.research.coverage.compiler import compile_coverage_contract
from app.research.spec.compiler import compile_research_spec


def test_landscape_contract_expands_after_candidate_discovery():
    spec = compile_research_spec("国内有哪些值得关注的 AI 初创公司？")
    initial = compile_coverage_contract(spec)
    expanded = compile_coverage_contract(spec, candidate_names=["Alpha", "Beta"])
    assert any(unit.dimension_id == "candidate_set" for unit in initial.units)
    assert not any(unit.dimension_id == "candidate_set" for unit in expanded.units)
    assert len(expanded.units) > len(initial.units)
    assert {u.subject_id for u in expanded.units} >= {"candidate:Alpha", "candidate:Beta"}


def test_coverage_is_recomputed_from_claims_and_evidence():
    spec = compile_research_spec("OpenAI 的收入是多少？")
    contract = compile_coverage_contract(spec)
    claims = [
        {
            "claim_id": "c1",
            "text": "OpenAI revenue 10",
            "subject_id": "subject_1",
            "dimension_id": "key_fact",
            "evidence_ids": ["e1", "e2"],
            "confidence": 0.9,
        }
    ]
    evidence = [
        {"evidence_id": "e1", "source_id": "openai.com", "locator": "https://openai.com/a", "authority_score": 0.9},
        {"evidence_id": "e2", "source_id": "reuters.com", "locator": "https://reuters.com/a", "authority_score": 0.75},
    ]
    state_one = assess_coverage(contract, claims=claims, evidence=evidence)
    state_two = assess_coverage(contract, claims=claims, evidence=evidence)
    assert state_one.to_dict() == state_two.to_dict()
    assert state_one.coverage_ratio == 1.0
    assert state_one.covered_ids == [unit.coverage_id for unit in contract.units]
