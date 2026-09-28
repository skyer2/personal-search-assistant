from __future__ import annotations

from datetime import datetime
from dataclasses import replace
from zoneinfo import ZoneInfo

from app.agent.harness.orchestration import build_worker_output_instruction
from app.agent.harness.state import PlanStep, StepResult
from app.research.claims.models import ClaimRecord, SupportEdge
from app.research.brief.compiler import compile_structured_brief
from app.research.coverage.answer_units import answer_unit_coverage
from app.research.delivery.unit_models import AnswerUnit, UnitField
from app.research.delivery.unit_assembler import assemble_candidate_unit
from app.research.delivery.unit_validator import validate_answer_units
from app.research.delivery.unit_renderer import render_validated_units
from app.research.domain.completion_v2 import evaluate_completion_v2
from app.research.domain.termination import FinalOutcome, decide_terminal_outcome
from app.research.control.terminal_policy import terminal_update
from app.research.evidence.quality import score_source
from app.research.runtime.isolation import worker_row
from app.research.runtime.ingestion import _v2_source_matches_claim
from app.research.planning.brief_plan import execution_plan_from_brief, validate_brief_plan
from app.research.spec.compiler import compile_research_spec
from app.research.spec.models import AnswerSpec
from app.research.spec.validator import validate_answer_spec
from app.research.evidence.models import EvidenceRecord


def _spec(query: str = "推荐2家有意思的AI初创公司") -> AnswerSpec:
    value = compile_research_spec(
        query,
        as_of=datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        engine_version="answer_contract_v2",
    ).answer_spec
    assert value is not None
    return value


def _supported_claim(claim_id: str, field_id: str, evidence_id: str) -> tuple[ClaimRecord, SupportEdge]:
    claim = ClaimRecord(
        claim_id=claim_id,
        text=f"supported {field_id}",
        field_ids=[field_id],
        evidence_ids=[evidence_id],
        validation_status="supported",
        validation_version="claim-validator-v2",
    )
    edge = SupportEdge(
        edge_id=f"edge:{claim_id}",
        claim_id=claim_id,
        evidence_id=evidence_id,
        span_id="span-1",
        relation="supports",
        checked_by="test-validator",
        validator_version="claim-validator-v2",
    )
    return claim, edge


def _recommendation_unit(spec: AnswerSpec, suffix: str) -> tuple[AnswerUnit, list[ClaimRecord], list[SupportEdge]]:
    fields = {}
    claims: list[ClaimRecord] = []
    edges: list[SupportEdge] = []
    for field_id in ("company", "product"):
        claim, edge = _supported_claim(f"c:{suffix}:{field_id}", field_id, f"e:{suffix}:{field_id}")
        claims.append(claim)
        edges.append(edge)
        fields[field_id] = UnitField(f"{suffix}-{field_id}", "fact", (claim.claim_id,))
    premise, premise_edge = _supported_claim(f"c:{suffix}:reason", "why_interesting", f"e:{suffix}:reason")
    claims.append(premise)
    edges.append(premise_edge)
    fields["why_interesting"] = UnitField(
        "有可验证进展", "inference", (), (premise.claim_id,), "与 verified_progress 标准直接相关"
    )
    fields["limitations"] = UnitField(
        "客户规模尚未验证", "limitation", (), (), "", "not_verified"
    )
    return (
        AnswerUnit(f"unit:{suffix}", spec.asks[0].ask_id, spec.revision, "recommendation", (suffix,), fields),
        claims,
        edges,
    )


def test_spec_uses_fixed_clock_and_one_authoritative_kind() -> None:
    spec = _spec()
    assert spec.schema_version == 2
    assert spec.as_of == "2026-09-28T10:00:00+08:00"
    assert spec.asks[0].kind == "recommendation"
    assert spec.asks[0].target_units == 2
    assert not validate_answer_spec(spec)


