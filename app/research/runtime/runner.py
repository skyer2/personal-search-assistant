"""Dumb executor bridge between the canonical ResearchState graph and harness services."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, cast

from app.agent.harness.citations import SourceTier
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep
from app.research.assessment.delivery import assess_delivery
from app.research.assessment.evidence import assess_evidence
from app.research.assessment.execution_health import assess_execution_health
from app.research.assessment.progress import assess_progress
from app.research.control.runtime_policy import answerable_user_ask, decide_control
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
    note_substep_duration,
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
        attributes = dict(event_attributes(assessment))
        if key == "progress_assessment":
            attributes["dispatch_wave_id"] = int(state.get("dispatch_wave_id") or 0)
        _emit(
            session,
            event_type,
            phase=phase,
            status=str(assessment.get("status") or "unknown"),
            attributes=attributes,
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

    @staticmethod
    def _runtime_fingerprint() -> dict[str, Any]:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        def git(*args: str) -> str:
            try:
                return subprocess.check_output(["git", *args], cwd=root, text=True, timeout=2).strip()
            except (OSError, subprocess.SubprocessError):
                return "unknown"
        return {
            "git_sha": git("rev-parse", "HEAD"),
            "branch": git("branch", "--show-current"),
            "dirty": bool(git("status", "--porcelain")),
            "prompt_version": os.getenv("PROMPT_VERSION", "research-prompt.v1"),
            "eval_version": os.getenv("EVAL_VERSION", "blind-eval.v1"),
        }

    def _bootstrap_run(self, ctx: Any) -> tuple[str, dict[str, Any]]:
        """Resolve the profile before a session can create its hard budget manager."""
        bootstrap_started = time.perf_counter()
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
        from app.research.routing.intent_router import route_intent

        intent_route = route_intent(
            ctx.task_query,
            attachments=list(getattr(ctx, "attachments", None) or []),
        )
        metadata["intent_router"] = intent_route.to_dict()
        metadata["runtime_fingerprint"] = self._runtime_fingerprint()
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
        # Semantic repair is controlled by the configured semantic budget.
        # Never silently clamp it to one repair/two waves: execution failures
        # use their separate retry budget and must not spend this allowance.
        semantic_repairs = max(
            0, int(run_budget.get("max_replan_count", 1) or 0)
        )
        run_budget["max_replan_count"] = semantic_repairs
        run_budget["max_research_waves"] = max(
            1,
            int(
                run_budget.get("max_research_waves")
                or (semantic_repairs + 1)
            ),
        )
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
            max_research_waves=int(run_budget["max_research_waves"]),
        )
        metadata["run_budget"] = run_budget
        metadata["route_decision"] = decision.to_dict()
        try:
            note_stage_duration(
                ctx.state,
                "intent_router",
                int((time.perf_counter() - bootstrap_started) * 1000),
            )
        except Exception:
            logger.debug("intent router timing unavailable", exc_info=True)
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
        payload["intent"] = dict(session.state.metadata.get("intent_router") or {})
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

        started = time.perf_counter()
        try:
            brief = await compile_structured_brief_with_llm(
                session.ctx.task_query,
                agent=getattr(self.harness, "control_agent", None),
                budget_manager=session.budget_manager,
                conversation_delta=str(getattr(session.ctx, "conversation_summary", "") or ""),
                session_id=session.session_id,
            )
        except BaseException:
            elapsed = int((time.perf_counter() - started) * 1000)
            note_substep_duration(
                session.state,
                "understand",
                "llm_request",
                elapsed,
                input_size=len(session.ctx.task_query),
                status="error",
            )
            note_stage_duration(session.state, "brief", elapsed)
            note_stage_duration(session.state, "understand", elapsed)
            raise
        elapsed = int((time.perf_counter() - started) * 1000)
        note_substep_duration(
            session.state,
            "understand",
            "llm_request",
            elapsed,
            input_size=len(session.ctx.task_query),
            output_size=len(str(brief.to_dict() if brief else "")),
        )
        note_stage_duration(session.state, "brief", elapsed)
        note_stage_duration(session.state, "understand", elapsed)
        return brief

    async def node_brief(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import FastPathEligibility, StructuredResearchBrief
        from app.research.brief.validator import validate_structured_brief
        from app.research.runtime.graph import brief_node

        session = _require_session(gstate)
        brief_started = time.perf_counter()
        sync_execution_projection(session.state, gstate)
        state = dict(gstate)
        context_started = time.perf_counter()
        if not isinstance(state.get("brief"), dict) or not state.get("brief"):
            brief = await self._compile_initial_brief(session)
            state["brief"] = brief.to_dict()
        note_substep_duration(
            session.state,
            "understand",
            "context_build",
            int((time.perf_counter() - context_started) * 1000),
            input_size=len(str(gstate)),
            output_size=len(str(state.get("brief") or "")),
        )
        parse_started = time.perf_counter()
        update = brief_node(cast(ResearchState, state))
        plan_build_ms = int((time.perf_counter() - parse_started) * 1000)
        note_stage_duration(session.state, "brief_plan", plan_build_ms)
        brief = StructuredResearchBrief.from_dict(update.get("brief"))
        note_substep_duration(
            session.state,
            "understand",
            "parse",
            int((time.perf_counter() - parse_started) * 1000),
            output_size=len(str(update.get("brief") or "")),
        )
        topology_started = time.perf_counter()
        eligibility = FastPathEligibility.from_brief(brief)
        issues = validate_structured_brief(brief)
        from app.research.intent import UserAskContract, evaluate_semantic_fidelity

        contract = UserAskContract(
            contract_id=f"asks_{brief.brief_id}",
            raw_query=brief.raw_query or session.ctx.task_query,
            asks=brief.user_asks,
        )
        fidelity = evaluate_semantic_fidelity(contract, brief)
        update["user_ask_contract"] = contract.to_dict()
        update["semantic_fidelity"] = fidelity.to_dict()
        if not fidelity.passed:
            issues = [*issues, "semantic_fidelity_failed"]
        plan_semantic_validation: dict[str, Any] = {}
        planner_enabled = bool(
            getattr(self.harness.harness_config, "planner_llm_enabled", True)
        )
        if not eligibility.eligible and fidelity.passed and planner_enabled:
            from app.research.planning.semantic_planner import SemanticPlanner
            from app.research.runtime.graph import _plan_from_tasks
            from app.research.planning.brief_plan import (
                build_brief_and_plan,
                validate_brief_plan,
            )

            semantic_planner = SemanticPlanner(
                getattr(self.harness, "control_agent", None),
                session.budget_manager,
            )
            initial_action, validation = await semantic_planner.plan(
                brief,
                findings=[],
                budget=dict(state.get("budget") or {}),
                previous_fingerprints=set(),
            )
            plan_semantic_validation = validation.to_dict()
            if validation.passed and initial_action.research_tasks:
                semantic_plan = _plan_from_tasks(
                    list(initial_action.research_tasks),
                    plan_version=int(state.get("plan_version") or 1),
                    planning_mode="semantic_planner",
                )
                update.update(
                    {
                        "plan": semantic_plan.to_dict(),
                        "tasks": initialize_tasks(semantic_plan),
                        "plan_validation": validate_brief_plan(
                            semantic_plan, brief=brief
                        ),
                        "brief_plan": build_brief_and_plan(
                            brief, plan_version=semantic_plan.plan_version
                        ).to_dict(),
                        "dispatch_wave_id": 1,
                        "plan_semantic_validation": plan_semantic_validation,
                    }
                )
        note_substep_duration(
            session.state,
            "understand",
            "validate",
            int((time.perf_counter() - topology_started) * 1000),
            output_size=len(issues),
        )
        if eligibility.eligible:
            session.active_wave_size = 1
            # The fast path builds its bounded lookup plan inside
            # ``brief_node``.  Keep that deterministic work visible as a
            # separate plan stage instead of folding it into topology.
            note_stage_duration(
                session.state,
                "plan",
                int((time.perf_counter() - topology_started) * 1000),
            )
        else:
            # The initial research wave is now produced from the brief in a
            # deterministic pass.  Supervisor is reserved for an actionable
            # repair after gap precheck.
            note_stage_duration(
                session.state,
                "plan",
                int((time.perf_counter() - topology_started) * 1000),
            )
        plan_validation = update.get("plan_validation") or []
        note_stage_duration(
            session.state,
            "plan_validate",
            int((time.perf_counter() - topology_started) * 1000),
        )
        if isinstance(session.state.metadata, dict):
            session.state.metadata["plan_validation"] = {
                "passed": not bool(plan_validation),
                "issues": list(plan_validation),
                "source": "deterministic",
            }
            if isinstance(update.get("brief_plan"), dict):
                session.state.metadata["brief_plan"] = dict(update["brief_plan"])
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
                    "user_ask_contract": contract.to_dict(),
                    "semantic_fidelity": fidelity.to_dict(),
                    "original_ask_count": len(brief.user_asks),
                    "research_question_count": len(brief.research_questions),
                    "control_plane": {
                        "brief_source": brief.compiler_source,
                        "brief_fallback": brief.compiler_source != "structured_llm",
                        "supervisor_sources": [],
                        "supervisor_structured_success": 0,
                        "supervisor_fallback_count": 0,
                        "fallback_rate": 0.0,
                        "control_plane_degraded": brief.compiler_source != "structured_llm",
                        "plan_source": "brief_and_plan",
                        "plan_validation_passed": not bool(plan_validation),
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
                "brief_source": brief.compiler_source,
                "brief_fallback_reason": (
                    "" if brief.compiler_source == "structured_llm" else "deterministic_fallback"
                ),
                "original_ask_count": len(brief.user_asks),
                "research_question_count": len(brief.research_questions),
                "ask_ids": [ask.ask_id for ask in brief.user_asks],
                "semantic_fidelity_score": fidelity.score,
                "semantic_fidelity_passed": fidelity.passed,
                "semantic_fidelity_issues": list(fidelity.issues),
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
        if isinstance(update.get("plan"), dict):
            from app.agent.harness.state import ExecutionPlan

            initial_plan = ExecutionPlan.from_dict(update["plan"])
            _emit(
                session,
                "plan.created",
                phase=WorkflowPhase.BRIEF.value,
                status="ok" if not plan_validation else "warning",
                plan_version=initial_plan.plan_version,
                attributes={
                    **plan_event_attributes(
                        initial_plan,
                        brief.to_dict(),
                        run_id=session.run_id,
                        planner_source="brief_and_plan",
                    ),
                    "validation_issues": list(plan_validation),
                },
            )
        note_stage_duration(
            session.state,
            "brief",
            int((time.perf_counter() - brief_started) * 1000),
        )
        note_stage_duration(
            session.state,
            "understand",
            int((time.perf_counter() - brief_started) * 1000),
        )
        note_stage_duration(
            session.state,
            "topology",
            int((time.perf_counter() - topology_started) * 1000),
        )
        return update

    async def node_supervisor(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.brief.models import StructuredResearchBrief
        from app.research.coverage.judge import CoverageJudgement
        from app.research.control.gap_precheck import precheck_gap
        from app.research.runtime.admission import admit_dispatch
        from app.research.supervisor.agent import SupervisorAgent
        from app.research.supervisor.models import ResearchTaskRequest

        session = _require_session(gstate)
        supervisor_started = time.perf_counter()
        sync_execution_projection(session.state, gstate)
        context_started = time.perf_counter()
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
        gap_started = time.perf_counter()
        harness_config = getattr(self.harness, "harness_config", None)
        configured_repairs = max(
            0,
            int(getattr(harness_config, "max_replan_count", 1) or 0),
        )
        gap_precheck = precheck_gap(
            state,
            max_waves=2,
            max_repairs=configured_repairs,
        )
        note_stage_duration(
            session.state,
            "gap_precheck",
            int((time.perf_counter() - gap_started) * 1000),
        )
        note_substep_duration(
            session.state,
            "supervisor",
            "context_build",
            int((time.perf_counter() - context_started) * 1000),
            input_size=len(str(gstate)),
            output_size=len(findings),
        )
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
        from app.research.domain.research_budget import (
            is_execution_recovery_pass,
            worker_failures_by_type,
        )

        # Execution recovery must not spend a semantic research wave.
        execution_recovery = is_execution_recovery_pass(state)
        previous_iteration = int((state.get("supervisor") or {}).get("iteration") or 0)
        semantic_iteration = previous_iteration + (
            1 if state.get("plan") and not execution_recovery else 0
        )
        execution_retries = int(state.get("execution_retries") or 0) + (
            1 if execution_recovery else 0
        )

        supervisor = SupervisorAgent(getattr(self.harness, "control_agent", None), session.budget_manager)
        plan_semantic_validation: dict[str, Any] = {}
        llm_started = time.perf_counter()
        if gap_precheck.action == "RESEARCH_COMPLETE":
            from app.research.supervisor.models import SupervisorAction

            action = SupervisorAction(
                "COMPLETE",
                f"gap_precheck:{gap_precheck.reason}",
                (),
                "deterministic_gap_precheck",
            )
        elif gap_precheck.action in {"STOP_BUDGET_PARTIAL", "STOP_FAILURE"}:
            from app.research.supervisor.models import SupervisorAction

            action = SupervisorAction(
                gap_precheck.action,
                f"gap_precheck:{gap_precheck.reason}",
                (),
                "deterministic_gap_precheck",
            )
        elif gap_precheck.action == "RETRY_EXECUTION":
            from app.research.supervisor.models import SupervisorAction

            action = SupervisorAction(
                "CONDUCT_RESEARCH",
                f"gap_precheck:{gap_precheck.reason}",
                (),
                "deterministic_execution_retry",
            )
        else:
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
                budget=budget,
            )
        note_substep_duration(
            session.state,
            "supervisor",
            "llm_request",
            int((time.perf_counter() - llm_started) * 1000),
            input_size=len(str(brief.to_dict())) + len(str(findings[:3])),
            output_size=len(str(action.to_dict() if action else "")),
        )
        runtime_decision_started = time.perf_counter()
        payload: dict[str, Any] = {
            "execution_retries": execution_retries,
            "semantic_repairs": semantic_iteration,
            "worker_failures_by_type": worker_failures_by_type(state),
            "plan_semantic_validation": plan_semantic_validation,
            "supervisor_action": action.to_dict(),
            "supervisor": {
                "iteration": semantic_iteration,
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
                "semantic_action": gap_precheck.action,
                "gap_precheck": gap_precheck.to_dict(),
            },
            "gap_precheck": gap_precheck.to_dict(),
        }
        raw_iteration_limit = state.get("budget", {}).get("max_replan_count")
        iteration_limit = 3 if raw_iteration_limit is None else max(0, int(raw_iteration_limit))
        # The deterministic initial wave never consumes a targeted-repair
        # allowance. `dispatch_wave_id=1` is that initial wave, so permit the
        # first Supervisor repair when the configured limit is one.
        completed_repair_waves = max(0, int(state.get("dispatch_wave_id") or 1) - 1)
        supervisor_iteration_exceeded = completed_repair_waves >= iteration_limit
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

                plan_started = time.perf_counter()
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
                            "ask_id": item.ask_id,
                            "question_id": item.question_id,
                            "target_criteria": list(item.target_criteria),
                            "target_gaps": list(item.target_gaps),
                            "criterion_id": item.criterion_id,
                            "question_id": item.question_id,
                            "hypothesis_id": item.hypothesis_id,
                            "gap_id": item.gap_id,
                            "missing_evidence_types": list(item.missing_evidence_types),
                            "blocking_conflict_ids": list(item.blocking_conflict_ids),
                            "objective": item.objective,
                            "expected_evidence": list(item.expected_evidence),
                            "coverage_keys": list(item.target_criteria),
                            "estimated_queries": min(5 if item.repair else 7, max(1, int(item.max_queries or 1))),
                            "max_queries": min(5 if item.repair else 7, max(1, int(item.max_queries or 1))),
                            "max_fetches": min(5 if item.repair else 7, max(1, int(item.max_fetches or 1))),
                            "source_hints": list(item.source_hints),
                            "source_strategy": [
                                "primary_source",
                                "independent_corroboration",
                                "counter_evidence",
                            ],
                            "evidence_needed": list(item.expected_evidence),
                            "counter_evidence_needed": (
                                ["counter-evidence or competing estimate"]
                                if str(brief.user_intent) in {
                                    "comparison", "trend_forecast", "conflict_analysis",
                                    "recommendation", "structured_report", "explanation",
                                }
                                else []
                            ),
                            "novelty_reason": item.novelty_reason,
                            "estimated_effort": item.estimated_effort,
                            "token_ceiling": (
                                budget_profile := task_budget_profile(item.estimated_effort)
                            ).token_ceiling,
                            "max_llm_calls": min(int(item.max_llm_calls or 4), (
                                max(
                                    budget_profile.max_llm_calls,
                                    session.worker_llm_call_limit(),
                                )
                                if session.search_mode == "deep_debug"
                                else budget_profile.max_llm_calls
                            )),
                            "repair": bool(item.repair or wave_id > 1),
                            "budget_stage": "repair" if bool(item.repair or wave_id > 1) else "research",
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
                from app.research.planning.bounded import split_task
                bounded_steps: list[PlanStep] = []
                for step in steps:
                    bounded_steps.extend(split_task(step))
                steps = bounded_steps
                plan_duration = int((time.perf_counter() - plan_started) * 1000)
                note_substep_duration(
                    session.state,
                    "plan",
                    "validate",
                    plan_duration,
                    input_size=len(approved_requests),
                    output_size=len(steps),
                )
                note_stage_duration(session.state, "plan", plan_duration)
                plan = ExecutionPlan(
                    steps=steps,
                    summary="Budget-approved Supervisor research action",
                    plan_version=int(state.get("plan_version") or 1) + (1 if state.get("plan") else 0),
                    planning_mode="supervisor_action",
                )
                expanded_ids = [step.resolved_task_id(index) for index, step in enumerate(plan.steps)]
                payload["dispatch_admission"] = {
                    **dict(payload.get("dispatch_admission") or {}),
                    "approved_task_ids": expanded_ids,
                }
                session.state.plan = plan
                session.active_wave_size = len(steps)
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
        # The visible Supervisor decision must describe the action that the
        # runtime can actually execute.  Leaving CONDUCT_RESEARCH in the
        # trace after admission rejects every task made partial delivery look
        # like a contradictory control-plane failure.
        if action.action == "CONDUCT_RESEARCH":
            admission_payload = payload.get("dispatch_admission")
            admission_dict = admission_payload if isinstance(admission_payload, dict) else {}
            if not admission_dict.get("approved_task_ids"):
                from app.research.supervisor.models import SupervisorAction

                denied = admission_dict.get("denied_reason")
                reason = next(iter(denied.values()), "runtime_dispatch_unavailable") if isinstance(denied, dict) else "runtime_dispatch_unavailable"
                action = SupervisorAction("COMPLETE", f"runtime_admission:{reason}", (), "runtime_admission")
                payload["supervisor_action"] = action.to_dict()
                payload["supervisor"] = {
                    **dict(payload.get("supervisor") or {}),
                    "last_action": action.action,
                    "reasoning_summary": action.reason,
                    "source": action.source,
                    "runtime_admission_reason": reason,
                }
        decision = decide_control({**state, **payload})
        # A run with salvageable evidence but no repair budget is a partial
        # delivery.  Preserve that distinction even though the semantic
        # Supervisor action is COMPLETE.
        if (
            gap_precheck.action == "SYNTHESIZE"
            and gap_precheck.reason in {"repair_budget_unavailable", "max_research_waves", "repair_limit"}
            and not bool((state.get("coverage_judgement") or {}).get("sufficient"))
            and bool(state.get("evidence_records"))
        ):
            decision = type(decision)("deliver_partial", (gap_precheck.reason, "usable_evidence"), ())
        if action.action == "CONDUCT_RESEARCH":
            raw_admission = payload.get("dispatch_admission")
            admission_payload = raw_admission if isinstance(raw_admission, dict) else {}
            if admission_payload.get("approved_task_ids"):
                # Admission has already checked the hard budget and created
                # the replacement plan.  It is the authoritative runtime
                # decision for this Supervisor action: do not let the stale
                # task table or a generic iteration check convert it into
                # partial delivery before the approved repair is dispatched.
                decision = type(decision)(
                    "dispatch",
                    ("supervisor_conduct_research", "budget_admission"),
                    tuple(str(item) for item in admission_payload["approved_task_ids"]),
                )
            elif answerable_user_ask({**state, **payload}):
                decision = type(decision)(
                    "deliver_partial",
                    ("budget_stop", "answerable_user_ask", "no_approved_dispatch"),
                    (),
                )
            else:
                decision = type(decision)(
                    "finalize_failure",
                    ("budget_stop", "no_answerable_user_ask", "no_approved_dispatch"),
                    (),
                )
        control_decision = {
            "action": decision.action,
            "reason_codes": list(decision.reason_codes),
            "task_ids": list(decision.task_ids),
            "policy_version": "runtime-policy.v1",
            "semantic_action": gap_precheck.action,
            "runtime_action": decision.action,
            "override_reason": "" if gap_precheck.action == "TARGETED_RESEARCH" else gap_precheck.reason,
        }
        note_substep_duration(
            session.state,
            "supervisor",
            "runtime_decision",
            int((time.perf_counter() - runtime_decision_started) * 1000),
            input_size=len(str(payload)),
            output_size=len(str(control_decision)),
        )
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
                "semantic_action": gap_precheck.action,
                "override_reason": "" if gap_precheck.action == "TARGETED_RESEARCH" else gap_precheck.reason,
                "supervisor_calls_avoided": gap_precheck.supervisor_calls_avoided,
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
        from app.research.evidence.quality import source_quality_metrics
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
        source_metrics = source_quality_metrics(evidence_records)
        plan_metadata: dict[str, dict[str, Any]] = {}
        plan_raw = state.get("plan")
        if isinstance(plan_raw, dict):
            for index, step in enumerate(plan_raw.get("steps") or []):
                if isinstance(step, dict):
                    task_id = str(step.get("task_id") or f"step_{index}")
                    metadata = step.get("metadata")
                    if isinstance(metadata, dict):
                        plan_metadata[task_id] = dict(metadata)
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
            worker_results=[row for row in state.get("worker_results") or [] if isinstance(row, dict)],
            task_metadata=plan_metadata,
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
            "key_question_coverage": [item.to_dict() for item in judgement.key_question_coverage],
            "blocking_gap_count": sum(1 for item in judgement.key_question_coverage if item.blocking),
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
                "key_question_coverage": [row.to_dict() for row in judgement.key_question_coverage],
                "blocking_gap_count": sum(1 for row in judgement.key_question_coverage if row.blocking),
                "source_quality": source_metrics,
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
        note_stage_duration(
            session.state,
            "gap_check",
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
        current_task_raw = normalize_tasks(gstate.get("tasks")).get(task_id)
        current_task = dict(current_task_raw) if isinstance(current_task_raw, dict) else None
        attempt = int(cast(Any, (current_task or {}).get("attempt")) or 0) + 1
        if current_task is not None and current_task["execution_status"] != TaskExecutionStatus.PENDING.value:
            _emit(
                session,
                "recovery.decided",
                phase=WorkflowPhase.EXECUTE.value,
                status="duplicate",
                plan_version=int(gstate.get("plan_version") or 1),
                task_id=task_id,
                attempt=int(cast(Any, current_task.get("attempt")) or 0),
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
        result.metrics["dispatch_wave_id"] = dispatch_wave_id
        result.metrics["worker_attempt"] = attempt
        note_worker_durations(session.state, [result])
        # A worker fan-out has no single graph node duration.  Record the
        # worker wall time here; ``critical_path_summary`` later projects the
        # slowest worker per wave as the research stage wall time.
        note_stage_duration(
            session.state,
            "research",
            int(result.duration_ms or result.execution_ms or 0),
        )
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
        raw_payload = row.get("payload")
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        row["payload"] = {
            **payload,
            "findings": normalized_findings,
            "evidence_ids": list(
                payload.get("evidence_ids")
                or result.evidence_refs
                or []
            ),
            "artifact_ids": list(payload.get("artifact_ids") or []),
            "search_queries": list(payload.get("search_queries") or []),
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
            # A timed-out worker may have a source-backed raw artifact that
            # was intentionally not promoted to canonical evidence. Preserve
            # that execution fact as PARTIAL; it still cannot satisfy
            # Coverage or Completion without later verified evidence.
            salvageable_raw = bool(
                result.evidence_refs
                and (
                    result.sources
                    or any(
                        isinstance(item, dict) and (item.get("sources") or item.get("source"))
                        for item in result.findings
                    )
                )
            )
            if admitted_evidence_count == 0 and not salvageable_raw:
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
        transitioned_task = tasks.get(task_id)
        transitioned_attempt = int(
            cast(Any, dict(transitioned_task).get("attempt")) or 0
        ) if isinstance(transitioned_task, dict) else 0
        _emit(
            session,
            "task.transitioned",
            phase=WorkflowPhase.EXECUTE.value,
            status=execution_status.value,
            task_id=task_id,
            attempt=transitioned_attempt,
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
        )
        from app.research.delivery.insight_synthesis import (
            build_insight_synthesis,
        )
        from app.research.delivery.answer_renderer import render_answer
        from app.research.delivery.answer_view_builder import (
            build_partial_answer_view,
            build_recovery_view,
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
            # The fast path still owes the Completion Contract a typed answer so
            # terminal state never depends on a quality-verdict shortcut.
            fast_brief = StructuredResearchBrief.from_dict(gstate.get("brief") or {})
            # Citation source ids are manager-local; the Completion Contract
            # validates against evidence-record ids, so resolve to locators.
            locator_by_source = {
                str(getattr(source, "source_id", "") or ""): str(getattr(source, "locator", "") or "")
                for source in (list(getattr(manager, "sources", []) or []) if manager is not None else [])
            }
            evidence_refs: list[str] = []
            for item in answer.supporting_source_ids or payload.get("evidence_ids") or []:
                key = str(item).strip()
                if not key:
                    continue
                evidence_refs.append(locator_by_source.get(key) or key)
            evidence_refs = list(dict.fromkeys(evidence_refs))
            fast_contract = {
                "objective": fast_brief.objective,
                "synthesis_mode": "atomic_fact_structured",
                "answers": [
                    {
                        "question_id": "q1",
                        "ask_id": fast_brief.ask_id_for_question_index(1),
                        "direct_answer": answer.answer if answer.sufficient else "",
                        "evidence_refs": evidence_refs,
                        "finding_refs": [],
                        "confidence": 0.9 if answer.sufficient else 0.0,
                        "claim_type": "fact",
                    }
                ],
            }
            return transition_update(
                gstate,
                WorkflowPhase.SYNTHESIS,
                {
                    "final_content": content,
                    "synthesis_attempts": 1,
                    "synthesis_failed": not answer.sufficient,
                    "answer_contract": fast_contract,
                    "quality_assessment": {},
                },
            )
        attempts_before = int(gstate.get("synthesis_attempts") or 0)
        compact = attempts_before >= 1
        report_repair = attempts_before >= 2
        decision = dict(gstate.get("control_decision") or {})
        _emit(
            session,
            "control.decided",
            phase=WorkflowPhase.SYNTHESIS.value,
            status=str(decision.get("action") or "synthesize"),
            attributes=control_decision_event_attributes(decision or {"action": "synthesize"}),
        )
        context_started = time.perf_counter()
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
            "report_repair"
            if report_repair
            else "degraded"
            if compact
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
            limitations=[
                *list(judgement.get("missing") or []),
                *(
                    [
                        "报告修复必须解决："
                        + ", ".join(
                            str(item)
                            for item in (gstate.get("quality_assessment") or {}).get("issues", [])
                        )
                    ]
                    if report_repair
                    else []
                ),
            ],
            unresolved_conflicts=list(judgement.get("conflicts") or []),
            compact=compact,
        )
        note_substep_duration(
            session.state,
            "synthesis",
            "evidence_select",
            int((time.perf_counter() - context_started) * 1000),
            input_size=len(str(gstate)),
            output_size=len(synthesis_context.findings),
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
        # The report contract must remain self-contained when a caller does
        # not install a CitationManager (for example the standalone live
        # evaluator).  Preserve registered numbers, then assign stable local
        # numbers to the remainder so every visible evidence binding can be
        # resolved in the report and Trace.
        next_citation_number = max(citation_numbers.values(), default=0) + 1
        for record in evidence_records:
            evidence_id = str(record.get("evidence_id") or "").strip()
            if evidence_id and evidence_id not in citation_numbers:
                citation_numbers[evidence_id] = next_citation_number
                next_citation_number += 1
        pack_started = time.perf_counter()
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
        insight_synthesis = build_insight_synthesis(
            findings=list(evidence_pack.findings),
            evidence_records=evidence_records,
            citation_numbers=citation_numbers,
            limitations=list(judgement.get("missing") or []),
        )
        insight_layer = {
            "signals": [row.to_dict() for row in insight_synthesis.signals],
            "mechanisms": [row.to_dict() for row in insight_synthesis.mechanisms],
        }
        digests = [
            replace(
                digest,
                citation_number=int(citation_numbers.get(str(digest.evidence_id), 0) or 0),
            )
            for digest in selected_digests(evidence_pack, compact_pack=compact)
        ]
        note_substep_duration(
            session.state,
            "synthesis",
            "evidence_pack",
            int((time.perf_counter() - pack_started) * 1000),
            output_size=len(evidence_pack.findings) + len(evidence_pack.evidence_refs),
        )
        prompt_started = time.perf_counter()
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
            insight_signals=list(insight_layer["signals"]),
            insight_mechanisms=list(insight_layer["mechanisms"]),
            insight_cards=[row.to_dict() for row in insight_synthesis.insight_cards],
            forecast_cards=[row.to_dict() for row in insight_synthesis.forecast_cards],
            claim_evidence_bindings=[row.to_dict() for row in insight_synthesis.bindings],
            coverage_summary={
                "sufficient": bool(judgement.get("sufficient")),
                "missing": list(judgement.get("missing") or []),
            },
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
        note_substep_duration(
            session.state,
            "synthesis",
            "prompt_build",
            int((time.perf_counter() - prompt_started) * 1000),
            output_size=request.pack_tokens_estimated,
        )
        digest_ready = validate_synthesis_digests(
            evidence_pack.findings,
            evidence_pack.evidence_refs,
            digests,
            evidence_records,
        )
        _emit(
            session,
            "synthesis.report_repair.started" if report_repair else "synthesis.compact.started" if compact else "synthesis.primary.started",
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
        result: Any
        if skip_llm_synthesis:
            partial_view = build_partial_answer_view(
                brief=brief,
                claims=[
                    row.to_dict() for row in insight_synthesis.claims
                ],
                bindings=[
                    row.to_dict() for row in insight_synthesis.bindings
                ],
                coverage=judgement,
                source_registry=evidence_records,
                limitations=list(synthesis_context.limitations),
            )
            content = render_partial_delivery(partial_view)
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
            provider_started = time.perf_counter()
            span_key = self._start_synthesis_span(
                session,
                attempt=request.attempt,
                mode=request.mode,
                attributes={
                    "pack_tokens_estimated": request.pack_tokens_estimated,
                    "prompt_tokens_estimated": self._estimate_synthesis_input_tokens(executor, request, context),
                },
            )
            if report_repair:
                # A report repair is presentation-only.  It must not spend a
                # second long provider window after primary/compact attempts.
                import inspect

                timeout = min(30.0, float(session.synthesis_timeout_sec()))
                supports_timeout = "timeout_sec" in inspect.signature(executor.execute).parameters
                result = await (
                    executor.execute(request, context, timeout_sec=timeout)
                    if supports_timeout
                    else executor.execute(request, context)
                )
            else:
                # Do not pass an explicit ``None``: lightweight deterministic
                # executors used by integrations intentionally expose the
                # historical two-argument interface.
                result = await executor.execute(request, context)
            self._end_synthesis_span(
                session, span_key, status="ok" if result.ok else "failed",
                duration_ms=result.duration_ms,
            )
            provider_duration_ms = int((time.perf_counter() - provider_started) * 1000)
            provider_metadata = dict(getattr(result, "metadata", {}) or {})
            note_substep_duration(
                session.state,
                "synthesis",
                "provider_queue",
                int(provider_metadata.get("provider_queue_ms") or 0),
                status="ok" if result.ok else "error",
            )
            note_substep_duration(
                session.state,
                "synthesis",
                "generation",
                int(provider_metadata.get("generation_ms") or provider_duration_ms),
                status="ok" if result.ok else "error",
                model=str(provider_metadata.get("model") or ""),
                tokens=int(provider_metadata.get("actual_output_tokens") or 0),
            )
            note_substep_duration(
                session.state,
                "synthesis",
                "ttft",
                int(provider_metadata.get("ttft_ms") or 0),
                status="ok" if result.ok else "error",
            )
        synthesis_metadata = dict(getattr(result, "metadata", {}) or {})
        note_substep_duration(
            session.state,
            "synthesis",
            "parse",
            int(synthesis_metadata.get("parse_ms") or 0),
            status="ok" if result.ok else "error",
            output_size=len(str(getattr(result, "summary", "") or "")),
        )
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
            and not report_repair
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
            compact_insight_synthesis = build_insight_synthesis(
                findings=list(compact_pack.findings),
                evidence_records=evidence_records,
                citation_numbers=citation_numbers,
                limitations=list(judgement.get("missing") or []),
            )
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
                    insight_cards=[row.to_dict() for row in compact_insight_synthesis.insight_cards],
                    forecast_cards=[row.to_dict() for row in compact_insight_synthesis.forecast_cards],
                    claim_evidence_bindings=[row.to_dict() for row in compact_insight_synthesis.bindings],
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
                retry_metadata = dict(getattr(result, "metadata", {}) or {})
                note_substep_duration(
                    session.state,
                    "synthesis",
                    "provider_queue",
                    int(retry_metadata.get("provider_queue_ms") or 0),
                    status="ok" if result.ok else "error",
                )
                note_substep_duration(
                    session.state,
                    "synthesis",
                    "generation",
                    int(retry_metadata.get("generation_ms") or result.duration_ms or 0),
                    status="ok" if result.ok else "error",
                    model=str(retry_metadata.get("model") or ""),
                    tokens=int(retry_metadata.get("actual_output_tokens") or 0),
                )
                note_substep_duration(
                    session.state,
                    "synthesis",
                    "ttft",
                    int(retry_metadata.get("ttft_ms") or 0),
                    status="ok" if result.ok else "error",
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
        # A coverage label or a partial worker does not degrade synthesis by
        # itself. Degraded delivery means a retry/recovery was needed (or a
        # blocking conflict forced degraded mode).
        synthesis_degraded = retried or fallback or has_blocking_conflict
        successful_attempt = attempts_before + (2 if retried else 1) if not fallback else 0
        successful_pack_tokens = (
            compact_pack.token_budget if retried else evidence_pack.token_budget
        ) if not fallback else 0
        synthesis_failed = fallback
        answer_complete = False
        answer_contract: dict[str, Any] = {}
        recovery_mode = ""
        # Answerability is deliberately independent from the Coverage label.
        # A conservative coverage judge may leave a gap (for example, a
        # missing criterion binding) even though every user question has
        # grounded findings and evidence. In that case recovery must still
        # produce a direct answer instead of an evidence dump.
        # Always compile the evidence-bound contract on the recovery path: the
        # Completion Contract needs a typed answer to decide PARTIAL vs FAILED,
        # and unanswerable questions render as explicit refusals, not filler.
        if fallback:
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
                # A repair can only fix the write-up. When answerability itself
                # is short on evidence, rewriting the report changes nothing.
                "repair_eligible": bool(answerability.answerable),
            }
            _emit(
                session,
                "answer_recovery.started",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="start",
                attributes={"mode": "evidence_bound_recovery"},
            )
            if answer_complete:
                recovery_mode = "evidence_bound_recovery"
                recovery_view = build_recovery_view(
                    brief=brief,
                    claims=[
                        row.to_dict() for row in insight_synthesis.claims
                    ],
                    bindings=[
                        row.to_dict() for row in insight_synthesis.bindings
                    ],
                    coverage=judgement,
                    source_registry=evidence_records,
                    limitations=list(synthesis_context.limitations),
                )
                content = render_answer(recovery_view)
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
                    attributes={"mode": "evidence_bound_recovery", "answer_complete": False},
                )
            _emit(
                session,
                "answer_completeness.assessed",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="pass" if completeness.complete else "fail",
                attributes=completeness.to_dict(),
            )
        if fallback:
            partial_view = build_partial_answer_view(
                brief=brief,
                claims=[
                    row.to_dict() for row in insight_synthesis.claims
                ],
                bindings=[
                    row.to_dict() for row in insight_synthesis.bindings
                ],
                coverage=judgement,
                source_registry=evidence_records,
                limitations=list(synthesis_context.limitations),
            )
            content = render_partial_delivery(partial_view)
            recovery_mode = "evidence_bound_recovery"
        else:
            content = content if recovery_mode else result.summary
            # Provider synthesis is a free-form report today; a non-empty,
            # grounded result under sufficient coverage satisfies the delivery
            # contract while structured recovery carries the full answer model.
            answer_complete = bool(content.strip() and answerability.answerable)
        content = scrub_internal_ids(content)
        manager = session.ctx.citation_manager
        citation_started = time.perf_counter()
        explicit_citation_render = recovery_mode == "evidence_bound_recovery"
        if manager is not None and content and not explicit_citation_render:
            selected_findings = list(
                (compact_pack if retried else evidence_pack).findings
            )
            content = manager.inject_finding_citations(
                content,
                selected_findings,
                citation_numbers,
            )
            content = manager.build_cited_report(content)
        # A provider can return content with citations but no closed References
        # block (or this run may not install CitationManager). Recover through
        # the explicit ViewModel instead of asking the provider to rewrite it.
        from app.research.delivery.answer_renderer import validate_reference_closure

        closure = validate_reference_closure(content)
        if not closure.passed and answerability.answerable:
            recovered = compile_deterministic_answer(
                objective=brief.objective or str(gstate.get("task_query") or ""),
                brief=brief,
                findings=[
                    row
                    for row in gstate.get("findings") or []
                    if isinstance(row, dict)
                ],
                answerability=answerability,
                synthesis_degraded=True,
            )
            completeness = assess_answer_completeness(recovered, brief)
            recovery_mode = "evidence_bound_recovery"
            answer_contract = {
                "final_answer": recovered.to_dict(),
                "completeness": completeness.to_dict(),
                "repair_eligible": False,
            }
            recovery_view = build_recovery_view(
                brief=brief,
                claims=[row.to_dict() for row in insight_synthesis.claims],
                bindings=[row.to_dict() for row in insight_synthesis.bindings],
                coverage=judgement,
                source_registry=evidence_records,
                limitations=list(synthesis_context.limitations),
            )
            content = render_answer(recovery_view)
            fallback = False
            synthesis_failed = False
            answer_complete = completeness.complete
            synthesis_degraded = True
            _emit(
                session,
                "answer_recovery.completed",
                phase=WorkflowPhase.SYNTHESIS.value,
                status="ok" if completeness.complete else "partial",
                attributes={
                    "mode": recovery_mode,
                    "reason": "reference_closure_failed",
                },
            )
        note_substep_duration(
            session.state,
            "synthesis",
            "citation_binding",
            int((time.perf_counter() - citation_started) * 1000),
            output_size=len(content),
        )
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
                    "fallback_used": "evidence_bound_recovery" if fallback else recovery_mode,
                    "answerability": answerability.to_dict(),
                    "answer_complete": answer_complete,
                    "answer_contract": answer_contract,
                    "synthesis_budget_low": skip_llm_synthesis,
                    "insight_layer": insight_layer,
                    "insight_synthesis": insight_synthesis.to_dict(),
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
                    "provider_failure_class": str(synthesis_metadata.get("provider_failure_class") or ""),
                    "retryable": bool(synthesis_metadata.get("retryable")),
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
                "fallback_action": recovery_mode or ("evidence_bound_recovery" if fallback else ""),
                "synthesis_degraded": synthesis_degraded,
                "synthesis_retry_count": int(retried),
                "successful_attempt": successful_attempt,
                "answer_complete": answer_complete,
                "synthesis_mode": recovery_mode or mode,
                "insight_signal_count": len(insight_layer["signals"]),
                "insight_mechanism_count": len(insight_layer["mechanisms"]),
                "insight_card_count": len(insight_synthesis.insight_cards),
                "forecast_card_count": len(insight_synthesis.forecast_cards),
                "claim_evidence_bindings": [row.to_dict() for row in insight_synthesis.bindings],
                "insight_metrics": dict(insight_synthesis.metrics),
                "source_upgrades": [row.to_dict() for row in insight_synthesis.source_upgrades],
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
                "insight_synthesis": insight_synthesis.to_dict(),
                "quality_assessment": {},
            },
        )

    async def node_quality_gate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.delivery.insights import insight_density
        from app.research.domain.completion import evaluate_completion
        from app.research.coverage.gap_check import gap_check
        from app.research.quality.gate import evaluate_report_quality
        session = _require_session(gstate)
        quality_started = time.perf_counter()
        sync_execution_projection(session.state, gstate)
        content = str(gstate.get("final_content") or "").strip()
        judgement = dict(gstate.get("coverage_judgement") or {})
        strict_quality_contract = not bool(gstate.get("fast_path")) and "key_question_coverage" in judgement
        evidence_records = [row for row in gstate.get("evidence_records") or [] if isinstance(row, dict)]
        issues: list[str] = []
        if not content:
            issues.append("no_content")
        answerability = gstate.get("answerability")
        if not (isinstance(answerability, dict) and answerability):
            # The gate must always have question→evidence lineage; derive it
            # rather than treating a missing projection as "nothing answered".
            from app.research.brief.models import StructuredResearchBrief as _Brief
            from app.research.delivery.answer_contract import assess_answerability

            answerability = assess_answerability(
                brief=_Brief.from_dict(gstate.get("brief") or {}),
                findings=[row for row in gstate.get("findings") or [] if isinstance(row, dict)],
                evidence_records=[row for row in gstate.get("evidence_records") or [] if isinstance(row, dict)],
                coverage=dict(gstate.get("coverage_judgement") or {}),
                conflicts=[row for row in gstate.get("claim_resolutions") or [] if isinstance(row, dict)],
            ).to_dict()
        answerable = bool(answerability.get("answerable")) if isinstance(answerability, dict) and answerability else False
        gap_result = gap_check(brief=gstate.get("brief") or {}, answerability=answerability if isinstance(answerability, dict) else {})
        answer_complete = bool(gstate.get("answer_complete"))
        if not bool(judgement.get("sufficient")) and not (answerable and answer_complete):
            issues.append("coverage_gap")
        if not evidence_records:
            issues.append("no_usable_evidence")
        if bool(gstate.get("synthesis_failed")) and (not content or not evidence_records):
            issues.append("synthesis_failed")
        if strict_quality_contract and isinstance(answerability, dict) and answerability and not bool(answerability.get("answerable")):
            issues.append("answerability_gap")
        answer_contract = dict(gstate.get("answer_contract") or {})
        insight_synthesis = dict(
            gstate.get("insight_synthesis")
            or session.state.metadata.get("insight_synthesis")
            or {}
        )
        brief_intent = str((gstate.get("brief") or {}).get("user_intent") or "")
        insight = insight_density(
            content=content,
            findings=[row for row in gstate.get("findings") or [] if isinstance(row, dict)],
            analytical=brief_intent in {"comparison", "trend_forecast", "conflict_analysis", "recommendation", "structured_report", "explanation"},
        )
        citation_valid = True
        citation_reason = ""
        manager = session.ctx.citation_manager
        if manager is not None and content:
            citation_valid, citation_reason = manager.validate_citations(content)
            if not citation_valid:
                issues.append(citation_reason or "citation_validation_failed")
        from app.research.delivery.answer_renderer import validate_reference_closure

        closure = validate_reference_closure(content)
        if not closure.passed:
            citation_valid = False
            if closure.missing:
                issues.append("reference_missing")
            if closure.orphan:
                issues.append("reference_orphan")
        # A free-form provider report carries no typed answer. Attribute it to
        # the questions that actually have grounded evidence so the Completion
        # Contract stays authoritative; ungrounded questions stay unanswered.
        if not answer_contract and content:
            statuses = answerability.get("question_status") if isinstance(answerability, dict) else []
            if not statuses and not strict_quality_contract:
                questions = list((gstate.get("brief") or {}).get("key_questions") or [])
                statuses = [
                    {
                        "question_id": f"q{index}",
                        "supporting_evidence": [
                            str(row.get("evidence_id") or "")
                            for row in evidence_records[:1]
                            if str(row.get("evidence_id") or "")
                        ],
                        "supporting_findings": [],
                    }
                    for index, _question in enumerate(questions or ["objective"], 1)
                ]
            answers = []
            for index, status in enumerate(statuses or [], 1):
                grounded = [str(item) for item in status.get("supporting_evidence") or [] if str(item).strip()]
                answers.append({
                    "question_id": str(status.get("question_id") or f"q{index}"),
                    "ask_id": str(status.get("ask_id") or ""),
                    "direct_answer": content if grounded else "",
                    "evidence_refs": grounded,
                    "finding_refs": list(status.get("supporting_findings") or []),
                    "confidence": 0.7 if grounded else 0.0,
                })
            if answers:
                answer_contract = {
                    "objective": str((gstate.get("brief") or {}).get("objective") or ""),
                    "answers": answers,
                }
        if strict_quality_contract:
            report_quality = evaluate_report_quality(
                content=content,
                brief=dict(gstate.get("brief") or {}),
                evidence_records=evidence_records,
                answer_contract=answer_contract,
                insight_metrics=dict(insight_synthesis.get("metrics") or {}),
            )
        else:
            from app.research.quality.gate import QualityGateResult
            report_quality = QualityGateResult("PASS", (), {"legacy_or_fast_path": True})
        issues.extend(report_quality.issues)
        broken_evidence_count = int(report_quality.metrics.get("broken_sentence_count") or 0)
        source_requirements = (
            dict((gstate.get("brief") or {}).get("source_requirements") or {})
            if isinstance((gstate.get("brief") or {}).get("source_requirements"), dict)
            else {}
        )
        primary_required = bool(source_requirements.get("primary_required"))
        # Coverage identifies research gaps and can trigger the one allowed
        # repair wave.  Once every key question has a direct, evidence-bound
        # answer, it is diagnostic rather than a second terminal authority.
        completion_coverage = (
            judgement
            if not (answerable and answer_complete)
            else {}
        )
        from app.research.brief.models import StructuredResearchBrief
        from app.research.evidence.source_tier import evaluate_source_quality
        from app.research.quality import evaluate_relevance

        brief_model = StructuredResearchBrief.from_dict(gstate.get("brief") or {})
        relevance = evaluate_relevance(
            brief=brief_model,
            answer_contract=answer_contract,
            final_content=content,
        )
        source_quality = evaluate_source_quality(
            evidence_records,
            require_core_support=bool(
                brief_model.freshness_requirements.required
                or brief_model.source_requirements.primary_required
            ),
        )
        # Only the partial-floor violations are hard blockers; "not every ask is
        # answered" is a degradation signal that downgrades SUCCESS to PARTIAL.
        if not relevance.passed:
            issues.extend(f"relevance:{reason}" for reason in relevance.blocking_reasons)
        if not source_quality.passed:
            issues.append(f"source_quality:{source_quality.reason or 'insufficient_tier'}")
        completion = evaluate_completion(
            brief=brief_model,
            answer_contract=answer_contract,
            evidence_records=evidence_records,
            final_content=content,
            citation_valid=citation_valid,
            coverage=completion_coverage if strict_quality_contract else {},
            broken_evidence_count=broken_evidence_count,
            minimum_high_authority_ratio=0.6 if strict_quality_contract and primary_required else 0.0,
            require_authoritative_per_question=strict_quality_contract and primary_required,
            source_quality_pass=source_quality.passed,
            relevance_pass=relevance.passed,
            relevance_partial_pass=relevance.partial_passed,
            source_quality_partial_pass=source_quality.partial_passed,
        )
        if completion.passed and report_quality.verdict == "PASS":
            answer_complete = True
        if isinstance(answer_contract, dict) and answer_contract:
            completeness = dict(answer_contract.get("completeness") or {})
            if completeness and not bool(completeness.get("complete")):
                issues.append("answer_incomplete")
        issues = list(dict.fromkeys(issues))
        blocking = bool(issues)
        # Relevance and source-tier failures are never mere degradation: an
        # off-topic or tier3-only report must not be delivered as PARTIAL pass.
        degradation_issues: list[str] = [
            item for item in ("coverage_gap", "synthesis_failed", "answerability_gap") if item in issues
        ]
        hard_relevance = {f"relevance:{reason}" for reason in relevance.partial_blocking_reasons}
        hard_source = set()
        if not source_quality.partial_passed:
            hard_source = {item for item in issues if item.startswith("source_quality:")}
        hard_issues = [item for item in issues if item in hard_relevance or item in hard_source]
        degradation_issues.extend(
            item
            for item in issues
            if (item.startswith("relevance:") or item.startswith("source_quality:"))
            and item not in hard_relevance
            and item not in hard_source
        )
        # Only a contract that carries its own completeness assessment can ask
        # for a synthesis repair; an attributed provider report must not trigger
        # an extra provider call.
        assessed_incomplete = (
            isinstance(answer_contract.get("completeness"), dict)
            and not bool((answer_contract.get("completeness") or {}).get("complete"))
            and bool(answer_contract.get("repair_eligible", True))
        )
        report_repairable = (
            report_quality.repairable
            and "weak_sources" not in set(report_quality.issues)
        )
        repairable = (
            not bool(gstate.get("fast_path"))
            # When the provider produced nothing, rewriting the report cannot
            # help; recovery already salvaged what the evidence supports.
            and not bool(gstate.get("synthesis_failed"))
            and bool(content)
            and int(gstate.get("synthesis_attempts") or 0) < 3
            and (
                not citation_valid
                or (bool(evidence_records) and assessed_incomplete)
                or report_repairable
            )
        )
        verdict = (
            "pass"
            if completion.passed and report_quality.verdict == "PASS" and not issues
            else "repairable"
            if repairable
            else "partial"
            if content
            and evidence_records
            and not hard_issues
            and completion.partial_contract.passed
            and issues == degradation_issues
            else "fail"
        )
        note_substep_duration(
            session.state,
            "quality",
            "validation",
            int((time.perf_counter() - quality_started) * 1000),
            input_size=len(content),
            output_size=len(issues),
            status="ok" if verdict == "pass" else "warning",
        )
        assessment = {
            "verdict": verdict,
            "issues": issues,
            "repairable": repairable,
                    "suggested_action": "repair_report" if repairable and answer_contract else "repair" if repairable else "",
            "grounding": bool(content and evidence_records and citation_valid),
            "citation_metrics": {
                "evidence_count": len(evidence_records),
                "finding_count": len(gstate.get("findings") or []),
                "citation_valid": citation_valid,
            },
            "answer_complete": bool(gstate.get("answer_complete")),
            "completion_contract": completion.to_dict(),
            "relevance": relevance.to_dict(),
            "source_quality": source_quality.to_dict(),
            "gap_check": gap_result.to_dict(),
            "synthesis_mode": str(
                (gstate.get("synthesis_mode") or session.state.metadata.get("synthesis_mode") or "")
            ),
            "insight_density": insight,
            "report_quality": report_quality.to_dict(),
            "quality_metrics": report_quality.metrics,
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
        update = transition_update(gstate, WorkflowPhase.QUALITY, {
            "quality_assessment": assessment,
            "answer_contract": answer_contract,
            "answer_complete": bool(answer_complete or completion.passed),
            "relevance_assessment": relevance.to_dict(),
            "source_quality": source_quality.to_dict(),
        })
        if isinstance(session.state.metadata, dict):
            session.state.metadata.update(
                {
                    "quality": assessment,
                    "quality_attempted": True,
                    "answer_grounded": bool(assessment.get("grounding")),
                    "control_decision": decision,
                    "quality_rejection": verdict == "fail",
                    "partial_delivery": verdict == "partial",
                    "gap_check": gap_result.to_dict(),
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
                    "execution_retries": int(gstate.get("execution_retries") or 0),
                    "semantic_repairs": int(gstate.get("semantic_repairs") or 0),
                    "worker_failures_by_type": dict(
                        gstate.get("worker_failures_by_type") or {}
                    ),
                }
            )
        control_plane = session.state.metadata.get("control_plane")
        control_plane_attributes = dict(control_plane) if isinstance(control_plane, dict) else {}
        _emit(
            session,
            "run.terminated",
            phase=WorkflowPhase.FINALIZE.value,
            status=outcome,
            attributes={
                **termination_event_attributes(termination_payload),
                "final_content_chars": len(session.state.final_content),
                **control_plane_attributes,
                "control_plane_degraded": bool(
                    control_plane_attributes.get("control_plane_degraded")
                ),
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
                    "brief_source": str(brief.get("compiler_source") or ""),
                    "brief_fallback_reason": (
                        ""
                        if str(brief.get("compiler_source") or "") == "structured_llm"
                        else "deterministic_fallback"
                    ),
                    "original_ask_count": len(brief.get("user_asks") or []),
                    "research_question_count": len(
                        brief.get("research_questions") or []
                    ),
                    "semantic_fidelity": dict(
                        gstate.get("semantic_fidelity") or {}
                    ),
                    "execution_retries": int(gstate.get("execution_retries") or 0),
                    "semantic_repairs": int(gstate.get("semantic_repairs") or 0),
                    "worker_failures_by_type": dict(
                        gstate.get("worker_failures_by_type") or {}
                    ),
                    "synthesis_mode": str(
                        (gstate.get("synthesis_mode") or
                         session.state.metadata.get("synthesis_mode") or "")
                    ),
                    "final_relevance_score": float(
                        (gstate.get("relevance_assessment") or {}).get(
                            "ask_answer_rate", 0.0
                        )
                    ),
                    "boilerplate_ratio": float(
                        (gstate.get("relevance_assessment") or {}).get(
                            "boilerplate_ratio", 0.0
                        )
                    ),
                    "completion_reason": str(
                        (
                            (gstate.get("quality_assessment") or {}).get(
                                "completion_contract"
                            )
                            or {}
                        ).get("failure_reason")
                        or termination_payload.get("reason")
                        or ""
                    ),
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
