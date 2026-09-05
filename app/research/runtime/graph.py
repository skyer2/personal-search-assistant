"""
Research StateGraph：workflow 调度权威。

无 runtime 时保留可单测的 placeholder worker（编译/控制结构）。
传入 runtime=ResearchGraphRunner 后，节点调用现有 harness 领域服务。
"""

from __future__ import annotations

from typing import Any, Literal, cast

from app.agent.harness.planner import build_plan, understand_task
from app.agent.harness.state import ExecutionPlan
from app.research.routing.mode_router import canonicalize_mode
from app.research.runtime.project import brief_from_intent
from app.research.runtime.scheduler import (
    annotate_plan_tasks,
    next_synthesis_step,
    ready_research_steps,
)
from app.research.runtime.state import ResearchState, empty_research_state


def _plan_from_state(state: ResearchState) -> ExecutionPlan | None:
    if not state.get("plan"):
        return None
    return ExecutionPlan.from_dict(state["plan"])


def intent_node(state: ResearchState) -> dict[str, Any]:
    query = str(state.get("resolved_query") or state.get("task_query") or "")
    intent = understand_task(query)
    payload = intent.to_dict()
    brief = brief_from_intent(payload)
    from app.research.routing.task_shape import (
        classify_task_shape,
        execution_profile_for_shape,
    )

    shape = classify_task_shape(query, brief)
    profile = execution_profile_for_shape(shape.shape)
    budget = dict(state["budget"])
    budget["max_parallel_workers"] = int(cast(Any, profile)["parallel_workers"])
    existing_replan = budget.get("max_replan_count")
    existing_replan = (
        int(cast(Any, existing_replan))
        if existing_replan is not None
        else int(cast(Any, profile)["max_replan_count"])
    )
    budget["max_replan_count"] = min(
        existing_replan, int(cast(Any, profile)["max_replan_count"])
    )
    return {
        "intent": payload,
        "brief": brief,
        "needs_clarification": bool(intent.needs_clarification),
        "search_mode": "agent",
        "route_signals": [f"task_shape:{shape.shape.value}"],
        "budget": budget,
        "progress": "intent",
    }


def plan_node(state: ResearchState) -> dict[str, Any]:
    from app.agent.harness.planner import finalize_plan
    from app.agent.harness.state import TaskIntent

    raw = state.get("intent") or {}
    intent = TaskIntent.from_dict(raw) if raw else understand_task(state["task_query"])
    plan = annotate_plan_tasks(finalize_plan(build_plan(intent)))
    status = {
        step.resolved_task_id(i): str(step.metadata.get("status") or "pending")
        for i, step in enumerate(plan.steps)
    }
    return {
        "plan": plan.to_dict(),
        "plan_version": plan.plan_version,
        "task_status": status,
        "needs_plan_review": False,
        "progress": "planned",
    }


def vanilla_agent_node(state: ResearchState) -> dict[str, Any]:
    """Direct baseline：单 Agent + search tool，无 Brief/Plan/Progress。仅对照实验。"""
    query = str(state.get("resolved_query") or state.get("task_query") or "")
    return {
        "final_content": f"[direct baseline] {query}".strip(),
        "search_mode": "direct",
        "status": "completed",
        "quality_passed": True,
        "progress": "vanilla",
        "plan": None,
    }


def route_after_intent(state: ResearchState) -> Literal["clarify", "plan"]:
    if state.get("needs_clarification"):
        return "clarify"
    return "plan"


def clarify_node(state: ResearchState) -> dict[str, Any]:
    """无 runtime 时自动保守解析；生产路径由 runner 用 interrupt() 等待。"""
    from app.agent.harness.planner import auto_resolve_clarification
    from app.agent.harness.state import TaskIntent

    intent = TaskIntent.from_dict(state.get("intent") or {})
    resolved = auto_resolve_clarification(intent)
    return {"intent": resolved.to_dict(), "needs_clarification": False, "progress": "clarified"}


def plan_validate_node(state: ResearchState) -> dict[str, Any]:
    return {"needs_plan_review": False, "progress": "plan_validated"}


def dispatch_node(state: ResearchState) -> dict[str, Any]:
    return {"progress": "dispatch"}


def _max_replan(state: ResearchState) -> int:
    budget = state["budget"]
    return int(budget.get("max_replan_count") or 3)


def _wave_parallel(state: ResearchState) -> int:
    budget = state["budget"]
    try:
        n = int(budget.get("max_parallel_workers") or 3)
    except (TypeError, ValueError):
        n = 3
    return max(1, n)


