"""Dumb executor bridge between the canonical ResearchState graph and harness services."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.agent.harness.planner import auto_resolve_clarification, understand_task
from app.agent.harness.citations import SourceTier
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep, TaskIntent
from app.research.assessment.delivery import assess_delivery
from app.research.assessment.evidence import assess_evidence
from app.research.assessment.execution_health import assess_execution_health
from app.research.assessment.progress import assess_progress
from app.research.control.policy import decide_control
from app.research.control.terminal_policy import terminal_update
from app.research.control.transitions import transition_update
from app.research.domain.contracts import (
    BudgetStatus,
    WorkflowPhase,
    new_replan_budget,
    recovery_limits_from_state,
    replan_budget_from_state,
)
from app.research.domain.failure import classify_failure
from app.research.domain.gaps import active_research_steps, step_gap_ids, sync_business_gaps
from app.research.domain.recovery import build_replacement_patch
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
from app.observability.semantic_events import (
    brief_event_attributes,
    control_decision_event_attributes,
    delivery_event_attributes,
    evidence_event_attributes,
    execution_health_event_attributes,
    plan_event_attributes,
    progress_event_attributes,
    quality_event_attributes,
    termination_event_attributes,
)

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
    evidence_refs = [str(item) for item in state.get("evidence_refs") or [] if str(item).strip()]
    return {
        "status": "sufficient" if primary >= 1 or high_quality >= 2 else ("partial" if sources or evidence_refs else "insufficient"),
        "evidence_count": len(sources) or len(evidence_refs),
        "trusted_evidence_count": primary + high_quality,
        "primary_source_count": primary,
        "independent_source_count": len(domains),
        "supported_claims": len(state.get("findings") or []),
        "unsupported_claims": 0,
        "unresolved_conflicts": [],
        "stale_sources": [],
        "reason_codes": ["citation_subsystem"] if sources else (["artifact_evidence_refs"] if evidence_refs else []),
    }


def _sync_assessments(session: RunSession, state: dict[str, Any]) -> dict[str, Any]:
    state["evidence_assessment"] = _evidence_snapshot(session, state)
    state["progress_assessment"] = assess_progress(state)
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
    plan_version: int | None = None,
    task_id: str | None = None,
    attempt: int | None = None,
    attributes: dict[str, Any] | None = None,
    input_refs: list[dict[str, Any]] | None = None,
    output_refs: list[dict[str, Any]] | None = None,
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
                plan_version=plan_version,
                task_id=task_id,
                attempt=attempt,
                attributes=attributes or {},
                input_refs=input_refs,
                output_refs=output_refs,
            )
    except Exception:
        logger.debug("observability emit skipped", exc_info=True)


def _emit_assessments(
    session: RunSession,
    state: dict[str, Any],
    decision: dict[str, Any],
    phase: str,
    *,
    include_progress: bool = True,
) -> None:
    plan_version = int(state.get("plan_version") or 1)
    assessment_events = (
        ("progress.assessed", "progress_assessment", progress_event_attributes),
        ("evidence.assessed", "evidence_assessment", evidence_event_attributes),
        ("execution_health.assessed", "execution_health", execution_health_event_attributes),
        ("delivery.assessed", "delivery_readiness", delivery_event_attributes),
    )
    for event_type, key, event_attributes in assessment_events:
        if key == "progress_assessment" and not include_progress:
            continue
        assessment = dict(state.get(key) or {})
        _emit(
            session,
            event_type,
            phase=phase,
            status=str(assessment.get("status") or "unknown"),
            attributes=(
                event_attributes(
                    assessment,
                    dispatch_wave_id=int(state.get("dispatch_wave_id") or 0),
                )
                if key == "progress_assessment"
                else event_attributes(assessment)
            ),
        )
    _emit(
        session,
        "control.decided",
        phase=phase,
        status=str(decision["action"]),
        attributes=control_decision_event_attributes(decision),
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

    RECURSION_LIMIT = 30

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
        harness_replan_limit = getattr(self.harness.harness_config, "max_replan_count", None)
        if harness_replan_limit is not None:
            budget_cfg["max_replan_count"] = min(
                int(budget_cfg["max_replan_count"]),
                max(0, int(harness_replan_limit)),
            )
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
        config = {"configurable": {"thread_id": session.run_id}, "recursion_limit": self.RECURSION_LIMIT}
        active_checkpointer = checkpointer
        owns_checkpointer = False
        try:
            if route_decision.execution_path == "fast_path":
                return await self._execute_simple_fact_fast_path(session, route_decision)
            if active_checkpointer is None:
                active_checkpointer = await _default_checkpointer()
                owns_checkpointer = True
            graph = self.compile(active_checkpointer, profile=profile)
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
            if owns_checkpointer and active_checkpointer is not None:
                from app.research.runtime.checkpointer import close_async_checkpointer

                await close_async_checkpointer(active_checkpointer)

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
        _emit(
            session,
            "brief.compiled",
            phase=WorkflowPhase.UNDERSTAND.value,
            status="ok",
            attributes=brief_event_attributes(
                brief_from_intent(payload),
                run_id=session.run_id,
                planner_source="intent",
            ),
        )
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
        from app.agent.llm import compression_model
        from app.agent.harness.planner import finalize_plan
        from app.research.planning.compose import PlanningLimits
        from app.research.planning.effort import resolve_effective_budget
        from app.research.planning.lead_planner import heuristic_dynamic_plan, lead_plan_with_llm
        from app.research.planning.policy import parse_source_policy
        session = _require_session(gstate)
        intent = TaskIntent.from_dict(gstate.get("intent") or {}) if gstate.get("intent") else understand_task(gstate["task_query"])
        config = self.harness.harness_config
        limits = PlanningLimits.from_config(config)
        policy = parse_source_policy(intent.raw_query)
        effective = resolve_effective_budget(intent, config)
        plan = None
        planner_source = "heuristic"
        if bool(getattr(config, "planner_llm_enabled", False)) and bool(
            getattr(config, "planner_dynamic_lead_enabled", True)
        ):
            try:
                plan = await asyncio.wait_for(
                    lead_plan_with_llm(
                        intent,
                        policy,
                        model=compression_model,
                        session_id=session.session_id,
                        max_tasks=limits.max_research_tasks,
                        effort=effective,
                    ),
                    timeout=max(5.0, float(getattr(config, "planner_wall_budget_sec", 45) or 45)),
                )
            except Exception:
                plan = None
        if plan is None:
            plan = heuristic_dynamic_plan(intent, policy)
        else:
            planner_source = "lead_llm"
        plan = research_only_plan(annotate_plan_tasks(finalize_plan(plan), intent))
        tasks = initialize_tasks(plan)
        business_gaps = sync_business_gaps(plan, tasks, {})
        session.state.intent = intent
        session.state.plan = plan
        session.state.replan_count = 0
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "effort_plan": effective.to_dict(),
                    "run_budget": effective.as_run_budget(),
                    "planner_source": planner_source,
                }
            )
        _emit(
            session,
            "plan.created",
            phase=WorkflowPhase.PLAN.value,
            status="ok",
            plan_version=plan.plan_version,
            attributes=plan_event_attributes(
                plan,
                brief_from_intent(intent.to_dict()),
                run_id=session.run_id,
                planner_source=planner_source,
            ),
        )
        return transition_update(
            gstate,
            WorkflowPhase.PLAN,
            {
                "plan": plan.to_dict(),
                "plan_version": plan.plan_version,
                "tasks": tasks,
                "business_gaps": business_gaps,
                "needs_plan_review": False,
                "replan_budget": new_replan_budget(
                    int((gstate.get("budget") or {}).get("max_replan_count") or 0)
                ),
            },
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
        dispatch_wave_id = int(gstate.get("dispatch_wave_id") or 0) + 1
        state["dispatch_wave_id"] = dispatch_wave_id
        _emit_assessments(
            session,
            state,
            decision,
            WorkflowPhase.DISPATCH.value,
            include_progress=False,
        )
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
                    "dispatch_wave_id",
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
        current_task = normalize_tasks(gstate.get("tasks")).get(task_id)
        attempt = int((current_task or {}).get("attempt") or 0) + 1
        if current_task is not None and current_task["execution_status"] != TaskExecutionStatus.PENDING.value:
            _emit(
                session,
                "recovery.decided",
                phase=WorkflowPhase.EXECUTE.value,
                status="duplicate",
                plan_version=int(gstate.get("plan_version") or 1),
                task_id=task_id,
                attempt=int(current_task.get("attempt") or 0),
                attributes={
                    "decision": "skip_duplicate_worker",
                    "execution_status": current_task["execution_status"],
                    "dispatch_wave_id": int(gstate.get("dispatch_wave_id") or 0),
                },
            )
            return transition_update(
                gstate,
                WorkflowPhase.EXECUTE,
                {
                    "worker_results": [
                        {
                            "task_id": task_id,
                            "ok": False,
                            "status": "duplicate_skipped",
                            "summary": "duplicate task attempt suppressed",
                            "payload": {"duplicate": True},
                        }
                    ]
                },
            )
        dispatch_wave_id = int(gstate.get("dispatch_wave_id") or 0)
        task = ResearchTask(task_id=task_id, objective=str(step.objective or step.description), step_type=step.step_type, step_index=step_index, description=step.description, subagent=step.subagent or "", allowed_tools=list(step.allowed_tools or []), plan_version=int(gstate.get("plan_version") or 1), attempt=attempt, dispatch_wave_id=dispatch_wave_id)
        context = ResearchContext(run_id=session.run_id, query=session.ctx.task_query, user_id=session.ctx.user_id, tenant_id=session.ctx.tenant_id, project_id=session.ctx.project_id, session_id=session.session_id)
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
                "dispatch_wave_id": dispatch_wave_id,
            },
        )
        for evidence_id in result.evidence_refs:
            _emit(
                session,
                "evidence.registered",
                phase=WorkflowPhase.EXECUTE.value,
                status="ok",
                task_id=task_id,
                attempt=attempt,
                attributes={
                    "evidence_id": evidence_id,
                    "artifact_id": evidence_id,
                    "source_kind": "artifact",
                    "support_type": "partial",
                    "source_quality": "untrusted_external",
                },
            )
        return transition_update(
            gstate,
            WorkflowPhase.EXECUTE,
            {
                "tasks": {task_id: tasks[task_id]},
                "worker_results": [row],
                "evidence_refs": result.evidence_refs,
                "findings": normalized_findings,
            },
        )

    async def node_progress(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        plan = session.state.plan
        if plan is None:
            return transition_update(gstate, WorkflowPhase.ASSESS, {"abort_reason": "missing_plan"})
        business_gaps = sync_business_gaps(
            plan,
            state.get("tasks"),
            gstate.get("business_gaps"),
            resolved_gap_ids=[str(item) for item in state["progress_assessment"].get("resolved_gap_ids") or []],
        )
        state["business_gaps"] = business_gaps
        snapshot = {
            "gap_ids": [str(item) for item in state["progress_assessment"].get("gap_ids") or []],
            "resolved_gap_ids": [
                str(item) for item in state["progress_assessment"].get("resolved_gap_ids") or []
            ],
            "evidence_refs": sorted(
                {str(item) for item in gstate.get("evidence_refs") or [] if str(item).strip()}
            ),
            "delivery_mode": str(state["delivery_readiness"].get("mode") or "none"),
            "delivery_status": str(state["delivery_readiness"].get("status") or "unknown"),
        }
        previous_snapshot = dict(gstate.get("recovery_snapshot") or {})
        stalled = int(gstate.get("stalled_cycles") or 0) + 1 if snapshot == previous_snapshot else 0
        state["stalled_cycles"] = stalled
        state["recovery_snapshot"] = snapshot
        state = _sync_assessments(session, state)
        state["business_gaps"] = business_gaps
        state["recovery_snapshot"] = snapshot
        decision = decide_control(state)
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
        return transition_update(
            gstate,
            WorkflowPhase.ASSESS,
            {
                key: state[key]
                for key in (
                    "progress_assessment",
                    "evidence_assessment",
                    "execution_health",
                    "delivery_readiness",
                    "control_decision",
                    "business_gaps",
                    "recovery_snapshot",
                    "budget",
                )
            }
            | {
                "stalled_cycles": stalled,
                "budget_status": state["budget_status"],
                "dispatch_wave_id": int(gstate.get("dispatch_wave_id") or 0),
            },
        )

    async def node_replan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        plan = session.state.plan
        if plan is None or session.state.intent is None:
            return transition_update(gstate, WorkflowPhase.REPLAN, {})
        budget = dict(replan_budget_from_state(gstate))
        assessment = dict(gstate.get("progress_assessment") or {})
        gap_ids = [str(item) for item in assessment.get("gap_ids") or [] if str(item).strip()]
        if not gap_ids:
            gap_ids = [
                gap_id
                for _index, step in active_research_steps(plan)
                if not (isinstance(step.metadata, dict) and step.metadata.get("optional"))
                for gap_id in step_gap_ids(step)
            ]
        patch = build_replacement_patch(
            plan,
            gstate.get("tasks"),
            gap_ids=gap_ids,
            limits=recovery_limits_from_state(gstate),
            rejected_hashes=[str(item) for item in gstate.get("rejected_patch_hashes") or []],
            previous_gaps=gstate.get("business_gaps"),
        )
        attempted = int(budget.get("attempted") or 0) + 1
        applied = int(budget.get("applied") or 0) + (1 if patch["applied"] else 0)
        budget.update(attempted=attempted, applied=applied)
        rejected_hashes = [str(item) for item in gstate.get("rejected_patch_hashes") or []]
        if not patch["applied"]:
            rejected_hashes = list(dict.fromkeys([*rejected_hashes, patch["fingerprint"]]))
        session.state.plan = patch["plan"]
        session.state.replan_count = applied
        from_plan_version = int(gstate.get("plan_version") or patch["plan"].plan_version)
        to_plan_version = patch["plan"].plan_version if patch["applied"] else from_plan_version
        event_type = "replan.applied" if patch["applied"] else "replan.rejected"
        common_attributes = {
            "patch_id": f"patch:{session.run_id}:{attempted}",
            "triggered_by": "progress_gap",
            "target_gap_ids": patch["target_gap_ids"],
            "from_plan_version": from_plan_version,
            "to_plan_version": to_plan_version,
            "superseded_task_ids": patch["superseded_task_ids"],
            "added_task_ids": patch["added_task_ids"],
            "recovery_generation": patch["recovery_generation"],
            "max_recovery_generation": int(recovery_limits_from_state(gstate)["max_recovery_generation"]),
            "attempted": attempted,
            "max_attempts": int(budget.get("max_attempts") or 0),
            "fingerprint": patch["fingerprint"],
        }
        _emit(
            session,
            event_type,
            phase=WorkflowPhase.REPLAN.value,
            status="ok" if patch["applied"] else "no_op",
            plan_version=to_plan_version,
            attributes=common_attributes,
        )
        return transition_update(
            gstate,
            WorkflowPhase.REPLAN,
            {
                "plan": patch["plan"].to_dict(),
                "plan_version": to_plan_version,
                "tasks": patch["tasks"],
                "business_gaps": patch["business_gaps"],
                "replan_budget": budget,
                "rejected_patch_hashes": rejected_hashes,
            },
        )

    def _synthesis_budget_attributes(self, session: RunSession, executor: Any) -> dict[str, Any]:
        manager = getattr(session, "budget_manager", None)
        snapshot = manager.snapshot() if manager is not None and callable(getattr(manager, "snapshot", None)) else None
        token_limit = int(getattr(snapshot, "token_limit", 0) or 0)
        used_tokens = int(getattr(snapshot, "used_tokens", 0) or 0)
        reserved_tokens = int(getattr(snapshot, "reserved_tokens", 0) or 0)
        remaining_run_sec = (
            float(manager.remaining_run_sec())
            if manager is not None and callable(getattr(manager, "remaining_run_sec", None))
            else 0.0
        )
        timeout_method = getattr(executor, "_timeout_sec", None)
        synthesis_timeout_sec = (
            float(timeout_method())
            if callable(timeout_method)
            else float(getattr(self.harness.harness_config, "synthesis_step_timeout_sec", 0) or 0)
        )
        return {
            "remaining_run_tokens": max(0, token_limit - used_tokens - reserved_tokens) if token_limit else 0,
            "remaining_run_sec": max(0.0, remaining_run_sec),
            "synthesis_reserve_tokens": int(getattr(snapshot, "synthesis_reserve_tokens", 0) or 0),
            "synthesis_timeout_sec": synthesis_timeout_sec,
        }

    def _start_synthesis_span(
        self,
        session: RunSession,
        *,
        attempt: int,
        mode: str,
        attributes: dict[str, Any],
    ) -> str:
        try:
            from app.observability import get_recorder

            recorder = get_recorder()
            if not recorder.is_active:
                return ""
            return recorder.start_span(
                "synthesis.execute",
                phase=WorkflowPhase.SYNTHESIS.value,
                attempt=attempt,
                attributes={**attributes, "mode": mode},
            )
        except Exception as exc:
            _emit(
                session,
                "observability.internal_error",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="warning",
                attributes={"operation": "synthesis_start_span", "error_type": type(exc).__name__},
            )
            return ""

    def _end_synthesis_span(
        self,
        session: RunSession,
        span_key: str,
        *,
        status: str,
        duration_ms: int,
    ) -> None:
        if not span_key:
            return
        try:
            from app.observability import get_recorder

            get_recorder().end_span(span_key, status=status, duration_ms=duration_ms)
        except Exception as exc:
            _emit(
                session,
                "observability.internal_error",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="warning",
                attributes={"operation": "synthesis_end_span", "error_type": type(exc).__name__},
            )

    async def _synthesize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        import app.research.execution.synthesis_executor as synthesis_executor_module
        from app.agent.harness.token_counter import estimate_tokens
        from app.research.delivery.partial_renderer import render_partial_delivery
        from app.research.delivery.synthesis_context import SynthesisContextBuilder
        from app.research.runtime.worker import ResearchContext

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        decision = dict(gstate.get("control_decision") or {})
        delivery = dict(gstate.get("delivery_readiness") or {})
        mode = str(decision.get("mode") or delivery.get("mode") or "degraded")
        if mode not in {"normal", "degraded"}:
            mode = "degraded"
        progress = dict(gstate.get("progress_assessment") or {})
        evidence = dict(gstate.get("evidence_assessment") or {})
        conflicts = [
            *[str(item) for item in progress.get("unresolved_conflicts") or []],
            *[str(item) for item in evidence.get("unresolved_conflicts") or []],
        ]
        builder = SynthesisContextBuilder(self.harness, session)
        context = builder.build(
            gstate,
            limitations=[str(item) for item in delivery.get("limitations") or []],
            unresolved_conflicts=conflicts,
        )
        executor = synthesis_executor_module.SynthesisExecutor(self.harness, session)
        research_context = ResearchContext(
            run_id=session.run_id,
            query=session.ctx.task_query,
            user_id=session.ctx.user_id,
            tenant_id=session.ctx.tenant_id,
            project_id=session.ctx.project_id,
            session_id=session.session_id,
        )
        evidence_refs = list(context.evidence_refs)
        input_refs = [{"type": "evidence", "id": item} for item in evidence_refs]
        answer_refs = [{"type": "answer", "id": "synthesis:latest"}]
        attempts = 0
        last_result = None
        fallback_action = ""

        while attempts < 2:
            attempts += 1
            request = synthesis_executor_module.SynthesisRequest(
                mode=mode,
                evidence_refs=evidence_refs,
                limitations=list(context.limitations),
                unresolved_conflicts=list(context.unresolved_conflicts),
                research_summary=context.research_summary(),
                evidence_digests=list(context.evidence_digests),
                findings=list(context.findings),
                worker_summaries=list(context.worker_summaries),
                token_budget=context.token_budget,
            )
            estimate_input_tokens = getattr(executor, "estimate_input_tokens", None)
            estimated_tokens = (
                int(estimate_input_tokens(request, research_context))
                if callable(estimate_input_tokens)
                else estimate_tokens(request.research_summary)
            )
            budget_attrs = self._synthesis_budget_attributes(session, executor)
            span_key = self._start_synthesis_span(
                session,
                attempt=attempts,
                mode=mode,
                attributes={"mode": mode, "attempt": attempts},
            )
            _emit(
                session,
                "synthesis.started",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="start",
                attempt=attempts,
                attributes={
                    "mode": mode,
                    "attempt": attempts,
                    "evidence_count": len(evidence_refs),
                    "evidence_ids": evidence_refs,
                    "input_tokens_estimated": estimated_tokens,
                    **budget_attrs,
                },
                input_refs=input_refs,
            )
            result = await executor.execute(request, research_context)
            last_result = result
            if result.ok:
                manager = session.ctx.citation_manager
                final_content = (
                    manager.build_cited_report(result.summary)
                    if manager is not None
                    else result.summary
                )
                session.state.final_content = final_content
                if isinstance(session.state.metadata, dict):
                    session.state.metadata.update(
                        {
                            "synthesis_attempted": True,
                            "synthesis_attempts": attempts,
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
                    attempt=attempts,
                    attributes={
                        "mode": mode,
                        "attempt": attempts,
                        "duration_ms": result.duration_ms,
                        "input_tokens_estimated": estimated_tokens,
                        "evidence_count": len(result.evidence_refs),
                        "evidence_ids": result.evidence_refs,
                        "content_chars": len(final_content),
                    },
                    input_refs=input_refs,
                    output_refs=answer_refs,
                )
                self._end_synthesis_span(session, span_key, status="ok", duration_ms=result.duration_ms)
                return transition_update(
                    gstate,
                    WorkflowPhase.SYNTHESIS,
                    {"final_content": final_content, "draft_ref": "synthesis:latest", "quality_assessment": {}},
                )

            retryable = result.fail_reason in synthesis_executor_module.RETRYABLE_SYNTHESIS_FAILURES
            can_retry = attempts < 2 and retryable
            if result.fail_reason == "context_length_exceeded" and attempts < 2 and not context.compacted:
                context = builder.compact(context)
                fallback_action = "compact_retry"
            elif can_retry:
                fallback_action = "retry"
            elif context.findings or context.evidence_digests:
                fallback_action = "deterministic_partial_renderer"
            else:
                fallback_action = "explicit_failed_no_evidence"
            _emit(
                session,
                "synthesis.failed",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="failed",
                duration_ms=result.duration_ms,
                attempt=attempts,
                attributes={
                    "mode": mode,
                    "attempt": attempts,
                    "fail_reason": result.fail_reason,
                    "duration_ms": result.duration_ms,
                    "input_tokens_estimated": estimated_tokens,
                    "evidence_count": len(result.evidence_refs),
                    "evidence_ids": result.evidence_refs,
                    "fallback_action": fallback_action,
                },
                input_refs=input_refs,
                output_refs=answer_refs if fallback_action != "explicit_failed_no_evidence" else None,
            )
            self._end_synthesis_span(session, span_key, status="failed", duration_ms=result.duration_ms)
            if fallback_action in {"retry", "compact_retry"}:
                continue
            break

        fail_reason = str(getattr(last_result, "fail_reason", "") or "synthesis_failed")
        if not (context.findings or context.evidence_digests):
            if isinstance(session.state.metadata, dict):
                session.state.metadata.update(
                    {
                        "synthesis_attempted": True,
                        "synthesis_attempts": attempts,
                        "synthesis_mode": mode,
                        "synthesis_status": "failed",
                        "synthesis_failed": True,
                        "synthesis_fail_reason": fail_reason,
                        "fallback_used": "",
                    }
                )
            return transition_update(gstate, WorkflowPhase.SYNTHESIS, {"quality_assessment": {}})

        worker_failures = [
            str(row.get("fail_reason") or row.get("status") or "")
            for row in gstate.get("worker_results") or []
            if isinstance(row, dict) and not bool(row.get("ok", True))
        ]
        fallback_content = render_partial_delivery(
            objective=session.ctx.task_query,
            findings=list(context.findings),
            evidence_digests=list(context.evidence_digests),
            worker_summaries=list(context.worker_summaries),
            business_gaps=list(context.business_gaps),
            limitations=list(context.limitations),
            unresolved_conflicts=list(context.unresolved_conflicts),
            worker_failure_reasons=worker_failures,
            synthesis_failure_reason=fail_reason,
        )
        manager = session.ctx.citation_manager
        final_content = (
            manager.build_cited_report(fallback_content)
            if manager is not None and fallback_content
            else fallback_content
        )
        session.state.final_content = final_content
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "synthesis_attempted": True,
                    "synthesis_attempts": attempts,
                    "synthesis_mode": mode,
                    "synthesis_status": "failed",
                    "synthesis_failed": True,
                    "synthesis_fail_reason": fail_reason,
                    "fallback_used": "deterministic_partial_renderer",
                }
            )
        return transition_update(
            gstate,
            WorkflowPhase.SYNTHESIS,
            {"final_content": final_content, "draft_ref": "synthesis:fallback", "quality_assessment": {}},
        )

    async def node_synthesize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        return await self._synthesize(gstate)

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
            attributes=quality_event_attributes(assessment),
        )
        _emit(
            session,
            "control.decided",
            phase=WorkflowPhase.QUALITY.value,
            status=str(decision["action"]),
            attributes=control_decision_event_attributes(decision),
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
        _emit(
            session,
            "run.terminated",
            phase=WorkflowPhase.FINALIZE.value,
            status=outcome,
            attributes={
                **termination_event_attributes(termination["termination"]),
                "final_content_chars": len(session.state.final_content),
            },
        )
        result = await self.harness._phase_finalize(
            session.state,
            session.ctx.session_dir,
            success=outcome == "success",
            started_at=session.ctx.run_started,
            deliverable_dir=session.ctx.deliverable_dir,
        )
        session.result = result
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
