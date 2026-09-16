"""Dumb executor bridge between the canonical ResearchState graph and harness services."""

from __future__ import annotations

import asyncio
import logging
import os
import time
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
from app.research.runtime.reducers import merge_findings, merge_records, merge_strings
from app.research.runtime.latency import (
    critical_path_summary,
    note_final_answer,
    note_stage_duration,
    note_worker_durations,
)
from app.research.runtime.task_budget import task_budget_metadata, task_budget_profile
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
        self.search_mode = str(getattr(ctx, "search_mode", "agent") or "agent")
        self.result: Any = None
        self.worker_sem = asyncio.Semaphore(self._resolve_max_workers())
        self.active_wave_size = 1
        self.wave_early_stop = False

    def _resolve_max_workers(self) -> int:
        hard = max(1, int(getattr(self.harness.harness_config, "max_parallel_workers", 3) or 3))
        metadata = getattr(self.state, "metadata", None) or {}
        budget = metadata.get("run_budget") if isinstance(metadata, dict) else None
        if isinstance(budget, dict) and budget.get("max_parallel_workers") is not None:
            return max(1, min(hard, int(budget["max_parallel_workers"])))
        return hard

    def _run_budget_value(self, key: str, default: int | float) -> int | float:
        metadata = getattr(self.state, "metadata", None) or {}
        budget = metadata.get("run_budget") if isinstance(metadata, dict) else None
        if isinstance(budget, dict) and budget.get(key) is not None:
            return type(default)(budget[key])
        return default

    def step_timeout_sec(self) -> int:
        return int(
            self._run_budget_value(
                "step_timeout_sec",
                getattr(self.harness.harness_config, "step_timeout_sec", 120),
            )
        )

    def worker_idle_timeout_sec(self) -> float:
        return float(
            self._run_budget_value(
                "worker_idle_timeout_sec",
                getattr(self.harness.harness_config, "worker_idle_timeout_sec", 120),
            )
        )

    def worker_llm_call_limit(self) -> int:
        return int(
            self._run_budget_value(
                "max_llm_calls_per_worker",
                getattr(self.harness.harness_config, "max_llm_calls_per_worker", 24),
            )
        )

    def synthesis_timeout_sec(self) -> float:
        profile_timeout = float(
            self._run_budget_value(
                "synthesis_step_timeout_sec",
                getattr(self.harness.harness_config, "synthesis_step_timeout_sec", 60),
            )
        )
        explicit = (os.getenv("HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC") or "").strip()
        if explicit:
            try:
                requested = float(explicit)
                if requested > 0:
                    profile_timeout = min(profile_timeout, requested)
            except ValueError:
                pass
        return min(profile_timeout, 180.0)

    def synthesis_retry_timeout_sec(self) -> float:
        return float(
            self._run_budget_value(
                "synthesis_retry_timeout_sec",
                getattr(self.harness.harness_config, "synthesis_retry_timeout_sec", 30),
            )
        )


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


def _citation_numbers_by_evidence(manager: Any) -> dict[str, int]:
    if manager is None or not hasattr(manager, "evidence_number_map"):
        return {}
    return dict(manager.evidence_number_map())


