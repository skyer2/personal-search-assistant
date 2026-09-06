"""
Research StateGraph：workflow 调度权威。

无 runtime 时保留可单测的 placeholder worker（编译/控制结构）。
传入 runtime=ResearchGraphRunner 后，节点调用现有 harness 领域服务。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, cast

from app.agent.harness.planner import build_plan, understand_task
from app.agent.harness.state import ExecutionPlan
from app.research.routing.mode_router import canonicalize_mode
from app.research.runtime.project import brief_from_intent
from app.research.runtime.scheduler import (
    annotate_plan_tasks,
    next_synthesis_step,
)
from app.research.runtime.state import ResearchState, empty_research_state
from app.research.control.policy import (
    decide_after_quality,
    decide_dispatch,
    decide_progress,
    max_replan_attempts,
    wave_parallel,
    workflow_task_status,
)
from app.research.domain.contracts import (
    OutcomeStatus,
    ProgressDecision,
    QualityDecision,
    TaskStatus,
    WorkflowPhase,
    initialize_tasks,
    merge_task_state,
    task_status_projection,
)


class GraphInvariantViolation(RuntimeError):
    """Raised when Graph routing and node admission disagree."""


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
        "phase": WorkflowPhase.UNDERSTAND.value,
    }


def plan_node(state: ResearchState) -> dict[str, Any]:
    from app.agent.harness.planner import finalize_plan
    from app.agent.harness.state import TaskIntent
    from app.research.planning.candidate import annotate_candidate_dependencies

    raw = state.get("intent") or {}
    intent = TaskIntent.from_dict(raw) if raw else understand_task(state["task_query"])
    plan = annotate_plan_tasks(finalize_plan(build_plan(intent)))
    annotate_candidate_dependencies(plan)
    tasks = initialize_tasks(plan)
    return {
        "plan": plan.to_dict(),
        "plan_version": plan.plan_version,
        "tasks": tasks,
        "task_status": task_status_projection(tasks),
        "needs_plan_review": False,
        "progress": "planned",
        "phase": WorkflowPhase.PLAN.value,
        "outcome": OutcomeStatus.RUNNING.value,
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
    return {
        "intent": resolved.to_dict(),
        "needs_clarification": False,
        "progress": "clarified",
        "phase": WorkflowPhase.CLARIFY.value,
    }


def plan_validate_node(state: ResearchState) -> dict[str, Any]:
    return {
        "needs_plan_review": False,
        "progress": "plan_validated",
        "phase": WorkflowPhase.PLAN_VALIDATED.value,
    }


def dispatch_node(state: ResearchState) -> dict[str, Any]:
    return {"progress": "dispatch", "phase": WorkflowPhase.DISPATCH.value}


def control_fingerprint(
    state: dict[str, Any],
    assessment: dict[str, Any],
    candidate_set: dict[str, Any],
) -> str:
    payload = {
        "plan_version": int(state.get("plan_version") or 1),
        "task_status": dict(
            sorted(workflow_task_status(cast(dict[str, Any], state)).items())
        ),
        "progress_verdict": str(assessment.get("verdict") or ""),
        "progress_reason": str(assessment.get("reason") or ""),
        "replan_count": int(state.get("replan_count") or 0),
        "status": str(state.get("status") or ""),
        "candidate_status": str(candidate_set.get("status") or ""),
        "candidate_items": [str(x) for x in candidate_set.get("items") or []],
        "findings_count": len(state.get("findings") or []),
        "evidence_count": len(state.get("evidence_refs") or []),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def control_plane_fingerprint(state: dict[str, Any]) -> str:
    assessment = dict(state.get("progress_assessment") or {})
    payload = {
        "plan_version": int(state.get("plan_version") or 1),
        "task_status": dict(
            sorted(workflow_task_status(cast(dict[str, Any], state)).items())
        ),
        "open_gap_ids": [str(x) for x in assessment.get("open_gap_ids") or []],
        "replan_exhausted": bool(state.get("replan_exhausted")),
        "replan_attempts": int(state.get("replan_attempts") or 0),
        "quality_reason": str(state.get("quality_reason") or ""),
        "quality_repair_action": str(state.get("quality_repair_action") or ""),
        "synthesis_status": str(state.get("status") or ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def route_dispatch(state: ResearchState) -> list[Any] | str:
    from langgraph.types import Send

    from app.research.runtime.scheduler import select_dispatch_wave

    control = decide_dispatch(cast(dict[str, Any], state))
    if control == "abort":
        return "abort"
    if control == "quality_gate":
        return "quality_gate"
    if control == "finalize":
        return "finalize"
    plan = _plan_from_state(state)
    if plan is None:
        return "finalize"
    status = workflow_task_status(cast(dict[str, Any], state))
    from app.research.planning.candidate import candidate_artifact_status

    status.update(candidate_artifact_status(state.get("candidate_set")))
    assessment = dict(state.get("progress_assessment") or {})
    early_stop = (
        str(assessment.get("verdict") or "") == "enough"
        or str(assessment.get("reason") or "") == "force_synthesis_budget"
        or bool(state.get("replan_exhausted"))
    )
    if early_stop:
        return "progress"
    # Required-first: 永远不要在第一波把 optional 和 P0 一起 Send
    ready = select_dispatch_wave(
        plan,
        status,
        include_optional=False,
        max_parallel=wave_parallel(cast(dict[str, Any], state)),
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
                        "candidate_context": str(
                            (state.get("candidate_set") or {}).get("context") or ""
                        ),
                        "candidate_set": dict(state.get("candidate_set") or {}),
                    },
                )
            )
        return sends
    return "progress"


def progress_node(state: ResearchState) -> dict[str, Any]:
    from app.research.planning.progress import assess_progress
    from app.research.planning.candidate import (
        build_candidate_set,
        candidate_artifact_status,
    )

    plan = _plan_from_state(state)
    worker_rows = list(state.get("worker_results") or [])
    reconciliation = None
    try:
        from app.research.claims import reconcile_worker_results

        reconciliation = reconcile_worker_results(worker_rows)
    except Exception:
        reconciliation = None
    candidate_set = build_candidate_set(
        plan,
        worker_rows=worker_rows,
        task_status=workflow_task_status(cast(dict[str, Any], state)),
        query=str(state.get("resolved_query") or state.get("task_query") or ""),
        brief=state.get("brief"),
    )
    progress_status = workflow_task_status(cast(dict[str, Any], state))
    progress_status.update(candidate_artifact_status(candidate_set))
    assessment = assess_progress(
        plan,
        task_status=progress_status,
        worker_results=worker_rows,
        query=str(state.get("resolved_query") or state.get("task_query") or ""),
        aborted=bool(state.get("status") == "aborted" or state.get("abort_reason")),
        intent=state.get("intent"),
        reconciliation=reconciliation,
    )
    assessment_payload = assessment.to_dict()
    fingerprint = control_plane_fingerprint(
        {**cast(dict[str, Any], state), "progress_assessment": assessment_payload}
    )
    stagnant_cycles = (
        int(state.get("stagnant_cycles") or 0) + 1
        if str(state.get("control_fingerprint") or "") == fingerprint
        else 0
    )
    if stagnant_cycles >= 2:
        assessment.verdict = "enough"
        assessment.reason = "graph_no_progress"
        assessment_payload = assessment.to_dict()
    payload = {
        "progress_assessment": assessment.to_dict(),
        "candidate_set": candidate_set,
        "progress": "progress_eval",
        "phase": WorkflowPhase.PROGRESS.value,
        "control_fingerprint": fingerprint,
        "stagnant_cycles": stagnant_cycles,
        "abort_reason": state.get("abort_reason")
        or (assessment.reason if assessment.verdict == "abort" else ""),
    }
    if stagnant_cycles >= 2:
        payload["progress_assessment"] = assessment_payload
        payload["replan_exhausted"] = True
        payload["control_no_progress"] = True
        payload["synthesis_admission"] = True
    if reconciliation is not None:
        payload["claim_reconciliation"] = reconciliation.to_dict()
    return payload


def prepare_synthesis_node(state: ResearchState) -> dict[str, Any]:
    from app.research.runtime.synthesis_admission import prepare_synthesis_update

    plan = _plan_from_state(state)
    if plan is None:
        raise GraphInvariantViolation("prepare_synthesis routed without a plan")
    assessment = dict(state.get("progress_assessment") or {})
    try:
        return prepare_synthesis_update(
            cast(dict[str, Any], state),
            plan,
            forced=str(assessment.get("reason") or "") == "force_synthesis_budget",
        )
    except ValueError as exc:
        raise GraphInvariantViolation(str(exc)) from exc


def route_progress(state: ResearchState) -> str:
    decision = decide_progress(cast(dict[str, Any], state))
    if decision is ProgressDecision.ABORT:
        return "abort"
    if decision is ProgressDecision.QUALITY:
        return "quality_gate"
    if decision is ProgressDecision.REPLAN:
        return "replan"
    if decision is ProgressDecision.PREPARE_SYNTHESIS:
        return "prepare_synthesis"
    if decision is ProgressDecision.DISPATCH:
        return "dispatch"

    raise GraphInvariantViolation(f"unhandled progress decision: {decision}")


def research_worker_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    Leaf 执行由运行时注入的 invoke_worker 完成。
    默认占位：把任务标为 done，供图编译与单测使用。
    """
    invoke = state.get("_invoke_worker")
    if callable(invoke):
        return invoke(state)
    task_id = str(state.get("task_id") or "")
    objective = str(state.get("description") or state.get("objective") or "placeholder")[:400]
    finding = {
        "task_id": task_id,
        "summary": objective,
    }
    from app.research.runtime.findings import normalize_findings

    findings, _rejected = normalize_findings(
        [finding], task_id=task_id, subject_id="general", dimension="general"
    )
    tasks = merge_task_state({}, task_id, TaskStatus.DONE)
    return {
        "worker_results": [
            {
                "task_id": task_id,
                "step_type": state.get("step_type"),
                "ok": True,
                "payload": {"summary": "placeholder", "facts": [], "sources": [], "findings": [finding]},
            }
        ],
        "tasks": tasks,
        "task_status": task_status_projection(tasks),
        "evidence_refs": [task_id] if task_id else [],
        "findings": findings,
    }