def test_comparison_spec_preserves_all_explicit_subjects_and_rejects_one_sided_unit() -> None:
    brief = compile_structured_brief("比较 Cursor 和 Claude Code 的团队适用场景与局限。")
    spec = AnswerSpec.from_dict(brief.answer_spec)
    assert spec.asks[0].entity_scope == {"subjects": ["Cursor", "Claude Code"]}
    one_sided = AnswerUnit(
        "unit:one-sided", "A1", spec.revision, "comparison", ("Cursor",), {},
    )
    checked = validate_answer_units(spec, [one_sided], [], [], evidence_version="e1")[0]
    assert any(
        item.check_id == "comparison_subjects" and item.status == "fail"
        for item in checked.validation.checks
    )


def test_comparison_identity_list_requires_claim_for_each_subject() -> None:
    spec = AnswerSpec.from_dict(
        compile_structured_brief("比较 Cursor 和 Claude Code 的团队适用场景与局限。 ").answer_spec
    )
    candidate = {
        "unit_id": "U1", "entity_ids": ["Cursor", "Claude Code"],
        "fields": {"subjects": {"value": "Cursor 和 Claude Code", "claim_ids": ["C1"]}},
    }
    unit = assemble_candidate_unit(
        spec, spec.asks[0], candidate, task_id="task", index=1,
        supported_claim_ids=["task:C1"],
        claim_text_by_id={"task:C1": "Cursor 和 Claude Code 是两个编码工具"},
    )
    assert unit.fields["subjects"].value == ["Cursor", "Claude Code"]
    assert unit.fields["subjects"].claim_ids == ("task:C1",)
    missing = assemble_candidate_unit(
        spec, spec.asks[0], candidate, task_id="task", index=1,
        supported_claim_ids=["task:C1"],
        claim_text_by_id={"task:C1": "Cursor 是编码工具"},
    )
    assert missing.fields["subjects"].claim_ids == ()


def test_v2_worker_contract_and_row_preserve_exact_answer_unit_fields() -> None:
    step = PlanStep(
        step_type="research",
        description="fact",
        metadata={
            "engine_version": "answer_contract_v2",
            "ask_id": "A1",
            "spec_revision": 1,
            "target_field_ids": ["answer", "scope"],
            "unit_assembly_field_ids": ["answer", "scope"],
        },
    )
    instruction = build_worker_output_instruction(step)
    assert '"answer"' in instruction and '"scope"' in instruction
    result = StepResult(
        step_type="research",
        content="done",
        metadata={
            "worker_payload": {
                "summary": "done",
                "candidate_answer_units": [{"unit_id": "U1", "fields": {"answer": {"value": "x"}}}],
            }
        },
    )
    row = worker_row("task", step, True, result)
    assert row["payload"]["candidate_answer_units"][0]["unit_id"] == "U1"


def test_candidate_cannot_redefine_frozen_answer_fields() -> None:
    spec = _spec("DeepSeek-R1 何时首次发布？请给出日期口径和直接来源。")
    ask = spec.asks[0]
    unit = assemble_candidate_unit(
        spec,
        ask,
        {
            "unit_id": "U1",
            "entity_ids": ["DeepSeek-R1"],
            "fields": {"first_release_date": "2025-01-20", "invented": "ignored"},
        },
        task_id="t1",
        index=1,
        supported_claim_ids=["C1"],
        claim_text_by_id={"C1": "DeepSeek-R1 于 2025-01-20 首次发布"},
    )
    assert unit.fields == {}


def test_candidate_field_requires_matching_supported_claim_text() -> None:
    spec = _spec("DeepSeek-R1 何时首次发布？")
    unit = assemble_candidate_unit(
        spec,
        spec.asks[0],
        {"fields": {"answer": {"value": "2025-01-20", "claim_ids": ["C1"]}}},
        task_id="t1",
        index=1,
        supported_claim_ids=["t1:C1"],
        claim_text_by_id={"t1:C1": "DeepSeek-R1 于 2025 年 1 月 20 日首次发布。"},
    )
    assert unit.fields["answer"].claim_ids == ("t1:C1",)
    unsupported = assemble_candidate_unit(
        spec,
        spec.asks[0],
        {"fields": {"answer": {"value": "2025-01-27", "claim_ids": ["C1"]}}},
        task_id="t2",
        index=1,
        supported_claim_ids=["t2:C1"],
        claim_text_by_id={"t2:C1": "DeepSeek-R1 于 2025-01-20 首次发布。"},
    )
    assert unsupported.fields["answer"].claim_ids == ()


