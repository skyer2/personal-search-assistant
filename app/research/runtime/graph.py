"""Research StateGraph. Routing authority is ControlPolicy; nodes are executors."""

from __future__ import annotations

from typing import Any, Literal, cast

from app.agent.harness.planner import understand_task
from app.agent.harness.state import ExecutionPlan
from app.research.control.policy import decide_control
from app.research.control.terminal_policy import terminal_update
from app.research.control.transitions import transition_update
from app.research.domain.contracts import ControlAction, WorkflowPhase
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    initialize_tasks,
    transition_task,
)
from app.research.routing.mode_router import canonicalize_mode
from app.research.runtime.project import brief_from_intent
from app.research.runtime.scheduler import (
    annotate_plan_tasks,
    dispatch_sends,
    research_only_plan,
    required_research_ids,
    select_dispatch_wave,
)
from app.research.runtime.state import ResearchState, empty_research_state


class GraphInvariantViolation(RuntimeError):
    """Raised only for programming-level graph invariant violations."""


def _plan_from_state(state: ResearchState) -> ExecutionPlan | None:
    if not state.get("plan"):
        return None
    return ExecutionPlan.from_dict(state["plan"])


def intent_node(state: ResearchState) -> dict[str, Any]:
    from app.research.routing.task_shape import classify_task_shape, execution_profile_for_shape

    query = str(state.get("resolved_query") or state.get("task_query") or "")
    intent = understand_task(query)
    payload = intent.to_dict()
    brief = brief_from_intent(payload)
    shape = classify_task_shape(query, brief)
    profile = execution_profile_for_shape(shape.shape)
    budget = dict(state["budget"])
    budget["max_parallel_workers"] = int(profile["parallel_workers"])
    existing_replan = budget.get("max_replan_count")
    profile_replan = int(profile["max_replan_count"])
    budget["max_replan_count"] = (
        profile_replan
        if existing_replan is None
        else min(int(cast(int, existing_replan)), profile_replan)
    )
    return transition_update(
        state,
        WorkflowPhase.UNDERSTAND,
        {
            "intent": payload,
            "brief": brief,
            "needs_clarification": bool(intent.needs_clarification),
            "route_signals": [f"task_shape:{shape.shape.value}"],
            "budget": budget,
        },
    )


def clarify_node(state: ResearchState) -> dict[str, Any]:
    from app.agent.harness.planner import auto_resolve_clarification
    from app.agent.harness.state import TaskIntent

    intent = TaskIntent.from_dict(state.get("intent") or {})
    resolved = auto_resolve_clarification(intent)
    return transition_update(
        state,
        WorkflowPhase.CLARIFY,
        {"intent": resolved.to_dict(), "needs_clarification": False},
    )


def plan_node(state: ResearchState) -> dict[str, Any]:
    from app.agent.harness.planner import finalize_plan
    from app.agent.harness.state import TaskIntent
    from app.research.planning.lead_planner import heuristic_dynamic_plan
    from app.research.planning.policy import parse_source_policy

    raw = state.get("intent") or {}
    intent = TaskIntent.from_dict(raw) if raw else understand_task(state["task_query"])
    plan = heuristic_dynamic_plan(intent, parse_source_policy(intent.raw_query))
    plan = research_only_plan(annotate_plan_tasks(finalize_plan(plan)))
    return transition_update(
        state,
        WorkflowPhase.PLAN,
        {
            "plan": plan.to_dict(),
            "plan_version": plan.plan_version,
            "tasks": initialize_tasks(plan),
            "needs_plan_review": False,
        },
    )


def plan_validate_node(state: ResearchState) -> dict[str, Any]:
    plan = _plan_from_state(state)
    if plan is None or not plan.steps or any(
        step.step_type not in {"research", "network_search", "file_read"} for step in plan.steps
    ):
        return transition_update(state, WorkflowPhase.PLAN_VALIDATED, {"abort_reason": "empty_plan"})
    return transition_update(state, WorkflowPhase.PLAN_VALIDATED, {})