def synthesize_node(state: ResearchState) -> dict[str, Any]:
    from app.research.runtime.synthesis_admission import (
        evaluate_synthesis_admission,
    )

    plan = _plan_from_state(state)
    status = workflow_task_status(cast(dict[str, Any], state))
    if plan is None:
        raise GraphInvariantViolation("synthesize routed without a plan")
    assessment = dict(state.get("progress_assessment") or {})
    admission = evaluate_synthesis_admission(
        cast(dict[str, Any], state),
        plan,
        status,
        forced=str(assessment.get("reason") or "") == "force_synthesis_budget",
    )
    if not admission.allowed and state.get("status") != "partial":
        raise GraphInvariantViolation(
            f"synthesize routed but admission rejected: {admission.reason}"
        )
    if (
        state.get("status") == "partial"
        and str(state.get("synthesis_mode") or "") == "no_evidence_partial"
    ):
        return {
            "status": "partial",
            "outcome": OutcomeStatus.PARTIAL.value,
            "phase": WorkflowPhase.SYNTHESIS.value,
            "tasks": dict(state.get("tasks") or {}),
            "task_status": status,
            "progress": "no_evidence_partial",
            "synthesis_mode": "no_evidence_partial",
            "synthesis_admission_reason": admission.reason,
        }
    allow_failed_deps = bool(
        state.get("synthesis_admission")
        or admission.mode != "normal"
    )
    nxt = next_synthesis_step(
        plan,
        status,
        allow_failed_deps=allow_failed_deps,
    )
    if nxt is None:
        synthesis_steps = [
            step.resolved_task_id(index)
            for index, step in enumerate(plan.steps)
            if step.step_type in {"generate_markdown", "summarize", "convert_pdf"}
        ]
        if synthesis_steps and all(
            status.get(task_id) in {"done", "failed", "skipped"}
            for task_id in synthesis_steps
        ):
            return {
                "status": "synthesized",
                "outcome": OutcomeStatus.RUNNING.value,
                "phase": WorkflowPhase.SYNTHESIS.value,
                "tasks": dict(state.get("tasks") or {}),
                "task_status": status,
                "progress": "synthesized",
            }
        raise GraphInvariantViolation(
            "synthesize routed but no synthesis step is runnable"
        )
    index, step = nxt
    tid = step.resolved_task_id(index)
    status[tid] = "done"
    tasks = merge_task_state(state.get("tasks"), tid, TaskStatus.DONE)
    return {
        "tasks": tasks,
        "task_status": status,
        "status": "synthesized",
        "outcome": OutcomeStatus.RUNNING.value,
        "progress": "synthesized",
        "phase": WorkflowPhase.SYNTHESIS.value,
    }


