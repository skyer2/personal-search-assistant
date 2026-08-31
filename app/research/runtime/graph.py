"""
Research StateGraph：workflow 调度权威。

无 runtime 时保留可单测的 placeholder worker（编译/控制结构）。
传入 runtime=ResearchGraphRunner 后，节点调用现有 harness 领域服务。
"""

from __future__ import annotations

from typing import Any, Literal

from app.agent.harness.planner import build_plan, understand_task
from app.agent.harness.state import ExecutionPlan
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
    return {
        "intent": intent.to_dict(),
        "needs_clarification": bool(intent.needs_clarification),
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


def conversation_node(state: ResearchState) -> dict[str, Any]:
    from app.conversation.store import ConversationStore, rewrite_query

    store = ConversationStore.default(
        user_id=str(state.get("user_id") or "me"),
        project_id=str(state.get("project_id") or "Inbox"),
    )
    thread = store.get(str(state.get("session_id") or ""))
    original = str(state.get("task_query") or "")
    resolved = rewrite_query(original, thread)
    return {
        "conversation_summary": thread.rolling_summary,
        "resolved_query": resolved,
        "progress": "conversation",
    }


def mode_router_node(state: ResearchState) -> dict[str, Any]:
    from app.research.routing.mode_router import budget_for_mode, route

    decision = route(
        str(state.get("resolved_query") or state.get("task_query") or ""),
        user_mode=str(state.get("search_mode_requested") or "auto"),
        conversation_summary=str(state.get("conversation_summary") or ""),
    )
    budget_cfg = budget_for_mode(decision.mode)
    current = dict(state.get("budget") or {})
    current["max_tool_calls"] = int(budget_cfg["max_tool_calls"])
    current["max_replan_count"] = int(budget_cfg["max_replan_count"])
    return {
        "search_mode": decision.mode,
        "route_signals": decision.signals,
        "budget": current,
        "progress": "routed",
    }


def route_after_mode(state: ResearchState) -> Literal["quick_search", "intent"]:
    return "quick_search" if str(state.get("search_mode") or "") == "quick" else "intent"


def quick_search_node(state: ResearchState) -> dict[str, Any]:
    query = str(state.get("resolved_query") or state.get("task_query") or "")
    cards = [
        {
            "title": "placeholder",
            "url": "https://example.com",
            "snippet": query[:120],
        }
    ]
    return {"search_cards": cards, "progress": "quick_search"}


def quick_fetch_node(state: ResearchState) -> dict[str, Any]:
    cards = list(state.get("search_cards") or [])
    refs = [str(c.get("url") or "") for c in cards if c.get("url")]
    return {"evidence_refs": refs[:2], "progress": "quick_fetch"}


def quick_synthesize_node(state: ResearchState) -> dict[str, Any]:
    from app.research.runtime.quick import compose_quick_answer

    query = str(state.get("resolved_query") or state.get("task_query") or "")
    cards = list(state.get("search_cards") or [])
    answer = compose_quick_answer(query, cards)
    return {
        "final_content": answer,
        "status": "completed",
        "quality_passed": True,
        "progress": "quick_synth",
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
    budget = state.get("budget") or {}
    return int(budget.get("max_replan_count") or 3)


def route_dispatch(state: ResearchState) -> list[Any] | str:
    from langgraph.types import Send

    if state.get("status") == "aborted" or state.get("abort_reason"):
        return "abort"
    plan = _plan_from_state(state)
    if plan is None:
        return "finalize"
    status = dict(state.get("task_status") or {})
    ready = ready_research_steps(plan, status)
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
    assessment = assess_progress(
        plan,
        task_status=dict(state.get("task_status") or {}),
        worker_results=list(state.get("worker_results") or []),
        query=str(state.get("resolved_query") or state.get("task_query") or ""),
        aborted=bool(state.get("status") == "aborted" or state.get("abort_reason")),
        intent=state.get("intent"),
    )
    return {
        "progress_assessment": assessment.to_dict(),
        "progress": "progress_eval",
        "abort_reason": state.get("abort_reason")
        or (assessment.reason if assessment.verdict == "abort" else ""),
    }


def route_progress(state: ResearchState) -> str:
    if state.get("status") == "aborted" or state.get("abort_reason"):
        return "abort"
    assessment = dict(state.get("progress_assessment") or {})
    verdict = str(assessment.get("verdict") or "enough")
    plan = _plan_from_state(state)
    status = dict(state.get("task_status") or {})
    replan_count = int(state.get("replan_count") or 0)
    exhausted = bool(state.get("replan_exhausted"))
    if verdict == "abort":
        return "abort"
    if verdict == "run" and plan is not None and ready_research_steps(plan, status):
        return "dispatch"
    can_replan = (
        verdict == "gap"
        and not exhausted
        and replan_count < _max_replan(state)
    )
    if can_replan:
        return "replan"
    if plan is not None:
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
    return {
        "worker_results": [
            {
                "task_id": task_id,
                "step_type": state.get("step_type"),
                "ok": True,
                "payload": {"summary": "placeholder", "facts": [], "sources": []},
            }
        ],
        "task_status": {task_id: "done"},
        "evidence_refs": [task_id] if task_id else [],
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
):
    """可执行的 Domain Harness 表示。runtime 注入后走真实 Leaf / 领域服务。"""
    from langgraph.graph import END, START, StateGraph

    def _worker(payload: dict[str, Any]) -> dict[str, Any]:
        if invoke_worker is not None:
            payload = dict(payload)
            payload["_invoke_worker"] = invoke_worker
        return research_worker_node(payload)

    builder = StateGraph(ResearchState)
    if runtime is not None:
        builder.add_node("conversation", runtime.node_conversation)
        builder.add_node("mode_router", runtime.node_mode_router)
        builder.add_node("quick_search", runtime.node_quick_search)
        builder.add_node("quick_fetch", runtime.node_quick_fetch)
        builder.add_node("quick_synthesize", runtime.node_quick_synthesize)
        builder.add_node("intent", runtime.node_intent)
        builder.add_node("clarify", runtime.node_clarify)
        builder.add_node("plan", runtime.node_plan)
        builder.add_node("plan_validate", runtime.node_plan_validate)
        builder.add_node("dispatch", runtime.node_dispatch)
        builder.add_node("research_worker", runtime.node_research_worker)
        builder.add_node("progress", runtime.node_progress)
        builder.add_node("synthesize", runtime.node_synthesize)
        builder.add_node("replan", runtime.node_replan)
        builder.add_node("quality_gate", runtime.node_quality_gate)
        builder.add_node("finalize", runtime.node_finalize)
        builder.add_node("abort", runtime.node_abort)
    else:
        builder.add_node("conversation", conversation_node)
        builder.add_node("mode_router", mode_router_node)
        builder.add_node("quick_search", quick_search_node)
        builder.add_node("quick_fetch", quick_fetch_node)
        builder.add_node("quick_synthesize", quick_synthesize_node)
        builder.add_node("intent", intent_node)
        builder.add_node("clarify", clarify_node)
        builder.add_node("plan", plan_node)
        builder.add_node("plan_validate", plan_validate_node)
        builder.add_node("dispatch", dispatch_node)
        builder.add_node("research_worker", _worker)
        builder.add_node("progress", progress_node)
        builder.add_node("synthesize", synthesize_node)
        builder.add_node("replan", replan_node)
        builder.add_node("quality_gate", quality_gate_node)
        builder.add_node("finalize", finalize_node)
        builder.add_node("abort", abort_node)

    builder.add_edge(START, "conversation")
    builder.add_edge("conversation", "mode_router")
    builder.add_conditional_edges(
        "mode_router",
        route_after_mode,
        {"quick_search": "quick_search", "intent": "intent"},
    )
    builder.add_edge("quick_search", "quick_fetch")
    builder.add_edge("quick_fetch", "quick_synthesize")
    builder.add_edge("quick_synthesize", "finalize")
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
    builder.add_edge("research_worker", "dispatch")
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

    kwargs: dict[str, Any] = {}
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
    "conversation_node",
    "mode_router_node",
    "intent_node",
    "plan_node",
    "progress_node",
    "route_after_mode",
    "route_dispatch",
    "route_progress",
]
