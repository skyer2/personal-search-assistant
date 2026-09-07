"""Dumb executor bridge between the canonical ResearchState graph and harness services."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.agent.harness.planner import auto_resolve_clarification, build_plan, finalize_plan, understand_task
from app.agent.harness.citations import SourceTier
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep, TaskIntent
from app.research.assessment.delivery import assess_delivery
from app.research.assessment.evidence import assess_evidence
from app.research.assessment.execution_health import assess_execution_health
from app.research.control.policy import decide_control
from app.research.control.terminal_policy import terminal_update
from app.research.control.transitions import transition_update
from app.research.domain.contracts import BudgetStatus, WorkflowPhase
from app.research.domain.failure import classify_failure
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    initialize_tasks,
    normalize_tasks,
    retry_task,
    transition_task,
)
from app.research.runtime.project import brief_from_intent, sync_execution_projection
from app.research.runtime.scheduler import annotate_plan_tasks, research_only_plan, required_research_ids
from app.research.runtime.state import empty_research_state

logger = logging.getLogger(__name__)
_SESSIONS: dict[str, RunSession] = {}


class RunSession:
    """Process-local handles. LoopState remains a read-only execution projection."""

    def __init__(self, harness: Any, ctx: Any):
        self.harness = harness
        self.ctx = ctx
        self.state: LoopState = ctx.state
        self.run_id = str(getattr(ctx, "run_id", None) or getattr(ctx.state, "run_id", None) or ctx.session_id)
        self.session_id = ctx.session_id
        self.lock = ctx.lock
        if ctx.budget_manager is None:
            from app.agent.harness.run_budget import create_run_budget_manager

            ctx.budget_manager = create_run_budget_manager(
                harness.harness_config,
                run_started=ctx.run_started,
            )
        self.budget_manager = ctx.budget_manager
        self.result: Any = None
        self.worker_sem = asyncio.Semaphore(self._resolve_max_workers())

    def _resolve_max_workers(self) -> int:
        hard = max(1, int(getattr(self.harness.harness_config, "max_parallel_workers", 3) or 3))
        metadata = getattr(self.state, "metadata", None) or {}
        budget = metadata.get("run_budget") if isinstance(metadata, dict) else None
        if isinstance(budget, dict) and budget.get("max_parallel_workers") is not None:
            return max(1, min(hard, int(budget["max_parallel_workers"])))
        return hard


def get_session(run_id: str) -> RunSession | None:
    return _SESSIONS.get(run_id)


def bind_session(session: RunSession) -> None:
    _SESSIONS[session.run_id] = session


def drop_session(run_id: str) -> None:
    _SESSIONS.pop(run_id, None)


def _require_session(gstate: dict[str, Any]) -> RunSession:
    session = get_session(str(gstate.get("run_id") or ""))
    if session is None:
        raise RuntimeError("research graph session missing")
    return session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _budget_snapshot(session: RunSession) -> dict[str, Any]:
    manager = session.budget_manager
    budget: dict[str, Any] = {
        "tool_calls": int(getattr(session.state, "tool_calls_count", 0) or 0),
        "max_tool_calls": int(getattr(manager, "max_tool_calls", 80) or 80),
        "llm_calls": int(getattr(manager, "llm_calls", 0) or 0),
        "max_llm_calls": int(getattr(manager, "max_llm_calls", 80) or 80),
        "total_tokens": int(getattr(manager, "total_tokens", 0) or 0),
        "max_total_tokens": int(getattr(manager, "max_total_tokens", 300000) or 300000),
    }
    try:
        budget["deadline_remaining_sec"] = float(manager.remaining_run_sec())
        budget["synthesis_reserve_sec"] = float(manager.synthesis_reserve_sec)
    except Exception:
        budget["deadline_remaining_sec"] = 0.0
        budget["synthesis_reserve_sec"] = 0.0
    budget["exhausted"] = bool(
        budget["tool_calls"] >= budget["max_tool_calls"]
        or budget["llm_calls"] >= budget["max_llm_calls"]
        or budget["total_tokens"] >= budget["max_total_tokens"]
        or budget["deadline_remaining_sec"] <= budget["synthesis_reserve_sec"]
    )
    budget["low"] = not budget["exhausted"] and (
        budget["deadline_remaining_sec"] <= budget["synthesis_reserve_sec"] * 2
        or budget["tool_calls"] >= budget["max_tool_calls"] * 0.8
    )
    return budget


def _evidence_snapshot(session: RunSession, state: dict[str, Any]) -> dict[str, Any]:
    manager = session.ctx.citation_manager
    sources = list(getattr(manager, "sources", None) or []) if manager is not None else []
    tiers = [str(getattr(source, "source_tier", "") or "") for source in sources]
    primary = sum(tier == "PRIMARY" for tier in tiers)
    high_quality = sum(tier == "HIGH_QUALITY_SECONDARY" for tier in tiers)
    locators = [str(getattr(source, "locator", "") or "") for source in sources]
    domains: set[str] = set()
    for locator in locators:
        if locator.startswith(("http://", "https://")):
            domains.add(locator.split("/")[2].lower())
    return {
        "status": "sufficient" if primary >= 1 or high_quality >= 2 else ("partial" if sources else "insufficient"),
        "evidence_count": len(sources) or len(state.get("evidence_refs") or []),
        "trusted_evidence_count": primary + high_quality,
        "primary_source_count": primary,
        "independent_source_count": len(domains),
        "supported_claims": len(state.get("findings") or []),
        "unsupported_claims": 0,
        "unresolved_conflicts": [],
        "stale_sources": [],
        "reason_codes": ["citation_subsystem"],
    }


def _progress_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    plan_raw = state.get("plan")
    plan = ExecutionPlan.from_dict(plan_raw) if isinstance(plan_raw, dict) and plan_raw else None
    required = required_research_ids(plan) if plan is not None else []
    tasks = normalize_tasks(state.get("tasks"))
    succeeded = [task_id for task_id in required if tasks.get(task_id, {}).get("execution_status") == TaskExecutionStatus.SUCCEEDED.value]
    partial = [
        task_id
        for task_id in required
        if tasks.get(task_id, {}).get("execution_status") == TaskExecutionStatus.FAILED.value
        and tasks.get(task_id, {}).get("result_status") == ResultStatus.PARTIAL.value
    ]
    terminal = [task_id for task_id in required if tasks.get(task_id, {}).get("execution_status") not in {TaskExecutionStatus.PENDING.value, TaskExecutionStatus.RUNNING.value}]
    evidence = assess_evidence(state)
    if not required:
        status = "unknown"
    elif terminal and len(terminal) == len(required):
        status = "sufficient" if succeeded or (partial and evidence["trusted_evidence_count"]) else "gap"
    elif succeeded or partial:
        status = "gap"
    else:
        status = "unknown"
    missing = [
        task_id
        for task_id in required
        if tasks.get(task_id, {}).get("execution_status") in {TaskExecutionStatus.PENDING.value, TaskExecutionStatus.RUNNING.value}
    ]
    return {
        "status": status,
        "coverage_gaps": [f"task:{task_id}" for task_id in missing],
        "missing_dimensions": [f"task:{task_id}" for task_id in missing],
        "unresolved_conflicts": [],
        "low_confidence_claims": [],
        "stale_evidence": [],
        "unmet_success_criteria": [],
        "reason_codes": ["required_research_state"],
    }


def _sync_assessments(session: RunSession, state: dict[str, Any]) -> dict[str, Any]:
    state["evidence_assessment"] = _evidence_snapshot(session, state)
    state["progress_assessment"] = _progress_snapshot(state)
    state["execution_health"] = assess_execution_health(state)
    state["delivery_readiness"] = assess_delivery(state)
    state["budget"] = {**state.get("budget", {}), **_budget_snapshot(session)}
    state["budget_status"] = BudgetStatus.EXHAUSTED.value if state["budget"].get("exhausted") else (
        BudgetStatus.LOW.value if state["budget"].get("low") else BudgetStatus.AVAILABLE.value
    )
    return state


def _emit(
    session: RunSession,
    event_type: str,
    *,
    phase: str,
    status: str,
    duration_ms: int | None = None,
    task_id: str | None = None,
    attempt: int | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    try:
        from app.observability import get_recorder

        recorder = get_recorder()
        if recorder.is_active:
            recorder.emit(
                event_type,
                phase=phase,
                status=status,
                duration_ms=duration_ms,
                task_id=task_id,
                attempt=attempt,
                attributes=attributes or {},
            )
    except Exception:
        logger.debug("observability emit skipped", exc_info=True)


def _emit_assessments(session: RunSession, state: dict[str, Any], decision: dict[str, Any], phase: str) -> None:
    for event_type, key in (
        ("progress.assessed", "progress_assessment"),
        ("evidence.assessed", "evidence_assessment"),
        ("execution_health.assessed", "execution_health"),
        ("delivery.assessed", "delivery_readiness"),
    ):
        _emit(
            session,
            event_type,
            phase=phase,
            status=str(dict(state.get(key) or {}).get("status") or "unknown"),
            attributes=dict(state.get(key) or {}),
        )
    _emit(
        session,
        "control.decided",
        phase=phase,
        status=str(decision["action"]),
        attributes=decision,
    )


async def _default_checkpointer() -> Any:
    from app.research.runtime.checkpointer import aget_research_checkpointer

    return await aget_research_checkpointer()


async def _initial_or_resume_payload(graph: Any, payload: Any, config: dict[str, Any]) -> Any:
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        return payload
    if snapshot is None:
        return payload
    if tuple(getattr(snapshot, "next", None) or ()) or list(getattr(snapshot, "interrupts", None) or []):
        return None
    return payload


async def _ainvoke_resilient(graph: Any, payload: Any, config: dict[str, Any]) -> Any:
    try:
        return await graph.ainvoke(payload, config)
    except Exception as exc:
        if type(exc).__name__ not in {"GraphInterrupt", "NodeInterrupt", "GraphBubbleUp"}:
            raise
        value = getattr(exc, "args", None)
        interrupts = list(value or []) if value else [exc]
        return {"__interrupt__": interrupts}


def _has_interrupt(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("__interrupt__"))


def _interrupt_payloads(result: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item in result.get("__interrupt__") or []:
        value = getattr(item, "value", item)
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


class ResearchGraphRunner:
    """Compile and execute the graph; semantic routing stays in ControlPolicy."""

    def __init__(self, harness: Any):
        self.harness = harness

    def compile(self, checkpointer: Any = None, profile: str = "agent") -> Any:
        from app.research.runtime.graph import compile_research_graph

        return compile_research_graph(checkpointer=checkpointer, runtime=self, profile=profile)

    async def execute(self, ctx: Any, *, checkpointer: Any = None) -> Any:
        from langgraph.types import Command

        from app.research.routing.mode_router import budget_for_mode, route

        session = RunSession(self.harness, ctx)
        bind_session(session)
        session.state.metadata["workflow_authority"] = "research_state"
        route_decision = route(
            ctx.task_query,
            user_mode=getattr(ctx, "search_mode", "agent") or "agent",
            conversation_summary=str(getattr(ctx, "conversation_summary", "") or ""),
        )
        profile = route_decision.mode
        budget_cfg = budget_for_mode(profile, getattr(self.harness.harness_config, "personal_search", None) or {})
        if route_decision.execution_path == "fast_path":
            budget_cfg = {**budget_cfg, "max_tool_calls": 3, "max_replan_count": 0, "parallel": False}
        payload = empty_research_state(
            run_id=session.run_id,
            session_id=session.session_id,
            task_query=ctx.task_query,
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            max_tool_calls=int(budget_cfg["max_tool_calls"]),
            max_replan_count=int(budget_cfg["max_replan_count"]),
            search_mode=profile,
        )
        payload["budget"]["max_parallel_workers"] = session._resolve_max_workers()
        from app.agent.harness.usage_tracker import reset_current_budget_manager, set_current_budget_manager

        token = set_current_budget_manager(session.budget_manager)
        config = {"configurable": {"thread_id": session.run_id}, "recursion_limit": 30}
        try:
            if route_decision.execution_path == "fast_path":
                return await self._execute_simple_fact_fast_path(session, route_decision)
            graph = self.compile(checkpointer or await _default_checkpointer(), profile=profile)
            initial = await _initial_or_resume_payload(graph, payload, config)
            if initial is None:
                result = await _ainvoke_resilient(graph, Command(resume=True), config)
            else:
                result = await _ainvoke_resilient(graph, initial, config)
            while _has_interrupt(result):
                result = await _ainvoke_resilient(
                    graph,
                    Command(resume=await self._bridge_interrupts(result, session)),
                    config,
                )
            if session.result is not None:
                return session.result
            raise RuntimeError("graph completed without terminal executor result")
        finally:
            reset_current_budget_manager(token)
            drop_session(session.run_id)

    async def _bridge_interrupts(self, result: dict[str, Any], session: RunSession) -> Any:
        from app.agent.harness.hitl import hitl_coordinator
        from app.api.monitor import monitor

        payloads = _interrupt_payloads(result)
        if not payloads:
            return True
        item = payloads[0]
        payload = dict(item.get("coordinator_payload") or item)
        monitor.report_hitl_interrupt(
            session.session_id,
            list(payload.get("action_requests") or []),
            list(payload.get("review_configs") or []),
            step_index=int(payload.get("step_index", -1)),
            gate_type=str(payload.get("gate_type") or "step"),
            editable=bool(self.harness.harness_config.hitl_allow_edit),
        )
        try:
            return await hitl_coordinator.wait_for_decisions(
                session.session_id,
                payload,
                timeout_sec=self.harness.harness_config.hitl_timeout_sec,
            )
        except TimeoutError:
            return {"_timeout": True}

    async def _execute_simple_fact_fast_path(self, session: RunSession, route_decision: Any) -> Any:
        from app.research.execution.worker_executor import WorkerExecutorV2
        from app.research.runtime.simple_fact import render_simple_fact_answer
        from app.research.runtime.worker import ResearchContext, ResearchTask, WorkerResult

        state = session.state
        state.plan = ExecutionPlan(
            steps=[PlanStep(step_type="network_search", description="检索并确认单一事实", task_id="simple_fact:search", allowed_tools=["internet_search"], metadata={"simple_fact_fast_path": True})],
            summary="Simple fact fast path",
            planning_mode="simple_fact_fast_path",
        )
        state.metadata.update(
            {
                "task_shape": "simple_fact",
                "execution_path": "fast_path",
                "planner_calls": 0,
                "synthesis_calls": 0,
                "replan_count": 0,
                "workers": 1,
                "route_signals": list(route_decision.signals),
            }
        )
        task = ResearchTask(task_id="simple_fact:search", objective=session.ctx.task_query, step_type="network_search", step_index=0, description="检索并确认单一事实", allowed_tools=["internet_search"], plan_version=1)
        context = ResearchContext(run_id=session.run_id, query=session.ctx.task_query, user_id=session.ctx.user_id, tenant_id=session.ctx.tenant_id, project_id=session.ctx.project_id, session_id=session.session_id)
        worker_result = await WorkerExecutorV2(self.harness, session).execute(task, context)
        if worker_result.raw is not None:
            state.step_results.append(worker_result.raw)
        manager = session.ctx.citation_manager
        answer = render_simple_fact_answer(query=session.ctx.task_query, worker_result=worker_result, citation_manager=manager) if manager is not None else None
        source_counts = manager.source_counts_by_tier() if manager is not None else {}
        passed = bool(worker_result.ok and answer is not None and answer.sufficient)
        if passed and manager is not None and answer is not None:
            state.final_content = manager.build_cited_report(answer.content)
        else:
            state.final_content = "未能从一手来源或两个独立高质量来源确认该事实。"
            state.abort_reason = state.abort_reason or "insufficient_trusted_evidence"
        state.metadata.update(
            {
                "quality": {"verdict": "pass" if passed else "fail", "issues": [] if passed else ["insufficient_trusted_evidence"], "attempts": 1},
                "quality_attempted": True,
                "source_counts": source_counts,
                "answer_grounded": passed,
                "planner_calls": 0,
                "synthesis_calls": 0,
                "workers": 1,
            }
        )
        if manager is not None:
            manager.save_evidence_json(session.ctx.run_dir, run_id=session.run_id)
        evidence_assessment = _evidence_snapshot(session, {})
        terminal = terminal_update(
            {
                "cancel_reason": "",
                "abort_reason": state.abort_reason,
                "quality_assessment": {"verdict": "pass" if passed else "fail"},
                "evidence_assessment": evidence_assessment,
                "final_content": state.final_content,
            },
            reason="simple_fact_fast_path",
            stage=WorkflowPhase.FINALIZE.value,
            research_completed=passed,
            synthesis_attempted=False,
            quality_attempted=True,
        )
        outcome = str(terminal["termination"]["outcome"])
        if isinstance(state.metadata, dict):
            state.metadata["termination"] = terminal["termination"]
        result = await self.harness._phase_finalize(
            state,
            session.ctx.session_dir,
            success=outcome == "success",
            started_at=session.ctx.run_started,
            deliverable_dir=session.ctx.deliverable_dir,
        )
        session.result = result
        if isinstance(result.metadata, dict):
            result.metadata.update(
                {
                    "task_shape": "simple_fact",
                    "execution_path": "fast_path",
                    "planner_calls": 0,
                    "synthesis_calls": 0,
                    "replan_count": 0,
                    "workers": 1,
                    "tool_calls_count": int(state.tool_calls_count or 0),
                    "source_counts": source_counts,
                    "primary_sources": int(source_counts.get(SourceTier.PRIMARY.value, 0)),
                    "budget_reservation_errors": 0 if worker_result.ok else 1,
                    "partial_renderer_called": False,
                    "quality": "pass" if passed else "fail",
                    "outcome": outcome,
                    "termination": terminal["termination"],
                }
            )
        return result

    async def node_vanilla_agent(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.isolation import worker_row
        from app.research.runtime.worker import LangChainWorkerRuntime, ResearchContext, ResearchTask

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        query = str(gstate.get("resolved_query") or session.ctx.task_query or "")
        step = PlanStep(step_type="research", description=query, objective=query, task_id="vanilla")
        session.state.plan = ExecutionPlan(summary="direct baseline", steps=[step], planning_mode="direct")
        result = await LangChainWorkerRuntime(self.harness, session).execute(
            ResearchTask(task_id="vanilla", objective=query, step_type="research", step_index=0, description=query),
            ResearchContext(run_id=session.run_id, query=query, user_id=session.ctx.user_id, tenant_id=session.ctx.tenant_id, project_id=session.ctx.project_id, session_id=session.session_id),
        )
        session.state.final_content = result.summary or query
        row = worker_row("vanilla", step, result.ok, getattr(result.raw, "result", None)) if result.raw is not None else {"task_id": "vanilla", "ok": result.ok, "summary": result.summary, "step_type": "research", "payload": {}}
        return transition_update(
            gstate,
            WorkflowPhase.DIRECT,
            {"final_content": session.state.final_content, "worker_results": [row], "evidence_refs": result.evidence_refs, "quality_assessment": {"verdict": "pass" if result.ok else "fail", "grounding": False}},
        )

    async def node_intent(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        if gstate.get("intent"):
            intent = TaskIntent.from_dict(gstate["intent"])
            needs = bool(gstate.get("needs_clarification"))
        else:
            session.state = await self.harness._phase_understand(session.state, session.ctx.task_query, bool(session.ctx.uploaded_prompt))
            intent = session.state.intent or understand_task(session.ctx.task_query)
            needs = bool(intent.needs_clarification and not intent.clarification_resolved)
        payload = intent.to_dict()
        return transition_update(
            gstate,
            WorkflowPhase.UNDERSTAND,
            {"intent": payload, "brief": brief_from_intent(payload), "needs_clarification": needs},
        )

    async def node_clarify(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        intent = TaskIntent.from_dict(gstate.get("intent") or {})
        resolved = auto_resolve_clarification(intent)
        session.state.intent = resolved
        return transition_update(gstate, WorkflowPhase.CLARIFY, {"intent": resolved.to_dict(), "needs_clarification": False})

    async def node_plan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        intent = TaskIntent.from_dict(gstate.get("intent") or {}) if gstate.get("intent") else understand_task(gstate["task_query"])
        plan = research_only_plan(annotate_plan_tasks(finalize_plan(build_plan(intent)), intent))
        session.state.intent = intent
        session.state.plan = plan
        session.state.replan_count = 0
        return transition_update(
            gstate,
            WorkflowPhase.PLAN,
            {"plan": plan.to_dict(), "plan_version": plan.plan_version, "tasks": initialize_tasks(plan), "needs_plan_review": False, "replan_budget": {"attempted": 0, "applied": 0, "max_attempts": int((gstate.get("budget") or {}).get("max_replan_count") or 0)}},
        )

    async def node_plan_validate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        plan_raw = gstate.get("plan")
        plan = ExecutionPlan.from_dict(plan_raw) if isinstance(plan_raw, dict) and plan_raw else None
        if plan is None or not plan.steps or any(step.step_type not in {"research", "network_search", "file_read"} for step in plan.steps):
            return transition_update(gstate, WorkflowPhase.PLAN_VALIDATED, {"abort_reason": "empty_plan"})
        return transition_update(gstate, WorkflowPhase.PLAN_VALIDATED, {})

    async def node_dispatch(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        decision = decide_control(state)
        state["control_decision"] = decision
        _emit_assessments(session, state, decision, WorkflowPhase.DISPATCH.value)
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "progress_assessment": state["progress_assessment"],
                    "evidence_assessment": state["evidence_assessment"],
                    "execution_health": state["execution_health"],
                    "delivery_readiness": state["delivery_readiness"],
                    "control_decision": decision,
                }
            )
        return transition_update(
            gstate,
            WorkflowPhase.DISPATCH,
            {
                key: state[key]
                for key in (
                    "progress_assessment",
                    "evidence_assessment",
                    "execution_health",
                    "delivery_readiness",
                    "control_decision",
                    "budget",
                    "budget_status",
                )
            },
        )

    async def node_retry(self, gstate: dict[str, Any]) -> dict[str, Any]:
        decision = dict(gstate.get("control_decision") or {})
        task_ids = [str(item) for item in decision.get("task_ids") or []]
        tasks = dict(gstate.get("tasks") or {})
        for task_id in task_ids:
            tasks = retry_task(tasks, task_id)
        return {"tasks": tasks}

    async def node_research_worker(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from langgraph.types import interrupt

        from app.research.execution.worker_executor import WorkerExecutorV2
        from app.research.runtime.findings import normalize_findings
        from app.research.runtime.isolation import worker_row
        from app.research.runtime.worker import ResearchContext, ResearchTask, WorkerResult

        session = _require_session(gstate)
        step_index = int(gstate.get("step_index") or 0)
        task_id = str(gstate.get("task_id") or f"s{step_index}")
        plan = session.state.plan
        if plan is None or step_index >= len(plan.steps):
            failure = classify_failure("missing_step")
            return transition_update(gstate, WorkflowPhase.EXECUTE, {"tasks": transition_task(gstate.get("tasks"), task_id, execution_status=TaskExecutionStatus.FAILED, result_status=ResultStatus.NONE, failure=failure, timestamp=_now()), "worker_results": [{"task_id": task_id, "ok": False, "status": "failed", "summary": "missing_step"}]})
        step = plan.steps[step_index]
        if self.harness.harness_config.hitl_enabled and step.step_type in set(self.harness.harness_config.hitl_step_gate_types):
            resume = interrupt({"kind": "step_gate", "step_index": step_index, "description": step.description})
            if isinstance(resume, dict) and resume.get("_timeout"):
                return transition_update(gstate, WorkflowPhase.EXECUTE, {"cancel_reason": "user_cancelled"})
        sync_execution_projection(session.state, gstate)
        task = ResearchTask(task_id=task_id, objective=str(step.objective or step.description), step_type=step.step_type, step_index=step_index, description=step.description, subagent=step.subagent or "", allowed_tools=list(step.allowed_tools or []), plan_version=int(gstate.get("plan_version") or 1))
        context = ResearchContext(run_id=session.run_id, query=session.ctx.task_query, user_id=session.ctx.user_id, tenant_id=session.ctx.tenant_id, project_id=session.ctx.project_id, session_id=session.session_id)
        attempt = int(normalize_tasks(gstate.get("tasks")).get(task_id, {}).get("attempt") or 0) + 1
        running = transition_task(gstate.get("tasks"), task_id, execution_status=TaskExecutionStatus.RUNNING, attempt=attempt, timestamp=_now())
        result = await WorkerExecutorV2(self.harness, session).execute(task, context)
        if not isinstance(result, WorkerResult):
            raise TypeError("WorkerRuntime.execute must return WorkerResult")
        if result.raw is not None:
            session.state.step_results.append(result.raw)
        row = worker_row(task_id, step, result.ok, getattr(result.raw, "result", None)) if result.raw is not None else {"task_id": task_id, "ok": result.ok, "summary": result.summary, "step_type": step.step_type, "payload": {"facts": result.facts, "sources": result.sources, "findings": result.findings, "evidence_ids": result.evidence_refs}}
        row.update(status=result.status, fail_reason=result.fail_reason, queue_ms=result.queue_ms, execution_ms=result.execution_ms)
        normalized_findings, _ = normalize_findings(result.findings, task_id=task_id, subject_id=str(step.metadata.get("subject_id") or "general"), dimension=str((step.metadata.get("coverage_keys") or ["general"])[0]))
        row["payload"] = {**(row.get("payload") or {}), "findings": normalized_findings, "evidence_ids": result.evidence_refs}
        if result.ok:
            execution_status = TaskExecutionStatus.SUCCEEDED
            result_status = ResultStatus.COMPLETE
            failure = None
        elif result.status == "skipped":
            execution_status = TaskExecutionStatus.SKIPPED
            result_status = ResultStatus.NONE
            failure = None
        else:
            execution_status = TaskExecutionStatus.FAILED
            result_status = ResultStatus.PARTIAL if result.evidence_refs or result.findings else ResultStatus.NONE
            failure = classify_failure(result.fail_reason or result.status)
        tasks = transition_task(
            running,
            task_id,
            execution_status=execution_status,
            result_status=result_status,
            attempt=attempt,
            failure=failure,
            evidence_refs=result.evidence_refs,
            timestamp=_now(),
        )
        _emit(
            session,
            "task.transitioned",
            phase=WorkflowPhase.EXECUTE.value,
            status=execution_status.value,
            task_id=task_id,
            attempt=int(tasks.get(task_id, {}).get("attempt") or 0),
            attributes={
                "result_status": result_status.value,
                "failure": failure or {},
                "evidence_refs": result.evidence_refs,
            },
        )
        return transition_update(gstate, WorkflowPhase.EXECUTE, {"tasks": tasks, "worker_results": [row], "evidence_refs": result.evidence_refs, "findings": normalized_findings})

    async def node_progress(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        previous = dict(gstate.get("control_decision") or {})
        decision = decide_control(state)
        stalled = int(gstate.get("stalled_cycles") or 0) + 1 if previous.get("decision_id") == decision["decision_id"] else 0
        state["execution_health"] = {**state["execution_health"], "stalled_cycles": stalled}
        state["control_decision"] = decision
        _emit_assessments(session, state, decision, WorkflowPhase.ASSESS.value)
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "progress_assessment": state["progress_assessment"],
                    "evidence_assessment": state["evidence_assessment"],
                    "execution_health": state["execution_health"],
                    "delivery_readiness": state["delivery_readiness"],
                    "control_decision": decision,
                }
            )
        return transition_update(gstate, WorkflowPhase.ASSESS, {key: state[key] for key in ("progress_assessment", "evidence_assessment", "execution_health", "delivery_readiness", "control_decision", "budget", "stalled_cycles") if key != "stalled_cycles"} | {"stalled_cycles": stalled, "budget_status": state["budget_status"]})

    async def node_replan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.planning.lead_planner import research_step_from_task

        session = _require_session(gstate)
        plan = session.state.plan
        if plan is None or session.state.intent is None:
            return transition_update(gstate, WorkflowPhase.REPLAN, {})
        budget = dict(gstate.get("replan_budget") or {})
        assessment = dict(gstate.get("progress_assessment") or {})
        gaps = [str(item).replace("task:", "") for item in assessment.get("missing_dimensions") or []][:2]
        for gap in gaps:
            if gap not in plan.steps and len(plan.steps) < 12:
                plan.steps.append(research_step_from_task(task_id=f"{gap}_gap", objective=f"补充证据：{gap}", depends_on=[], sources=["web"], required=True))
        plan.plan_version += 1
        session.state.plan = plan
        budget.update(attempted=int(budget.get("attempted") or 0) + 1, applied=int(budget.get("applied") or 0) + len(gaps))
        session.state.replan_count = int(budget["applied"])
        existing_tasks = normalize_tasks(gstate.get("tasks"))
        tasks = {**initialize_tasks(plan), **existing_tasks}
        _emit(
            session,
            "replan.applied",
            phase=WorkflowPhase.REPLAN.value,
            status="ok",
            attributes={"plan_version": plan.plan_version, "added_tasks": gaps, "replan_budget": budget},
        )
        return transition_update(
            gstate,
            WorkflowPhase.REPLAN,
            {"plan": plan.to_dict(), "plan_version": plan.plan_version, "tasks": tasks, "replan_budget": budget},
        )

    async def node_synthesize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.execution.synthesis_executor import SynthesisExecutor, SynthesisRequest
        from app.research.runtime.worker import ResearchContext

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        decision = dict(gstate.get("control_decision") or {})
        delivery = dict(gstate.get("delivery_readiness") or {})
        mode = str(decision.get("mode") or delivery.get("mode") or "")
        evidence_refs = [str(item) for item in gstate.get("evidence_refs") or []]
        limitations = [str(item) for item in delivery.get("limitations") or []]
        progress = dict(gstate.get("progress_assessment") or {})
        evidence = dict(gstate.get("evidence_assessment") or {})
        conflicts = [
            *[str(item) for item in progress.get("unresolved_conflicts") or []],
            *[str(item) for item in evidence.get("unresolved_conflicts") or []],
        ]
        request = SynthesisRequest(
            mode=mode,
            evidence_refs=evidence_refs,
            limitations=limitations,
            unresolved_conflicts=conflicts,
            research_summary=self._research_summary(gstate),
        )
        _emit(
            session,
            "synthesis.started",
            phase=WorkflowPhase.SYNTHESIS.value,
            status="start",
            attributes={"mode": mode, "evidence_count": len(evidence_refs)},
        )
        result = await SynthesisExecutor(self.harness, session).execute(
            request,
            ResearchContext(
                run_id=session.run_id,
                query=session.ctx.task_query,
                user_id=session.ctx.user_id,
                tenant_id=session.ctx.tenant_id,
                project_id=session.ctx.project_id,
                session_id=session.session_id,
            ),
        )
        if result.ok:
            manager = session.ctx.citation_manager
            final_content = manager.build_cited_report(result.summary) if manager is not None else result.summary
            session.state.final_content = final_content
            if isinstance(session.state.metadata, dict):
                session.state.metadata.update(
                    {
                        "synthesis_attempted": True,
                        "synthesis_mode": mode,
                        "synthesis_status": result.status,
                    }
                )
            _emit(
                session,
                "synthesis.completed",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="ok",
                duration_ms=result.duration_ms,
                attributes={"mode": mode, "content_chars": len(final_content)},
            )
            return transition_update(
                gstate,
                WorkflowPhase.SYNTHESIS,
                {
                    "final_content": final_content,
                    "draft_ref": "synthesis:latest",
                    "quality_assessment": {},
                },
            )
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "synthesis_attempted": True,
                    "synthesis_mode": mode,
                    "synthesis_status": result.status,
                    "synthesis_fail_reason": result.fail_reason,
                }
            )
        _emit(
            session,
            "synthesis.failed",
            phase=WorkflowPhase.SYNTHESIS.value,
            status="failed",
            duration_ms=result.duration_ms,
            attributes={"mode": mode, "fail_reason": result.fail_reason},
        )
        return transition_update(
            gstate,
            WorkflowPhase.SYNTHESIS,
            {"quality_assessment": {}},
        )

    async def node_quality_gate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.assessment.evidence import EvidenceStatus, assess_evidence
        from app.research.assessment.quality import QualityVerdict

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        final_content = str(gstate.get("final_content") or "").strip()
        manager = session.ctx.citation_manager
        evidence = assess_evidence(gstate)
        if not final_content:
            assessment = {
                "verdict": QualityVerdict.FAIL.value,
                "issues": ["no_content"],
                "repairable": False,
                "suggested_action": "",
                "grounding": False,
                "citation_metrics": {},
            }
        elif bool((session.state.metadata or {}).get("synthesis_failed")):
            assessment = {
                "verdict": QualityVerdict.FAIL.value,
                "issues": ["synthesis_failed"],
                "repairable": False,
                "suggested_action": "",
                "grounding": False,
                "citation_metrics": {},
            }
        elif manager is None:
            assessment = {
                "verdict": QualityVerdict.UNKNOWN.value,
                "issues": ["citation_manager_missing"],
                "repairable": False,
                "suggested_action": "",
                "grounding": False,
                "citation_metrics": {},
            }
        else:
            metrics: dict[str, Any] = dict(manager.compute_metrics(final_content))
            citations_ok, citation_issue = manager.validate_citations(final_content)
            grounding = bool(
                citations_ok
                and evidence["status"] == EvidenceStatus.SUFFICIENT.value
                and int(metrics.get("registered_sources") or 0) > 0
            )
            issues = [] if citations_ok else [citation_issue]
            if evidence["status"] != EvidenceStatus.SUFFICIENT.value:
                issues.append(f"evidence_{evidence['status']}")
            assessment = {
                "verdict": QualityVerdict.PASS.value if grounding else QualityVerdict.FAIL.value,
                "issues": issues,
                "repairable": bool(citation_issue == "citation_coverage_low" and manager.sources),
                "suggested_action": (
                    "repair"
                    if citation_issue == "citation_coverage_low"
                    else "replan"
                    if evidence["status"] == EvidenceStatus.INSUFFICIENT.value
                    else ""
                ),
                "grounding": grounding,
                "citation_metrics": metrics,
            }
        state = dict(gstate)
        state["quality_assessment"] = assessment
        decision = decide_control(state)
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "quality": assessment,
                    "quality_attempted": True,
                    "answer_grounded": bool(assessment["grounding"]),
                    "control_decision": decision,
                }
            )
        _emit(
            session,
            "quality.assessed",
            phase=WorkflowPhase.QUALITY.value,
            status=str(assessment["verdict"]),
            attributes={"issues": assessment["issues"], "citation_metrics": assessment["citation_metrics"]},
        )
        _emit(
            session,
            "control.decided",
            phase=WorkflowPhase.QUALITY.value,
            status=str(decision["action"]),
            attributes=decision,
        )
        return transition_update(
            gstate,
            WorkflowPhase.QUALITY,
            {"quality_assessment": assessment, "control_decision": decision},
        )

    async def node_repair_synthesis(self, gstate: dict[str, Any]) -> dict[str, Any]:
        return transition_update(gstate, WorkflowPhase.REPAIR_SYNTHESIS, {})

    async def node_finalize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        session.state.final_content = str(gstate.get("final_content") or "")
        plan = session.state.plan
        required = required_research_ids(plan) if plan is not None else []
        tasks = normalize_tasks(gstate.get("tasks"))
        terminal_required = [
            task_id
            for task_id in required
            if tasks.get(task_id, {}).get("execution_status")
            not in {TaskExecutionStatus.PENDING.value, TaskExecutionStatus.RUNNING.value}
        ]
        termination = terminal_update(
            gstate,
            reason="",
            stage=WorkflowPhase.FINALIZE.value,
            research_completed=bool(required) and len(terminal_required) == len(required) and bool(gstate.get("evidence_refs")),
            synthesis_attempted=bool(gstate.get("final_content")),
            quality_attempted=bool(gstate.get("quality_assessment")),
        )
        outcome = str(termination["termination"]["outcome"])
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "termination": termination["termination"],
                    "quality": dict(gstate.get("quality_assessment") or {}),
                    "quality_attempted": bool(gstate.get("quality_assessment")),
                }
            )
        result = await self.harness._phase_finalize(
            session.state,
            session.ctx.session_dir,
            success=outcome == "success",
            started_at=session.ctx.run_started,
            deliverable_dir=session.ctx.deliverable_dir,
        )
        session.result = result
        _emit(
            session,
            "run.terminated",
            phase=WorkflowPhase.FINALIZE.value,
            status=outcome,
            attributes={"termination": termination["termination"]},
        )
        return transition_update(
            gstate,
            WorkflowPhase.FINALIZE,
            {**termination, "final_content": session.state.final_content},
        )

    def _research_summary(self, gstate: dict[str, Any]) -> str:
        lines: list[str] = []
        for finding in list(gstate.get("findings") or [])[:120]:
            if not isinstance(finding, dict):
                continue
            claim = str(finding.get("claim") or finding.get("summary") or "").strip()
            if not claim:
                continue
            evidence_ids = ", ".join(str(item) for item in finding.get("evidence_ids") or [])
            lines.append(
                f"[{finding.get('finding_id', '')}] {claim}"
                + (f"（证据：{evidence_ids}）" if evidence_ids else "")
            )
        for row in list(gstate.get("worker_results") or [])[:80]:
            if not isinstance(row, dict):
                continue
            summary = str(row.get("summary") or "").strip()
            if not summary:
                continue
            lines.append(f"[worker:{row.get('task_id', '')}] {summary}")
        return "\n".join(lines)