def replan_node(state: ResearchState) -> dict[str, Any]:
    attempts = int(state.get("replan_attempts") or 0) + 1
    applied = int(state.get("replan_applied_count") or state.get("replan_count") or 0)
    return {
        "replan_attempts": attempts,
        "replan_applied_count": applied,
        "replan_count": applied,
        "replan_exhausted": attempts
        >= max_replan_attempts(cast(dict[str, Any], state)),
        "progress": "run",
        "phase": WorkflowPhase.REPLAN.value,
    }


def route_after_quality(state: ResearchState) -> str:
    decision = decide_after_quality(cast(dict[str, Any], state))
    if decision is QualityDecision.REPAIR_SYNTHESIS:
        return "repair_synthesis"
    if decision is QualityDecision.REPLAN:
        return "replan"
    return "finalize"


def quality_gate_node(state: ResearchState) -> dict[str, Any]:
    return {
        "quality_passed": True,
        "quality_reason": "",
        "quality_repairable": False,
        "quality_repair_action": "",
        "quality_attempts": int(state.get("quality_attempts") or 0),
        "progress": "quality",
        "phase": WorkflowPhase.QUALITY.value,
    }


def repair_synthesis_node(state: ResearchState) -> dict[str, Any]:
    plan = _plan_from_state(state)
    if plan is None:
        return {"progress": "repair_synthesis", "phase": WorkflowPhase.REPAIR_SYNTHESIS.value}
    tasks = dict(state.get("tasks") or {})
    status = task_status_projection(tasks)
    for index, step in enumerate(plan.steps):
        if step.step_type in {"generate_markdown", "summarize", "convert_pdf"}:
            task_id = step.resolved_task_id(index)
            tasks = merge_task_state(tasks, task_id, TaskStatus.PENDING)
            status[task_id] = TaskStatus.PENDING.value
    return {
        "tasks": tasks,
        "task_status": status,
        "status": "running",
        "outcome": OutcomeStatus.RUNNING.value,
        "final_content": "",
        "progress": "repair_synthesis",
        "phase": WorkflowPhase.REPAIR_SYNTHESIS.value,
    }


