from app.research.coverage.assessor import assess_coverage
from app.research.coverage.compiler import compile_coverage_contract
from app.research.coverage.judge import judge_coverage
from app.research.brief.models import SourceRequirements, StructuredResearchBrief
from app.research.findings.models import ResearchFinding
from app.research.planning.candidate import stable_candidate_id
from app.research.spec.compiler import compile_research_spec
from dataclasses import replace


def test_landscape_contract_expands_after_candidate_discovery():
    spec = compile_research_spec("国内有哪些值得关注的 AI 初创公司？")
    initial = compile_coverage_contract(spec)
    candidate_ids = [stable_candidate_id("Alpha"), stable_candidate_id("Beta")]
    expanded = compile_coverage_contract(spec, candidate_ids=candidate_ids)
    assert any(unit.dimension_id == "candidate_set" for unit in initial.units)
    assert not any(unit.dimension_id == "candidate_set" for unit in expanded.units)
    assert len(expanded.units) > len(initial.units)
    assert {u.subject_id for u in expanded.units} >= {
        f"candidate:{candidate_id}" for candidate_id in candidate_ids
    }


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


def test_success_criteria_and_source_identity_drive_coverage():
    brief = StructuredResearchBrief(
        brief_id="brief-coverage",
        version=1,
        objective="Evaluate Company A",
        user_intent="research",
        key_questions=("How should Company A be studied?",),
        success_criteria=("Confirm Company A funding",),
        source_requirements=SourceRequirements(min_independent_sources=2),
    )
    finding = ResearchFinding(
        finding_id="finding-1",
        task_id="task-1",
        summary="Unrelated wording for text-overlap fallback",
        claims=("Unrelated claim",),
        evidence_ids=("evidence-1", "evidence-2"),
        supported_criteria=("Confirm Company A funding",),
    )
    same_source = [
        {"evidence_id": "evidence-1", "source_id": "company-a.com"},
        {"evidence_id": "evidence-2", "source_id": "company-a.com"},
    ]
    partial = judge_coverage(brief, [finding], evidence=same_source)
    assert partial.sufficient is False
    assert partial.criteria[0].status == "partial"
    assert partial.criteria[0].evidence_ids == ("evidence-1", "evidence-2")
    assert partial.criteria[0].source_ids == ("company-a.com",)

    independent_finding = replace(finding, evidence_ids=("evidence-1", "evidence-3"))
    independent = judge_coverage(
        brief,
        [independent_finding],
        evidence=[
            *same_source,
            {"evidence_id": "evidence-3", "source_id": "reuters.com"},
        ],
    )
    assert independent.sufficient is True
    assert independent.criteria[0].status == "supported"


def test_no_evidence_delta_cannot_close_gap():
    brief = StructuredResearchBrief(
        brief_id="brief-no-delta",
        version=1,
        objective="Evaluate Company A",
        user_intent="research",
        success_criteria=("Confirm Company A funding",),
        source_requirements=SourceRequirements(min_independent_sources=2),
    )
    finding = ResearchFinding(
        finding_id="finding-1",
        task_id="task-1",
        summary="Company A funding",
        claims=("Company A funding",),
        evidence_ids=("evidence-1",),
        supported_criteria=("Confirm Company A funding",),
    )
    evidence = [{"evidence_id": "evidence-1", "source_id": "company-a.com"}]
    previous = judge_coverage(brief, [finding], evidence=evidence)
    repeated = judge_coverage(brief, [finding], evidence=evidence, previous=previous)
    assert previous.sufficient is False
    assert repeated.sufficient is False
    assert repeated.status == "gap"
