"""Contract-driven Research StateGraph. ControlPolicy is the route authority."""

from __future__ import annotations

from typing import Any, Literal, cast

from app.agent.harness.state import ExecutionPlan
from app.research.assessment.delivery import assess_delivery
from app.research.assessment.evidence import assess_evidence
from app.research.assessment.execution_health import assess_execution_health
from app.research.assessment.progress import assess_progress
from app.research.coverage.compiler import compile_coverage_contract
from app.research.control.policy import decide_control
from app.research.control.terminal_policy import terminal_update
from app.research.control.transitions import transition_update
from app.research.domain.contracts import ControlAction, WorkflowPhase, action_budget_from_state
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    initialize_tasks,
    retry_task,
    transition_task,
)
from app.research.planning.expansion import expand_plan
from app.research.planning.gap_fill import gap_fill
from app.research.planning.planner import plan_for_spec
from app.research.planning.replan import replan
from app.research.planning.validator import validate_execution_plan
from app.research.runtime.semantic_ingest import ingest_semantics
from app.research.runtime.state import ResearchState, empty_research_state
from app.research.routing.mode_router import canonicalize_mode
from app.research.spec.compiler import compile_research_spec
from app.research.spec.models import ResearchSpec
from app.research.spec.validator import validate_research_spec


class GraphInvariantViolation(RuntimeError):
    """Raised only for programming-level graph invariant violations."""


def _plan_from_state(state: ResearchState) -> ExecutionPlan | None:
    if not state.get("plan"):
        return None
    return ExecutionPlan.from_dict(state["plan"])


def compile_spec_node(state: ResearchState) -> dict[str, Any]:
    spec = compile_research_spec(
        str(state.get("resolved_query") or state.get("task_query") or ""),
        conversation_delta=str(state.get("conversation_summary") or ""),
        existing_spec=state.get("research_spec") if isinstance(state.get("research_spec"), dict) and state.get("research_spec") else None,
    )
    contract = compile_coverage_contract(spec)
    return transition_update(
        state,
        WorkflowPhase.COMPILE_SPEC,
        {
            "research_spec": spec.to_dict(),
            "coverage_contract": contract.to_dict(),
            "route_signals": [f"task_shape:{spec.task_shape}"],
        },
    )


def spec_gate_node(state: ResearchState) -> dict[str, Any]:
    issues = validate_research_spec(state.get("research_spec"))
    blocking = any(issue in {"blocking_ambiguity", "contradicted_premise"} for issue in issues)
    return transition_update(
        state,
        WorkflowPhase.SPEC_GATE,
        {
            "needs_clarification": blocking,
            "route_signals": [*(state.get("route_signals") or []), *(f"spec_issue:{issue}" for issue in issues)],
        },
    )


def route_after_spec_gate(state: ResearchState) -> Literal["clarify", "plan"]:
    return "clarify" if state.get("needs_clarification") else "plan"


def clarify_node(state: ResearchState) -> dict[str, Any]:
    spec = ResearchSpec.from_dict(state.get("research_spec"))
    for ambiguity in spec.ambiguities:
        if ambiguity.blocking and not ambiguity.resolution:
            ambiguity.resolution = "auto_resolved: retain the original objective and disclose the assumption"
            spec.assumptions.append(f"{ambiguity.text}: auto-resolved by retaining the original objective")
    spec.interaction_requirements.requires_clarification = False
    return transition_update(
        state,
        WorkflowPhase.CLARIFY,
        {
            "research_spec": spec.to_dict(),
            "needs_clarification": False,
            "conversation_summary": str(state.get("conversation_summary") or ""),
        },
    )


def plan_node(state: ResearchState) -> dict[str, Any]:
    plan = plan_for_spec(
        state.get("research_spec"),
        state.get("coverage_contract"),
        candidate_set=state.get("candidate_set"),
        plan_version=int(state.get("plan_version") or 1),
    )
    tasks = initialize_tasks(plan)
    return transition_update(
        state,
        WorkflowPhase.PLAN,
        {
            "plan": plan.to_dict(),
            "plan_version": plan.plan_version,
            "tasks": tasks,
            "needs_plan_review": False,
        },
    )


def plan_validate_node(state: ResearchState) -> dict[str, Any]:
    plan = _plan_from_state(state)
    issues = validate_execution_plan(
        plan,
        spec=state.get("research_spec"),
        coverage_contract=state.get("coverage_contract"),
        candidate_set=state.get("candidate_set"),
    ) if plan is not None else ["missing_plan"]
    payload: dict[str, Any] = {}
    if issues:
        payload["abort_reason"] = f"plan_validation_failed:{','.join(issues)}"
    return transition_update(state, WorkflowPhase.PLAN_VALIDATED, payload)


