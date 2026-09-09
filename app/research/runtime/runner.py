"""Dumb executor bridge between the canonical ResearchState graph and harness services."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.agent.harness.citations import SourceTier
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep
from app.research.assessment.delivery import assess_delivery
from app.research.assessment.evidence import assess_evidence
from app.research.assessment.execution_health import assess_execution_health
from app.research.assessment.progress import assess_progress
from app.research.control.policy import decide_control
from app.research.control.terminal_policy import terminal_update
from app.research.control.transitions import transition_update
from app.research.domain.contracts import (
    BudgetStatus,
    StopReason,
    WorkflowPhase,
)
from app.research.domain.failure import classify_failure
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    initialize_tasks,
    normalize_tasks,
    retry_task,
    transition_task,
    worker_result_lifecycle,
)
from app.research.runtime.project import sync_execution_projection
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


def _sync_assessments(session: RunSession, state: dict[str, Any]) -> dict[str, Any]:
    state["evidence_assessment"] = assess_evidence(state)
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

    RECURSION_LIMIT = 64

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
        from app.research.coverage.compiler import compile_coverage_contract
        from app.research.domain.task_state import TaskExecutionStatus, initialize_tasks, transition_task
        from app.research.runtime.semantic_ingest import ingest_semantics
        from app.research.runtime.simple_fact import render_simple_fact_answer
        from app.research.spec.compiler import compile_research_spec
        from app.research.runtime.worker import ResearchContext, ResearchTask, WorkerResult

        state = session.state
        spec = compile_research_spec(
            session.ctx.task_query,
            conversation_delta=str(getattr(session.ctx, "conversation_summary", "") or ""),
        )
        contract = compile_coverage_contract(spec)
        state.plan = ExecutionPlan(
            steps=[PlanStep(step_type="network_search", description="检索并确认单一事实", task_id="simple_fact:search", allowed_tools=["internet_search"], metadata={"simple_fact_fast_path": True, "task_kind": "lookup", "subject_id": spec.subjects[0].subject_id if spec.subjects else "general", "coverage_keys": [unit.dimension_id for unit in contract.units], "coverage_ids": [unit.coverage_id for unit in contract.units]})],
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
        semantic_state = empty_research_state(
            run_id=session.run_id,
            session_id=session.session_id,
            task_query=session.ctx.task_query,
            user_id=session.ctx.user_id,
            tenant_id=session.ctx.tenant_id,
            project_id=session.ctx.project_id,
        )
        semantic_state.update(
            {
                "research_spec": spec.to_dict(),
                "coverage_contract": contract.to_dict(),
                "plan": state.plan.to_dict(),
                "tasks": transition_task(
                    transition_task(initialize_tasks(state.plan), "simple_fact:search", execution_status=TaskExecutionStatus.RUNNING),
                    "simple_fact:search",
                    execution_status=TaskExecutionStatus.SUCCEEDED,
                    evidence_refs=worker_result.evidence_refs,
                ),
            }
        )
        semantic_state.update(
            ingest_semantics(
                {
                    **semantic_state,
                    "worker_results": [
                        {
                            "task_id": "simple_fact:search",
                            "task_metadata": dict(state.plan.steps[0].metadata or {}),
                            "ok": worker_result.ok,
                            "status": worker_result.status,
                            "summary": worker_result.summary,
                            "payload": {
                                "facts": worker_result.facts,
                                "sources": worker_result.sources,
                                "findings": worker_result.findings,
                                "evidence_ids": worker_result.evidence_refs,
                                "confidence": 0.9 if worker_result.ok else 0.0,
                            },
                        }
                    ],
                }
            )
        )
        manager = session.ctx.citation_manager
        answer = render_simple_fact_answer(query=session.ctx.task_query, worker_result=worker_result, citation_manager=manager) if manager is not None else None
        source_counts = manager.source_counts_by_tier() if manager is not None else {}
        progress_assessment = assess_progress(semantic_state)
        evidence_assessment = assess_evidence(semantic_state)
        passed = bool(worker_result.ok and answer is not None and answer.sufficient and progress_assessment["status"] == "sufficient")
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
                "research_spec": semantic_state["research_spec"],
                "coverage_contract": semantic_state["coverage_contract"],
                "coverage_state": semantic_state["coverage_state"],
                "progress_assessment": progress_assessment,
            }
        )
        if manager is not None:
            manager.save_evidence_json(session.ctx.run_dir, run_id=session.run_id)
        terminal = terminal_update(
            {
                "cancel_reason": "",
                "abort_reason": state.abort_reason,
                "quality_assessment": {"verdict": "pass" if passed else "fail"},
                "progress_assessment": progress_assessment,
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
                    "coverage_ratio": float(semantic_state["coverage_state"].get("coverage_ratio") or 0.0),
                    "progress_assessment": progress_assessment,
                    "evidence_assessment": evidence_assessment,
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
        row = worker_row("vanilla", step, result.ok, result.raw) if result.raw is not None else {"task_id": "vanilla", "ok": result.ok, "summary": result.summary, "step_type": "research", "payload": {}}
        return transition_update(
            gstate,
            WorkflowPhase.DIRECT,
            {"final_content": session.state.final_content, "worker_results": [row], "evidence_refs": result.evidence_refs, "quality_assessment": {"verdict": "pass" if result.ok else "fail", "grounding": False}},
        )

    async def node_compile_spec(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import compile_spec_node

        session = _require_session(gstate)
        update = compile_spec_node(gstate)
        spec = dict(update.get("research_spec") or {})
        _emit(
            session,
            "spec.compiled",
            phase=WorkflowPhase.COMPILE_SPEC.value,
            status="ok",
            attributes={
                "spec_id": spec.get("spec_id"),
                "task_shape": spec.get("task_shape"),
                "objective": spec.get("objective"),
                "entities": [
                    str(row.get("name") or row.get("subject_id") or "")
                    for row in spec.get("subjects") or []
                    if isinstance(row, dict)
                ],
                "dimensions": [
                    str(row.get("dimension_id") or "")
                    for row in spec.get("dimensions") or []
                    if isinstance(row, dict)
                ],
                "deliverable": (spec.get("delivery_requirements") or {}).get("format"),
                "prefer_primary": (spec.get("evidence_requirements") or {}).get("prefer_primary"),
                "coverage_unit_count": len((update.get("coverage_contract") or {}).get("units") or []),
            },
        )
        contract = dict(update.get("coverage_contract") or {})
        _emit(
            session,
            "coverage.compiled",
            phase=WorkflowPhase.COMPILE_SPEC.value,
            status="ok",
            attributes={
                "contract_id": contract.get("contract_id"),
                "spec_id": spec.get("spec_id"),
                "version": contract.get("version"),
                "unit_count": len(contract.get("units") or []),
                "unit_ids": [str(row.get("coverage_id")) for row in contract.get("units") or []],
            },
        )
        return update

    async def node_spec_gate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import spec_gate_node

        session = _require_session(gstate)
        update = spec_gate_node(gstate)
        spec = dict(gstate.get("research_spec") or {})
        _emit(
            session,
            "spec.validated",
            phase=WorkflowPhase.SPEC_GATE.value,
            status="needs_clarification" if update.get("needs_clarification") else "ok",
            attributes={"spec_id": spec.get("spec_id"), "needs_clarification": update.get("needs_clarification")},
        )
        return update

    async def node_clarify(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import clarify_node

        return clarify_node(gstate)

    async def node_plan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.planning.planner import plan_for_spec
        from app.research.runtime.graph import plan_node

        session = _require_session(gstate)
        update = plan_node(gstate)
        plan = ExecutionPlan.from_dict(update["plan"])
        sync_execution_projection(session.state, update)
        if isinstance(session.state.metadata, dict):
            session.state.metadata["planner_source"] = "spec_driven"
        _emit(
            session,
            "plan.created",
            phase=WorkflowPhase.PLAN.value,
            status="ok",
            plan_version=plan.plan_version,
            attributes=plan_event_attributes(
                plan,
                {"objective": (gstate.get("research_spec") or {}).get("objective") or gstate["task_query"]},
                run_id=session.run_id,
                planner_source="spec_driven",
            ),
        )
        return update

    async def node_plan_validate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import plan_validate_node

        return plan_validate_node(gstate)

    async def node_dispatch(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import dispatch_node

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        update = dispatch_node(state)
        decision = dict(update.get("control_decision") or {})
        _emit(
            session,
            "control.decided",
            phase=WorkflowPhase.DISPATCH.value,
            status=str(decision.get("action") or "unknown"),
            plan_version=int(state.get("plan_version") or 1),
            attributes=control_decision_event_attributes(decision),
        )
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update({"control_decision": decision})
        return {**update, "budget": state["budget"], "budget_status": state["budget_status"]}

    async def node_retry(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import retry_node

        return retry_node(gstate)

    async def node_research_worker(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from langgraph.types import interrupt

        from app.research.execution.worker_executor import WorkerExecutorV2
        from app.research.runtime.findings import normalize_findings
        from app.research.runtime.isolation import worker_row
        from app.research.runtime.worker import ResearchContext, ResearchTask, WorkerResult

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        step_index = int(gstate.get("step_index") or 0)
        task_id = str(gstate.get("task_id") or f"s{step_index}")
        plan = session.state.plan
        if plan is None or step_index >= len(plan.steps):
            failure = classify_failure("missing_step")
            return {
                "tasks": transition_task(gstate.get("tasks"), task_id, execution_status=TaskExecutionStatus.FAILED, result_status=ResultStatus.NONE, failure=failure, timestamp=_now()),
                "worker_results": [{"task_id": task_id, "ok": False, "status": "failed", "summary": "missing_step"}],
            }
        step = plan.steps[step_index]
        if self.harness.harness_config.hitl_enabled and step.step_type in set(self.harness.harness_config.hitl_step_gate_types):
            resume = interrupt({"kind": "step_gate", "step_index": step_index, "description": step.description})
            if isinstance(resume, dict) and resume.get("_timeout"):
                return transition_update(gstate, WorkflowPhase.EXECUTE, {"cancel_reason": "user_cancelled"})
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
            return {
                "worker_results": [
                    {
                        "task_id": task_id,
                        "task_metadata": dict(step.metadata or {}),
                        "ok": False,
                        "status": "duplicate_skipped",
                        "summary": "duplicate task attempt suppressed",
                        "payload": {"duplicate": True},
                    }
                ]
            }
        dispatch_wave_id = int(gstate.get("dispatch_wave_id") or 0)
        task = ResearchTask(task_id=task_id, objective=str(step.objective or step.description), step_type=step.step_type, step_index=step_index, description=step.description, subagent=step.subagent or "", allowed_tools=list(step.allowed_tools or []), plan_version=int(gstate.get("plan_version") or 1), attempt=attempt, dispatch_wave_id=dispatch_wave_id)
        context = ResearchContext(run_id=session.run_id, query=session.ctx.task_query, user_id=session.ctx.user_id, tenant_id=session.ctx.tenant_id, project_id=session.ctx.project_id, session_id=session.session_id)
        running = transition_task(gstate.get("tasks"), task_id, execution_status=TaskExecutionStatus.RUNNING, attempt=attempt, timestamp=_now())
        result = await WorkerExecutorV2(self.harness, session).execute(task, context)
        if not isinstance(result, WorkerResult):
            raise TypeError("WorkerRuntime.execute must return WorkerResult")
        if result.raw is not None:
            session.state.step_results.append(result.raw)
        row = worker_row(task_id, step, result.ok, result.raw) if result.raw is not None else {"task_id": task_id, "ok": result.ok, "summary": result.summary, "step_type": step.step_type, "payload": {"facts": result.facts, "sources": result.sources, "findings": result.findings, "evidence_ids": result.evidence_refs, "candidates": result.candidates}}
        row.update(status=result.status, fail_reason=result.fail_reason, queue_ms=result.queue_ms, execution_ms=result.execution_ms)
        normalized_findings, _ = normalize_findings(result.findings, task_id=task_id, subject_id=str(step.metadata.get("subject_id") or "general"), dimension=str((step.metadata.get("coverage_keys") or ["general"])[0]))
        row["payload"] = {**(row.get("payload") or {}), "findings": normalized_findings, "evidence_ids": result.evidence_refs}
        execution_status, result_status, stop_reason, failure = worker_result_lifecycle(result)
        tasks = transition_task(
            running,
            task_id,
            execution_status=execution_status,
            result_status=result_status,
            attempt=attempt,
            failure=failure,
            stop_reason=stop_reason.value,
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
        row["task_metadata"] = dict(step.metadata or {})
        return {
            "tasks": {task_id: tasks[task_id]},
            "worker_results": [row],
            "evidence_refs": result.evidence_refs,
            "findings": normalized_findings,
        }

    async def node_ingest_semantics(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.semantic_ingest import ingest_semantics

        session = _require_session(gstate)
        update = ingest_semantics(gstate)
        sync_execution_projection(session.state, {**gstate, **update})
        coverage = dict(update.get("coverage_state") or {})
        contract = dict(update.get("coverage_contract") or {})
        _emit(
            session,
            "coverage.assessed",
            phase=WorkflowPhase.INGEST_SEMANTICS.value,
            status=str(coverage.get("status") or "unknown"),
            plan_version=int(gstate.get("plan_version") or 1),
            attributes={
                "contract_id": coverage.get("contract_id") or contract.get("contract_id"),
                "coverage_ratio": coverage.get("coverage_ratio"),
                "covered_count": len(coverage.get("covered_ids") or []),
                "covered_ids": coverage.get("covered_ids") or [],
                "partial_ids": coverage.get("partial_ids") or [],
                "missing_ids": coverage.get("missing_ids") or [],
                "conflicted_ids": coverage.get("conflicted_ids") or [],
                "stale_ids": coverage.get("stale_ids") or [],
            },
        )
        for claim in update.get("claims") or []:
            _emit(
                session,
                "claim.extracted",
                phase=WorkflowPhase.INGEST_SEMANTICS.value,
                status="ok",
                attributes={"claim_id": claim.get("claim_id"), "normalized_key": claim.get("normalized_key")},
            )
        for edge in update.get("claim_conflicts") or []:
            _emit(
                session,
                "claim.conflict_detected",
                phase=WorkflowPhase.INGEST_SEMANTICS.value,
                status="ok",
                attributes={
                    "edge_id": edge.get("edge_id"),
                    "left_claim_id": edge.get("left_id"),
                    "right_claim_id": edge.get("right_id"),
                    "kind": edge.get("kind"),
                },
            )
        for resolution in update.get("claim_resolutions") or []:
            _emit(
                session,
                "claim.conflict_resolved",
                phase=WorkflowPhase.INGEST_SEMANTICS.value,
                status=str(resolution.get("status") or "ok"),
                attributes={
                    "edge_id": resolution.get("edge_id"),
                    "status": resolution.get("status"),
                    "winner_claim_id": resolution.get("winner_id"),
                    "evidence_ids": resolution.get("evidence_ids") or [],
                },
            )
        candidate_set = update.get("candidate_set")
        if isinstance(candidate_set, dict):
            _emit(
                session,
                "candidate_set.materialized",
                phase=WorkflowPhase.INGEST_SEMANTICS.value,
                status=str(candidate_set.get("status") or "unknown"),
                plan_version=int(gstate.get("plan_version") or 1),
                attributes={
                    "candidate_set_id": candidate_set.get("candidate_set_id"),
                    "available": candidate_set.get("available"),
                    "item_count": len(candidate_set.get("items") or []),
                    "items": candidate_set.get("items") or [],
                },
            )
        for gap_id in update.get("semantic_gaps") or {}:
            _emit(
                session,
                "semantic_gap.opened",
                phase=WorkflowPhase.INGEST_SEMANTICS.value,
                status="ok",
                attributes={"gap_id": gap_id},
            )
        for gap_id in set(gstate.get("semantic_gaps") or {}) - set(update.get("semantic_gaps") or {}):
            _emit(
                session,
                "semantic_gap.closed",
                phase=WorkflowPhase.INGEST_SEMANTICS.value,
                status="ok",
                attributes={"gap_id": gap_id},
            )
        _emit(
            session,
            "semantic_gain.assessed",
            phase=WorkflowPhase.INGEST_SEMANTICS.value,
            status="ok",
            attributes=update.get("marginal_gain") or {},
        )
        return transition_update(gstate, WorkflowPhase.INGEST_SEMANTICS, update)

    async def node_assess(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import assess_node

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        update = assess_node(state)
        decision = dict(update.get("control_decision") or {})
        _emit_assessments(session, {**state, **update}, decision, WorkflowPhase.ASSESS.value)
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "progress_assessment": update.get("progress_assessment") or {},
                    "evidence_assessment": update.get("evidence_assessment") or {},
                    "execution_health": update.get("execution_health") or {},
                    "delivery_readiness": update.get("delivery_readiness") or {},
                    "control_decision": decision,
                }
            )
        return {**update, "budget": state["budget"], "budget_status": state["budget_status"]}

    async def node_gap_fill(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import gap_fill_node

        session = _require_session(gstate)
        update = gap_fill_node(gstate)
        _emit(
            session,
            "plan.gap_fill_applied" if update.get("plan") else "plan.gap_fill_rejected",
            phase=WorkflowPhase.GAP_FILL.value,
            status="ok" if update.get("plan") else "no_op",
            plan_version=int(update.get("plan_version") or gstate.get("plan_version") or 1),
            attributes={"reason": update.get("abort_reason", "")},
        )
        return update

    async def node_expand_plan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import expand_plan_node

        session = _require_session(gstate)
        update = expand_plan_node(gstate)
        _emit(
            session,
            "plan.expanded" if update.get("plan") else "plan.expansion_rejected",
            phase=WorkflowPhase.EXPAND_PLAN.value,
            status="ok" if update.get("plan") else "no_op",
            plan_version=int(update.get("plan_version") or gstate.get("plan_version") or 1),
            attributes={"reason": update.get("abort_reason", "")},
        )
        return update

    async def node_replan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import replan_node

        session = _require_session(gstate)
        update = replan_node(gstate)
        _emit(
            session,
            "replan.applied" if update.get("plan") else "replan.rejected",
            phase=WorkflowPhase.REPLAN.value,
            status="ok" if update.get("plan") else "no_op",
            plan_version=int(update.get("plan_version") or gstate.get("plan_version") or 1),
            attributes={"reason": update.get("abort_reason", "")},
        )
        return update

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

    def _semantic_synthesis_digest(self, gstate: dict[str, Any], *, compact: bool) -> str:
        spec = dict(gstate.get("research_spec") or {})
        claims = [dict(row) for row in gstate.get("claims") or [] if isinstance(row, dict)]
        coverage = dict(gstate.get("coverage_state") or {})
        lines = [f"# {spec.get('objective') or gstate.get('task_query')}", ""]
        for claim in claims[:12 if compact else 40]:
            evidence_ids = ", ".join(str(item) for item in claim.get("evidence_ids") or [])
            text = str(claim.get("text") or claim.get("claim") or "").strip()
            lines.append(f"- {text} [{evidence_ids}]")
        missing = [str(item) for item in coverage.get("missing_ids") or []]
        if missing:
            lines.extend(["", "## Known limitations"])
            lines.extend(f"- Uncovered coverage unit: {item}" for item in missing[:8 if compact else 20])
        conflicts = [str(item) for item in coverage.get("conflicted_ids") or []]
        if conflicts:
            lines.extend(["", "## Conflict disclosures"])
            lines.extend(f"- Unresolved coverage unit: {item}" for item in conflicts[:8 if compact else 20])
        return "\n".join(lines)

    async def node_synthesize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        import app.research.execution.synthesis_executor as synthesis_executor_module
        from dataclasses import replace
        from app.research.delivery.synthesis_context import EvidenceDigest
        from app.research.runtime.worker import ResearchContext

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        attempts_before = int(gstate.get("synthesis_attempts") or 0)
        compact = attempts_before >= 1
        decision = dict(gstate.get("control_decision") or {})
        mode = "degraded" if compact or decision.get("action") == "deliver_partial" else "normal"
        evidence_records = [dict(row) for row in gstate.get("evidence_records") or [] if isinstance(row, dict)]
        claims = [dict(row) for row in gstate.get("claims") or [] if isinstance(row, dict)]
        evidence_refs = [str(row.get("evidence_id")) for row in evidence_records]
        digests = [
            EvidenceDigest(
                evidence_id=str(row.get("evidence_id") or ""),
                title=str(row.get("source_id") or row.get("locator") or row.get("evidence_id") or ""),
                locator=str(row.get("locator") or ""),
                excerpt=str(row.get("excerpt_ref") or ""),
                supported_claims=tuple(
                    str(claim.get("text") or claim.get("claim") or "")
                    for claim in claims
                    if str(row.get("evidence_id") or "") in [str(item) for item in claim.get("evidence_ids") or []]
                ),
            )
            for row in evidence_records[:20 if compact else 40]
        ]
        coverage = dict(gstate.get("coverage_state") or {})
        request = synthesis_executor_module.SynthesisRequest(
            mode=mode,
            evidence_refs=evidence_refs,
            limitations=[f"Uncovered coverage unit: {item}" for item in coverage.get("missing_ids") or []],
            unresolved_conflicts=[f"Unresolved coverage unit: {item}" for item in coverage.get("conflicted_ids") or []],
            research_summary=self._semantic_synthesis_digest(gstate, compact=compact),
            evidence_digests=digests,
            findings=claims[:12 if compact else 40],
            worker_summaries=[],
            token_budget=20_000 if compact else 40_000,
        )
        executor = synthesis_executor_module.SynthesisExecutor(self.harness, session)
        context = ResearchContext(
            run_id=session.run_id,
            query=str((gstate.get("research_spec") or {}).get("objective") or gstate["task_query"]),
            user_id=session.ctx.user_id,
            tenant_id=session.ctx.tenant_id,
            project_id=session.ctx.project_id,
            session_id=session.session_id,
        )
        result = await executor.execute(request, context)
        retried = False
        if not result.ok and (
            result.fail_reason in synthesis_executor_module.RETRYABLE_SYNTHESIS_FAILURES
            or result.fail_reason == "context_length_exceeded"
        ):
            retried = True
            _emit(
                session,
                "synthesis.failed",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="failed",
                attempt=attempts_before + 1,
                duration_ms=result.duration_ms,
                attributes={
                    "mode": mode,
                    "compact": False,
                    "evidence_count": len(evidence_refs),
                    "claim_count": len(claims),
                    "fail_reason": result.fail_reason,
                    "fallback_action": "compact_retry",
                },
            )
            mode = "degraded"
            request = replace(
                request,
                mode=mode,
                research_summary=self._semantic_synthesis_digest(gstate, compact=True),
                evidence_digests=digests[:20],
                findings=claims[:12],
                token_budget=20_000,
            )
            result = await executor.execute(request, context)
        fallback = not result.ok or not str(result.summary or "").strip()
        if fallback:
            from app.research.runtime.graph import synthesize_node

            fallback_state = {
                **gstate,
                "control_decision": {
                    **decision,
                    "action": "deliver_partial",
                    "reason_codes": [*decision.get("reason_codes", []), "synthesis_fallback"],
                },
            }
            content = str(synthesize_node(fallback_state).get("final_content") or "")
        else:
            content = result.summary
        if not fallback:
            missing_evidence_ids = [
                evidence_id
                for evidence_id in evidence_refs
                if evidence_id and evidence_id not in content
            ]
            if missing_evidence_ids:
                content = (
                    content.rstrip()
                    + "\n\n## Evidence references\n"
                    + "\n".join(f"- [{evidence_id}]" for evidence_id in missing_evidence_ids)
                )
        manager = session.ctx.citation_manager
        if manager is not None and content:
            content = manager.build_cited_report(content)
        session.state.final_content = content
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "synthesis_attempted": True,
                    "synthesis_attempts": attempts_before + (2 if retried else 1),
                    "synthesis_mode": mode,
                    "synthesis_status": result.status,
                    "synthesis_failed": fallback,
                    "synthesis_fail_reason": result.fail_reason,
                    "fallback_used": "semantic_digest" if fallback else "",
                }
            )
        _emit(
            session,
            "synthesis.completed" if not fallback else "synthesis.failed",
            phase=WorkflowPhase.SYNTHESIS.value,
            status="ok" if not fallback else "failed",
            attempt=attempts_before + (2 if retried else 1),
            duration_ms=result.duration_ms,
            attributes={
                "mode": mode,
                "compact": compact or retried,
                "evidence_ids": list(evidence_refs),
                "evidence_count": len(evidence_refs),
                "claim_count": len(claims),
                "fail_reason": result.fail_reason,
                "fallback_action": "semantic_digest" if fallback else "",
            },
        )
        return transition_update(
            gstate,
            WorkflowPhase.SYNTHESIS,
            {
                "final_content": content,
                "synthesis_attempts": attempts_before + (2 if retried else 1),
                "synthesis_failed": fallback,
                "quality_assessment": {},
            },
        )

    async def node_quality_gate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import quality_gate_node

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        update = quality_gate_node(gstate)
        assessment = dict(update.get("quality_assessment") or {})
        decision = dict(update.get("control_decision") or {})
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "quality": assessment,
                    "quality_attempted": True,
                    "answer_grounded": bool(assessment.get("grounding")),
                    "control_decision": decision,
                }
            )
        _emit(
            session,
            "quality.assessed",
            phase=WorkflowPhase.QUALITY.value,
            status=str(assessment.get("verdict") or "unknown"),
            attributes=quality_event_attributes(assessment),
        )
        _emit(
            session,
            "control.decided",
            phase=WorkflowPhase.QUALITY.value,
            status=str(decision.get("action") or "unknown"),
            attributes=control_decision_event_attributes(decision),
        )
        return update

    async def node_repair_synthesis(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import repair_synthesis_node

        return repair_synthesis_node(gstate)

    async def node_finalize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import finalize_node

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        session.state.final_content = str(gstate.get("final_content") or "")
        termination = finalize_node(gstate)
        termination_payload = dict(termination.get("termination") or {})
        outcome = str(termination["termination"]["outcome"])
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "termination": termination_payload,
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
                **termination_event_attributes(termination_payload),
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
        return termination
