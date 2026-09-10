"""Eight-node semantic research graph.

The canonical authorities are StructuredResearchBrief, Supervisor, and the thin
RuntimePolicy. Legacy ResearchSpec/Coverage fields are projections only.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.brief.compiler import compile_structured_brief
from app.research.brief.models import FastPathEligibility, StructuredResearchBrief
from app.research.control.runtime_policy import decide_control
from app.research.control.terminal_policy import terminal_update
from app.research.control.transitions import transition_update
from app.research.coverage.judge import CoverageJudgement, judge_coverage
from app.research.domain.contracts import WorkflowPhase
from app.research.domain.task_state import initialize_tasks, retry_task
from app.research.delivery.partial_renderer import render_partial_delivery, scrub_internal_ids
from app.research.delivery.synthesis_context import EvidenceDigest
from app.research.runtime.ingestion import ingest_new_worker_results
from app.research.runtime.task_identity import execution_task_id, semantic_fingerprint
from app.research.routing.mode_router import canonicalize_mode
from app.research.runtime.state import ResearchState
from app.research.supervisor.agent import SupervisorAgent
from app.research.supervisor.models import ResearchTaskRequest, SupervisorAction

AGENT_GRAPH_NODES = (
    "brief",
    "supervisor",
    "researcher",
    "ingest_findings",
    "coverage_judge",
    "synthesize",
    "quality_gate",
    "finalize",
)


def agent_graph_nodes() -> tuple[str, ...]:
    return AGENT_GRAPH_NODES


def _brief(state: ResearchState) -> StructuredResearchBrief:
    raw = state.get("brief")
    if isinstance(raw, dict) and raw:
        return StructuredResearchBrief.from_dict(raw)
    return compile_structured_brief(
        str(state.get("resolved_query") or state.get("task_query") or ""),
        conversation_delta=str(state.get("conversation_summary") or ""),
    )


def _plan_from_tasks(
    tasks: list[ResearchTaskRequest],
    *,
    plan_version: int,
    planning_mode: str,
) -> ExecutionPlan:
    steps = [
        PlanStep(
            step_type="research",
            description=item.objective,
            objective=item.objective,
            task_id=item.task_id,
            allowed_tools=["internet_search", "fetch_url"],
            metadata={
                "kind": "research_task",
                "task_kind": "supervisor_research",
                "priority": item.priority,
                "target_criteria": list(item.target_criteria),
                "target_gaps": list(item.target_gaps),
                "expected_evidence": list(item.expected_evidence),
                "source_hints": list(item.source_hints),
                "required": True,
                "optional": False,
            },
        )
        for item in tasks
        if str(item.objective).strip()
    ]
    return ExecutionPlan(
        steps=steps,
        summary="Supervisor research action",
        plan_version=plan_version,
        planning_mode=planning_mode,
    )


def _decision_dict(decision: Any) -> dict[str, Any]:
    return {
        "action": decision.action,
        "reason_codes": list(decision.reason_codes),
        "task_ids": list(decision.task_ids),
        "policy_version": "runtime-policy.v1",
    }


def brief_node(state: ResearchState) -> dict[str, Any]:
    brief = _brief(state)
    eligibility = FastPathEligibility.from_brief(brief)
    payload: dict[str, Any] = {
        "brief": brief.to_dict(),
        "fast_path": eligibility.eligible,
        "route_signals": [
            f"brief_intent:{brief.user_intent}",
            "topology:fast_path" if eligibility.eligible else "topology:supervisor_loop",
        ],
    }
    if eligibility.eligible:
        plan = _plan_from_tasks(
            [
                ResearchTaskRequest(
                    brief.objective,
                    priority="high",
                    expected_evidence=("primary source",),
                    target_criteria=tuple(brief.success_criteria or brief.key_questions),
                    task_id="fast_path:search",
                )
            ],
            plan_version=int(state.get("plan_version") or 1),
            planning_mode="brief_fast_path",
        )
        plan.steps[0].step_type = "network_search"
        plan.steps[0].metadata.update({"simple_fact_fast_path": True, "task_kind": "lookup"})
        payload.update({"plan": plan.to_dict(), "tasks": initialize_tasks(plan)})
    return transition_update(state, WorkflowPhase.BRIEF, payload)


def route_after_brief(state: ResearchState) -> Any:
    from langgraph.types import Send

    if not bool(state.get("fast_path")):
        return "supervisor"
    plan = ExecutionPlan.from_dict(state["plan"])
    index = 0
    step = plan.steps[index]
    return Send(
        "researcher",
        {
            **state,
            "phase": WorkflowPhase.EXECUTE.value,
            "task_id": step.resolved_task_id(index),
            "step_index": index,
            "step_type": step.step_type,
            "description": step.description,
            "subagent": step.subagent or "",
            "task_query": state["task_query"],
            "task_metadata": dict(step.metadata or {}),
            "tasks": dict(state.get("tasks") or {}),
        },
    )


def supervisor_node(state: ResearchState) -> dict[str, Any]:
    brief = _brief(state)
    findings = [row for row in state.get("findings") or [] if isinstance(row, dict)]
    judgement_raw = state.get("coverage_judgement")
    judgement = None
    if isinstance(judgement_raw, dict) and judgement_raw:
        from app.research.coverage.judge import CoverageJudgement

        judgement = CoverageJudgement.from_dict(judgement_raw)
    budget = dict(state.get("budget"))
    action = SupervisorAgent(agent=None).fallback_action(brief, judgement, budget)
    action = SupervisorAgent(agent=None).resolve_action(action, judgement, brief)
    from dataclasses import replace

    known = {
        str(item) for item in (state.get("task_fingerprints") or {}).keys() if str(item).strip()
    }
    wave_id = int(state.get("dispatch_wave_id") or 0) + 1
    approved_tasks = []
    fingerprints: dict[str, dict[str, Any]] = {}
    for item in action.research_tasks:
        fingerprint = semantic_fingerprint(
            objective=item.objective,
            target_gaps=item.target_gaps,
            target_criteria=item.target_criteria,
        )
        if fingerprint in known:
            continue
        task_id = execution_task_id(wave_id, fingerprint)
        approved_tasks.append(replace(item, task_id=task_id))
        fingerprints[fingerprint] = {"task_id": task_id, "objective": item.objective}
        known.add(fingerprint)
    action = SupervisorAction(action.action, action.reason, tuple(approved_tasks), action.source)
    payload: dict[str, Any] = {
        "supervisor_action": action.to_dict(),
        "supervisor": {
            "iteration": int((state.get("supervisor") or {}).get("iteration") or 0)
            + (1 if state.get("plan") else 0),
            "last_action": action.action,
            "reasoning_summary": action.reason,
        },
    }
    if action.action == "CONDUCT_RESEARCH" and action.research_tasks:
        plan = _plan_from_tasks(
            list(action.research_tasks),
            plan_version=int(state.get("plan_version") or 1) + (1 if state.get("plan") else 0),
            planning_mode="supervisor_action",
        )
        payload.update(
            {
                "plan": plan.to_dict(),
                "plan_version": plan.plan_version,
                "tasks": initialize_tasks(plan),
                "dispatch_wave_id": wave_id,
                "task_fingerprints": fingerprints,
            }
        )
    enriched = {**state, **payload}
    decision = decide_control(enriched)
    if decision.action == "retry":
        tasks = enriched.get("tasks")
        for task_id in decision.task_ids:
            tasks = retry_task(tasks, task_id)
        payload["tasks"] = tasks
    payload["control_decision"] = _decision_dict(decision)
    return transition_update(state, WorkflowPhase.SUPERVISOR, payload)


def route_supervisor(state: ResearchState) -> Any:
    from langgraph.types import Send

    decision = state.get("control_decision") if isinstance(state.get("control_decision"), dict) else {}
    action = str(decision.get("action") or "")
    if action in {"dispatch", "retry"}:
        plan = ExecutionPlan.from_dict(state["plan"])
        selected = set(decision.get("task_ids") or [])
        sends: list[Any] = []
        for index, step in enumerate(plan.steps):
            task_id = step.resolved_task_id(index)
            if selected and task_id not in selected:
                continue
            sends.append(
                Send(
                    "researcher",
                    {
                        **state,
                        "phase": WorkflowPhase.EXECUTE.value,
                        "task_id": task_id,
                        "step_index": index,
                        "step_type": step.step_type,
                        "description": step.description,
                        "subagent": step.subagent or "",
                        "task_query": state["task_query"],
                        "task_metadata": dict(step.metadata or {}),
                        "tasks": dict(state.get("tasks") or {}),
                    },
                )
            )
        return sends or "coverage_judge"
    if action in {"synthesize", "deliver_partial"}:
        return "synthesize"
    if action == "wait":
        return "coverage_judge"
    return "finalize"


def researcher_node(state: ResearchState) -> dict[str, Any]:
    task_id = str(state.get("task_id") or "researcher")
    summary = f"Research completed for {task_id}"
    return {
        "phase": WorkflowPhase.EXECUTE.value,
        "worker_results": [
            {
                "task_id": task_id,
                "ok": True,
                "status": "done",
                "summary": summary,
                "payload": {"facts": [], "sources": [], "evidence_ids": []},
            }
        ]
    }


def ingest_findings_node(state: ResearchState) -> dict[str, Any]:
    update = ingest_new_worker_results(dict(state))
    return transition_update(state, WorkflowPhase.INGEST_FINDINGS, update)


def coverage_judge_node(state: ResearchState) -> dict[str, Any]:
    brief = _brief(state)
    previous_raw = state.get("coverage_judgement")
    previous = (
        CoverageJudgement.from_dict(previous_raw)
        if isinstance(previous_raw, dict) and previous_raw
        else None
    )
    judgement = judge_coverage(
        brief,
        [row for row in state.get("findings") or [] if isinstance(row, dict)],
        claims=[row for row in state.get("claims") or [] if isinstance(row, dict)],
        evidence=[row for row in state.get("evidence_records") or [] if isinstance(row, dict)],
        previous=previous,
    )
    progress_projection = {
        "status": judgement.status,
        "coverage_ratio": 1.0 if judgement.sufficient else 0.0,
        "unresolved_conflicts": list(judgement.conflicts),
        "missing": list(judgement.missing),
        "missing_ids": list(judgement.missing),
        "semantic_gap_ids": [
            row.criterion_id for row in judgement.criteria if row.status != "supported"
        ],
        "reason_codes": [] if judgement.sufficient else ["coverage_gap"],
    }
    return transition_update(
        state,
        WorkflowPhase.COVERAGE_JUDGE,
        {
            "coverage_judgement": judgement.to_dict(),
            "progress_assessment": progress_projection,
            "control_decision": {},
        },
    )


def route_coverage_judge(state: ResearchState) -> str:
    judgement = state.get("coverage_judgement") if isinstance(state.get("coverage_judgement"), dict) else {}
    decision = state.get("control_decision") if isinstance(state.get("control_decision"), dict) else {}
    if str(decision.get("action") or "") in {"synthesize", "deliver_partial"}:
        return "synthesize"
    return "synthesize" if bool(judgement.get("sufficient")) else "supervisor"


def synthesize_node(state: ResearchState) -> dict[str, Any]:
    brief = _brief(state)
    decision = state.get("control_decision") if isinstance(state.get("control_decision"), dict) else {}
    judgement = state.get("coverage_judgement") if isinstance(state.get("coverage_judgement"), dict) else {}
    findings = [row for row in state.get("findings") or [] if isinstance(row, dict)]
    partial_findings = findings or [
        {"claim": row.get("text"), "evidence_ids": row.get("evidence_ids")}
        for row in state.get("claims") or []
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    ]
    evidence_digests = [
        EvidenceDigest(
            evidence_id=str(row.get("evidence_id") or row.get("source_id") or ""),
            title=str(row.get("title") or row.get("source_id") or "来源")[:120],
            locator=str(row.get("locator") or row.get("source_id") or "")[:240],
            excerpt=str(row.get("excerpt") or "")[:500],
        )
        for row in state.get("evidence_records") or []
        if isinstance(row, dict) and (row.get("evidence_id") or row.get("locator") or row.get("source_id"))
    ]
    if str(decision.get("action") or "") == "deliver_partial" or not bool(judgement.get("sufficient")):
        content = render_partial_delivery(
            objective=brief.objective,
            findings=partial_findings,
            evidence_digests=evidence_digests,
            worker_summaries=[
                {"task_id": row.get("task_id"), "summary": row.get("summary")}
                for row in state.get("worker_results") or []
                if isinstance(row, dict) and row.get("summary")
            ],
            semantic_gaps=[str(item) for item in judgement.get("missing") or []],
            limitations=[],
            unresolved_conflicts=[str(item) for item in judgement.get("conflicts") or []],
            worker_failure_reasons=[],
            synthesis_failure_reason="coverage_gap",
        )
    else:
        content = scrub_internal_ids(
            "\n".join(
                [
                    f"# {brief.objective}",
                    "",
                    *[
                        f"- {row.get('summary') or row.get('claim')}"
                        for row in findings
                        if str(row.get("summary") or row.get("claim") or "").strip()
                    ],
                ]
            )
        )
    return transition_update(
        state,
        WorkflowPhase.SYNTHESIS,
        {"final_content": content, "synthesis_attempts": int(state.get("synthesis_attempts") or 0) + 1, "quality_assessment": {}},
    )


def quality_gate_node(state: ResearchState) -> dict[str, Any]:
    content = str(state.get("final_content") or "").strip()
    evidence = bool(state.get("evidence_records"))
    judgement = state.get("coverage_judgement") if isinstance(state.get("coverage_judgement"), dict) else {}
    issues = []
    if not content:
        issues.append("no_content")
    if not evidence:
        issues.append("no_usable_evidence")
    if not bool(judgement.get("sufficient")):
        issues.append("coverage_gap")
    verdict = (
        "pass"
        if not issues
        else "partial"
        if content and evidence and issues == [item for item in ("coverage_gap", "synthesis_failed") if item in issues]
        else "fail"
    )
    assessment = {
        "verdict": verdict,
        "issues": issues,
        "repairable": False,
        "suggested_action": "",
        "grounding": verdict == "pass",
        "citation_metrics": {"evidence_count": len(state.get("evidence_records") or [])},
    }
    return transition_update(state, WorkflowPhase.QUALITY, {"quality_assessment": assessment})


def route_after_quality(state: ResearchState) -> str:
    assessment = state.get("quality_assessment") if isinstance(state.get("quality_assessment"), dict) else {}
    if str(assessment.get("verdict") or "") == "pass":
        return "finalize"
    if bool(assessment.get("repairable")) and int(state.get("synthesis_attempts") or 0) < 2:
        return "synthesize"
    return "finalize"


def finalize_node(state: ResearchState) -> dict[str, Any]:
    assessment = state.get("quality_assessment") if isinstance(state.get("quality_assessment"), dict) else {}
    return terminal_update(
        state,
        reason=str((state.get("termination") or {}).get("reason") or ""),
        stage=WorkflowPhase.FINALIZE.value,
        research_completed=str(assessment.get("verdict") or "") == "pass",
        synthesis_attempted=bool(state.get("final_content")) or int(state.get("synthesis_attempts") or 0) > 0,
        quality_attempted=bool(assessment),
    )


def vanilla_agent_node(state: ResearchState) -> dict[str, Any]:
    query = str(state.get("resolved_query") or state.get("task_query") or "")
    return transition_update(
        state,
        WorkflowPhase.DIRECT,
        {
            "final_content": f"[direct baseline] {query}".strip(),
            "search_mode": "direct",
            "quality_assessment": {"verdict": "unknown", "grounding": False},
            "plan": None,
        },
    )


def compile_research_graph(
    *,
    checkpointer: Any = None,
    invoke_worker: Any = None,
    runtime: Any = None,
    profile: str = "agent",
):
    from langgraph.graph import END, START, StateGraph

    mode = canonicalize_mode(profile)

    def _worker(payload: dict[str, Any]) -> dict[str, Any]:
        if invoke_worker is not None:
            return invoke_worker(payload)
        return researcher_node(state=cast(ResearchState, dict(payload)))

    if runtime is not None:
        brief = runtime.node_brief
        supervisor = runtime.node_supervisor
        worker = runtime.node_researcher
        ingest = runtime.node_ingest_findings
        coverage = runtime.node_coverage_judge
        synthesize = runtime.node_synthesize
        quality_gate = runtime.node_quality_gate
        finalize = runtime.node_finalize
    else:
        brief = brief_node
        supervisor = supervisor_node
        worker = _worker
        ingest = ingest_findings_node
        coverage = coverage_judge_node
        synthesize = synthesize_node
        quality_gate = quality_gate_node
        finalize = finalize_node

    builder = StateGraph(ResearchState)
    if mode == "direct":
        builder.add_node("vanilla", vanilla_agent_node)
        builder.add_node("finalize", finalize)
        builder.add_edge(START, "vanilla")
        builder.add_edge("vanilla", "finalize")
        builder.add_edge("finalize", END)
        kwargs: dict[str, Any] = {"checkpointer": checkpointer} if checkpointer is not None else {}
        return builder.compile(**kwargs)

    for name, node in {
        "brief": brief,
        "supervisor": supervisor,
        "researcher": worker,
        "ingest_findings": ingest,
        "coverage_judge": coverage,
        "synthesize": synthesize,
        "quality_gate": quality_gate,
        "finalize": finalize,
    }.items():
        builder.add_node(name, node)

    builder.add_edge(START, "brief")
    builder.add_conditional_edges("brief", route_after_brief, ["supervisor", "researcher"])
    builder.add_conditional_edges(
        "supervisor",
        route_supervisor,
        ["researcher", "coverage_judge", "synthesize", "finalize"],
    )
    builder.add_edge("researcher", "ingest_findings")
    builder.add_edge("ingest_findings", "coverage_judge")
    builder.add_conditional_edges("coverage_judge", route_coverage_judge, ["supervisor", "synthesize"])
    builder.add_edge("synthesize", "quality_gate")
    builder.add_conditional_edges("quality_gate", route_after_quality, ["finalize", "synthesize"])
    builder.add_edge("finalize", END)
    kwargs = {"checkpointer": checkpointer} if checkpointer is not None else {}
    return builder.compile(**kwargs)


def initial_graph_state(**kwargs: Any) -> ResearchState:
    from app.research.runtime.state import empty_research_state

    return empty_research_state(**kwargs)


__all__ = [
    "agent_graph_nodes",
    "compile_research_graph",
    "initial_graph_state",
    "route_after_brief",
    "route_after_quality",
    "route_coverage_judge",
    "route_supervisor",
]