def _decision(state: ResearchState) -> dict[str, Any]:
    return decide_control(cast(dict[str, Any], state))


def _state_decision(state: ResearchState) -> dict[str, Any]:
    decision = state.get("control_decision")
    if isinstance(decision, dict) and decision.get("action"):
        return dict(decision)
    return _decision(state)


def dispatch_node(state: ResearchState) -> dict[str, Any]:
    decision = _decision(state)
    payload: dict[str, Any] = {"control_decision": decision}
    if decision["action"] == ControlAction.DISPATCH.value:
        payload["dispatch_wave_id"] = int(state.get("dispatch_wave_id") or 0) + 1
    return transition_update(state, WorkflowPhase.DISPATCH, payload)


def route_dispatch(state: ResearchState) -> list[Any] | str:
    from langgraph.types import Send

    decision = _state_decision(state)
    action = decision["action"]
    if action == ControlAction.DISPATCH.value:
        plan = _plan_from_state(state)
        if plan is None:
            return "finalize"
        sends: list[Any] = []
        selected = set(decision.get("task_ids") or [])
        for index, step in enumerate(plan.steps):
            task_id = step.resolved_task_id(index)
            if selected and task_id not in selected:
                continue
            sends.append(
                Send(
                    "research_worker",
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
        if sends:
            return sends
        return "ingest_semantics"
    if action == ControlAction.RETRY.value:
        return "retry"
    if action == ControlAction.GAP_FILL.value:
        return "gap_fill"
    if action == ControlAction.EXPAND_PLAN.value:
        return "expand_plan"
    if action == ControlAction.REPLAN.value:
        return "replan"
    if action in {ControlAction.SYNTHESIZE.value, ControlAction.DELIVER_PARTIAL.value}:
        return "synthesize"
    if action == ControlAction.WAIT.value:
        return "dispatch"
    return "finalize"


def research_worker_node(state: ResearchState) -> dict[str, Any]:
    task_id = str(state.get("task_id") or "")
    current = dict(state.get("tasks") or {}).get(task_id)
    if current is not None and current.get("execution_status") != TaskExecutionStatus.PENDING.value:
        return {
            "worker_results": [
                {
                    "task_id": task_id,
                    "ok": False,
                    "status": "duplicate_skipped",
                    "summary": "duplicate task attempt suppressed",
                }
            ]
        }
    running = transition_task(state.get("tasks"), task_id, execution_status=TaskExecutionStatus.RUNNING)
    tasks = transition_task(
        running,
        task_id,
        execution_status=TaskExecutionStatus.SUCCEEDED,
        result_status=ResultStatus.COMPLETE,
        evidence_refs=[f"evidence:{task_id}:primary", f"evidence:{task_id}:secondary"],
    )
    evidence_ids = [f"evidence:{task_id}:primary", f"evidence:{task_id}:secondary"]
    objective = str(state.get("description") or state.get("task_query") or "")
    metadata = dict(state.get("task_metadata") or {})
    candidates: list[dict[str, Any]] = []
    if str(metadata.get("task_kind") or "") == "discovery":
        target_items = max(1, int(metadata.get("target_items") or 4))
        candidates = [
            {"candidate_id": f"candidate_{index}", "name": f"Candidate {index}"}
            for index in range(1, target_items + 1)
        ]
    return {
        "tasks": {task_id: tasks[task_id]},
        "worker_results": [
                {
                    "task_id": task_id,
                    "task_metadata": metadata,
                    "ok": True,
                "status": "succeeded",
                "summary": objective,
                "payload": {
                        "findings": [{"claim": objective, "evidence_ids": evidence_ids, "confidence": 0.9}],
                        "facts": [objective],
                        "candidates": candidates,
                        "sources": [f"https://primary.example/{task_id}", f"https://secondary.example/{task_id}"],
                    "evidence_ids": evidence_ids,
                    "confidence": 0.9,
                },
            }
        ],
        "evidence_refs": evidence_ids,
        "artifact_refs": [f"artifact:{task_id}"],
    }


def dispatch_barrier_node(_state: ResearchState) -> dict[str, Any]:
    """Join all worker branches before one semantic ingest superstep."""
    return {}


def ingest_semantics_node(state: ResearchState) -> dict[str, Any]:
    return transition_update(state, WorkflowPhase.INGEST_SEMANTICS, ingest_semantics(dict(state)))


def assess_node(state: ResearchState) -> dict[str, Any]:
    progress = assess_progress(dict(state))
    evidence = assess_evidence(dict(state))
    health = assess_execution_health(dict(state))
    delivery = assess_delivery(dict(state))
    enriched = {
        **state,
        "progress_assessment": progress,
        "evidence_assessment": evidence,
        "execution_health": health,
        "delivery_readiness": delivery,
    }
    decision = decide_control(enriched)
    return transition_update(
        state,
        WorkflowPhase.ASSESS,
        {
            "progress_assessment": progress,
            "evidence_assessment": evidence,
            "execution_health": health,
            "delivery_readiness": delivery,
            "control_decision": decision,
        },
    )


def route_assess(state: ResearchState) -> str:
    action = _state_decision(state)["action"]
    if action in {ControlAction.DISPATCH.value, ControlAction.WAIT.value}:
        return "dispatch"
    if action == ControlAction.RETRY.value:
        return "retry"
    if action == ControlAction.GAP_FILL.value:
        return "gap_fill"
    if action == ControlAction.EXPAND_PLAN.value:
        return "expand_plan"
    if action == ControlAction.REPLAN.value:
        return "replan"
    if action in {ControlAction.SYNTHESIZE.value, ControlAction.DELIVER_PARTIAL.value}:
        return "synthesize"
    return "finalize"


def _increment_action(state: ResearchState, action: str) -> dict[str, int]:
    budget = action_budget_from_state(dict(state))
    budget[action] = int(budget.get(action) or 0) + 1
    return budget


def gap_fill_node(state: ResearchState) -> dict[str, Any]:
    budget = action_budget_from_state(dict(state))
    result = gap_fill(
        state.get("research_spec"),
        state.get("semantic_gaps"),
        plan_version=int(state.get("plan_version") or 1),
        max_tasks=max(0, int(budget["max_gap_fill"] - budget["gap_fill"])),
    )
    if not result.applied:
        return transition_update(state, WorkflowPhase.GAP_FILL, {"abort_reason": result.reason})
    tasks = {**state.get("tasks", {}), **initialize_tasks(result.plan)}
    return transition_update(
        state,
        WorkflowPhase.GAP_FILL,
        {
            "plan": result.plan.to_dict(),
            "plan_version": result.plan.plan_version,
            "tasks": tasks,
            "semantic_gaps": result.semantic_gaps,
            "action_budget": _increment_action(state, "gap_fill"),
        },
    )


def expand_plan_node(state: ResearchState) -> dict[str, Any]:
    result = expand_plan(
        state.get("research_spec"),
        state.get("candidate_set"),
        plan_version=int(state.get("plan_version") or 1),
    )
    if not result.applied:
        return transition_update(state, WorkflowPhase.EXPAND_PLAN, {"abort_reason": result.reason})
    tasks = {**state.get("tasks", {}), **initialize_tasks(result.plan)}
    return transition_update(
        state,
        WorkflowPhase.EXPAND_PLAN,
        {
            "candidate_set": result.candidate_set,
            "coverage_contract": result.coverage_contract.to_dict(),
            "plan": result.plan.to_dict(),
            "plan_version": result.plan.plan_version,
            "tasks": tasks,
            "action_budget": _increment_action(state, "expand_plan"),
        },
    )


def replan_node(state: ResearchState) -> dict[str, Any]:
    result = replan(
        state.get("research_spec"),
        state.get("plan"),
        state.get("semantic_gaps"),
        state.get("tasks"),
        reason="semantic_gain_low",
        plan_version=int(state.get("plan_version") or 1),
    )
    if not result.applied:
        return transition_update(state, WorkflowPhase.REPLAN, {"abort_reason": result.reason})
    tasks = {**result.tasks, **initialize_tasks(result.plan)}
    return transition_update(
        state,
        WorkflowPhase.REPLAN,
        {
            "plan": result.plan.to_dict(),
            "plan_version": result.plan.plan_version,
            "tasks": tasks,
            "semantic_gaps": result.semantic_gaps,
            "action_budget": _increment_action(state, "replan"),
        },
    )


def retry_node(state: ResearchState) -> dict[str, Any]:
    decision = _state_decision(state)
    tasks = dict(state.get("tasks") or {})
    for task_id in decision.get("task_ids") or []:
        tasks = retry_task(tasks, str(task_id))
    return {
        "tasks": tasks,
        "action_budget": _increment_action(state, "retry"),
        "control_decision": {},
    }


def _semantic_digest(state: ResearchState, *, compact: bool) -> str:
    spec = ResearchSpec.from_dict(state.get("research_spec"))
    claims = [row for row in state.get("claims") or [] if isinstance(row, dict)]
    coverage = state.get("coverage_state") if isinstance(state.get("coverage_state"), dict) else {}
    lines = [f"# {spec.objective}", ""]
    selected = claims[:12] if compact else claims[:40]
    for claim in selected:
        evidence_ids = ", ".join(str(item) for item in claim.get("evidence_ids") or [])
        lines.append(f"- {claim.get('text') or claim.get('claim')} [{evidence_ids}]")
    limitations = [str(item) for item in coverage.get("missing_ids") or []]
    if limitations:
        lines.extend(["", "## Known limitations", *[f"- Uncovered coverage unit: {item}" for item in limitations[:8 if compact else 20]]])
    conflicts = [str(item) for item in coverage.get("conflicted_ids") or []]
    if conflicts:
        lines.extend(["", "## Conflict disclosures", *[f"- Unresolved coverage unit: {item}" for item in conflicts[:8 if compact else 20]]])
    return "\n".join(lines)


def synthesize_node(state: ResearchState) -> dict[str, Any]:
    compact = int(state.get("synthesis_attempts") or 0) >= 1
    content = _semantic_digest(state, compact=compact)
    return transition_update(
        state,
        WorkflowPhase.SYNTHESIS,
        {
            "final_content": content,
            "synthesis_attempts": int(state.get("synthesis_attempts") or 0) + 1,
            "synthesis_failed": False,
            "delivery_readiness": assess_delivery(dict(state)),
        },
    )


def _quality_assessment(state: ResearchState) -> dict[str, Any]:
    content = str(state.get("final_content") or "").strip()
    progress = assess_progress(dict(state))
    evidence = assess_evidence(dict(state))
    issues: list[str] = []
    if not content:
        issues.append("no_content")
    if bool(state.get("synthesis_failed")):
        issues.append("synthesis_failed")
    if progress["status"] != "sufficient":
        issues.append("coverage_gate_failed")
    if progress["unresolved_conflicts"]:
        issues.append("blocking_conflict_unresolved")
    evidence_ids = {str(row.get("evidence_id") or "") for row in state.get("evidence_records") or [] if isinstance(row, dict)}
    claims = [row for row in state.get("claims") or [] if isinstance(row, dict)]
    unsupported = [
        claim
        for claim in claims
        if not [item for item in claim.get("evidence_ids") or [] if str(item) in evidence_ids]
    ]
    if unsupported:
        issues.append("claim_evidence_grounding_failed")
    if claims and not all(any(str(item) in content for item in claim.get("evidence_ids") or []) for claim in claims):
        issues.append("citation_missing")
    blocking = {"no_content", "coverage_gate_failed", "blocking_conflict_unresolved", "claim_evidence_grounding_failed"}
    repairable = bool(content) and bool(set(issues) & {"citation_missing"}) and not (set(issues) & blocking - {"coverage_gate_failed"})
    suggested_action = "repair" if repairable else "gap_fill" if "coverage_gate_failed" in issues else ""
    return {
        "verdict": "pass" if not issues else "fail",
        "issues": issues,
        "repairable": repairable,
        "suggested_action": suggested_action,
        "grounding": not unsupported,
        "citation_metrics": {
            "claim_count": len(claims),
            "supported_claim_count": len(claims) - len(unsupported),
            "evidence_count": len(evidence_ids),
            "coverage_ratio": progress["coverage_ratio"],
            "evidence_status": evidence["status"],
        },
    }


def quality_gate_node(state: ResearchState) -> dict[str, Any]:
    assessment = _quality_assessment(state)
    decision = decide_control({**state, "quality_assessment": assessment})
    return transition_update(
        state,
        WorkflowPhase.QUALITY,
        {"quality_assessment": assessment, "control_decision": decision},
    )


def route_after_quality(state: ResearchState) -> str:
    action = _state_decision(state)["action"]
    if action == ControlAction.REPAIR_SYNTHESIS.value:
        return "repair_synthesis"
    if action == ControlAction.GAP_FILL.value:
        return "gap_fill"
    if action == ControlAction.REPLAN.value:
        return "replan"
    return "finalize"


def repair_synthesis_node(state: ResearchState) -> dict[str, Any]:
    compact = _semantic_digest(state, compact=True)
    return transition_update(
        state,
        WorkflowPhase.REPAIR_SYNTHESIS,
        {
            "final_content": compact,
            "synthesis_attempts": int(state.get("synthesis_attempts") or 0) + 1,
            "synthesis_failed": False,
        },
    )


def finalize_node(state: ResearchState) -> dict[str, Any]:
    progress = assess_progress(dict(state))
    terminal = terminal_update(
        state,
        reason=str((state.get("termination") or {}).get("outcome") or "incomplete"),
        stage="finalize",
        research_completed=progress["status"] == "sufficient",
        synthesis_attempted=bool(state.get("final_content")),
        quality_attempted=bool(state.get("quality_assessment")),
    )
    return {
        **terminal,
        "final_content": state.get("final_content") or "",
        "control_decision": _decision(state),
    }


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
        return research_worker_node(cast(ResearchState, payload))

    if runtime is not None:
        compile_spec = runtime.node_compile_spec
        spec_gate = runtime.node_spec_gate
        clarify = runtime.node_clarify
        plan = runtime.node_plan
        plan_validate = runtime.node_plan_validate
        dispatch = runtime.node_dispatch
        worker = runtime.node_research_worker
        ingest = runtime.node_ingest_semantics
        assess = runtime.node_assess
        gap_fill = runtime.node_gap_fill
        expand = runtime.node_expand_plan
        replan = runtime.node_replan
        retry = runtime.node_retry
        synthesize = runtime.node_synthesize
        quality_gate = runtime.node_quality_gate
        repair_synthesis = runtime.node_repair_synthesis
        finalize = runtime.node_finalize
    else:
        compile_spec = compile_spec_node
        spec_gate = spec_gate_node
        clarify = clarify_node
        plan = plan_node
        plan_validate = plan_validate_node
        dispatch = dispatch_node
        worker = _worker
        ingest = ingest_semantics_node
        assess = assess_node
        gap_fill = gap_fill_node
        expand = expand_plan_node
        replan = replan_node
        retry = retry_node
        synthesize = synthesize_node
        quality_gate = quality_gate_node
        repair_synthesis = repair_synthesis_node
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
        "compile_spec": compile_spec,
        "spec_gate": spec_gate,
        "clarify": clarify,
        "plan": plan,
        "plan_validate": plan_validate,
        "dispatch": dispatch,
        "research_worker": worker,
        "dispatch_barrier": dispatch_barrier_node,
        "ingest_semantics": ingest,
        "assess": assess,
        "gap_fill": gap_fill,
        "expand_plan": expand,
        "replan": replan,
        "retry": retry,
        "synthesize": synthesize,
        "quality_gate": quality_gate,
        "repair_synthesis": repair_synthesis,
        "finalize": finalize,
    }.items():
        builder.add_node(name, node)

    builder.add_edge(START, "compile_spec")
    builder.add_edge("compile_spec", "spec_gate")
    builder.add_conditional_edges("spec_gate", route_after_spec_gate, {"clarify": "clarify", "plan": "plan"})
    builder.add_edge("clarify", "compile_spec")
    builder.add_edge("plan", "plan_validate")
    builder.add_edge("plan_validate", "dispatch")
    builder.add_conditional_edges(
        "dispatch",
        route_dispatch,
        [
            "research_worker",
            "ingest_semantics",
            "retry",
            "gap_fill",
            "expand_plan",
            "replan",
            "synthesize",
            "finalize",
            "dispatch",
        ],
    )
    builder.add_edge("research_worker", "dispatch_barrier")
    builder.add_edge("dispatch_barrier", "ingest_semantics")
    builder.add_edge("ingest_semantics", "assess")
    builder.add_conditional_edges(
        "assess",
        route_assess,
        ["dispatch", "retry", "gap_fill", "expand_plan", "replan", "synthesize", "finalize"],
    )
    builder.add_edge("retry", "dispatch")
    builder.add_edge("gap_fill", "plan_validate")
    builder.add_edge("expand_plan", "plan_validate")
    builder.add_edge("replan", "plan_validate")
    builder.add_edge("synthesize", "quality_gate")
    builder.add_edge("repair_synthesis", "synthesize")
    builder.add_conditional_edges(
        "quality_gate",
        route_after_quality,
        ["finalize", "repair_synthesis", "gap_fill", "replan"],
    )
    builder.add_edge("finalize", END)
    kwargs = {"checkpointer": checkpointer} if checkpointer is not None else {}
    return builder.compile(**kwargs)


def initial_graph_state(**kwargs: Any) -> ResearchState:
    return empty_research_state(**kwargs)


__all__ = [
    "GraphInvariantViolation",
    "compile_research_graph",
    "initial_graph_state",
    "route_after_quality",
    "route_assess",
    "route_dispatch",
]