def route_dispatch(state: ResearchState) -> list[Any] | str:
    from langgraph.types import Send

    from app.research.runtime.scheduler import select_dispatch_wave, skip_optional_pending

    if state.get("status") == "aborted" or state.get("abort_reason"):
        return "abort"
    plan = _plan_from_state(state)
    if plan is None:
        return "finalize"
    status = dict(state.get("task_status") or {})
    assessment = dict(state.get("progress_assessment") or {})
    early_stop = (
        str(assessment.get("verdict") or "") == "enough"
        or str(assessment.get("reason") or "") == "force_synthesis_budget"
        or bool(state.get("replan_exhausted"))
    )
    if early_stop:
        status = skip_optional_pending(plan, status, reason="early_stop_enough")
    # Required-first: 永远不要在第一波把 optional 和 P0 一起 Send
    ready = select_dispatch_wave(
        plan,
        status,
        include_optional=False,
        max_parallel=_wave_parallel(state),
    )
    if ready:
        sends: list[Any] = []
        for index, step in ready:
            sends.append(
                Send(
                    "research_worker",
                    {
                        "run_id": state["run_id"],
                        "session_id": state["session_id"],
                        "plan_version": int(state.get("plan_version") or 1),
                        "task_id": step.resolved_task_id(index),
                        "step_index": index,
                        "step_type": step.step_type,
                        "description": step.description,
                        "subagent": step.subagent or "",
                        "task_query": state["task_query"],
                    },
                )
            )
        return sends
    return "progress"


def progress_node(state: ResearchState) -> dict[str, Any]:
    from app.research.planning.progress import assess_progress

    plan = _plan_from_state(state)
    worker_rows = list(state.get("worker_results") or [])
    reconciliation = None
    try:
        from app.research.claims import reconcile_worker_results

        reconciliation = reconcile_worker_results(worker_rows)
    except Exception:
        reconciliation = None
    assessment = assess_progress(
        plan,
        task_status=dict(state.get("task_status") or {}),
        worker_results=worker_rows,
        query=str(state.get("resolved_query") or state.get("task_query") or ""),
        aborted=bool(state.get("status") == "aborted" or state.get("abort_reason")),
        intent=state.get("intent"),
        reconciliation=reconciliation,
    )
    payload = {
        "progress_assessment": assessment.to_dict(),
        "progress": "progress_eval",
        "abort_reason": state.get("abort_reason")
        or (assessment.reason if assessment.verdict == "abort" else ""),
    }
    if reconciliation is not None:
        payload["claim_reconciliation"] = reconciliation.to_dict()
    return payload


def route_progress(state: ResearchState) -> str:
    from app.research.runtime.scheduler import skip_optional_pending

    if state.get("status") == "aborted" or state.get("abort_reason"):
        return "abort"
    if state.get("status") == "partial":
        return "quality_gate"
    assessment = dict(state.get("progress_assessment") or {})
    verdict = str(assessment.get("verdict") or "enough")
    plan = _plan_from_state(state)
    status = dict(state.get("task_status") or {})
    replan_count = int(state.get("replan_count") or 0)
    exhausted = bool(state.get("replan_exhausted"))
    force_synth = str(assessment.get("reason") or "") == "force_synthesis_budget"
    if verdict == "abort":
        return "abort"
    if force_synth or exhausted or verdict == "enough":
        if plan is not None:
            status = skip_optional_pending(plan, status, reason="early_stop_enough")
            nxt = next_synthesis_step(plan, status, allow_failed_deps=True)
            if nxt is not None:
                return "synthesize"
        return "quality_gate"
    if verdict == "run" and plan is not None and ready_research_steps(
        plan, status, include_optional=False
    ):
        return "dispatch"
    can_replan = (
        verdict == "gap"
        and not exhausted
        and not force_synth
        and replan_count < _max_replan(state)
    )
    if can_replan:
        return "replan"
    if plan is not None:
        status = skip_optional_pending(plan, status, reason="early_stop_synthesize")
        nxt = next_synthesis_step(plan, status, allow_failed_deps=True)
        if nxt is not None:
            return "synthesize"
    return "quality_gate"