def finalize_node(state: ResearchState) -> dict[str, Any]:
    status = str(state.get("status") or "")
    if status not in {"completed", "partial", "aborted", "interrupted"}:
        status = "completed"
    return {
        "status": status,
        "outcome": status,
        "phase": WorkflowPhase.TERMINATED.value,
        "termination": {
            "outcome": status,
            "reason": state.get("abort_reason") or "completed",
            "stage": "finalize",
            "detected_stage": "finalize",
            "research_completed": bool(state.get("trusted_evidence_count")),
            "synthesis_attempted": bool(state.get("final_content")),
        },
        "final_content": state.get("final_content") or "",
        "progress": "done",
    }


def abort_node(state: ResearchState) -> dict[str, Any]:
    return {
        "status": "aborted",
        "outcome": OutcomeStatus.ABORTED.value,
        "phase": WorkflowPhase.TERMINATED.value,
        "termination": {
            "outcome": OutcomeStatus.ABORTED.value,
            "reason": state.get("abort_reason") or "aborted",
            "stage": "abort",
            "detected_stage": "abort",
            "research_completed": False,
            "synthesis_attempted": False,
        },
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
        prepare_synthesis = runtime.node_prepare_synthesis
        synthesize = runtime.node_synthesize
        replan = runtime.node_replan
        quality_gate = runtime.node_quality_gate
        repair_synthesis = getattr(
            runtime, "node_repair_synthesis", repair_synthesis_node
        )
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
        prepare_synthesis = prepare_synthesis_node
        synthesize = synthesize_node
        replan = replan_node
        quality_gate = quality_gate_node
        repair_synthesis = repair_synthesis_node
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
    builder.add_node("prepare_synthesis", prepare_synthesis)
    builder.add_node("synthesize", synthesize)
    builder.add_node("replan", replan)
    builder.add_node("quality_gate", quality_gate)
    builder.add_node("repair_synthesis", repair_synthesis)
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
        ["research_worker", "progress", "quality_gate", "abort", "finalize"],
    )
    # 每一波 Worker 结束后必须 Progress，禁止 Worker → greedy Dispatch drain
    builder.add_edge("research_worker", "progress")
    builder.add_conditional_edges(
        "progress",
        route_progress,
        ["dispatch", "replan", "prepare_synthesis", "quality_gate", "abort"],
    )
    builder.add_edge("prepare_synthesis", "synthesize")
    builder.add_edge("synthesize", "quality_gate")
    builder.add_edge("replan", "plan_validate")
    builder.add_edge("repair_synthesis", "synthesize")
    builder.add_conditional_edges(
        "quality_gate",
        route_after_quality,
        ["finalize", "repair_synthesis", "replan"],
    )
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
    "prepare_synthesis_node",
    "route_dispatch",
    "route_progress",
    "GraphInvariantViolation",
]
