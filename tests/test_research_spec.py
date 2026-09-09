from app.research.spec.compiler import compile_research_spec
from app.research.spec.validator import validate_research_spec
from app.research.coverage.compiler import compile_coverage_contract
from app.research.planning.planner import plan_for_spec
from app.research.planning.gap_fill import gap_fill


def test_spec_is_stable_and_serializable():
    first = compile_research_spec("对比 OpenAI、Anthropic 和 Google 的最新研究架构")
    second = compile_research_spec("对比 OpenAI、Anthropic 和 Google 的最新研究架构")
    assert first.spec_id == second.spec_id
    restored = compile_research_spec
    _ = restored
    assert first.task_shape == "BREADTH_HEAVY"
    assert first.reasoning_requirements.compare
    assert first.evidence_requirements.freshness_required
    assert validate_research_spec(first) == []
    assert ResearchSpecRoundtrip(first.to_dict()).version == 1


class ResearchSpecRoundtrip:
    def __init__(self, data):
        from app.research.spec.models import ResearchSpec

        self.version = ResearchSpec.from_dict(data).version


def test_empty_spec_has_blocking_ambiguity():
    spec = compile_research_spec("")
    assert validate_research_spec(spec) == [
        "empty_objective",
        "no_subjects",
        "no_dimensions",
        "blocking_ambiguity",
    ]


def test_offline_source_policy_flows_into_spec_and_plan():
    spec = compile_research_spec("不要联网，只根据内部附件比较两家公司的交付进度")
    contract = compile_coverage_contract(spec)
    plan = plan_for_spec(spec, contract)
    assert spec.source_policy.allowed == ["file"]
    assert spec.source_policy.forbidden == ["web"]
    assert plan.steps
    assert all(step.metadata["allowed_sources"] == ["file"] for step in plan.steps)
    assert all(step.allowed_tools == ["read_file_content"] for step in plan.steps)
    result = gap_fill(
        spec,
        {
            "gap_offline": {
                "gap_id": "gap_offline",
                "gap_type": "coverage",
                "subject_id": "subject_1",
                "dimension_id": "delivery",
                "actionable": True,
                "attempt_count": 0,
            }
        },
        plan_version=1,
    )
    assert result.applied
    assert all(step.allowed_tools == ["read_file_content"] for step in result.plan.steps)