def dispatch_node(state: ResearchState) -> dict[str, Any]:
    return transition_update(state, WorkflowPhase.DISPATCH, {})


def route_after_intent(state: ResearchState) -> Literal["clarify", "plan"]:
    return "clarify" if state.get("needs_clarification") else "plan"


def _decision(state: ResearchState) -> dict[str, Any]:
    return decide_control(cast(dict[str, Any], state))


def _state_decision(state: ResearchState) -> dict[str, Any]:
    decision = state.get("control_decision")
    if isinstance(decision, dict) and decision.get("action"):
        return dict(decision)
    return _decision(state)


def route_dispatch(state: ResearchState) -> list[Any] | str:
    from langgraph.types import Send

    decision = _state_decision(state)
    action = decision["action"]
    if action == ControlAction.DISPATCH.value:
        plan = _plan_from_state(state)
        if plan is None:
            return "finalize"
        sends: list[Any] = []
        for index, step in select_dispatch_wave(
            plan,
            state.get("tasks"),
            task_ids=decision["task_ids"],
            max_parallel=max(1, int(state["budget"]["max_parallel_workers"])),
        ):
            sends.append(
                Send(
                    "research_worker",
                    {
                        "run_id": state["run_id"],
                        "session_id": state["session_id"],
                        "phase": state.get("phase") or WorkflowPhase.DISPATCH.value,
                        "plan_version": int(state.get("plan_version") or 1),
                        "task_id": step.resolved_task_id(index),
                        "step_index": index,
                        "step_type": step.step_type,
                        "description": step.description,
                        "subagent": step.subagent or "",
                        "task_query": state["task_query"],
                        "tasks": dict(state.get("tasks") or {}),
                        "candidate_context": str((state.get("candidate_set") or {}).get("context") or ""),
                        "candidate_set": dict(state.get("candidate_set") or {}),
                    },
                )
            )
        if sends:
            return sends
        return "progress"
    if action in {ControlAction.WAIT.value, ControlAction.RETRY.value}:
        return "retry" if action == ControlAction.RETRY.value else "progress"
    if action == ControlAction.REPLAN.value:
        return "replan"
    if action in {ControlAction.SYNTHESIZE.value, ControlAction.DELIVER_PARTIAL.value}:
        return "synthesize"
    if action == ControlAction.CANCEL.value:
        return "finalize"
    return "finalize"


def research_worker_node(state: ResearchState) -> dict[str, Any]:
    task_id = str(state.get("task_id") or "")
    running = transition_task(
        state.get("tasks"),
        task_id,
        execution_status=TaskExecutionStatus.RUNNING,
    )
    tasks = transition_task(
        running,
        task_id,
        execution_status=TaskExecutionStatus.SUCCEEDED,
        result_status=ResultStatus.COMPLETE,
        evidence_refs=[f"evidence:{task_id}"],
    )
    return transition_update(
        state,
        WorkflowPhase.EXECUTE,
        {
            "tasks": tasks,
            "worker_results": [
                {
                    "task_id": task_id,
                    "ok": True,
                    "status": "succeeded",
                    "summary": "deterministic evidence",
                    "payload": {"evidence_ids": [f"evidence:{task_id}"]},
                }
            ],
            "evidence_refs": [f"evidence:{task_id}"],
        },
    )