def research_worker_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    Leaf 执行由运行时注入的 invoke_worker 完成。
    默认占位：把任务标为 done，供图编译与单测使用。
    """
    invoke = state.get("_invoke_worker")
    if callable(invoke):
        return invoke(state)
    task_id = str(state.get("task_id") or "")
    finding = {
        "task_id": task_id,
        "summary": str(state.get("description") or state.get("objective") or "placeholder")[:400],
    }
    return {
        "worker_results": [
            {
                "task_id": task_id,
                "step_type": state.get("step_type"),
                "ok": True,
                "payload": {"summary": "placeholder", "facts": [], "sources": [], "findings": [finding]},
            }
        ],
        "task_status": {task_id: "done"},
        "evidence_refs": [task_id] if task_id else [],
        "findings": [finding],
    }


def synthesize_node(state: ResearchState) -> dict[str, Any]:
    plan = _plan_from_state(state)
    status = dict(state.get("task_status") or {})
    nxt = next_synthesis_step(plan, status) if plan else None
    if nxt is None:
        return {"status": "synthesized", "progress": "synthesized"}
    index, step = nxt
    tid = step.resolved_task_id(index)
    status[tid] = "done"
    return {"task_status": status, "status": "synthesized", "progress": "synthesized"}


def replan_node(state: ResearchState) -> dict[str, Any]:
    return {
        "replan_count": int(state.get("replan_count") or 0) + 1,
        "progress": "run",
    }


def quality_gate_node(state: ResearchState) -> dict[str, Any]:
    return {"quality_passed": True, "progress": "quality"}


def finalize_node(state: ResearchState) -> dict[str, Any]:
    return {
        "status": state.get("status") or "completed",
        "final_content": state.get("final_content") or "",
        "progress": "done",
    }


def abort_node(state: ResearchState) -> dict[str, Any]:
    return {
        "status": "aborted",
        "abort_reason": state.get("abort_reason") or "aborted",
        "progress": "abort",
    }


def compile_research_graph(
    *,
    checkpointer: Any = None,
    invoke_worker: Any = None,
    runtime: Any = None,
    profile: str = "agent",
):
    """可执行的 Domain Harness。profile=agent 走完整图；direct 仅对照实验。"""
    from langgraph.graph import END, START, StateGraph

    mode = canonicalize_mode(profile)

    def _worker(payload: dict[str, Any]) -> dict[str, Any]:
        if invoke_worker is not None:
            payload = dict(payload)
            payload["_invoke_worker"] = invoke_worker
        return research_worker_node(payload)

    builder = StateGraph(ResearchState)
    if runtime is not None:
        vanilla = runtime.node_vanilla_agent
        intent = runtime.node_intent
        clarify = runtime.node_clarify
        plan = runtime.node_plan
        plan_validate = runtime.node_plan_validate
        dispatch = runtime.node_dispatch
        worker = runtime.node_research_worker
        progress = runtime.node_progress
        synthesize = runtime.node_synthesize
        replan = runtime.node_replan
        quality_gate = runtime.node_quality_gate
        finalize = runtime.node_finalize
        abort = runtime.node_abort
    else:
        vanilla = vanilla_agent_node
        intent = intent_node
        clarify = clarify_node
        plan = plan_node
        plan_validate = plan_validate_node
        dispatch = dispatch_node
        worker = _worker
        progress = progress_node
        synthesize = synthesize_node
        replan = replan_node
        quality_gate = quality_gate_node
        finalize = finalize_node
        abort = abort_node

    builder.add_node("finalize", finalize)
    builder.add_node("abort", abort)
    if mode == "direct":
        builder.add_node("vanilla", vanilla)
        builder.add_edge(START, "vanilla")
        builder.add_edge("vanilla", "finalize")
        builder.add_edge("finalize", END)
        builder.add_edge("abort", END)
        kwargs: dict[str, Any] = {}
        if checkpointer is not None:
            kwargs["checkpointer"] = checkpointer
        return builder.compile(**kwargs)

    builder.add_node("intent", intent)
    builder.add_node("clarify", clarify)
    builder.add_node("plan", plan)
    builder.add_node("plan_validate", plan_validate)
    builder.add_node("dispatch", dispatch)
    builder.add_node("research_worker", worker)
    builder.add_node("progress", progress)
    builder.add_node("synthesize", synthesize)
    builder.add_node("replan", replan)
    builder.add_node("quality_gate", quality_gate)
    builder.add_edge(START, "intent")
    builder.add_conditional_edges(
        "intent",
        route_after_intent,
        {"clarify": "clarify", "plan": "plan"},
    )
    builder.add_edge("clarify", "plan")
    builder.add_edge("plan", "plan_validate")
    builder.add_edge("plan_validate", "dispatch")
    builder.add_conditional_edges(
        "dispatch",
        route_dispatch,
        ["research_worker", "progress", "abort", "finalize"],
    )
    # 每一波 Worker 结束后必须 Progress，禁止 Worker → greedy Dispatch drain
    builder.add_edge("research_worker", "progress")
    builder.add_conditional_edges(
        "progress",
        route_progress,
        ["dispatch", "replan", "synthesize", "quality_gate", "abort"],
    )
    builder.add_edge("synthesize", "dispatch")
    builder.add_edge("replan", "plan_validate")
    builder.add_edge("quality_gate", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("abort", END)

    kwargs = {}
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    return builder.compile(**kwargs)


def initial_graph_state(
    *,
    run_id: str,
    session_id: str,
    task_query: str,
    **kwargs: Any,
) -> ResearchState:
    return empty_research_state(
        run_id=run_id,
        session_id=session_id,
        task_query=task_query,
        **kwargs,
    )


from app.research.runtime.scheduler import dispatch_sends

__all__ = [
    "compile_research_graph",
    "dispatch_sends",
    "initial_graph_state",
    "vanilla_agent_node",
    "intent_node",
    "plan_node",
    "progress_node",
    "route_dispatch",
    "route_progress",
]