def test_candidate_rebinds_only_to_exact_validated_task_claim() -> None:
    spec = _spec("DeepSeek-R1 何时首次发布？")
    unit = assemble_candidate_unit(
        spec,
        spec.asks[0],
        {"fields": {"answer": {"value": "2025年1月20日", "claim_ids": ["C1"]}}},
        task_id="t1",
        index=1,
        supported_claim_ids=["t1:C5"],
        claim_text_by_id={"t1:C5": "2025年1月20日 DeepSeek-R1 正式发布。"},
    )
    assert unit.fields["answer"].claim_ids == ("t1:C5",)


def test_inference_rebinds_to_same_unit_verified_fact() -> None:
    spec = _spec()
    unit = assemble_candidate_unit(
        spec,
        spec.asks[0],
        {"fields": {
            "company": {"value": "Elythea", "claim_ids": ["C1"]},
            "product": {"value": "机器学习预防产妇死亡", "claim_ids": ["C2"]},
            "why_interesting": {
                "value": "医疗场景价值高", "premise_claim_ids": ["C99"],
                "rationale": "基于已核实的产品用途",
            },
        }},
        task_id="t1",
        index=1,
        supported_claim_ids=["t1:C5"],
        claim_text_by_id={"t1:C5": "Elythea 使用机器学习预防产妇死亡。"},
    )
    assert unit.fields["why_interesting"].premise_claim_ids == ("t1:C5",)