def progress_node(state: ResearchState) -> dict[str, Any]:
    plan = _plan_from_state(state)
    required = required_research_ids(plan) if plan is not None else []
    tasks = state.get("tasks")
    succeeded = [task_id for task_id in required if tasks.get(task_id, {}).get("execution_status") == TaskExecutionStatus.SUCCEEDED.value]
    partial = [
        task_id
        for task_id in required
        if tasks.get(task_id, {}).get("execution_status") == TaskExecutionStatus.FAILED.value
        and tasks.get(task_id, {}).get("result_status") == ResultStatus.PARTIAL.value
    ]
    if succeeded and len(succeeded) + len(partial) == len(required):
        status = "sufficient"
        reasons = ["required_research_terminal"]
    elif succeeded or partial:
        status = "gap"
        reasons = ["coverage_incomplete"]
    else:
        status = "unknown"
        reasons = ["no_completed_research"]
    evidence_count = len(state.get("evidence_refs") or [])
    return transition_update(
        state,
        WorkflowPhase.ASSESS,
        {
            "progress_assessment": {"status": status, "reason_codes": reasons},
            "evidence_assessment": {
                "status": "sufficient" if evidence_count else "insufficient",
                "evidence_count": evidence_count,
                "trusted_evidence_count": evidence_count,
                "primary_source_count": min(1, evidence_count),
                "independent_source_count": evidence_count,
            },
            "execution_health": {"status": "degraded" if partial else "healthy"},
            "delivery_readiness": {},
        },
    )


def route_progress(state: ResearchState) -> str:
    decision = _state_decision(state)
    action = decision["action"]
    if action in {ControlAction.DISPATCH.value, ControlAction.WAIT.value, ControlAction.RETRY.value}:
        return "retry" if action == ControlAction.RETRY.value else "dispatch"
    if action == ControlAction.REPLAN.value:
        return "replan"
    if action in {ControlAction.SYNTHESIZE.value, ControlAction.DELIVER_PARTIAL.value}:
        return "synthesize"
    return "finalize"


def replan_node(state: ResearchState) -> dict[str, Any]:
    budget = dict(state.get("replan_budget") or {})
    attempted = int(budget.get("attempted") or 0) + 1
    budget.update(attempted=attempted, applied=int(budget.get("applied") or 0))
    return transition_update(state, WorkflowPhase.REPLAN, {"replan_budget": budget})


def synthesize_node(state: ResearchState) -> dict[str, Any]:
    content = str(state.get("final_content") or "").strip()
    if not content:
        findings = list(state.get("findings") or [])
        content = "\n".join(
            str(item.get("claim") or item.get("summary") or "")
            for item in findings
            if isinstance(item, dict)
        ).strip()
    return transition_update(
        state,
        WorkflowPhase.SYNTHESIS,
        {
            "final_content": content,
            "delivery_readiness": dict(state.get("delivery_readiness") or {}),
        },
    )


def route_after_quality(state: ResearchState) -> str:
    decision = _state_decision(state)
    if decision["action"] == ControlAction.REPAIR_SYNTHESIS.value:
        return "repair_synthesis"
    if decision["action"] == ControlAction.REPLAN.value:
        return "replan"
    return "finalize"


def quality_gate_node(state: ResearchState) -> dict[str, Any]:
    content = bool(str(state.get("final_content") or "").strip())
    verdict = "unknown" if content else "fail"
    return transition_update(
        state,
        WorkflowPhase.QUALITY,
        {
            "quality_assessment": {
                "verdict": verdict,
                "issues": [] if content else ["no_content"],
                "repairable": False,
                "suggested_action": "",
                "grounding": False,
                "citation_metrics": {},
            },
            "control_decision": _decision({**state, "quality_assessment": {"verdict": verdict}}),
        },
    )


def repair_synthesis_node(state: ResearchState) -> dict[str, Any]:
    return transition_update(
        state,
        WorkflowPhase.REPAIR_SYNTHESIS,
        {"final_content": str(state.get("final_content") or "")},
    )


def retry_node(state: ResearchState) -> dict[str, Any]:
    from app.research.domain.task_state import retry_task

    decision = _state_decision(state)
    tasks = dict(state.get("tasks") or {})
    for task_id in decision.get("task_ids") or []:
        tasks = retry_task(tasks, str(task_id))
    return {"tasks": tasks}


