"""Dumb executor bridge between the canonical ResearchState graph and harness services."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, cast

from app.agent.harness.citations import SourceTier
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep
from app.research.assessment.delivery import assess_delivery
from app.research.assessment.evidence import assess_evidence
from app.research.assessment.execution_health import assess_execution_health
from app.research.assessment.progress import assess_progress
from app.research.control.runtime_policy import decide_control
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
from app.research.runtime.state import ResearchState, empty_research_state
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
        self.active_wave_size = 1

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
    """Compile and execute the graph; semantic routing stays in Supervisor."""

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
        brief = await self._compile_initial_brief(session)
        from app.research.brief.models import FastPathEligibility

        fast_path = FastPathEligibility.from_brief(brief).eligible
        if fast_path:
            budget_cfg = {**budget_cfg, "max_tool_calls": 3, "max_replan_count": 0, "parallel": False}
            if hasattr(session.budget_manager, "cap_tool_calls"):
                session.budget_manager.cap_tool_calls(3)
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
        payload["brief"] = brief.to_dict()
        payload["fast_path"] = fast_path
        payload["budget"]["max_parallel_workers"] = session._resolve_max_workers()
        from app.agent.harness.usage_tracker import reset_current_budget_manager, set_current_budget_manager

        token = set_current_budget_manager(session.budget_manager)
        config = {"configurable": {"thread_id": session.run_id}, "recursion_limit": self.RECURSION_LIMIT}
        active_checkpointer = checkpointer
        owns_checkpointer = False
        try:
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

    async def _compile_initial_brief(self, session: RunSession) -> Any:
        from app.research.brief.compiler import compile_structured_brief_with_llm

        return await compile_structured_brief_with_llm(
            session.ctx.task_query,
            agent=getattr(self.harness, "agent", None),
            budget_manager=session.budget_manager,
            conversation_delta=str(getattr(session.ctx, "conversation_summary", "") or ""),
            session_id=session.session_id,
        )

    async def node_brief(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import FastPathEligibility, StructuredResearchBrief
        from app.research.brief.validator import validate_structured_brief
        from app.research.runtime.graph import brief_node

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        state = dict(gstate)
        if not isinstance(state.get("brief"), dict) or not state.get("brief"):
            brief = await self._compile_initial_brief(session)
            state["brief"] = brief.to_dict()
        update = brief_node(cast(ResearchState, state))
        brief = StructuredResearchBrief.from_dict(update.get("brief"))
        eligibility = FastPathEligibility.from_brief(brief)
        issues = validate_structured_brief(brief)
        if eligibility.eligible:
            session.active_wave_size = 1
        sync_execution_projection(session.state, {**gstate, **update})
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "brief": brief.to_dict(),
                    "brief_id": brief.brief_id,
                    "brief_version": brief.version,
                    "user_intent": brief.user_intent,
                    "task_shape": brief.user_intent,
                    "execution_path": "fast_path" if eligibility.eligible else "harness",
                    "route_signals": update.get("route_signals") or [],
                }
            )
        _emit(
            session,
            "brief.compiled",
            phase=WorkflowPhase.BRIEF.value,
            status="warning" if issues else "ok",
            attributes={
                **brief_event_attributes(brief.to_dict()),
                "entities": list(brief.explicit_subjects),
                "dimensions": list(brief.key_questions),
                "validation_issues": issues,
                "compiler_source": brief.compiler_source,
            },
            input_refs=[{"type": "query", "id": session.run_id}],
            output_refs=[{"type": "brief", "id": brief.brief_id}],
        )
        _emit(
            session,
            "topology.decided",
            phase=WorkflowPhase.BRIEF.value,
            status="ok",
            attributes={
                "topology": "fast_path" if eligibility.eligible else "supervisor_loop",
                "eligible": eligibility.eligible,
                "reasons": list(eligibility.reasons),
            },
            input_refs=[{"type": "brief", "id": brief.brief_id}],
        )
        return update

    async def node_supervisor(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import StructuredResearchBrief
        from app.research.coverage.judge import CoverageJudgement
        from app.research.supervisor.agent import SupervisorAgent
        from app.research.supervisor.models import ResearchTaskRequest

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        brief = StructuredResearchBrief.from_dict(state.get("brief"))
        findings = [row for row in state.get("findings") or [] if isinstance(row, dict)]
        judgement_raw = state.get("coverage_judgement")
        judgement = CoverageJudgement.from_dict(judgement_raw) if isinstance(judgement_raw, dict) and judgement_raw else None
        budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
        _emit(
            session,
            "supervisor.started",
            phase=WorkflowPhase.SUPERVISOR.value,
            status="start",
            attributes={
                "iteration": int((state.get("supervisor") or {}).get("iteration") or 0) + 1,
                "finding_count": len(findings),
                "coverage_status": judgement.status if judgement else "unknown",
            },
        )
        supervisor = SupervisorAgent(getattr(self.harness, "agent", None), session.budget_manager)
        action = await supervisor.decide(brief, findings, judgement, budget)
        action = supervisor.resolve_action(action, judgement, brief)
        payload: dict[str, Any] = {
            "supervisor_action": action.to_dict(),
            "supervisor": {
                "iteration": int((state.get("supervisor") or {}).get("iteration") or 0) + 1,
                "last_action": action.action,
                "reasoning_summary": action.reason,
            },
        }
        if action.action == "CONDUCT_RESEARCH" and action.research_tasks:
            task_requests = [ResearchTaskRequest.from_dict(item.to_dict()) for item in action.research_tasks]
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
                        "expected_evidence": item.expected_evidence,
                        "source_hints": list(item.source_hints),
                        "required": True,
                        "optional": False,
                    },
                )
                for item in task_requests
            ]
            plan = ExecutionPlan(
                steps=steps,
                summary="Supervisor research action",
                plan_version=int(state.get("plan_version") or 1) + (1 if state.get("plan") else 0),
                planning_mode="supervisor_action",
            )
            session.state.plan = plan
            session.active_wave_size = max(1, len(plan.steps))
            payload.update(
                {
                    "plan": plan.to_dict(),
                    "plan_version": plan.plan_version,
                    "tasks": initialize_tasks(plan),
                    "dispatch_wave_id": int(state.get("dispatch_wave_id") or 0) + 1,
                }
            )
            _emit(
                session,
                "plan.created",
                phase=WorkflowPhase.SUPERVISOR.value,
                status="ok",
                plan_version=plan.plan_version,
                attributes=plan_event_attributes(
                    plan,
                    brief.to_dict(),
                    run_id=session.run_id,
                    planner_source="supervisor_action",
                ),
            )
        decision = decide_control({**state, **payload})
        control_decision = {
            "action": decision.action,
            "reason_codes": list(decision.reason_codes),
            "task_ids": list(decision.task_ids),
            "policy_version": "runtime-policy.v1",
        }
        if decision.action == "retry":
            tasks = state.get("tasks")
            for task_id in decision.task_ids:
                tasks = retry_task(tasks, task_id)
            payload["tasks"] = tasks
            session.active_wave_size = max(1, len(decision.task_ids))
        payload["control_decision"] = control_decision
        _emit(
            session,
            "control.decided",
            phase=WorkflowPhase.SUPERVISOR.value,
            status=decision.action,
            attributes=control_decision_event_attributes(control_decision),
        )
        sync_execution_projection(session.state, {**gstate, **payload})
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "supervisor": payload["supervisor"],
                    "supervisor_action": action.to_dict(),
                    "control_decision": control_decision,
                    "supervisor_iterations": payload["supervisor"]["iteration"],
                }
            )
        _emit(
            session,
            "supervisor.decided",
            phase=WorkflowPhase.SUPERVISOR.value,
            status=action.action,
            attributes={
                "action": action.action,
                "reason": action.reason,
                "task_count": len(action.research_tasks),
                "source": action.source,
                "runtime_action": decision.action,
                "runtime_reasons": list(decision.reason_codes),
            },
            output_refs=[{"type": "supervisor_action", "id": payload["supervisor"]["last_action"]}],
        )
        return transition_update(gstate, WorkflowPhase.SUPERVISOR, payload)

    async def node_researcher(self, gstate: dict[str, Any]) -> dict[str, Any]:
        return await self.node_research_worker(gstate)

    async def node_ingest_findings(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import StructuredResearchBrief
        from app.research.findings.compress import compress_worker_results
        from app.research.runtime.legacy import project_research_spec
        from app.research.runtime.semantic_ingest import ingest_semantics

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        brief = StructuredResearchBrief.from_dict(gstate.get("brief"))
        projected_state = {**gstate, "research_spec": project_research_spec(brief).to_dict()}
        update = ingest_semantics(projected_state)
        update.pop("findings", None)
        findings = [item.to_dict() for item in compress_worker_results(gstate.get("worker_results") or [])]
        update["findings"] = findings
        sync_execution_projection(session.state, {**gstate, **update})
        for finding in findings:
            _emit(
                session,
                "finding.compressed",
                phase=WorkflowPhase.INGEST_FINDINGS.value,
                status="ok",
                task_id=str(finding.get("task_id") or ""),
                attributes={
                    "finding_id": finding.get("finding_id"),
                    "evidence_ids": finding.get("evidence_ids") or [],
                    "claim_count": len(finding.get("claims") or []),
                    "confidence": finding.get("confidence"),
                    "limitations": finding.get("limitations") or [],
                },
                output_refs=[{"type": "finding", "id": str(finding.get("finding_id") or "")}],
            )
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "findings": findings,
                    "research_spec": update.get("research_spec") or {},
                    "coverage_contract": update.get("coverage_contract") or {},
                    "coverage_state": update.get("coverage_state") or {},
                }
            )
        return transition_update(gstate, WorkflowPhase.INGEST_FINDINGS, update)

    async def node_coverage_judge(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import StructuredResearchBrief
        from app.research.coverage.judge import CoverageJudge

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        brief = StructuredResearchBrief.from_dict(state.get("brief"))
        findings = [row for row in state.get("findings") or [] if isinstance(row, dict)]
        conflicts = [row for row in state.get("claim_conflicts") or [] if isinstance(row, dict)]
        judgement = await CoverageJudge(getattr(self.harness, "agent", None), session.budget_manager).evaluate(
            brief,
            findings,
            claim_conflicts=conflicts,
        )
        progress_projection = {
            "status": judgement.status,
            "coverage_ratio": 1.0 if judgement.sufficient else 0.0,
            "unresolved_conflicts": list(judgement.conflicts),
            "missing": list(judgement.missing),
            "missing_ids": list(judgement.missing),
            "semantic_gap_ids": list(judgement.missing),
            "reason_codes": [] if judgement.sufficient else ["coverage_gap"],
        }
        update = {
            "coverage_judgement": judgement.to_dict(),
            "progress_assessment": progress_projection,
            "control_decision": {},
        }
        sync_execution_projection(session.state, {**gstate, **update})
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "coverage_judgement": judgement.to_dict(),
                    "progress_assessment": progress_projection,
                }
            )
        _emit(
            session,
            "coverage.assessed",
            phase=WorkflowPhase.COVERAGE_JUDGE.value,
            status=judgement.status,
            attributes={
                "sufficient": judgement.sufficient,
                "missing": list(judgement.missing),
                "conflicts": list(judgement.conflicts),
                "weak_claims": list(judgement.weak_claims),
                "recommended_next_questions": list(judgement.recommended_next_questions),
                "source": judgement.source,
                "reason": judgement.reason,
            },
        )
        _emit(
            session,
            "progress.assessed",
            phase=WorkflowPhase.COVERAGE_JUDGE.value,
            status=judgement.status,
            plan_version=int(gstate.get("plan_version") or 1),
            attributes=progress_event_attributes(
                progress_projection,
                dispatch_wave_id=int(gstate.get("dispatch_wave_id") or 0),
            ),
        )
        return transition_update(gstate, WorkflowPhase.COVERAGE_JUDGE, update)

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
                "phase": WorkflowPhase.EXECUTE.value,
                "tasks": transition_task(gstate.get("tasks"), task_id, execution_status=TaskExecutionStatus.FAILED, result_status=ResultStatus.NONE, failure=failure, timestamp=_now()),
                "worker_results": [{"task_id": task_id, "ok": False, "status": "failed", "summary": "missing_step"}],
            }
        step = plan.steps[step_index]
        if self.harness.harness_config.hitl_enabled and step.step_type in set(self.harness.harness_config.hitl_step_gate_types):
            resume = interrupt({"kind": "step_gate", "step_index": step_index, "description": step.description})
            if isinstance(resume, dict) and resume.get("_timeout"):
                return {"phase": WorkflowPhase.EXECUTE.value, "cancel_reason": "user_cancelled"}
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
                "phase": WorkflowPhase.EXECUTE.value,
                "worker_results": [
                    {
                        "task_id": task_id,
                        "task_metadata": dict(step.metadata or {}),
                        "ok": False,
                        "status": "duplicate_skipped",
                        "summary": "duplicate task attempt suppressed",
                        "payload": {"duplicate": True},
                    }
                ],
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
            "phase": WorkflowPhase.EXECUTE.value,
            "tasks": {task_id: tasks[task_id]},
            "worker_results": [row],
            "evidence_refs": result.evidence_refs,
            "findings": normalized_findings,
        }

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
        from types import SimpleNamespace
        from app.research.delivery.synthesis_context import EvidenceDigest
        from app.research.runtime.simple_fact import render_simple_fact_answer
        from app.research.runtime.worker import ResearchContext

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        if bool(gstate.get("fast_path")):
            manager = getattr(session.ctx, "citation_manager", None)
            worker_row = next(
                (
                    row
                    for row in gstate.get("worker_results") or []
                    if isinstance(row, dict) and str(row.get("task_id") or "") == "fast_path:search"
                ),
                {},
            )
            raw_payload = worker_row.get("payload")
            payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
            worker_result = SimpleNamespace(
                facts=list(payload.get("facts") or []),
                summary=str(worker_row.get("summary") or ""),
            )
            if manager is None:
                answer: Any = SimpleNamespace(
                    content=str(worker_result.summary or "未能从可信来源确认该事实。"),
                    source_id="",
                    source_tier="UNKNOWN",
                    sufficient=bool(payload.get("evidence_ids")),
                )
                content = str(answer.content)
            else:
                answer = render_simple_fact_answer(
                    query=str(gstate.get("task_query") or ""),
                    worker_result=worker_result,
                    citation_manager=manager,
                )
                content = manager.build_cited_report(answer.content)
            session.state.final_content = content
            if isinstance(session.state.metadata, dict):
                session.state.metadata.update(
                    {
                        "synthesis_attempted": True,
                        "synthesis_attempts": 1,
                        "synthesis_mode": "brief_fast_path_deterministic",
                        "synthesis_status": "ok" if answer.sufficient else "failed",
                        "synthesis_failed": not answer.sufficient,
                        "synthesis_fail_reason": "" if answer.sufficient else "insufficient_trusted_evidence",
                        "fallback_used": "",
                    }
                )
            _emit(
                session,
                "synthesis.completed" if answer.sufficient else "synthesis.failed",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="ok" if answer.sufficient else "failed",
                attempt=1,
                attributes={
                    "mode": "brief_fast_path_deterministic",
                    "compact": False,
                    "evidence_count": len(payload.get("evidence_ids") or []),
                    "claim_count": len(payload.get("facts") or []),
                    "fail_reason": "" if answer.sufficient else "insufficient_trusted_evidence",
                    "fallback_action": "",
                },
            )
            return transition_update(
                gstate,
                WorkflowPhase.SYNTHESIS,
                {
                    "final_content": content,
                    "synthesis_attempts": 1,
                    "synthesis_failed": not answer.sufficient,
                    "quality_assessment": {},
                },
            )
        attempts_before = int(gstate.get("synthesis_attempts") or 0)
        compact = attempts_before >= 1
        decision = dict(gstate.get("control_decision") or {})
        _emit(
            session,
            "control.decided",
            phase=WorkflowPhase.SYNTHESIS.value,
            status=str(decision.get("action") or "synthesize"),
            attributes=control_decision_event_attributes(decision or {"action": "synthesize"}),
        )
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
            content = str(synthesize_node(cast(ResearchState, fallback_state)).get("final_content") or "")
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
        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        content = str(gstate.get("final_content") or "").strip()
        judgement = dict(gstate.get("coverage_judgement") or {})
        evidence_records = [row for row in gstate.get("evidence_records") or [] if isinstance(row, dict)]
        issues: list[str] = []
        if not content:
            issues.append("no_content")
        if bool(gstate.get("synthesis_failed")):
            issues.append("synthesis_failed")
        if not bool(judgement.get("sufficient")):
            issues.append("coverage_gap")
        if not evidence_records:
            issues.append("no_usable_evidence")
        citation_valid = True
        citation_reason = ""
        manager = session.ctx.citation_manager
        if manager is not None and content:
            citation_valid, citation_reason = manager.validate_citations(content)
            if not citation_valid:
                issues.append(citation_reason or "citation_validation_failed")
        repairable = bool(content) and not citation_valid and int(gstate.get("synthesis_attempts") or 0) < 2
        assessment = {
            "verdict": "pass" if not issues else "fail",
            "issues": issues,
            "repairable": repairable,
            "suggested_action": "repair" if repairable else "",
            "grounding": bool(content and evidence_records and citation_valid),
            "citation_metrics": {
                "evidence_count": len(evidence_records),
                "finding_count": len(gstate.get("findings") or []),
                "citation_valid": citation_valid,
            },
        }
        decision = {
            "action": "repair_synthesis" if repairable else ("finalize_success" if assessment["verdict"] == "pass" else "finalize_failure"),
            "reason_codes": issues or ["quality_pass"],
        }
        update = transition_update(gstate, WorkflowPhase.QUALITY, {"quality_assessment": assessment})
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

    async def node_finalize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import finalize_node

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        session.state.final_content = str(gstate.get("final_content") or "")
        termination = finalize_node(cast(ResearchState, gstate))
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
        if isinstance(result.metadata, dict):
            brief = dict(gstate.get("brief") or {})
            manager = getattr(session.ctx, "citation_manager", None)
            source_counts = manager.source_counts_by_tier() if manager is not None else {}
            result.metadata.update(
                {
                    "brief": brief,
                    "brief_id": str(brief.get("brief_id") or ""),
                    "brief_version": int(brief.get("version") or 1),
                    "user_intent": str(brief.get("user_intent") or ""),
                    "task_shape": str(brief.get("user_intent") or ""),
                    "execution_path": "fast_path" if bool(gstate.get("fast_path")) else "supervisor_loop",
                    "planner_calls": 0,
                    "workers": len(gstate.get("tasks") or {}),
                    "primary_sources": int(source_counts.get(SourceTier.PRIMARY.value, 0) or 0),
                    "partial_renderer_called": str((gstate.get("control_decision") or {}).get("action") or "") == "deliver_partial",
                }
            )
        session.result = result
        return termination