def test_v2_plan_uses_frozen_asks_not_brief_expansions(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_RESEARCH_ENGINE_VERSION", "answer_contract_v2")
    brief = compile_structured_brief("DeepSeek-R1 何时首次发布？请给出日期口径和直接来源。")
    expanded = replace(brief, key_questions=("日期是什么？", "论文是什么？", "谁发布？"))
    plan = execution_plan_from_brief(expanded)
    assert len(plan.steps) == 1
    assert plan.steps[0].metadata["ask_id"] == "A1"
    assert [row["field_id"] for row in plan.steps[0].metadata["unit_assembly_field_specs"]] == ["answer", "scope"]
    assert plan.steps[0].metadata["target_units"] == 1
    assert not validate_brief_plan(plan, brief=expanded)


def test_v2_rejects_official_claim_bound_to_repost() -> None:
    repost = EvidenceRecord(
        evidence_id="E1",
        source_id="example.com",
        source_kind="web",
        locator="https://example.com/repost",
        source_type="secondary",
    )
    assert not _v2_source_matches_claim(
        "官方公告 https://api-docs.deepseek.com/news/news250120 发布了 DeepSeek-R1。",
        [repost],
    )


def test_v2_terminal_never_upgrades_missing_completion_and_is_idempotent() -> None:
    state = {
        "engine_version": "answer_contract_v2",
        "final_content": "看似完整的答案 [1]",
        "quality_assessment": {"verdict": "pass"},
        "evidence_records": [{"evidence_id": "E1"}],
    }
    assert decide_terminal_outcome(state) == FinalOutcome.FAILED
    first = terminal_update(state, reason="", stage="finalize")
    second = terminal_update({**state, **first}, reason="different", stage="finalize")
    assert first == second


def test_invalid_unit_bounds_fail_closed() -> None:
    row = _spec().to_dict()
    row["asks"][0]["min_partial_units"] = 3
    assert "invalid_unit_bounds:A1" in validate_answer_spec(row)


def test_worker_confidence_cannot_validate_a_unit() -> None:
    spec = _spec()
    unit, claims, edges = _recommendation_unit(spec, "A")
    for claim in claims:
        claim.validation_status = "pending"
        claim.confidence = 1.0
    validated = validate_answer_units(spec, [unit], claims, edges, evidence_version="evidence-v1")
    assert validated[0].validation.status == "invalid"


def test_supported_edges_produce_valid_unit() -> None:
    spec = _spec()
    unit, claims, edges = _recommendation_unit(spec, "A")
    validated = validate_answer_units(spec, [unit], claims, edges, evidence_version="evidence-v1")
    assert validated[0].validation.status == "valid"


def test_validated_unit_renderer_closes_reference_numbers() -> None:
    spec = _spec()
    unit, claims, edges = _recommendation_unit(spec, "A")
    valid = validate_answer_units(spec, [unit], claims, edges, evidence_version="evidence-v1")
    evidence = [
        {"evidence_id": edge.evidence_id, "locator": f"https://example.com/{index}", "source_id": "example.com"}
        for index, edge in enumerate(edges, 1)
    ]
    numbers = {edge.evidence_id: index for index, edge in enumerate(edges, 1)}
    rendered = render_validated_units(spec, valid, claims, edges, evidence, numbers)
    assert "## 参考来源" in rendered.content
    assert "[1] 《example.com》，https://example.com/1" in rendered.content


def test_coverage_keeps_target_slots_in_denominator() -> None:
    spec = _spec()
    unit, claims, edges = _recommendation_unit(spec, "A")
    valid = validate_answer_units(spec, [unit], claims, edges, evidence_version="evidence-v1")
    coverage = answer_unit_coverage(spec, valid)
    assert coverage["required_field_slots"] == 8
    assert coverage["covered_required_field_slots"] == 4
    assert coverage["coverage_ratio"] == 0.5
    assert not coverage["sufficient"]


def test_completion_is_partial_for_one_of_two_units() -> None:
    spec = _spec()
    unit, claims, edges = _recommendation_unit(spec, "A")
    valid = validate_answer_units(spec, [unit], claims, edges, evidence_version="evidence-v1")
    completion = evaluate_completion_v2(
        spec,
        valid,
        evidence_version="evidence-v1",
        answer_version="answer-v1",
        final_document="validated output [1]",
        validation_versions={"unit": "unit-validator-v2"},
        citation_valid=True,
        quality_dimensions={"source": "pass", "relevance": "pass"},
    )
    assert completion.outcome == "partial"
    assert completion.per_ask[0].valid_units == 1
    assert completion.per_ask[0].target_units == 2


def test_completion_fails_with_zero_units_and_unknown_quality() -> None:
    spec = _spec()
    completion = evaluate_completion_v2(
        spec,
        [],
        evidence_version="evidence-v0",
        answer_version="answer-v0",
        final_document="diagnostic only",
        validation_versions={"unit": "unit-validator-v2"},
        citation_valid=None,
        quality_dimensions={"source": "unknown", "relevance": "unknown"},
    )
    assert completion.outcome == "failed"
    assert not completion.passed


def test_url_wording_does_not_upgrade_source_quality() -> None:
    quality = score_source(
        "https://example.com/official/blog/docs",
        content_kind="fulltext",
        publisher_relationship="unknown",
    )
    assert quality.source_type == "secondary"
    assert quality.independence == 0.0


def test_first_party_requires_exact_brand_domain_and_fetched_title() -> None:
    from app.agent.harness.artifacts import ArtifactStore, reset_artifact_store, set_artifact_store
    from app.research.runtime.ingestion import _verified_first_party_page

    store = ArtifactStore()
    set_artifact_store(store)
    try:
        official = store.put(
            "DeepSeek-R1 Release 2025/01/20",
            kind="web",
            locator="https://api-docs.deepseek.com/news/news250120",
            title="DeepSeek-R1 Release | DeepSeek API Docs",
        )
        fake = store.put(
            "DeepSeek-R1 Release 2025/01/20",
            kind="web",
            locator="https://deepseek-fan.example/news",
            title="DeepSeek-R1 Release | DeepSeek fan blog",
        )
        assert _verified_first_party_page(official.locator, official.artifact_id, ["DeepSeek-R1"])
        assert not _verified_first_party_page(fake.locator, fake.artifact_id, ["DeepSeek-R1"])
    finally:
        reset_artifact_store()