def finalize_node(state: ResearchState) -> dict[str, Any]:
    terminal = terminal_update(
        state,
        reason=str((state.get("termination") or {}).get("outcome") or "incomplete"),
        stage="finalize",
        research_completed=bool(state.get("evidence_refs")),
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
            payload = dict(payload)
            payload["_invoke_worker"] = invoke_worker
        return research_worker_node(cast(ResearchState, payload))

    if runtime is not None:
        vanilla = runtime.node_vanilla_agent
        intent = runtime.node_intent
        clarify = runtime.node_clarify
        plan = runtime.node_plan
        plan_validate = runtime.node_plan_validate
        dispatch = runtime.node_dispatch
        worker = runtime.node_research_worker
        progress = runtime.node_progress
        retry = runtime.node_retry
        replan = runtime.node_replan
        synthesize = runtime.node_synthesize
        quality_gate = runtime.node_quality_gate
        repair_synthesis = runtime.node_repair_synthesis
        finalize = runtime.node_finalize
    else:
        vanilla = vanilla_agent_node
        intent = intent_node
        clarify = clarify_node
        plan = plan_node
        plan_validate = plan_validate_node
        dispatch = dispatch_node
        worker = _worker
        progress = progress_node
        retry = retry_node
        replan = replan_node
        synthesize = synthesize_node
        quality_gate = quality_gate_node
        repair_synthesis = repair_synthesis_node
        finalize = finalize_node

    builder = StateGraph(ResearchState)
    builder.add_node("finalize", finalize)
    if mode == "direct":
        builder.add_node("vanilla", vanilla)
        builder.add_edge(START, "vanilla")
        builder.add_edge("vanilla", "finalize")
        builder.add_edge("finalize", END)
        kwargs: dict[str, Any] = {"checkpointer": checkpointer} if checkpointer is not None else {}
        return builder.compile(**kwargs)

    builder.add_node("intent", intent)
    builder.add_node("clarify", clarify)
    builder.add_node("plan", plan)
    builder.add_node("plan_validate", plan_validate)
    builder.add_node("dispatch", dispatch)
    builder.add_node("research_worker", worker)
    builder.add_node("progress", progress)
    builder.add_node("retry", retry)
    builder.add_node("replan", replan)
    builder.add_node("synthesize", synthesize)
    builder.add_node("quality_gate", quality_gate)
    builder.add_node("repair_synthesis", repair_synthesis)
    builder.add_edge(START, "intent")
    builder.add_conditional_edges("intent", route_after_intent, {"clarify": "clarify", "plan": "plan"})
    builder.add_edge("clarify", "plan")
    builder.add_edge("plan", "plan_validate")
    builder.add_edge("plan_validate", "dispatch")
    builder.add_conditional_edges(
        "dispatch",
        route_dispatch,
        ["research_worker", "progress", "retry", "replan", "synthesize", "finalize"],
    )
    builder.add_edge("research_worker", "progress")
    builder.add_conditional_edges(
        "progress",
        route_progress,
        ["dispatch", "retry", "replan", "synthesize", "finalize"],
    )
    builder.add_edge("retry", "dispatch")
    builder.add_edge("synthesize", "quality_gate")
    builder.add_edge("replan", "plan_validate")
    builder.add_edge("repair_synthesis", "synthesize")
    builder.add_conditional_edges(
        "quality_gate",
        route_after_quality,
        ["finalize", "repair_synthesis", "replan"],
    )
    builder.add_edge("finalize", END)
    kwargs = {"checkpointer": checkpointer} if checkpointer is not None else {}
    return builder.compile(**kwargs)


def initial_graph_state(**kwargs: Any) -> ResearchState:
    return empty_research_state(**kwargs)


__all__ = [
    "GraphInvariantViolation",
    "compile_research_graph",
    "dispatch_sends",
    "initial_graph_state",
    "route_dispatch",
    "route_progress",
]
