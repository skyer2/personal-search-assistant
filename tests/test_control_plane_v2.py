from app.agent.harness.state import ExecutionPlan
from app.research.brief.compiler import compile_structured_brief
from app.research.control.gap_precheck import precheck_gap
from app.research.planning.brief_plan import execution_plan_from_brief, validate_brief_plan
from app.research.routing.intent_router import route_intent


def test_intent_router_is_deterministic_and_does_not_call_model():
    first = route_intent("比较 MCP、A2A 和 function calling 的企业级适用场景")
    second = route_intent("比较 MCP、A2A 和 function calling 的企业级适用场景")
    assert first.kind == second.kind == "comparison"
    assert first.signals == second.signals
    assert first.needs_web is True


def test_brief_plan_is_bounded_and_has_hypothesis_contract():
    brief = compile_structured_brief("比较 Cursor 与 Claude Code 的能力边界、成本和风险")
    plan = execution_plan_from_brief(brief)
    assert isinstance(plan, ExecutionPlan)
    assert len(plan.steps) == len(brief.key_questions)
    assert validate_brief_plan(plan, brief=brief) == []
    for step in plan.steps:
        metadata = step.metadata
        assert metadata["question_id"]
        assert metadata["hypothesis"]
        assert len(metadata["entities"]) <= 2
        assert len(metadata["dimensions"]) <= 2
        assert metadata["max_queries"] <= 7
        assert metadata["counter_evidence_needed"]


def test_gap_precheck_skips_supervisor_when_coverage_is_sufficient():
    result = precheck_gap(
        {
            "coverage_judgement": {"sufficient": True, "gaps": []},
            "budget": {"exhausted": False, "max_replan_count": 1},
            "dispatch_wave_id": 1,
        }
    )
    assert result.action == "SYNTHESIZE"
    assert result.reason == "coverage_sufficient"
    assert result.supervisor_calls_avoided == 1


def test_gap_precheck_allows_one_targeted_repair_only_for_actionable_gap():
    result = precheck_gap(
        {
            "coverage_judgement": {"sufficient": False, "gaps": [{"gap_id": "q2", "blocking": True}]},
            "budget": {"exhausted": False, "max_replan_count": 1},
            "dispatch_wave_id": 1,
        }
    )
    assert result.action == "TARGETED_RESEARCH"
    assert result.blocking_gaps == ("q2",)
    assert result.supervisor_calls_avoided == 0


def test_gap_precheck_uses_configured_repair_default_when_snapshot_omits_limit():
    result = precheck_gap(
        {
            "coverage_judgement": {"sufficient": False, "gaps": [{"gap_id": "q3", "blocking": True}]},
            "budget": {"exhausted": False},
            "dispatch_wave_id": 1,
        }
    )
    assert result.action == "TARGETED_RESEARCH"


def test_gap_precheck_stops_at_max_wave_without_model_call():
    result = precheck_gap(
        {
            "coverage_judgement": {"sufficient": False, "gaps": [{"gap_id": "q2", "blocking": True}]},
            "budget": {"exhausted": False, "max_replan_count": 1},
            "dispatch_wave_id": 2,
        }
    )
    assert result.action == "SYNTHESIZE"
    assert result.reason == "max_research_waves"