def _budget_snapshot(session: RunSession) -> dict[str, Any]:
    manager = session.budget_manager
    budget: dict[str, Any] = {
        "tool_calls": int(getattr(session.state, "tool_calls_count", 0) or 0),
        "max_tool_calls": int(getattr(manager, "max_tool_calls", 80) or 80),
        "llm_calls": int(getattr(manager, "llm_calls", 0) or 0),
        "max_llm_calls": int(getattr(manager, "max_llm_calls", 80) or 80),
        "total_tokens": int(getattr(manager, "total_tokens", 0) or 0),
        "max_total_tokens": int(getattr(manager, "max_total_tokens", 500000) or 500000),
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

    def _bootstrap_run(self, ctx: Any) -> tuple[str, dict[str, Any]]:
        """Resolve the profile before a session can create its hard budget manager."""
        from app.agent.harness.run_budget import create_run_budget_manager
        from app.research.routing.mode_router import (
            budget_for_mode,
            canonicalize_mode,
            route,
            run_budget_overrides_for_mode,
        )

        personal = getattr(self.harness.harness_config, "personal_search", None) or {}
        decision = route(
            ctx.task_query,
            user_mode=getattr(ctx, "search_mode", "agent") or "agent",
            conversation_summary=str(getattr(ctx, "conversation_summary", "") or ""),
        )
        profile = decision.mode
        metadata = ctx.state.metadata
        previous = dict(metadata.get("run_budget") or {})
        manager = ctx.budget_manager
        previous_profile = canonicalize_mode(previous.get("profile")) if previous.get("profile") else None

        desired_overrides = run_budget_overrides_for_mode(profile, personal)
        manager_mismatch = bool(
            manager is not None
            and (
                (previous_profile is not None and previous_profile != profile)
                or (previous_profile is None and profile == "deep_debug")
                or (
                    profile == "deep_debug"
                    and any(
                        getattr(manager, field) != desired_overrides[key]
                        for key, field in (
                            ("max_total_tokens", "token_limit"),
                            ("max_run_sec", "deadline_sec"),
                            ("max_llm_calls", "llm_call_limit"),
                            ("max_tool_calls", "tool_call_limit"),
                            ("max_llm_calls_per_worker", "max_llm_calls_per_worker"),
                            ("max_parallel_workers", "max_parallel_workers"),
                            ("synthesis_reserve_sec", "synthesis_reserve_sec"),
                        )
                    )
                )
            )
        )
        if manager_mismatch:
            snapshot = manager.snapshot()
            has_usage = bool(
                snapshot.used_tokens
                or snapshot.llm_calls
                or snapshot.tool_calls
                or snapshot.reserved_tokens
                or snapshot.reserved_llm_calls
                or snapshot.active_worker_leases
            )
            if has_usage:
                profile = previous_profile or "agent"
                warning = (
                    f"budget profile mutation blocked after usage: requested={decision.mode}, "
                    f"active={profile}"
                    if profile != decision.mode
                    else f"budget limit mutation blocked after usage: profile={profile}"
                )
                logger.warning(warning)
                metadata["budget_profile_warning"] = warning
            else:
                manager = None
                previous = {}

        budget_cfg = budget_for_mode(profile, personal)
        overrides = run_budget_overrides_for_mode(profile, personal)
        run_budget = {**budget_cfg, **overrides, **previous}
        if manager is None:
            manager = create_run_budget_manager(
                self.harness.harness_config,
                run_budget=run_budget,
                run_started=getattr(ctx, "run_started", None),
            )
            ctx.budget_manager = manager

        snapshot = manager.snapshot()
        run_budget.update(
            profile=profile,
            max_total_tokens=snapshot.token_limit,
            max_run_sec=manager.deadline_sec,
            max_llm_calls=snapshot.llm_call_limit,
            max_tool_calls=snapshot.tool_call_limit,
            max_llm_calls_per_worker=manager.max_llm_calls_per_worker,
            max_parallel_workers=manager.max_parallel_workers,
            research_cap_tokens=snapshot.research_cap_tokens,
            synthesis_reserve_tokens=snapshot.synthesis_reserve_tokens,
            synthesis_reserve_sec=manager.synthesis_reserve_sec,
            deadline_at_monotonic=manager.deadline_at,
        )
        metadata["run_budget"] = run_budget
        metadata["route_decision"] = decision.to_dict()
        ctx.search_mode = profile
        return profile, budget_cfg

    async def execute(self, ctx: Any, *, checkpointer: Any = None) -> Any:
        from langgraph.types import Command

        profile, budget_cfg = self._bootstrap_run(ctx)
        session = RunSession(self.harness, ctx)
        bind_session(session)
        session.state.metadata["workflow_authority"] = "research_state"
        run_budget = (
            session.state.metadata.get("run_budget")
            if isinstance(session.state.metadata, dict)
            else None
        )
        if isinstance(run_budget, dict) and run_budget.get("max_replan_count") is not None:
            budget_cfg["max_replan_count"] = max(0, int(run_budget["max_replan_count"]))
        # The ordinary agent profile is capped by the harness setting; the
        # debug profile intentionally carries a larger, separate replan budget.
        if getattr(self.harness.harness_config, "max_replan_count", None) is not None:
            budget_cfg["max_replan_count"] = min(
                int(budget_cfg["max_replan_count"]),
                max(0, int(self.harness.harness_config.max_replan_count)),
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
        payload["budget"]["max_tool_calls"] = int(budget_cfg["max_tool_calls"])
        payload["budget"]["max_replan_count"] = int(budget_cfg["max_replan_count"])
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
            agent=getattr(self.harness, "control_agent", None),
            budget_manager=session.budget_manager,
            conversation_delta=str(getattr(session.ctx, "conversation_summary", "") or ""),
            session_id=session.session_id,
        )

    async def node_brief(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import FastPathEligibility, StructuredResearchBrief
        from app.research.brief.validator import validate_structured_brief
        from app.research.runtime.graph import brief_node

        session = _require_session(gstate)
        brief_started = time.perf_counter()
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
                    "control_plane": {
                        "brief_source": brief.compiler_source,
                        "brief_fallback": brief.compiler_source != "structured_llm",
                        "supervisor_sources": [],
                        "supervisor_structured_success": 0,
                        "supervisor_fallback_count": 0,
                        "fallback_rate": 0.0,
                        "control_plane_degraded": brief.compiler_source != "structured_llm",
                    },
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
        note_stage_duration(
            session.state,
            "brief",
            int((time.perf_counter() - brief_started) * 1000),
        )
        return update

    async def node_supervisor(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import StructuredResearchBrief
        from app.research.coverage.judge import CoverageJudgement
        from app.research.runtime.admission import admit_dispatch
        from app.research.supervisor.agent import SupervisorAgent
        from app.research.supervisor.models import ResearchTaskRequest

        session = _require_session(gstate)
        supervisor_started = time.perf_counter()
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        brief = StructuredResearchBrief.from_dict(state.get("brief"))
        findings = [row for row in state.get("findings") or [] if isinstance(row, dict)]
        judgement_raw = state.get("coverage_judgement")
        judgement = CoverageJudgement.from_dict(judgement_raw) if isinstance(judgement_raw, dict) and judgement_raw else None
        raw_budget = state.get("budget")
        budget = raw_budget if isinstance(raw_budget, dict) else {}
        previous_fingerprints = {
            str(item) for item in (state.get("task_fingerprints") or {}).keys() if str(item).strip()
        }
        raw_value_signal = state.get("research_value_signal")
        value_signal = raw_value_signal if isinstance(raw_value_signal, dict) else {}
        _emit(
            session,
            "supervisor.started",
            phase=WorkflowPhase.SUPERVISOR.value,
            status="start",
            attributes={
                "iteration": int((state.get("supervisor") or {}).get("iteration") or 0)
                + (1 if state.get("plan") else 0),
                "finding_count": len(findings),
                "coverage_status": judgement.status if judgement else "unknown",
            },
        )
        supervisor = SupervisorAgent(getattr(self.harness, "control_agent", None), session.budget_manager)
        action = await supervisor.decide(
            brief,
            findings,
            judgement,
            budget,
            previous_fingerprints=previous_fingerprints,
            duplicate_search_ratio=float(value_signal.get("duplicate_search_ratio") or 0.0),
        )
        action = supervisor.resolve_action(
            action,
            judgement,
            brief,
            previous_fingerprints=previous_fingerprints,
        )
        payload: dict[str, Any] = {
            "supervisor_action": action.to_dict(),
            "supervisor": {
                "iteration": int((state.get("supervisor") or {}).get("iteration") or 0)
                + (1 if state.get("plan") else 0),
                "last_action": action.action,
                "reasoning_summary": action.reason,
                "source": action.source,
                "source_history": [
                    *[
                        str(item)
                        for item in (state.get("supervisor") or {}).get("source_history") or []
                        if str(item).strip()
                    ],
                    action.source,
                ],
            },
        }
        raw_iteration_limit = state.get("budget", {}).get("max_replan_count")
        iteration_limit = 3 if raw_iteration_limit is None else max(0, int(raw_iteration_limit))
        supervisor_iteration_exceeded = (
            int(payload["supervisor"]["iteration"]) >= max(1, iteration_limit)
        )
        if action.action == "CONDUCT_RESEARCH" and action.research_tasks and not supervisor_iteration_exceeded:
            session.wave_early_stop = False
            task_requests = [ResearchTaskRequest.from_dict(item.to_dict()) for item in action.research_tasks]
            wave_id = int(state.get("dispatch_wave_id") or 0) + 1
            admission = admit_dispatch(
                task_requests,
                wave_id=wave_id,
                budget_manager=session.budget_manager,
                state=state,
            )
            payload["dispatch_admission"] = admission.to_dict()
            if admission.approved:
                from app.research.workers.registry import worker_tools_for_step

                approved_requests = [item.request for item in admission.approved]
                steps = [
                    PlanStep(
                        step_type="research",
                        description=item.objective,
                        objective=item.objective,
                        task_id=item.task_id,
                        allowed_tools=worker_tools_for_step("research"),
                        metadata={
                            "kind": "research_task",
                            "task_kind": "supervisor_research",
                            "priority": item.priority,
                            "target_criteria": list(item.target_criteria),
                            "target_gaps": list(item.target_gaps),
                            "criterion_id": item.criterion_id,
                            "gap_id": item.gap_id,
                            "missing_evidence_types": list(item.missing_evidence_types),
                            "blocking_conflict_ids": list(item.blocking_conflict_ids),
                            "objective": item.objective,
                            "expected_evidence": list(item.expected_evidence),
                            "source_hints": list(item.source_hints),
                            "novelty_reason": item.novelty_reason,
                            "estimated_effort": item.estimated_effort,
                            "token_ceiling": (
                                budget_profile := task_budget_profile(item.estimated_effort)
                            ).token_ceiling,
                            "max_llm_calls": (
                                max(
                                    budget_profile.max_llm_calls,
                                    session.worker_llm_call_limit(),
                                )
                                if session.search_mode == "deep_debug"
                                else budget_profile.max_llm_calls
                            ),
                            **{
                                key: value
                                for key, value in task_budget_metadata(budget_profile).items()
                                if key != "max_llm_calls"
                            },
                            "semantic_fingerprint": next(
                                approved.fingerprint
                                for approved in admission.approved
                                if approved.task_id == item.task_id
                            ),
                            "required": True,
                            "optional": False,
                        },
                    )
                    for item in approved_requests
                ]
                plan = ExecutionPlan(
                    steps=steps,
                    summary="Budget-approved Supervisor research action",
                    plan_version=int(state.get("plan_version") or 1) + (1 if state.get("plan") else 0),
                    planning_mode="supervisor_action",
                )
                session.state.plan = plan
                session.active_wave_size = len(approved_requests)
                payload.update(
                    {
                        "plan": plan.to_dict(),
                        "plan_version": plan.plan_version,
                        "tasks": initialize_tasks(plan),
                        "dispatch_wave_id": wave_id,
                        "task_fingerprints": {
                            item.fingerprint: {
                                "task_id": item.task_id,
                                "objective": item.request.objective,
                                "target_gaps": list(item.request.target_gaps),
                                "wave_id": wave_id,
                            }
                            for item in admission.approved
                        },
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
            else:
                session.active_wave_size = 1
        decision = decide_control({**state, **payload})
        if action.action == "CONDUCT_RESEARCH":
            raw_admission = payload.get("dispatch_admission")
            admission_payload = raw_admission if isinstance(raw_admission, dict) else {}
            if admission_payload.get("approved_task_ids"):
                if decision.action in {"dispatch", "retry"}:
                    decision = type(decision)(
                        decision.action,
                        (*decision.reason_codes, "budget_admission"),
                        tuple(str(item) for item in admission_payload["approved_task_ids"]),
                    )
            elif bool(state.get("evidence_records")):
                decision = type(decision)(
                    "deliver_partial",
                    ("budget_stop", "usable_evidence", "no_approved_dispatch"),
                    (),
                )
            else:
                decision = type(decision)(
                    "finalize_failure",
                    ("budget_stop", "no_usable_evidence", "no_approved_dispatch"),
                    (),
                )
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
                    "control_plane": self._control_plane_status(
                        brief.compiler_source,
                        list(payload["supervisor"]["source_history"]),
                    ),
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
        note_stage_duration(
            session.state,
            "supervisor",
            int((time.perf_counter() - supervisor_started) * 1000),
        )
        return transition_update(gstate, WorkflowPhase.SUPERVISOR, payload)

    async def node_researcher(self, gstate: dict[str, Any]) -> dict[str, Any]:
        return await self.node_research_worker(gstate)

    async def node_ingest_findings(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.ingestion import ingest_new_worker_results
        from app.research.runtime.atomic_fact import extract_atomic_fact_answer

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        update = ingest_new_worker_results(gstate)
        findings = list(update.get("findings") or [])
        citation_manager = getattr(session.ctx, "citation_manager", None)
        if citation_manager is not None:
            citation_manager.bind_evidence_records(
                [
                    dict(row)
                    for row in update.get("evidence_records") or []
                    if isinstance(row, dict)
                ],
                findings,
            )
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
                    "research_value_signal": dict(update.get("research_value_signal") or {}),
                    "processed_worker_result_ids": list(update.get("processed_worker_result_ids") or []),
                }
            )
        if bool(gstate.get("fast_path")):
            manager = citation_manager
            atomic_answer = await extract_atomic_fact_answer(
                query=str(gstate.get("task_query") or ""),
                sources=list(getattr(manager, "sources", []) or []) if manager is not None else [],
                model=getattr(self.harness, "control_agent", None),
                budget_manager=session.budget_manager,
                timeout_sec=10,
            )
            update["fast_path_answer"] = atomic_answer.to_dict()
        return transition_update(gstate, WorkflowPhase.INGEST_FINDINGS, update)

    async def node_coverage_judge(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import StructuredResearchBrief
        from app.research.coverage.judge import CoverageJudgement
        from app.research.coverage.judge import CoverageJudge
        from dataclasses import replace
        from app.research.runtime.atomic_fact import AtomicFactAnswer

        session = _require_session(gstate)
        coverage_started = time.perf_counter()
        sync_execution_projection(session.state, gstate)
        state = _sync_assessments(session, dict(gstate))
        brief = StructuredResearchBrief.from_dict(state.get("brief"))
        findings = [row for row in state.get("findings") or [] if isinstance(row, dict)]
        claims = [row for row in state.get("claims") or [] if isinstance(row, dict)]
        conflicts = [row for row in state.get("claim_conflicts") or [] if isinstance(row, dict)]
        evidence_records = [row for row in state.get("evidence_records") or [] if isinstance(row, dict)]
        previous_raw = state.get("coverage_judgement")
        previous = (
            CoverageJudgement.from_dict(previous_raw)
            if isinstance(previous_raw, dict) and previous_raw
            else None
        )
        judgement = await CoverageJudge(None, session.budget_manager).evaluate(
            brief,
            findings,
            claim_conflicts=conflicts,
            claim_resolutions=[
                row
                for row in state.get("claim_resolutions") or []
                if isinstance(row, dict)
            ],
            claims=claims,
            evidence=evidence_records,
            previous=previous,
        )
        if bool(gstate.get("fast_path")):
            atomic_answer = AtomicFactAnswer.from_dict(state.get("fast_path_answer"))
            judgement = replace(
                judgement,
                sufficient=atomic_answer.sufficient,
                status="sufficient" if atomic_answer.sufficient else "gap",
                source="simple_fact_evidence_policy",
                reason=atomic_answer.reason or "fast path evidence policy",
            )
        supported_count = sum(
            1 for row in judgement.criteria if row.status == "supported"
        )
        coverage_ratio = (
            supported_count / len(judgement.criteria) if judgement.criteria else 0.0
        )
        progress_projection = {
            "status": judgement.status,
            "coverage_ratio": 1.0 if judgement.sufficient else round(coverage_ratio, 4),
            "unresolved_conflicts": list(judgement.conflicts),
            "missing": [gap.description for gap in judgement.gaps],
            "missing_ids": [gap.gap_id for gap in judgement.gaps],
            "criterion_ids": [row.criterion_id for row in judgement.criteria],
            "semantic_gap_ids": [
                gap.gap_id or gap.criterion_id for gap in judgement.gaps
            ],
            "reason_codes": [] if judgement.sufficient else ["coverage_gap"],
        }
        value_signal = dict(state.get("research_value_signal") or {})
        value_signal.update(
            {
                "closed_criteria_count": len(judgement.delta.closed_criterion_ids),
                "new_high_quality_evidence_count": int(
                    value_signal.get("new_high_quality_evidence_count") or 0
                ),
                "new_supported_claim_count": len(judgement.delta.new_supported_claim_ids),
                "duplicate_search_ratio": float(value_signal.get("duplicate_search_ratio") or 0.0),
            }
        )
        low_value_rounds = int(state.get("low_value_rounds") or 0)
        has_value = bool(
            value_signal.get("closed_criteria_count")
            or value_signal.get("new_high_quality_evidence_count")
            or value_signal.get("new_supported_claim_count")
        )
        low_value_rounds = 0 if has_value else low_value_rounds + 1
        marginal_stop = low_value_rounds >= 2 and bool(state.get("evidence_records"))
        update = {
            "coverage_judgement": judgement.to_dict(),
            "progress_assessment": progress_projection,
            "research_value_signal": value_signal,
            "low_value_rounds": low_value_rounds,
            "control_decision": (
                {
                    "action": "deliver_partial",
                    "reason_codes": ["marginal_gain_low", "usable_evidence"],
                    "task_ids": [],
                    "policy_version": "runtime-policy.v1",
                }
                if marginal_stop
                else {}
            ),
            "stop_reason": "marginal_gain_low" if marginal_stop else "",
        }
        sync_execution_projection(session.state, {**gstate, **update})
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "coverage_judgement": judgement.to_dict(),
                    "progress_assessment": progress_projection,
                    "research_value_signal": value_signal,
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
                "criteria": [row.to_dict() for row in judgement.criteria],
                "delta": judgement.delta.to_dict(),
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
        note_stage_duration(
            session.state,
            "coverage",
            int((time.perf_counter() - coverage_started) * 1000),
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

        from app.research.brief.models import StructuredResearchBrief
        from app.research.coverage.judge import CoverageJudgement, judge_coverage
        from app.research.execution.worker_executor import WorkerExecutorV2
        from app.research.runtime.ingestion import ingest_new_worker_results
        from app.research.runtime.findings import normalize_findings
        from app.research.runtime.isolation import worker_row
        from app.research.runtime.worker import ResearchContext, ResearchTask, WorkerResult
        from app.research.runtime.task_identity import worker_result_id

        session = _require_session(gstate)
        sync_execution_projection(session.state, gstate)
        step_index = int(gstate.get("step_index") or 0)
        task_id = str(gstate.get("task_id") or f"s{step_index}")
        plan = session.state.plan
        if plan is None or step_index >= len(plan.steps):
            failure: dict[str, Any] = cast(
                dict[str, Any],
                classify_failure("missing_step"),
            )
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
        note_worker_durations(session.state, [result])
        if result.raw is not None:
            session.state.step_results.append(result.raw)
        row = worker_row(task_id, step, result.ok, result.raw) if result.raw is not None else {"task_id": task_id, "ok": result.ok, "summary": result.summary, "step_type": step.step_type, "payload": {"facts": result.facts, "sources": result.sources, "findings": result.findings, "evidence_ids": result.evidence_refs, "candidates": result.candidates}}
        row.update(status=result.status, fail_reason=result.fail_reason, queue_ms=result.queue_ms, execution_ms=result.execution_ms, metrics=result.metrics)
        row.update(
            dispatch_wave_id=dispatch_wave_id,
            attempt=attempt,
            execution_status="running",
            result_status="none",
            stop_reason="none",
            last_tool_error=(
                dict(result.metrics["last_tool_error"])
                if isinstance(result.metrics.get("last_tool_error"), dict)
                else {"tool": "", "error": str(result.metrics.get("last_tool_error") or "")}
                if str(result.metrics.get("last_tool_error") or "").strip()
                else {}
            ),
        )
        row["worker_result_id"] = worker_result_id(row)
        normalized_findings, _ = normalize_findings(result.findings, task_id=task_id, subject_id=str(step.metadata.get("subject_id") or "general"), dimension=str((step.metadata.get("coverage_keys") or ["general"])[0]))
        row["payload"] = {
            **(row.get("payload") or {}),
            "findings": normalized_findings,
            "evidence_ids": list(
                (row.get("payload") or {}).get("evidence_ids")
                or result.evidence_refs
                or []
            ),
            "artifact_ids": list((row.get("payload") or {}).get("artifact_ids") or []),
            "search_queries": list((row.get("payload") or {}).get("search_queries") or []),
        }
        row["task_metadata"] = dict(step.metadata or {})
        ingest_update = ingest_new_worker_results({**gstate, "worker_results": [row]})
        if ingest_update:
            brief = StructuredResearchBrief.from_dict(gstate.get("brief"))
            previous_raw = gstate.get("coverage_judgement")
            previous = (
                CoverageJudgement.from_dict(previous_raw)
                if isinstance(previous_raw, dict) and previous_raw
                else None
            )
            partial_state = {**gstate, **ingest_update}
            for record_field in (
                "claims",
                "claim_conflicts",
                "claim_resolutions",
                "evidence_records",
            ):
                partial_state[record_field] = merge_records(
                    gstate.get(record_field),
                    ingest_update.get(record_field),
                )
            partial_state["findings"] = merge_findings(
                gstate.get("findings"),
                ingest_update.get("findings"),
            )
            partial_state["search_query_fingerprints"] = merge_strings(
                gstate.get("search_query_fingerprints"),
                ingest_update.get("search_query_fingerprints"),
            )
            judgement = judge_coverage(
                brief,
                [row for row in partial_state.get("findings") or [] if isinstance(row, dict)],
                claim_conflicts=[
                    row
                    for row in partial_state.get("claim_conflicts") or []
                    if isinstance(row, dict)
                ],
                claim_resolutions=[
                    row
                    for row in partial_state.get("claim_resolutions") or []
                    if isinstance(row, dict)
                ],
                claims=[row for row in partial_state.get("claims") or [] if isinstance(row, dict)],
                evidence=[row for row in partial_state.get("evidence_records") or [] if isinstance(row, dict)],
                previous=previous,
            )
            if judgement.sufficient:
                session.wave_early_stop = True
        ingested_findings = [
            finding
            for finding in ingest_update.get("findings") or []
            if isinstance(finding, dict) and str(finding.get("task_id") or "") == task_id
        ]
        accepted_findings = [
            finding
            for finding in ingested_findings
            if not bool(finding.get("partial"))
            and list(finding.get("evidence_ids") or [])
        ]
        admitted_evidence_count = sum(
            1
            for evidence in ingest_update.get("evidence_records") or []
            if isinstance(evidence, dict) and str(evidence.get("task_id") or "") == task_id
        )
        row["finding_acceptance"] = {
            "raw_finding_count": int(result.metrics.get("raw_finding_count") or 0),
            "accepted_finding_count": len(accepted_findings),
            "partial_fallback_finding_count": sum(
                1 for finding in ingested_findings if bool(finding.get("partial"))
            ),
            "admitted_evidence_count": admitted_evidence_count,
            "diagnostics": [
                item
                for item in ingest_update.get("finding_diagnostics") or []
                if isinstance(item, dict)
                and str(item.get("task_id") or "") == task_id
            ],
        }
        execution_status, result_status, stop_reason, lifecycle_failure = worker_result_lifecycle(result)
        failure = lifecycle_failure
        if step.step_type in {"research", "network_search"}:
            if admitted_evidence_count == 0:
                execution_status = TaskExecutionStatus.FAILED
                result_status = ResultStatus.NONE
                stop_reason = StopReason.NONE
                failure = cast(dict[str, Any], classify_failure("no_usable_evidence"))
            elif not accepted_findings or not result.ok:
                execution_status = TaskExecutionStatus.STOPPED
                result_status = ResultStatus.PARTIAL
                failure = cast(
                    dict[str, Any],
                    classify_failure(
                        "no_accepted_findings"
                        if not accepted_findings
                        else str(result.fail_reason or "worker_failed")
                    ),
                )
        row.update(
            execution_status=execution_status.value,
            result_status=result_status.value,
            stop_reason=stop_reason.value,
            accepted_finding_count=len(accepted_findings),
            admitted_evidence_count=admitted_evidence_count,
        )
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
                "raw_finding_count": int(result.metrics.get("raw_finding_count") or 0),
                "accepted_finding_count": len(accepted_findings),
                "partial_fallback_finding_count": sum(
                    1 for finding in ingested_findings if bool(finding.get("partial"))
                ),
                "admitted_evidence_count": admitted_evidence_count,
                "dispatch_wave_id": dispatch_wave_id,
            },
        )
        for finding in ingested_findings:
            _emit(
                session,
                "finding.compressed",
                phase=WorkflowPhase.EXECUTE.value,
                status="ok" if not bool(finding.get("partial")) else "partial",
                task_id=task_id,
                attributes={
                    "finding_id": finding.get("finding_id"),
                    "evidence_ids": finding.get("evidence_ids") or [],
                    "claim_count": len(finding.get("claims") or []),
                    "confidence": finding.get("confidence"),
                    "partial": bool(finding.get("partial")),
                    "limitations": finding.get("limitations") or [],
                },
                output_refs=[{"type": "finding", "id": str(finding.get("finding_id") or "")}],
            )
        evidence_events = list(ingest_update.get("evidence_records") or [])
        if not evidence_events:
            evidence_events = [
                {
                    "evidence_id": evidence_id,
                    "artifact_id": evidence_id,
                    "source_kind": "artifact",
                    "source_tier": "SECONDARY",
                }
                for evidence_id in result.evidence_refs
            ]
        for evidence in evidence_events:
            _emit(
                session,
                "evidence.registered",
                phase=WorkflowPhase.EXECUTE.value,
                status="ok",
                task_id=task_id,
                attempt=attempt,
                attributes={
                    "evidence_id": str(evidence.get("evidence_id") or ""),
                    "source_id": str(evidence.get("source_id") or ""),
                    "artifact_id": str(evidence.get("artifact_ref") or evidence.get("artifact_id") or ""),
                    "source_kind": str(evidence.get("source_kind") or "artifact"),
                    "locator": str(evidence.get("locator") or ""),
                    "source_tier": str(evidence.get("source_tier") or "SECONDARY"),
                    "support_type": "partial",
                    "source_quality": str(evidence.get("source_tier") or "SECONDARY"),
                },
                input_refs=[{"type": "task", "id": task_id}] if task_id else None,
            )
        worker_update = {
            "phase": WorkflowPhase.EXECUTE.value,
            "tasks": {task_id: tasks[task_id]},
            "worker_results": [row],
            "evidence_refs": result.evidence_refs,
            "findings": normalized_findings,
        }
        if ingest_update:
            worker_update.update(ingest_update)
            worker_update["findings"] = list(ingest_update.get("findings") or normalized_findings)
        return worker_update

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
        synthesis_timeout_sec = session.synthesis_timeout_sec()
        return {
            "remaining_run_tokens": max(0, token_limit - used_tokens - reserved_tokens) if token_limit else 0,
            "remaining_run_sec": max(0.0, remaining_run_sec),
            "synthesis_reserve_tokens": int(getattr(snapshot, "synthesis_reserve_tokens", 0) or 0),
            "synthesis_timeout_sec": synthesis_timeout_sec,
        }

    @staticmethod
    def _estimate_synthesis_input_tokens(executor: Any, request: Any, context: Any) -> int:
        estimate = getattr(executor, "estimate_input_tokens", None)
        return max(0, int(estimate(request, context))) if callable(estimate) else 0

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

    async def node_synthesize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        import app.research.execution.synthesis_executor as synthesis_executor_module
        from dataclasses import replace
        from types import SimpleNamespace
        from app.research.brief.models import StructuredResearchBrief
        from app.research.delivery.partial_renderer import render_partial_delivery, scrub_internal_ids
        from app.research.delivery.evidence_pack import (
            COMPACT_SYNTHESIS_INPUT_TOKENS,
            NORMAL_SYNTHESIS_INPUT_TOKENS,
            build_evidence_pack,
        )
        from app.research.delivery.synthesis_context import (
            SynthesisContextBuilder,
            validate_synthesis_digests,
        )
        from app.research.delivery.answer_contract import (
            assess_answerability,
            assess_answer_completeness,
            compile_deterministic_answer,
            render_final_answer,
        )
        from app.research.runtime.atomic_fact import (
            AtomicFactAnswer,
            extract_atomic_fact_answer,
            render_atomic_fact_answer,
        )
        from app.research.runtime.worker import ResearchContext

        session = _require_session(gstate)
        synthesis_started = time.perf_counter()
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
            raw_answer = gstate.get("fast_path_answer")
            answer = (
                AtomicFactAnswer.from_dict(raw_answer)
                if isinstance(raw_answer, dict)
                else await extract_atomic_fact_answer(
                    query=str(gstate.get("task_query") or ""),
                    sources=list(getattr(manager, "sources", []) or []) if manager is not None else [],
                    model=getattr(self.harness, "control_agent", None),
                    budget_manager=session.budget_manager,
                    timeout_sec=10,
                )
            )
            content = render_atomic_fact_answer(answer, manager) if manager is not None else (
                "## 当前无法可靠确认\n\n当前运行缺少证据管理器，无法验证来源。"
            )
            session.state.final_content = content
            if isinstance(session.state.metadata, dict):
                session.state.metadata.update(
                    {
                        "synthesis_attempted": True,
                        "synthesis_attempts": 1,
                        "synthesis_mode": "atomic_fact_structured",
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
                    "answer_type": answer.answer_type,
                    "supporting_source_ids": list(answer.supporting_source_ids),
                    "fail_reason": "" if answer.sufficient else "insufficient_trusted_evidence",
                    "fallback_action": "",
                },
            )
            note_stage_duration(
                session.state,
                "synthesis",
                int((time.perf_counter() - synthesis_started) * 1000),
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
        evidence_records = [dict(row) for row in gstate.get("evidence_records") or [] if isinstance(row, dict)]
        claims = [dict(row) for row in gstate.get("claims") or [] if isinstance(row, dict)]
        judgement = dict(gstate.get("coverage_judgement") or {})
        conflict_resolutions = [
            dict(row)
            for row in gstate.get("claim_resolutions") or []
            if isinstance(row, dict)
        ]
        has_blocking_conflict = any(
            str(row.get("status") or "") == "unresolved" and bool(row.get("blocking"))
            for row in conflict_resolutions
        )
        mode = (
            "degraded"
            if compact
            or decision.get("action") == "deliver_partial"
            or has_blocking_conflict
            else "normal"
        )
        brief = StructuredResearchBrief.from_dict(gstate.get("brief"))
        answerability = assess_answerability(
            brief=brief,
            findings=[row for row in gstate.get("findings") or [] if isinstance(row, dict)],
            evidence_records=evidence_records,
            coverage=judgement,
            conflicts=conflict_resolutions,
        )
        if isinstance(session.state.metadata, dict):
            session.state.metadata["answerability"] = answerability.to_dict()
        _emit(
            session,
            "answerability.assessed",
            phase=WorkflowPhase.SYNTHESIS.value,
            status="answerable" if answerability.answerable else "unanswerable",
            attributes={
                "question_count": len(answerability.question_status),
                "answered_count": sum(1 for item in answerability.question_status if item.answerable),
                "answerable": answerability.answerable,
                "reason": answerability.reason,
            },
        )
        synthesis_context_builder = SynthesisContextBuilder(self.harness, session)
        synthesis_context = synthesis_context_builder.build(
            gstate,
            limitations=list(judgement.get("missing") or []),
            unresolved_conflicts=list(judgement.get("conflicts") or []),
            compact=compact,
        )

        def selected_digests(pack: Any, *, compact_pack: bool) -> list[Any]:
            claims_by_ref: dict[str, list[str]] = {}
            for finding in pack.findings:
                claim = str(finding.get("claim") or finding.get("summary") or "").strip()
                if not claim:
                    continue
                for ref in finding.get("evidence_ids") or []:
                    values = claims_by_ref.setdefault(str(ref), [])
                    if claim not in values:
                        values.append(claim)
            for claim_row in claims:
                text = str(claim_row.get("text") or claim_row.get("claim") or "").strip()
                if not text:
                    continue
                for ref in claim_row.get("evidence_ids") or []:
                    values = claims_by_ref.setdefault(str(ref), [])
                    if text not in values:
                        values.append(text)
            output: list[Any] = []
            for ref in pack.evidence_refs:
                digest = synthesis_context_builder.resolve_digest_by_evidence_id(
                    str(ref),
                    claims_by_ref.get(str(ref), []),
                    compact=compact_pack,
                )
                if digest is not None:
                    output.append(digest)
            return output

        criteria = list(
            brief.key_questions
            or brief.success_criteria
            or (brief.objective,)
        )
        citation_numbers = _citation_numbers_by_evidence(session.ctx.citation_manager)
        evidence_pack = build_evidence_pack(
            criteria,
            synthesis_context.findings,
            claims,
            evidence_records,
            conflict_resolutions,
            NORMAL_SYNTHESIS_INPUT_TOKENS,
            compact=compact,
        )
        evidence_refs = list(evidence_pack.evidence_refs)
        digests = [
            replace(
                digest,
                citation_number=int(citation_numbers.get(str(digest.evidence_id), 0) or 0),
            )
            for digest in selected_digests(evidence_pack, compact_pack=compact)
        ]
        request = synthesis_executor_module.SynthesisRequest(
            mode=mode,
            evidence_refs=evidence_refs,
            limitations=list(judgement.get("missing") or []),
            unresolved_conflicts=list(judgement.get("conflicts") or []),
            conflict_resolutions=list(evidence_pack.conflict_resolutions),
            research_summary=evidence_pack.research_summary,
            evidence_digests=digests,
            findings=list(evidence_pack.findings),
            worker_summaries=[],
            token_budget=evidence_pack.token_budget,
            attempt=attempts_before + 1,
            pack_tokens_estimated=evidence_pack.estimated_tokens,
        )
        remaining_synthesis_method = getattr(
            session.budget_manager, "remaining_for_synthesis_tokens", None
        )
        remaining_synthesis_tokens = (
            int(remaining_synthesis_method())
            if callable(remaining_synthesis_method)
            else int(getattr(session.budget_manager, "token_limit", 0) or 0)
        )
        request = replace(
            request,
            token_budget=min(request.token_budget, max(0, remaining_synthesis_tokens)),
        )
        executor = synthesis_executor_module.SynthesisExecutor(self.harness, session)
        context = ResearchContext(
            run_id=session.run_id,
            query=brief.objective or gstate["task_query"],
            user_id=session.ctx.user_id,
            tenant_id=session.ctx.tenant_id,
            project_id=session.ctx.project_id,
            session_id=session.session_id,
        )
        digest_ready = validate_synthesis_digests(
            evidence_pack.findings,
            evidence_pack.evidence_refs,
            digests,
            evidence_records,
        )
        _emit(
            session,
            "synthesis.compact.started" if compact else "synthesis.primary.started",
            phase=WorkflowPhase.SYNTHESIS.value,
            status="start",
            attempt=request.attempt,
            attributes={
                "mode": request.mode,
                "pack_tokens_estimated": request.pack_tokens_estimated,
                "evidence_count": len(request.evidence_refs),
            },
        )
        skip_llm_synthesis = bool(evidence_records) and remaining_synthesis_tokens < 1_000
        if skip_llm_synthesis:
            content = render_partial_delivery(
                objective=brief.objective,
                findings=list(synthesis_context.findings),
                evidence_digests=list(synthesis_context.evidence_digests),
                worker_summaries=[],
                semantic_gaps=list(synthesis_context.semantic_gaps),
                limitations=list(synthesis_context.limitations),
                unresolved_conflicts=list(synthesis_context.unresolved_conflicts),
                worker_failure_reasons=[],
                synthesis_failure_reason="synthesis_budget_low",
            )
            result = SimpleNamespace(
                ok=False,
                status="stopped",
                summary=content,
                duration_ms=0,
                fail_reason="synthesis_budget_low",
            )
        elif not digest_ready:
            result = SimpleNamespace(
                ok=False,
                status="stopped",
                summary="",
                duration_ms=0,
                fail_reason="synthesis_evidence_digest_missing",
                metadata={
                    "error": {
                        "type": "SynthesisContractError",
                        "message": "selected evidence has no usable digest",
                        "category": "local_validation",
                    },
                    "estimated_input_tokens": 0,
                },
            )
        else:
            span_key = self._start_synthesis_span(
                session,
                attempt=request.attempt,
                mode=request.mode,
                attributes={
                    "pack_tokens_estimated": request.pack_tokens_estimated,
                    "prompt_tokens_estimated": self._estimate_synthesis_input_tokens(executor, request, context),
                },
            )
            result = await executor.execute(request, context)
            self._end_synthesis_span(
                session, span_key, status="ok" if result.ok else "failed",
                duration_ms=result.duration_ms,
            )
        synthesis_metadata = dict(getattr(result, "metadata", {}) or {})
        first_attempt_reason = str(result.fail_reason or "")
        first_attempt_duration_ms = int(result.duration_ms or 0)
        attempt_metrics = [{
            **synthesis_metadata,
            "attempt": request.attempt,
            "pack_tokens_estimated": request.pack_tokens_estimated,
            "duration_ms": first_attempt_duration_ms,
            "fail_reason": first_attempt_reason,
            "status": "ok" if result.ok else "failed",
        }]
        if not result.ok:
            _emit(
                session,
                "synthesis.primary.failed" if not compact else "synthesis.compact.failed",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="failed",
                attempt=request.attempt,
                duration_ms=first_attempt_duration_ms,
                attributes={"reason": first_attempt_reason, "retry": "compact" if not compact else "none"},
            )
        retried = False
        retry_allowed = bool(
            not result.ok
            and result.fail_reason in synthesis_executor_module.RETRYABLE_SYNTHESIS_FAILURES
            and remaining_synthesis_tokens >= 1_000
        )
        remaining_run_method = getattr(session.budget_manager, "remaining_run_sec", None)
        retry_allowed = retry_allowed and (
            not callable(remaining_run_method) or float(remaining_run_method()) > 0
        )
        if retry_allowed:
            retried = True
            synthesis_error = dict(synthesis_metadata.get("error") or {})
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
                    "evidence_pack_tokens": int(evidence_pack.estimated_tokens),
                    "fail_reason": result.fail_reason,
                    "fallback_action": "compact_retry",
                    "error_type": str(synthesis_error.get("type") or ""),
                    "error_message": str(synthesis_error.get("message") or ""),
                    "error_category": str(synthesis_error.get("category") or ""),
                    "model": str(synthesis_metadata.get("model") or ""),
                    "provider": str(synthesis_metadata.get("provider") or ""),
                    "estimated_input_tokens": int(
                        synthesis_metadata.get("estimated_input_tokens") or 0
                    ),
                    "prompt_chars": int(synthesis_metadata.get("prompt_chars") or 0),
                    "digest_chars": int(synthesis_metadata.get("digest_chars") or 0),
                    "ttft_ms": synthesis_metadata.get("ttft_ms"),
                    "actual_input_tokens": int(synthesis_metadata.get("actual_input_tokens") or 0),
                    "actual_output_tokens": int(synthesis_metadata.get("actual_output_tokens") or 0),
                    "finish_reason": str(synthesis_metadata.get("finish_reason") or ""),
                    "remaining_run_tokens": int(
                        synthesis_metadata.get("remaining_run_tokens") or 0
                    ),
                    "remaining_run_sec": float(
                        synthesis_metadata.get("remaining_run_sec") or 0.0
                    ),
                    "retry_timeout_sec": session.synthesis_retry_timeout_sec(),
                },
            )
            mode = "degraded"
            compact_pack = build_evidence_pack(
                criteria,
                synthesis_context.findings,
                claims,
                evidence_records,
                conflict_resolutions,
                COMPACT_SYNTHESIS_INPUT_TOKENS,
                compact=True,
            )
            compact_digests = [
                replace(
                    digest,
                    citation_number=int(
                        citation_numbers.get(str(digest.evidence_id), 0) or 0
                    ),
                )
                for digest in selected_digests(compact_pack, compact_pack=True)
            ]
            if not validate_synthesis_digests(
                compact_pack.findings,
                compact_pack.evidence_refs,
                compact_digests,
                evidence_records,
            ):
                result = SimpleNamespace(
                    ok=False,
                    status="stopped",
                    summary="",
                    duration_ms=0,
                    fail_reason="synthesis_evidence_digest_missing",
                    metadata={
                        "error": {
                            "type": "SynthesisContractError",
                            "message": "compact evidence has no usable digest",
                            "category": "local_validation",
                        }
                    },
                )
            else:
                request = replace(
                    request,
                    mode=mode,
                    evidence_refs=list(compact_pack.evidence_refs),
                    research_summary=compact_pack.research_summary,
                    evidence_digests=compact_digests,
                    findings=list(compact_pack.findings),
                    conflict_resolutions=list(compact_pack.conflict_resolutions),
                    token_budget=min(
                        compact_pack.token_budget,
                        max(0, remaining_synthesis_tokens),
                    ),
                    attempt=attempts_before + 2,
                    pack_tokens_estimated=compact_pack.estimated_tokens,
                )
                evidence_refs = list(compact_pack.evidence_refs)
                retry_timeout_sec = session.synthesis_retry_timeout_sec()
                span_key = self._start_synthesis_span(
                    session,
                    attempt=request.attempt,
                    mode=request.mode,
                    attributes={
                        "pack_tokens_estimated": request.pack_tokens_estimated,
                        "prompt_tokens_estimated": self._estimate_synthesis_input_tokens(executor, request, context),
                    },
                )
                result = await executor.execute(
                    request,
                    context,
                    timeout_sec=retry_timeout_sec,
                )
                self._end_synthesis_span(
                    session, span_key, status="ok" if result.ok else "failed",
                    duration_ms=result.duration_ms,
                )
                _emit(
                    session,
                    "synthesis.compact.failed" if not result.ok else "synthesis.completed",
                    phase=WorkflowPhase.SYNTHESIS.value,
                    status="failed" if not result.ok else "ok",
                    attempt=request.attempt,
                    duration_ms=result.duration_ms,
                    attributes={
                        "reason": str(result.fail_reason or ""),
                        "pack_tokens_estimated": request.pack_tokens_estimated,
                    },
                )
            synthesis_metadata = dict(getattr(result, "metadata", {}) or {})
            attempt_metrics.append({
                **synthesis_metadata,
                "attempt": attempts_before + 2,
                "pack_tokens_estimated": compact_pack.estimated_tokens,
                "duration_ms": int(result.duration_ms or 0),
                "fail_reason": str(result.fail_reason or ""),
                "status": "ok" if result.ok else "failed",
            })
        fallback = not result.ok or not str(result.summary or "").strip()
        synthesis_degraded = retried or fallback or mode == "degraded"
        successful_attempt = attempts_before + (2 if retried else 1) if not fallback else 0
        successful_pack_tokens = (
            compact_pack.token_budget if retried else evidence_pack.token_budget
        ) if not fallback else 0
        synthesis_failed = fallback
        answer_complete = False
        answer_contract: dict[str, Any] = {}
        recovery_mode = ""
        if fallback and answerability.answerable and bool(judgement.get("sufficient")):
            recovered = compile_deterministic_answer(
                objective=brief.objective or str(gstate.get("task_query") or ""),
                brief=brief,
                findings=[row for row in gstate.get("findings") or [] if isinstance(row, dict)],
                answerability=answerability,
                synthesis_degraded=True,
            )
            completeness = assess_answer_completeness(recovered, brief)
            answer_complete = completeness.complete
            answer_contract = {
                "final_answer": recovered.to_dict(),
                "completeness": completeness.to_dict(),
            }
            _emit(
                session,
                "answer_recovery.started",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="start",
                attributes={"mode": "deterministic_recovery"},
            )
            if answer_complete:
                recovery_mode = "deterministic_recovery"
                content = render_final_answer(recovered, citation_numbers=citation_numbers)
                fallback = False
                _emit(
                    session,
                    "answer_recovery.completed",
                    phase=WorkflowPhase.SYNTHESIS.value,
                    status="ok",
                    attributes={"mode": recovery_mode, "answer_complete": True},
                )
            else:
                _emit(
                    session,
                    "answer_recovery.completed",
                    phase=WorkflowPhase.SYNTHESIS.value,
                    status="failed",
                    attributes={"mode": "deterministic_recovery", "answer_complete": False},
                )
            _emit(
                session,
                "answer_completeness.assessed",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="pass" if completeness.complete else "fail",
                attributes=completeness.to_dict(),
            )
        if fallback:
            worker_failure_reasons = [
                str(row.get("fail_reason") or "")
                for row in gstate.get("worker_results") or []
                if isinstance(row, dict) and str(row.get("fail_reason") or "").strip()
            ]
            content = render_partial_delivery(
                objective=brief.objective,
                findings=list(synthesis_context.findings),
                evidence_digests=list(synthesis_context.evidence_digests),
                worker_summaries=[],
                semantic_gaps=list(synthesis_context.semantic_gaps),
                limitations=list(synthesis_context.limitations),
                unresolved_conflicts=list(synthesis_context.unresolved_conflicts),
                worker_failure_reasons=worker_failure_reasons,
                synthesis_failure_reason=str(result.fail_reason or "synthesis_failed"),
            )
        else:
            content = content if recovery_mode else result.summary
            # Provider synthesis is a free-form report today; a non-empty,
            # grounded result under sufficient coverage satisfies the delivery
            # contract while structured recovery carries the full answer model.
            answer_complete = bool(content.strip() and judgement.get("sufficient"))
        content = scrub_internal_ids(content)
        manager = session.ctx.citation_manager
        if manager is not None and content:
            selected_findings = list(
                (compact_pack if retried else evidence_pack).findings
            )
            content = manager.inject_finding_citations(
                content,
                selected_findings,
                citation_numbers,
            )
            content = manager.build_cited_report(content)
        session.state.final_content = content
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "synthesis_attempted": True,
                    "synthesis_attempts": attempts_before + (2 if retried else 1),
                    "synthesis_mode": recovery_mode or mode,
                    "synthesis_status": result.status,
                    "synthesis_failed": synthesis_failed,
                    "synthesis_degraded": synthesis_degraded,
                    "synthesis_retry_count": int(retried),
                    "successful_attempt": successful_attempt,
                    "first_attempt_reason": first_attempt_reason,
                    "first_attempt_duration_ms": first_attempt_duration_ms,
                    "normal_pack_tokens": evidence_pack.token_budget,
                    "successful_pack_tokens": successful_pack_tokens,
                    "synthesis_attempt_metrics": attempt_metrics,
                    "synthesis_fail_reason": result.fail_reason,
                    "fallback_used": "deterministic_partial" if fallback else recovery_mode,
                    "answerability": answerability.to_dict(),
                    "answer_complete": answer_complete,
                    "answer_contract": answer_contract,
                    "synthesis_budget_low": skip_llm_synthesis,
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
                **{
                    "error_type": str((synthesis_metadata.get("error") or {}).get("type") or ""),
                    "error_message": str(
                        (synthesis_metadata.get("error") or {}).get("message") or ""
                    ),
                    "error_category": str(
                        (synthesis_metadata.get("error") or {}).get("category") or ""
                    ),
                    "model": str(synthesis_metadata.get("model") or ""),
                    "provider": str(synthesis_metadata.get("provider") or ""),
                    "estimated_input_tokens": int(
                        synthesis_metadata.get("estimated_input_tokens") or 0
                    ),
                    "pack_tokens_estimated": int(synthesis_metadata.get("pack_tokens_estimated") or 0),
                    "prompt_chars": int(synthesis_metadata.get("prompt_chars") or 0),
                    "digest_chars": int(synthesis_metadata.get("digest_chars") or 0),
                    "ttft_ms": synthesis_metadata.get("ttft_ms"),
                    "actual_input_tokens": int(synthesis_metadata.get("actual_input_tokens") or 0),
                    "actual_output_tokens": int(synthesis_metadata.get("actual_output_tokens") or 0),
                    "finish_reason": str(synthesis_metadata.get("finish_reason") or ""),
                    "remaining_run_tokens": int(
                        synthesis_metadata.get("remaining_run_tokens") or 0
                    ),
                    "remaining_run_sec": float(
                        synthesis_metadata.get("remaining_run_sec") or 0.0
                    ),
                },
                "mode": mode,
                "compact": compact or retried,
                "evidence_ids": list(evidence_refs),
                "evidence_count": len(evidence_refs),
                "claim_count": len(
                    (compact_pack if retried else evidence_pack).findings
                ),
                "evidence_pack_tokens": int(
                    (compact_pack if retried else evidence_pack).estimated_tokens
                ),
                "fail_reason": result.fail_reason,
                "fallback_action": recovery_mode or ("deterministic_partial" if fallback else ""),
                "synthesis_degraded": synthesis_degraded,
                "synthesis_retry_count": int(retried),
                "successful_attempt": successful_attempt,
                "answer_complete": answer_complete,
                "synthesis_mode": recovery_mode or mode,
            },
        )
        note_stage_duration(
            session.state,
            "synthesis",
            int((time.perf_counter() - synthesis_started) * 1000),
        )
        return transition_update(
            gstate,
            WorkflowPhase.SYNTHESIS,
            {
                "final_content": content,
                "synthesis_attempts": attempts_before + (2 if retried else 1),
                "synthesis_failed": synthesis_failed,
                "synthesis_degraded": synthesis_degraded,
                "answerability": answerability.to_dict(),
                "answer_complete": answer_complete,
                "answer_contract": answer_contract,
                "quality_assessment": {},
            },
        )

    async def node_quality_gate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = _require_session(gstate)
        quality_started = time.perf_counter()
        sync_execution_projection(session.state, gstate)
        content = str(gstate.get("final_content") or "").strip()
        judgement = dict(gstate.get("coverage_judgement") or {})
        evidence_records = [row for row in gstate.get("evidence_records") or [] if isinstance(row, dict)]
        issues: list[str] = []
        if not content:
            issues.append("no_content")
        if not bool(judgement.get("sufficient")):
            issues.append("coverage_gap")
        if not evidence_records:
            issues.append("no_usable_evidence")
        if bool(gstate.get("synthesis_failed")) and (not content or not evidence_records):
            issues.append("synthesis_failed")
        answerability = gstate.get("answerability")
        if isinstance(answerability, dict) and answerability and not bool(answerability.get("answerable")):
            issues.append("answerability_gap")
        if isinstance(gstate.get("answer_contract"), dict) and gstate.get("answer_contract"):
            completeness = dict((gstate.get("answer_contract") or {}).get("completeness") or {})
            if completeness and not bool(completeness.get("complete")):
                issues.append("answer_incomplete")
        citation_valid = True
        citation_reason = ""
        manager = session.ctx.citation_manager
        if manager is not None and content:
            citation_valid, citation_reason = manager.validate_citations(content)
            if not citation_valid:
                issues.append(citation_reason or "citation_validation_failed")
        blocking = bool(issues)
        degradation_issues = [
            item for item in ("coverage_gap", "synthesis_failed", "answerability_gap") if item in issues
        ]
        repairable = (
            not bool(gstate.get("fast_path"))
            and bool(content)
            and not citation_valid
            and int(gstate.get("synthesis_attempts") or 0) < 2
        )
        verdict = (
            "pass"
            if not issues
            else "partial"
            if content and evidence_records and issues == degradation_issues
            else "fail"
        )
        assessment = {
            "verdict": verdict,
            "issues": issues,
            "repairable": repairable,
            "suggested_action": "repair" if repairable else "",
            "grounding": bool(content and evidence_records and citation_valid),
            "citation_metrics": {
                "evidence_count": len(evidence_records),
                "finding_count": len(gstate.get("findings") or []),
                "citation_valid": citation_valid,
            },
            "answer_complete": bool(gstate.get("answer_complete")),
            "synthesis_mode": str(
                (gstate.get("synthesis_mode") or session.state.metadata.get("synthesis_mode") or "")
            ),
        }
        decision = {
            "action": (
                "repair_synthesis"
                if repairable
                else "finalize_success"
                if verdict == "pass"
                else "finalize_partial"
                if verdict == "partial"
                else "finalize_failure"
            ),
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
                    "quality_rejection": verdict == "fail",
                    "partial_delivery": verdict == "partial",
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
        note_stage_duration(
            session.state,
            "quality",
            int((time.perf_counter() - quality_started) * 1000),
        )
        return update

    async def node_finalize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import finalize_node

        session = _require_session(gstate)
        finalize_started = time.perf_counter()
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
                **(
                    session.state.metadata.get("control_plane")
                    if isinstance(session.state.metadata.get("control_plane"), dict)
                    else {}
                ),
                "control_plane_degraded": bool(
                    (session.state.metadata.get("control_plane") or {}).get(
                        "control_plane_degraded"
                    )
                ),
            },
        )
        result = await self.harness._phase_finalize(
            session.state,
            session.ctx.session_dir,
            success=outcome in {"success", "degraded_success"},
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
                    "control_plane": dict(session.state.metadata.get("control_plane") or {}),
                    "control_plane_degraded": bool(
                        (session.state.metadata.get("control_plane") or {}).get(
                            "control_plane_degraded"
                        )
                    ),
                    "latency": critical_path_summary(session.state.metadata),
                }
            )
        note_stage_duration(
            session.state,
            "finalize",
            int((time.perf_counter() - finalize_started) * 1000),
        )
        note_final_answer(session.state)
        if isinstance(result.metadata, dict):
            result.metadata["latency"] = critical_path_summary(session.state.metadata)
        session.result = result
        return termination

    @staticmethod
    def _control_plane_status(
        brief_source: str,
        supervisor_sources: list[str],
    ) -> dict[str, Any]:
        fallback_count = sum(
            1 for source in supervisor_sources if source != "structured_llm"
        )
        total = len(supervisor_sources)
        brief_fallback = brief_source != "structured_llm"
        fallback_rate = round(fallback_count / total, 4) if total else 0.0
        return {
            "brief_source": brief_source,
            "brief_fallback": brief_fallback,
            "supervisor_sources": list(supervisor_sources),
            "supervisor_structured_success": total - fallback_count,
            "supervisor_fallback_count": fallback_count,
            "fallback_rate": fallback_rate,
            "control_plane_degraded": brief_fallback or fallback_count > 0,
        }
