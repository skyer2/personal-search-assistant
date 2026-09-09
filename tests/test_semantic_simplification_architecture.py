"""Executable architecture invariants for the semantic-simplification cutover."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_mode_router_does_not_classify_task_shape() -> None:
    from app.research.routing.mode_router import route

    decision = route("写一份 2026 年脑机接口商业化前景深度报告。")
    assert decision.mode == "agent"
    assert decision.execution_path == "harness"
    assert decision.task_shape == ""
    assert decision.signals == ["agent"]


def test_graph_has_at_most_eight_agent_nodes() -> None:
    from app.research.runtime.graph import agent_graph_nodes

    nodes = agent_graph_nodes()
    assert len(nodes) <= 8
    assert list(nodes) == [
        "brief",
        "supervisor",
        "researcher",
        "ingest_findings",
        "coverage_judge",
        "synthesize",
        "quality_gate",
        "finalize",
    ]


def test_runtime_policy_has_no_semantic_actions() -> None:
    from app.research.control.runtime_policy import SEMANTIC_ACTIONS, decide_control

    assert SEMANTIC_ACTIONS == frozenset()
    decision = decide_control(
        {
            "coverage_judgement": {
                "sufficient": True,
                "status": "sufficient",
                "missing": [],
            },
            "supervisor_action": {"action": "COMPLETE"},
            "evidence_records": [{"evidence_id": "evidence_1"}],
        }
    )
    assert decision.action == "synthesize"


def test_structured_brief_is_fast_path_authority() -> None:
    from app.research.brief.compiler import compile_structured_brief
    from app.research.brief.models import FastPathEligibility

    query = "你觉得目前 Agent 的发展关注点是什么？未来1年可预期的进展和技术路径是什么？"
    brief = compile_structured_brief(query)
    assert FastPathEligibility.from_brief(brief).eligible is False
    assert brief.freshness_requirements.required is True
    assert "trend" in brief.user_intent or "forecast" in brief.user_intent


def test_numeric_year_does_not_make_long_report_fast_path() -> None:
    from app.research.brief.compiler import compile_structured_brief
    from app.research.brief.models import FastPathEligibility

    query = "写一份 2026 年脑机接口商业化前景深度报告。"
    brief = compile_structured_brief(query)
    assert FastPathEligibility.from_brief(brief).eligible is False
    assert brief.user_intent == "structured_report"
    assert brief.deliverable.format == "markdown"


def test_explicit_comparison_has_no_candidate_discovery_requirement() -> None:
    from app.research.brief.compiler import compile_structured_brief

    brief = compile_structured_brief("对比 GPT、Gemini、DeepSeek 的 Agent 能力。")
    assert list(brief.explicit_subjects) == ["GPT", "Gemini", "DeepSeek"]
    assert "candidate discovery" not in " ".join(brief.key_questions).lower()


def test_supervisor_conducts_research_for_actionable_gap() -> None:
    from app.research.coverage.judge import CoverageJudgement
    from app.research.supervisor.agent import SupervisorAgent
    from app.research.supervisor.models import SupervisorAction

    judgement = CoverageJudgement(
        sufficient=False,
        status="gap",
        missing=["缺少最近一年 Agent reliability / eval 进展"],
        recommended_next_questions=[
            "当前主流 Agent harness 在 reliability 上有哪些共同设计？"
        ],
    )
    action = SupervisorAction(action="CONDUCT_RESEARCH", reason="coverage gap", research_tasks=[])
    supervisor = SupervisorAgent(agent=None)
    resolved = supervisor.resolve_action(action, judgement)
    assert resolved.action == "CONDUCT_RESEARCH"
    assert resolved.research_tasks


def test_findings_are_compressed_and_evidence_backed() -> None:
    from app.research.findings.compress import compress_worker_result

    finding = compress_worker_result(
        task_id="task_1",
        summary="Reliability evidence was collected.",
        claims=["Agent harnesses expose reliability telemetry."],
        evidence_ids=["evidence_1"],
        confidence=0.9,
    )
    assert list(finding.evidence_ids) == ["evidence_1"]
    assert finding.claims
    assert finding.finding_id


def test_legacy_semantic_modules_are_deprecated_projections() -> None:
    from app.research.runtime import legacy

    registry = legacy.legacy_registry()
    assert registry["research_spec"] == "projection"
    assert registry["coverage_contract"] == "projection"
    assert registry["semantic_gaps"] == "projection"


def test_trace_summary_projects_semantic_loop_events() -> None:
    from app.observability.journal import summarize_trace

    events = [
        {
            "type": "topology.decided",
            "span_id": "span_topology",
            "attributes": {
                "topology": "supervisor_loop",
                "eligible": False,
                "reasons": ["requires_synthesis"],
            },
        },
        {
            "type": "supervisor.decided",
            "span_id": "span_supervisor",
            "plan_version": 1,
            "attributes": {
                "action": "CONDUCT_RESEARCH",
                "reason": "initial research",
                "task_count": 2,
                "source": "supervisor",
                "runtime_action": "dispatch",
                "runtime_reasons": ["ready_tasks"],
            },
        },
        {
            "type": "finding.compressed",
            "task_id": "task_1",
            "span_id": "span_finding",
            "attributes": {
                "finding_id": "finding_1",
                "evidence_ids": ["evidence_1"],
                "claim_count": 2,
                "confidence": 0.8,
                "limitations": ["sample_size"],
            },
        },
        {
            "type": "coverage.assessed",
            "span_id": "span_coverage",
            "plan_version": 1,
            "attributes": {
                "status": "gap",
                "sufficient": False,
                "missing": ["primary evidence"],
                "conflicts": [],
                "weak_claims": ["claim_1"],
                "recommended_next_questions": ["find primary source"],
                "source": "llm",
                "reason": "coverage gap",
            },
        },
    ]

    summary = summarize_trace(events)
    assert summary["topology"]["topology"] == "supervisor_loop"
    assert summary["supervisor_decision_count"] == 1
    assert summary["supervisor_decisions"][0]["action"] == "CONDUCT_RESEARCH"
    assert summary["finding_count"] == 1
    assert summary["findings"][0]["evidence_ids"] == ["evidence_1"]
    assert summary["coverage_judgement_count"] == 1
    assert summary["coverage_judgements"][0]["missing"] == ["primary evidence"]


def test_supervisor_iteration_limit_emits_control_decision(monkeypatch) -> None:
    from app.agent.harness.state import LoopState
    import app.research.runtime.runner as runner_module
    from app.research.runtime.runner import ResearchGraphRunner
    from app.research.runtime.state import empty_research_state
    from app.research.supervisor import agent as supervisor_module
    from app.research.supervisor.models import SupervisorAction

    class FixedSupervisor:
        async def decide(self, *_args, **_kwargs):
            return SupervisorAction(action="CONDUCT_RESEARCH", reason="iteration limit")

        def resolve_action(self, action, *_args, **_kwargs):
            return action

    state = empty_research_state(
        run_id="run-supervisor-limit",
        session_id="session-supervisor-limit",
        task_query="research an open-ended topic",
    )
    state["supervisor"] = {"iteration": 3, "last_action": "", "reasoning_summary": ""}
    state["budget"]["max_replan_count"] = 3
    state["phase"] = "coverage_judge"
    session = SimpleNamespace(
        run_id="run-supervisor-limit",
        session_id="session-supervisor-limit",
        state=LoopState(session_id="session-supervisor-limit"),
        budget_manager=SimpleNamespace(
            max_tool_calls=80,
            llm_calls=0,
            max_llm_calls=80,
            total_tokens=0,
            max_total_tokens=300000,
            remaining_run_sec=lambda: 1800.0,
            synthesis_reserve_sec=180.0,
        ),
        active_wave_size=1,
    )
    emitted = []
    monkeypatch.setattr(
        runner_module,
        "_emit",
        lambda _session, event_type, **kwargs: emitted.append((event_type, kwargs)),
    )
    monkeypatch.setattr(supervisor_module, "SupervisorAgent", lambda *_args: FixedSupervisor())
    runner = ResearchGraphRunner(SimpleNamespace(agent=None))
    runner_module.bind_session(session)
    try:
        update = asyncio.run(runner.node_supervisor(state))
    finally:
        runner_module.drop_session("run-supervisor-limit")

    assert update["control_decision"]["action"] == "finalize_failure"
    control_events = [kwargs for event_type, kwargs in emitted if event_type == "control.decided"]
    assert control_events
    assert control_events[-1]["status"] == "finalize_failure"
