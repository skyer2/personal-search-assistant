"""把 AgentHarness 领域服务接到 StateGraph：图是调度权威。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.agent.harness.planner import (
    auto_resolve_clarification,
    plan_to_editable_dict,
    should_request_plan_review,
)
from app.agent.harness.state import LoopState, Phase, StepStatus
from app.research.runtime.scheduler import annotate_plan_tasks, task_status_map
from app.research.runtime.state import empty_research_state

logger = logging.getLogger(__name__)

_SESSIONS: dict[str, "RunSession"] = {}


class RunSession:
    """进程内 handles：LoopState 不是 workflow checkpoint。

    resume / interrupt / plan / task_status 只存在 ResearchState（SQLite）。
    本对象只在一次 ainvoke 期间给 WorkerRuntime 和领域服务提供锁、stores、tracer。
    """

    def __init__(self, harness: Any, ctx: Any):
        self.harness = harness
        self.ctx = ctx
        self.state: LoopState = ctx.state
        self.run_id: str = (
            getattr(ctx, "run_id", None)
            or getattr(getattr(ctx, "state", None), "run_id", None)
            or ctx.session_id
        )
        self.session_id: str = ctx.session_id
        self.lock = ctx.lock
        if ctx.budget_manager is None:
            from app.agent.harness.run_budget import create_run_budget_manager

            metadata = getattr(ctx.state, "metadata", None) or {}
            run_budget = metadata.get("run_budget") if isinstance(metadata, dict) else None
            ctx.budget_manager = create_run_budget_manager(
                harness.harness_config,
                run_budget=run_budget if isinstance(run_budget, dict) else None,
                run_started=ctx.run_started,
            )
        self.budget_manager = ctx.budget_manager
        self.result: Any = None
        self.worker_sem = asyncio.Semaphore(self._resolve_max_workers())

    def _resolve_max_workers(self) -> int:
        """优先用 Adaptive Effort clamp 后的 run_budget；否则用 Hard Ceiling。"""
        hard = max(
            1, int(getattr(self.harness.harness_config, "max_parallel_workers", 3) or 3)
        )
        meta = getattr(self.state, "metadata", None) or {}
        budget = meta.get("run_budget") if isinstance(meta, dict) else None
        if isinstance(budget, dict) and budget.get("max_parallel_workers") is not None:
            return max(1, min(hard, int(budget["max_parallel_workers"])))
        return hard

    def refresh_worker_sem(self) -> int:
        """Plan 写入 run_budget 后刷新并发闸；返回实际并行度。"""
        n = self._resolve_max_workers()
        self.worker_sem = asyncio.Semaphore(n)
        return n


def get_session(run_id: str) -> RunSession | None:
    return _SESSIONS.get(run_id)


def bind_session(session: RunSession) -> None:
    _SESSIONS[session.run_id] = session


def drop_session(run_id: str) -> None:
    _SESSIONS.pop(run_id, None)


def _resolve_emergency_reason(
    session: RunSession, *, deadline_near: bool = False
) -> str:
    """Resolve degradation cause without defaulting to a wall deadline."""
    metadata = session.state.metadata if isinstance(session.state.metadata, dict) else {}
    assessment = metadata.get("progress_assessment")
    candidates = [
        str(metadata.get("budget_degrade_reason") or ""),
        str(assessment.get("budget_degrade_reason") or "")
        if isinstance(assessment, dict)
        else "",
        session.budget_manager.exhaustion_reason()
        if callable(getattr(session.budget_manager, "exhaustion_reason", None))
        else "",
    ]
    for candidate in candidates:
        if candidate.strip():
            return candidate.strip()
    if session.state.abort_reason:
        return str(session.state.abort_reason)
    if session.budget_manager.remaining_run_sec() <= 0:
        return "deadline_exceeded"
    if deadline_near:
        return "synthesis_time_reserve"
    return "budget_exhausted"


def _termination_causal_chain(reason: str, *, quality_attempted: bool) -> list[str]:
    chain = [f"budget_exhausted:{reason}", "force_synthesis"]
    if quality_attempted:
        chain.extend(["emergency_synthesis", "quality_evaluated"])
    else:
        chain.append("emergency_synthesis")
    chain.append("partial_finalize")
    return chain


class ResearchGraphRunner:
    """compiled graph.ainvoke + HITL interrupt 桥接到现有 HTTP coordinator。"""

    def __init__(self, harness: Any):
        self.harness = harness

    def compile(self, checkpointer: Any = None, profile: str = "agent"):
        from app.research.runtime.graph import compile_research_graph

        return compile_research_graph(
            checkpointer=checkpointer,
            runtime=self,
            profile=profile,
        )

    async def execute(self, ctx: Any, *, checkpointer: Any = None) -> Any:
        from langgraph.types import Command

        from app.research.routing.mode_router import budget_for_mode, canonicalize_mode

        session = RunSession(self.harness, ctx)
        bind_session(session)
        session.state.metadata["graph_runtime"] = True
        session.state.metadata["workflow_authority"] = "research_state"
        persist_loop = bool(
            getattr(self.harness.harness_config, "persist_loop_state", False)
        )
        # LoopState checkpoint 不再作为 Graph 的种子。仅当显式打开旧 persist 时保留 HITL 桥。
        if persist_loop and ctx.restored_full:
            waiting = dict((session.state.metadata or {}).get("hitl_waiting") or {})
            gate = str(waiting.get("gate_type") or "")
            if gate in {"clarification", "intent_clarification"}:
                session.state = await self.harness._maybe_intent_clarification(
                    session.state
                )
            elif gate == "plan_review":
                session.state = await self.harness._maybe_plan_hitl_review(
                    session.state
                )
        config = {
            "configurable": {"thread_id": session.run_id},
            "recursion_limit": 80,
        }
        profile = canonicalize_mode(getattr(ctx, "search_mode", "agent") or "agent")
        personal = getattr(self.harness.harness_config, "personal_search", None) or {}
        budget_cfg = budget_for_mode(profile, personal)
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
        from app.agent.harness.usage_tracker import (
            reset_current_budget_manager,
            set_current_budget_manager,
        )

        budget_context_token = set_current_budget_manager(session.budget_manager)
        try:
            graph = self.compile(
                checkpointer=checkpointer or await _default_checkpointer(),
                profile=profile,
            )
            mgr = session.budget_manager
            initial = await _initial_or_resume_payload(graph, payload, config, ctx)
            research_sec = mgr.remaining_for_research_sec()
            if research_sec <= 1.0:
                return await self._force_synthesis_then_finalize(
                    session, reason="synthesis_time_reserve"
                )
            try:
                result = await asyncio.wait_for(
                    _ainvoke_resilient(graph, initial, config),
                    timeout=research_sec,
                )
                while _has_interrupt(result):
                    remaining = max(1.0, mgr.remaining_for_research_sec())
                    resume_value = await self._bridge_interrupts(result, session)
                    result = await asyncio.wait_for(
                        _ainvoke_resilient(graph, Command(resume=resume_value), config),
                        timeout=remaining,
                    )
            except asyncio.TimeoutError:
                return await self._force_synthesis_then_finalize(
                    session, reason="synthesis_time_reserve"
                )
            if session.result is not None:
                return session.result
            return await self._complete_from_graph(session, result)
        finally:
            reset_current_budget_manager(budget_context_token)
            drop_session(session.run_id)

    async def _bridge_interrupts(
        self, result: dict[str, Any], session: RunSession
    ) -> Any:
        """图内 interrupt() 暂停后，用现有 coordinator 等前端 POST /resume。"""
        from app.agent.harness.hitl import hitl_coordinator
        from app.api.monitor import monitor

        payloads = _interrupt_payloads(result)
        if not payloads:
            return True
        item = payloads[0]
        coordinator_payload = dict(item.get("coordinator_payload") or item)
        gate_type = str(
            coordinator_payload.get("gate_type") or item.get("kind") or "step"
        )
        step_index = int(
            coordinator_payload.get("step_index", item.get("step_index", -1))
        )
        action_requests = list(coordinator_payload.get("action_requests") or [])
        review_configs = list(coordinator_payload.get("review_configs") or [])
        monitor.report_hitl_interrupt(
            session.session_id,
            action_requests,
            review_configs,
            step_index=step_index,
            gate_type=gate_type,
            editable=self.harness.harness_config.hitl_allow_edit,
        )
        await self.harness._persist_hitl_waiting(
            session.state,
            {
                "gate_type": gate_type,
                "step_index": step_index,
                "payload": coordinator_payload,
            },
        )
        try:
            decisions = await hitl_coordinator.wait_for_decisions(
                session.session_id,
                coordinator_payload,
                timeout_sec=self.harness.harness_config.hitl_timeout_sec,
            )
        except TimeoutError:
            await self.harness._clear_hitl_waiting(session.state)
            return {"_timeout": True, "kind": item.get("kind")}
        await self.harness._clear_hitl_waiting(session.state)
        return decisions

    async def _force_synthesis_then_finalize(
        self, session: RunSession, *, reason: str = ""
    ) -> Any:
        """Hard deadline hit mid-graph: skip remaining research and try synthesis."""
        from app.research.runtime.scheduler import (
            skip_pending_research,
            task_status_map,
        )

        state = session.state
        reason = str(reason or _resolve_emergency_reason(session))
        if isinstance(state.metadata, dict):
            state.metadata["force_synthesis"] = True
            state.metadata["budget_degrade_reason"] = reason
        if state.plan is not None:
            skip_pending_research(
                state.plan,
                task_status_map(state.plan),
                reason=reason,
                include_required=True,
                include_running=True,
            )
        # 尝试用剩余时间写终稿，而不是直接 partial dump
        gstate = {
            "run_id": session.run_id,
            "progress_assessment": dict(
                (state.metadata or {}).get("progress_assessment") or {}
            ),
            "findings": [],
        }
        synthesis_status = "not_started"
        try:
            # Emergency delivery is intentionally single-pass: produce the best
            # available partial answer instead of draining markdown/PDF pipelines.
            update = await self.node_synthesize(gstate)
            gstate.update(update or {})
            synthesis_status = (
                "partial_fast_path"
                if isinstance(state.metadata, dict)
                and state.metadata.get("emergency_synthesis")
                else "failed"
            )
        except Exception as exc:
            synthesis_status = "failed"
            self._record_emergency_failure(session, "synthesis", exc)
        quality_attempted = False
        try:
            remaining = session.budget_manager.remaining_run_sec()
        except Exception:
            remaining = 0.0
        quality_threshold = max(
            10.0,
            float(
                getattr(
                    self.harness.harness_config,
                    "fast_synthesis_threshold_sec",
                    45,
                )
                or 45
            ),
        )
        if remaining >= quality_threshold:
            quality_attempted = True
            try:
                update = await self.node_quality_gate(gstate)
                gstate.update(update or {})
            except Exception as exc:
                self._record_emergency_failure(session, "quality", exc)
        state.metadata["termination"] = {
            "status": "partial",
            "reason": reason,
            "stage": "quality" if quality_attempted else "synthesis",
            "research_completed": False,
            "synthesis_attempted": synthesis_status != "not_started",
            "synthesis_status": synthesis_status,
            "quality_attempted": quality_attempted,
            "origin_stage": "research",
            "detected_stage": "dispatch",
            "cause_event_id": str(
                (state.metadata or {}).get("budget_exhausted_event_id") or ""
            ),
            "causal_chain": _termination_causal_chain(
                reason, quality_attempted=quality_attempted
            ),
        }
        try:
            gstate["status"] = "partial"
            return await self.node_finalize(gstate)
        except Exception:
            from app.agent.harness.partial_report import render_partial_report

            if not str(state.final_content or "").strip():
                state.final_content = render_partial_report(
                    state=state,
                    abort_reason=reason,
                    assessment=dict(gstate.get("progress_assessment") or {}),
                )
            return await self._finalize_run(session, success=False)

    @staticmethod
    def _record_emergency_failure(
        session: RunSession, stage: str, exc: Exception
    ) -> None:
        """Never turn emergency-delivery failures into a false healthy trace."""
        logger.exception(
            "Emergency %s failed for run %s", stage, session.run_id, exc_info=exc
        )
        try:
            from app.observability import EventType, get_recorder

            get_recorder().emit(
                (
                    EventType.SYNTHESIS_FAILED
                    if stage == "synthesis"
                    else EventType.QUALITY_EVALUATED
                ),
                phase=stage,
                status="failed",
                attributes={
                    "failure.origin_stage": stage,
                    "failure.detected_stage": f"emergency_{stage}",
                    "error": repr(exc)[:1000],
                    "run_id": session.run_id,
                    "session_id": session.session_id,
                },
                run_id=session.run_id,
                session_id=session.session_id,
                trace_id=str(getattr(session.state, "trace_id", "") or ""),
            )
        except Exception:
            logger.exception(
                "Could not record emergency failure for run %s", session.run_id
            )

    async def _complete_from_graph(
        self, session: RunSession, graph_result: dict[str, Any]
    ) -> Any:
        ctx = session.ctx
        state = session.state
        if graph_result.get("status") == "aborted" or state.abort_reason:
            success = False
        else:
            success = True
        return await self._finalize_run(session, success=success)

    async def _finalize_run(self, session: RunSession, *, success: bool) -> Any:
        ctx = session.ctx
        state = session.state
        citation_manager = ctx.citation_manager
        if citation_manager and state.final_content:
            cited = citation_manager.build_cited_report(state.final_content)
            state.final_content = cited
            metrics = citation_manager.compute_metrics(cited)
            state.citation_coverage_rate = metrics["citation_coverage_rate"]
            state.hallucination_rate = metrics["hallucination_rate"]
            state.evidence_source_count = metrics["registered_sources"]
            state.numeric_citation_coverage = float(
                metrics.get("numeric_citation_coverage") or 0.0
            )
            citation_manager.save_evidence_json(ctx.run_dir, run_id=session.run_id)

        finalize_outcome = self.harness.validator.validate_finalize(
            state,
            ctx.session_dir,
            citation_manager=citation_manager,
            min_citation_coverage=self.harness.harness_config.citations_min_coverage_rate,
            deliverable_dir=ctx.deliverable_dir,
        )
        await self.harness._phase_validate(
            state,
            finalize_outcome,
            step_index=state.step_index,
            scope="finalize",
        )
        ok = (
            (finalize_outcome.passed or finalize_outcome.severity == "warning")
            and not state.abort_reason
            and success
        )
        result = await self.harness._phase_finalize(
            state,
            ctx.session_dir,
            success=ok,
            started_at=ctx.run_started,
            deliverable_dir=ctx.deliverable_dir,
        )
        session.result = result
        return result

    # --- nodes ---

    async def node_vanilla_agent(self, gstate: dict[str, Any]) -> dict[str, Any]:
        """对照实验：单 Worker + search/fetch/file，不跑 Brief/Plan/Progress。"""
        from app.agent.harness.state import ExecutionPlan, PlanStep
        from app.research.runtime.isolation import worker_row
        from app.research.runtime.project import apply_graph_to_loop
        from app.research.runtime.worker import (
            LangChainWorkerRuntime,
            ResearchContext,
            ResearchTask,
            WorkerResult,
        )

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        query = str(gstate.get("resolved_query") or session.ctx.task_query or "")
        from app.research.planning.policy import tools_for_sources

        step = PlanStep(
            step_type="research",
            description=query,
            objective=query,
            task_id="vanilla",
            allowed_tools=tools_for_sources(["web", "file"]),
        )
        session.state.plan = ExecutionPlan(
            summary="direct baseline",
            steps=[step],
            planning_mode="direct",
        )
        result = await LangChainWorkerRuntime(self.harness, session).execute(
            ResearchTask(
                task_id="vanilla",
                objective=query,
                step_type="research",
                step_index=0,
                description=query,
                allowed_tools=list(step.allowed_tools),
            ),
            ResearchContext(
                run_id=str(gstate.get("run_id") or session.run_id),
                query=query,
                user_id=session.ctx.user_id,
                tenant_id=session.ctx.tenant_id,
                project_id=session.ctx.project_id,
                session_id=session.session_id,
            ),
        )
        answer = result.summary or query
        if result.findings:
            bits = [
                str(item.get("summary") or "")
                for item in result.findings
                if isinstance(item, dict)
            ]
            bits = [b for b in bits if b]
            if bits:
                answer = "\n".join(bits[:8])
        session.state.final_content = answer
        session.state.phase = Phase.FINALIZE
        outcome = result.raw
        row = (
            worker_row("vanilla", step, result.ok, getattr(outcome, "result", None))
            if outcome is not None
            else {
                "task_id": "vanilla",
                "ok": result.ok,
                "summary": result.summary,
                "step_type": "research",
                "payload": {
                    "summary": result.summary,
                    "facts": result.facts,
                    "sources": result.sources,
                },
            }
        )
        return {
            "final_content": answer,
            "search_mode": "direct",
            "status": "completed" if result.ok else "aborted",
            "quality_passed": bool(result.ok),
            "progress": "vanilla",
            "plan": None,
            "findings": result.findings,
            "worker_results": [row],
            "evidence_refs": result.evidence_refs,
        }

    async def node_intent(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.project import apply_graph_to_loop, brief_from_intent

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        state = session.state
        ctx = session.ctx
        if gstate.get("intent"):
            from app.agent.harness.state import TaskIntent

            state.intent = TaskIntent.from_dict(gstate["intent"])
            needs = bool(gstate.get("needs_clarification"))
        else:
            try:
                session.state = await asyncio.wait_for(
                    self.harness._phase_understand(
                        state,
                        ctx.task_query,
                        bool(ctx.uploaded_prompt),
                    ),
                    timeout=max(
                        5.0,
                        float(
                            getattr(
                                self.harness.harness_config,
                                "understand_wall_budget_sec",
                                30,
                            )
                            or 30
                        ),
                    ),
                )
            except asyncio.TimeoutError:
                from app.agent.harness.planner import understand_task

                state.intent = understand_task(ctx.task_query)
                if isinstance(state.metadata, dict):
                    state.metadata["understand_timeout"] = True
                    state.metadata["understand_fallback"] = "rules"
            state = session.state
            needs = bool(
                self.harness.harness_config.hitl_enabled
                and self.harness.harness_config.planner_clarification_enabled
                and state.intent is not None
                and state.intent.needs_clarification
                and not state.intent.clarification_resolved
            )
            if needs and self.harness.harness_config.planner_clarification_auto_resolve:
                state.intent = auto_resolve_clarification(state.intent)
                needs = False
        intent_payload = state.intent.to_dict() if state.intent is not None else None
        brief = brief_from_intent(intent_payload)
        from app.research.routing.task_shape import (
            classify_task_shape,
            execution_profile_for_shape,
        )

        shape = classify_task_shape(ctx.task_query, brief)
        profile = execution_profile_for_shape(shape.shape)
        budget = dict(gstate.get("budget") or {})
        budget["max_parallel_workers"] = int(profile["parallel_workers"])
        existing_replan = budget.get("max_replan_count")
        existing_replan = (
            int(existing_replan)
            if existing_replan is not None
            else int(profile["max_replan_count"])
        )
        budget["max_replan_count"] = min(existing_replan, int(profile["max_replan_count"]))
        try:
            from app.observability import EventType, get_recorder
            from app.observability.events import new_id
            from app.observability.payload_store import get_payload_store

            recorder = get_recorder()
            if recorder.is_active and brief:
                brief_id = str(brief.get("brief_id") or f"brief_{new_id(8)}")
                brief = {**brief, "brief_id": brief_id}
                span_key = recorder.start_span(
                    "task.understand",
                    phase="understand",
                    attributes={"brief_id": brief_id},
                )
                store = get_payload_store()
                run_id = str(gstate.get("run_id") or session.session_id)
                ref = store.put(
                    run_id=run_id,
                    artifact_type="research_brief",
                    artifact_id=brief_id,
                    payload={
                        "objective": brief.get("objective"),
                        "entities": list(brief.get("entities") or []),
                        "dimensions": list(brief.get("dimensions") or []),
                        "depth": brief.get("depth"),
                        "freshness": brief.get("freshness"),
                        "deliverable": brief.get("deliverable"),
                        "prefer_primary": brief.get("prefer_primary"),
                        "constraints": brief.get("constraints"),
                        "success_criteria": brief.get("success_criteria"),
                    },
                )
                intent_obj = state.intent
                recorder.emit(
                    EventType.BRIEF_COMPILED,
                    phase="understand",
                    status="ok",
                    attributes={
                        "brief_id": brief_id,
                        "brief_version": 1,
                        "objective": str(brief.get("objective") or "")[:240],
                        "entities": list(brief.get("entities") or [])[:12],
                        "dimensions": list(brief.get("dimensions") or [])[:12],
                        "depth": brief.get("depth"),
                        "freshness": brief.get("freshness"),
                        "deliverable": brief.get("deliverable"),
                        "prefer_primary": brief.get("prefer_primary"),
                        "planner_source": (
                            getattr(intent_obj, "planner_source", None)
                            if intent_obj
                            else None
                        ),
                        "intent_confidence": (
                            getattr(intent_obj, "intent_confidence", None)
                            if intent_obj
                            else None
                        ),
                        "brief_ref": ref.ref,
                        "brief_hash": ref.sha256,
                    },
                    input_refs=[{"type": "user_query", "id": "query"}],
                    output_refs=[ref.to_dict()],
                )
                recorder.end_span(span_key, status="ok")
                if isinstance(session.state.metadata, dict):
                    session.state.metadata["brief_id"] = brief_id
                    session.state.metadata["brief_ref"] = ref.ref
        except Exception:
            import logging as _log

            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        return {
            "intent": intent_payload,
            "brief": brief,
            "needs_clarification": needs,
            "search_mode": "agent",
            "route_signals": [f"task_shape:{shape.shape.value}"],
            "budget": budget,
            "progress": "intent",
        }

    async def node_clarify(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from langgraph.types import interrupt

        session = _require_session(gstate)
        state = session.state
        if state.intent is None:
            return {"needs_clarification": False, "progress": "clarified"}
        if (
            not self.harness.harness_config.hitl_enabled
            or self.harness.harness_config.planner_clarification_auto_resolve
        ):
            state.intent = auto_resolve_clarification(state.intent)
            return {
                "intent": state.intent.to_dict(),
                "needs_clarification": False,
                "progress": "clarified",
            }
        payload = _clarification_payload(self.harness, state)
        resume = interrupt({"kind": "clarify", "coordinator_payload": payload})
        if _is_timeout(resume):
            state.intent = auto_resolve_clarification(state.intent)
        else:
            session.state = self.harness._apply_hitl_decisions(
                state, _as_decisions(resume), step=None, step_index=-1
            )
            await self.harness._flush_hitl_memories(session.state)
        state = session.state
        return {
            "intent": state.intent.to_dict() if state.intent is not None else None,
            "needs_clarification": False,
            "progress": "clarified",
        }

    async def node_plan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        ctx = session.ctx
        if gstate.get("plan"):
            from app.agent.harness.state import ExecutionPlan
            from app.research.planning.candidate import annotate_candidate_dependencies

            session.state.plan = ExecutionPlan.from_dict(gstate["plan"])
            annotate_candidate_dependencies(session.state.plan)
            plan = session.state.plan
        else:
            session.state = await self.harness._phase_plan(session.state)
            if session.state.plan is not None:
                session.state.plan = annotate_plan_tasks(
                    session.state.plan, intent=session.state.intent
                )
            plan = session.state.plan
        if plan is None or not plan.steps:
            session.state.abort_reason = "empty_plan"
            session.state.abort_message = "Harness plan is empty"
            return {"status": "aborted", "abort_reason": "empty_plan", "plan": None}
        # Effort clamp 后的并行度写入 run_budget；刷新 Worker 闸门
        session.refresh_worker_sem()
        needs_review = bool(
            not ctx.restored_full
            and self.harness.harness_config.hitl_enabled
            and self.harness.harness_config.hitl_plan_review_enabled
            and session.state.intent is not None
            and should_request_plan_review(
                session.state.intent,
                min_confidence=self.harness.harness_config.planner_plan_review_min_confidence,
            )
        )
        return {
            "plan": plan.to_dict(),
            "plan_version": int(getattr(plan, "plan_version", 1) or 1),
            "task_status": task_status_map(plan),
            "needs_plan_review": needs_review,
            "progress": "planned",
        }

    async def node_plan_validate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from langgraph.types import interrupt

        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        ctx = session.ctx
        state = session.state
        if state.abort_reason:
            return {
                "status": "aborted",
                "abort_reason": state.abort_reason,
                "needs_plan_review": False,
                "progress": "abort",
            }
        if gstate.get("needs_plan_review"):
            payload = _plan_review_payload(self.harness, state)
            resume = interrupt({"kind": "plan_review", "coordinator_payload": payload})
            if not _is_timeout(resume):
                session.state = self.harness._apply_hitl_decisions(
                    state, _as_decisions(resume), step=None, step_index=-1
                )
                await self.harness._flush_hitl_memories(session.state)
                state = session.state
                if state.plan is not None:
                    state.plan = annotate_plan_tasks(state.plan)
        if not ctx.context_built:
            try:
                session.state = await asyncio.wait_for(
                    self.harness._phase_build_context(session.state, ctx.task_query),
                    timeout=max(
                        5.0,
                        float(
                            getattr(
                                self.harness.harness_config,
                                "context_build_budget_sec",
                                30,
                            )
                            or 30
                        ),
                    ),
                )
            except asyncio.TimeoutError:
                if isinstance(session.state.metadata, dict):
                    session.state.metadata["context_build_timeout"] = True
                    session.state.metadata["context_build_fallback"] = "lightweight"
            from app.api.monitor import monitor

            monitor.report_session_dir(str(ctx.session_dir).replace("\\", "/"))
            ctx.context_built = True
        plan = session.state.plan
        return {
            "plan": plan.to_dict() if plan is not None else None,
            "plan_version": int(getattr(plan, "plan_version", 1) or 1) if plan else 1,
            "task_status": task_status_map(plan) if plan else {},
            "needs_plan_review": False,
            "progress": "plan_validated",
        }

    async def node_dispatch(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.project import apply_graph_to_loop
        from app.research.planning.candidate import candidate_artifact_status
        from app.research.runtime.scheduler import (
            select_dispatch_wave,
            task_status_map,
        )
        from app.research.runtime.latency import note_dispatch_wave

        session = get_session(str(gstate.get("run_id") or ""))
        if session is None:
            return {"progress": "dispatch"}
        apply_graph_to_loop(session.state, gstate)
        if session.state.plan is None:
            return {"progress": "dispatch"}
        artifact_status = candidate_artifact_status(gstate.get("candidate_set"))
        if self.harness._apply_run_guardrails(
            session.state,
            session.ctx.run_started,
            session.budget_manager,
        ):
            # 若仍可合成，优先走 synthesis 而不是直接 abort dump
            if isinstance(session.state.metadata, dict) and session.state.metadata.get(
                "force_synthesis"
            ):
                budget_reason = str(
                    session.state.metadata.get("budget_degrade_reason")
                    or session.budget_manager.exhaustion_reason()
                    or "budget_exhausted"
                )
                return {
                    "replan_exhausted": True,
                    "progress": "enough",
                    "progress_assessment": {
                        "verdict": "enough",
                        "reason": "force_synthesis_budget",
                        "budget_degrade_reason": budget_reason,
                    },
                    "status": "running",
                }
            return {
                "status": "aborted",
                "abort_reason": session.state.abort_reason or "guardrail",
                "progress": "abort",
            }
        # Research 触顶但未超 hard：阻止再开 research，推进 synthesis
        if isinstance(session.state.metadata, dict) and session.state.metadata.get(
            "force_synthesis"
        ):
            budget_reason = str(
                session.state.metadata.get("budget_degrade_reason")
                or session.budget_manager.exhaustion_reason()
                or "budget_exhausted"
            )
            return {
                "replan_exhausted": True,
                "progress": "enough",
                "progress_assessment": {
                        "verdict": "enough",
                        "reason": "force_synthesis_budget",
                        "budget_degrade_reason": budget_reason,
                    },
            }
        dispatch_status = task_status_map(session.state.plan)
        dispatch_status.update(artifact_status)
        ready = select_dispatch_wave(
            session.state.plan,
            dispatch_status,
            include_optional=False,
            max_parallel=session._resolve_max_workers(),
        )
        if ready:
            note_dispatch_wave(
                session.state,
                task_ids=[s.resolved_task_id(i) for i, s in ready],
                include_optional=False,
            )
        return {
            "replan_count": int(
                gstate.get("replan_count") or session.state.replan_count
            ),
            "progress": "dispatch",
        }

    async def node_research_worker(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from langgraph.types import interrupt

        from app.research.runtime.isolation import worker_row
        from app.research.runtime.project import apply_graph_to_loop
        from app.research.runtime.worker import (
            LangChainWorkerRuntime,
            ResearchContext,
            ResearchTask,
            WorkerResult,
        )

        session = get_session(str(gstate.get("run_id") or ""))
        step_index = int(gstate.get("step_index") or 0)
        task_id = str(gstate.get("task_id") or f"s{step_index}")
        step_type = str(gstate.get("step_type") or "")
        if session is None:
            return _failed_worker(task_id, step_type, "missing_session")
        apply_graph_to_loop(session.state, gstate)
        if session.state.plan is None:
            return _failed_worker(task_id, step_type, "missing_session")
        plan = session.state.plan
        if step_index >= len(plan.steps):
            return _failed_worker(task_id, step_type, "missing_step")
        step = plan.steps[step_index]
        cfg = self.harness.harness_config
        if cfg.hitl_enabled and step.step_type in set(cfg.hitl_step_gate_types):
            payload = _step_gate_payload(self.harness, step, step_index)
            resume = interrupt({"kind": "step_gate", "coordinator_payload": payload})
            if _is_timeout(resume) or _rejected(resume):
                step.metadata["status"] = StepStatus.FAILED.value
                session.state.abort_reason = "step_rejected"
                return {
                    "status": "aborted",
                    "abort_reason": "step_rejected",
                    "task_status": {step.resolved_task_id(step_index): "failed"},
                    "worker_results": [
                        {
                            "task_id": task_id,
                            "ok": False,
                            "summary": "step_rejected",
                            "step_type": step.step_type,
                        }
                    ],
                }
            session.state = self.harness._apply_hitl_decisions(
                session.state, _as_decisions(resume), step, step_index
            )
            await self.harness._flush_hitl_memories(session.state)
            session.state.metadata["graph_step_gated"] = True
        runtime = LangChainWorkerRuntime(self.harness, session)
        from app.research.planning.candidate import objective_with_candidate_context

        objective = objective_with_candidate_context(
            str(step.objective or step.description or ""),
            gstate.get("candidate_set"),
        )
        result = await runtime.execute(
            ResearchTask(
                task_id=task_id,
                objective=objective,
                step_type=step.step_type,
                step_index=step_index,
                description=step.description,
                subagent=step.subagent or "",
                allowed_tools=list(step.allowed_tools or []),
                plan_version=int(gstate.get("plan_version") or 1),
            ),
            ResearchContext(
                run_id=str(gstate.get("run_id") or session.run_id),
                query=session.ctx.task_query,
                user_id=session.ctx.user_id,
                tenant_id=session.ctx.tenant_id,
                project_id=session.ctx.project_id,
                session_id=session.session_id,
            ),
        )
        if not isinstance(result, WorkerResult):
            raise TypeError(
                "WorkerRuntime.execute must return WorkerResult, got "
                f"{type(result).__name__}"
            )
        outcome = result.raw
        tid = step.resolved_task_id(step_index)
        row = (
            worker_row(tid, step, result.ok, getattr(outcome, "result", None))
            if outcome is not None
            else {
                "task_id": tid,
                "ok": result.ok,
                "summary": result.summary,
                "step_type": step.step_type,
                "payload": {
                    "summary": result.summary,
                    "facts": result.facts,
                    "sources": result.sources,
                    "findings": result.findings,
                },
            }
        )
        row["status"] = result.status
        row["fail_reason"] = result.fail_reason or row.get("fail_reason", "")
        row["queue_ms"] = result.queue_ms or row.get("queue_ms", 0)
        row["execution_ms"] = result.execution_ms or row.get("execution_ms", 0)
        raw_row_payload = row.get("payload")
        row_payload: dict[str, Any] = (
            raw_row_payload if isinstance(raw_row_payload, dict) else {}
        )
        row_payload["evidence_ids"] = list(result.evidence_refs or [])
        from app.research.runtime.findings import normalize_findings

        normalized_findings, finding_rejections = normalize_findings(
            list(result.findings or []),
            task_id=tid,
            subject_id=str(step.metadata.get("subject_id") or "general"),
            dimension=str((step.metadata.get("coverage_keys") or ["general"])[0]),
        )
        row_payload["findings"] = normalized_findings
        if finding_rejections:
            row_payload["finding_rejections"] = finding_rejections
        row["payload"] = row_payload
        if not result.ok:
            row["summary"] = result.summary or row.get("summary", "")
        graph_task_status = {
            "done": "done",
            "failed": "failed",
            "skipped": "skipped",
            "blocked": "failed",
        }[result.status]
        projected_state: dict[str, Any] = {
            "worker_results": [row],
            "task_status": {tid: graph_task_status},
            "evidence_refs": result.evidence_refs or ([tid] if result.ok else []),
            "findings": normalized_findings,
        }
        if result.status == "blocked":
            projected_state.update(
                {
                    "replan_exhausted": True,
                    "progress_assessment": {
                        "verdict": "enough",
                        "reason": "force_synthesis_budget",
                    },
                }
            )
        return projected_state

    async def node_progress(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.planning.progress import assess_progress
        from app.research.planning.candidate import (
            build_candidate_set,
            candidate_artifact_status,
        )
        from app.research.runtime.project import apply_graph_to_loop

        session = get_session(str(gstate.get("run_id") or ""))
        if session is not None:
            apply_graph_to_loop(session.state, gstate)
        plan = session.state.plan if session is not None else None
        enabled = True
        query = str(gstate.get("task_query") or "")
        if session is not None:
            enabled = bool(
                getattr(session.harness.harness_config, "progress_eval_enabled", True)
            )
            query = session.ctx.task_query or query
        worker_rows = list(gstate.get("worker_results") or [])
        reconciliation = None
        try:
            from app.research.claims import reconcile_worker_results

            reconciliation = reconcile_worker_results(worker_rows)
            if session is not None and isinstance(session.state.metadata, dict):
                session.state.metadata["claim_reconciliation"] = (
                    reconciliation.to_dict()
                )
        except Exception:
            reconciliation = None
        candidate_set = build_candidate_set(
            plan,
            worker_rows=worker_rows,
            task_status=dict(gstate.get("task_status") or {}),
            query=query,
            brief=gstate.get("brief"),
        )
        progress_status = dict(gstate.get("task_status") or {})
        progress_status.update(candidate_artifact_status(candidate_set))
        assessment = assess_progress(
            plan,
            task_status=progress_status,
            state=session.state if session is not None else None,
            worker_results=worker_rows,
            query=query,
            aborted=bool(
                (session is not None and session.state.abort_reason)
                or gstate.get("abort_reason")
            ),
            enabled=enabled,
            intent=(
                session.state.intent if session is not None else gstate.get("intent")
            ),
            previous_gap_ids=list(
                (
                    (session.state.metadata.get("progress_assessment") or {})
                    if session is not None and isinstance(session.state.metadata, dict)
                    else {}
                ).get("open_gap_ids")
                or []
            ),
            reconciliation=reconciliation,
        )
        # Evidence-driven marginal gain：连续波次零增益 → 即使预算未耗尽也停止研究
        from app.research.planning.marginal_gain import (
            MarginalGainState,
            evaluate_marginal_gain,
            record_wave_gain,
        )

        marginal_state = MarginalGainState.from_dict(
            dict(gstate.get("marginal_gain") or {})
        )
        wave_gain = record_wave_gain(marginal_state, worker_rows)
        marginal_decision = evaluate_marginal_gain(marginal_state)
        if (
            marginal_decision.stop
            and assessment.verdict == "gap"
        ):
            assessment.verdict = "enough"
            assessment.reason = "marginal_gain_low"
        from app.research.runtime.graph import control_fingerprint

        assessment_payload = assessment.to_dict()
        fingerprint = control_fingerprint(gstate, assessment_payload, candidate_set)
        stagnant_cycles = (
            int(gstate.get("stagnant_cycles") or 0) + 1
            if str(gstate.get("control_fingerprint") or "") == fingerprint
            else 0
        )
        if stagnant_cycles >= 2:
            assessment.verdict = "enough"
            assessment.reason = "graph_no_progress"
            assessment_payload = assessment.to_dict()
        if session is not None:
            session.state.metadata["progress_assessment"] = assessment.to_dict()
            session.state.metadata["marginal_gain"] = marginal_state.to_dict()
            if assessment.verdict == "enough":
                try:
                    from app.research.runtime.latency import note_enough_evidence

                    note_enough_evidence(
                        session.state, reason=assessment.reason or "enough"
                    )
                except Exception:
                    pass
        try:
            from app.observability import EventType, get_recorder
            from app.observability.payload_store import get_payload_store

            recorder = get_recorder()
            if recorder.is_active:
                span_key = recorder.start_span(
                    "progress.evaluate",
                    phase="validate",
                    attributes={"progress_id": assessment.progress_id},
                )
                store = get_payload_store()
                run_id = str(
                    gstate.get("run_id")
                    or (session.session_id if session else "unknown")
                )
                ref = store.put(
                    run_id=run_id,
                    artifact_type="progress",
                    artifact_id=assessment.progress_id or "progress",
                    payload=assessment.to_dict(),
                )
                brief_id = ""
                if session is not None and isinstance(session.state.metadata, dict):
                    brief_id = str(session.state.metadata.get("brief_id") or "")
                recorder.emit(
                    EventType.PROGRESS_EVALUATED,
                    phase="validate",
                    status=assessment.verdict,
                    plan_version=int(getattr(plan, "plan_version", 0) or 0) or None,
                    attributes={
                        "progress_id": assessment.progress_id,
                        "verdict": assessment.verdict,
                        "reason": assessment.reason,
                        "gaps": list(assessment.gaps or []),
                        "open_gap_ids": list(assessment.open_gap_ids or []),
                        "resolved_gap_ids": list(assessment.resolved_gap_ids or []),
                        "conflict_count": len(assessment.conflicts or []),
                        "missing_dimensions": list(assessment.missing_dimensions or []),
                        "brief_id": brief_id,
                        "progress_ref": ref.ref,
                        "progress_hash": ref.sha256,
                        "marginal_gain": marginal_decision.to_dict(),
                        "wave_gain": wave_gain.to_dict(),
                    },
                    input_refs=[
                        item
                        for item in [
                            (
                                {"type": "research_brief", "id": brief_id}
                                if brief_id
                                else None
                            ),
                            {
                                "type": "research_plan",
                                "id": f"plan_v{int(getattr(plan, 'plan_version', 1) or 1)}",
                            },
                        ]
                        if item
                    ],
                    output_refs=[ref.to_dict()],
                )
                recorder.end_span(span_key, status=assessment.verdict)
        except Exception:
            import logging as _log

            _log.getLogger("observability").debug("obs emit failed", exc_info=True)
        payload = {
            "progress_assessment": assessment.to_dict(),
            "candidate_set": candidate_set,
            "marginal_gain": marginal_state.to_dict(),
            "progress": "progress_eval",
            "control_fingerprint": fingerprint,
            "stagnant_cycles": stagnant_cycles,
        }
        if stagnant_cycles >= 2:
            payload["progress_assessment"] = assessment_payload
            payload["replan_exhausted"] = True
            payload["synthesis_admission"] = True
        if assessment.verdict == "abort":
            payload["status"] = "aborted"
            payload["abort_reason"] = assessment.reason or "aborted"
        return payload

    async def node_prepare_synthesis(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import GraphInvariantViolation
        from app.research.runtime.project import apply_graph_to_loop
        from app.research.runtime.synthesis_admission import (
            prepare_synthesis_update,
            trusted_evidence_count as state_trusted_evidence_count,
        )

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        plan = session.state.plan
        if plan is None:
            raise GraphInvariantViolation("prepare_synthesis routed without a plan")
        citation_sources = (
            list(session.ctx.citation_manager.sources or [])
            if session.ctx.citation_manager is not None
            else []
        )
        citation_count = sum(
            1
            for source in citation_sources
            if str(getattr(source, "source_kind", "") or "")
            in {"url", "file", "sql", "kb", "extracted"}
            and not str(getattr(source, "locator", "") or "").startswith("step:")
        )
        trusted_count = max(citation_count, state_trusted_evidence_count(gstate))
        try:
            remaining_run_sec = session.budget_manager.remaining_run_sec()
        except Exception:
            remaining_run_sec = float("inf")
        deadline = remaining_run_sec < float(
            getattr(
                self.harness.harness_config,
                "fast_synthesis_threshold_sec",
                45,
            )
            or 45
        )
        forced = bool(
            isinstance(session.state.metadata, dict)
            and session.state.metadata.get("force_synthesis")
        )
        try:
            update = prepare_synthesis_update(
                gstate,
                plan,
                forced=forced,
                deadline=deadline,
                trusted_evidence_count_override=trusted_count,
            )
        except ValueError as exc:
            raise GraphInvariantViolation(str(exc)) from exc
        apply_graph_to_loop(session.state, update)
        return update

    async def node_synthesize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.graph import GraphInvariantViolation
        from app.research.runtime.scheduler import next_synthesis_step
        from app.research.runtime.project import apply_graph_to_loop
        from app.research.runtime.synthesis_admission import (
            evaluate_synthesis_admission,
            trusted_evidence_count as state_trusted_evidence_count,
        )

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        plan = session.state.plan
        if plan is None:
            raise GraphInvariantViolation("synthesize routed without a plan")
        status = task_status_map(plan)
        citation_sources = (
            list(session.ctx.citation_manager.sources or [])
            if session.ctx.citation_manager is not None
            else []
        )
        citation_count = sum(
            1
            for source in citation_sources
            if str(getattr(source, "source_kind", "") or "")
            in {"url", "file", "sql", "kb", "extracted"}
            and not str(getattr(source, "locator", "") or "").startswith("step:")
        )
        trusted_count = max(citation_count, state_trusted_evidence_count(gstate))
        try:
            remaining_run_sec = session.budget_manager.remaining_run_sec()
        except Exception:
            remaining_run_sec = float("inf")
        deadline = remaining_run_sec < float(
            getattr(
                self.harness.harness_config,
                "fast_synthesis_threshold_sec",
                45,
            )
            or 45
        )
        forced = bool(
            (
                isinstance(session.state.metadata, dict)
                and session.state.metadata.get("force_synthesis")
            )
        )
        admission = evaluate_synthesis_admission(
            gstate,
            plan,
            status,
            forced=forced,
            deadline=deadline,
            trusted_evidence_count_override=trusted_count,
        )
        if admission.mode == "no_evidence_partial":
            return {
                "status": "partial",
                "progress": "no_evidence_partial",
                "task_status": status,
                "synthesis_mode": "no_evidence_partial",
                "synthesis_admission_reason": admission.reason,
                "trusted_evidence_count": 0,
            }
        if not admission.allowed:
            raise GraphInvariantViolation(
                f"synthesize routed but admission rejected: {admission.reason}"
            )
        allow_failed_deps = bool(
            gstate.get("synthesis_admission")
            or admission.mode != "normal"
            or admission.trusted_evidence_count > 0
        )
        emergency_synthesis = admission.mode == "emergency"
        nxt = next_synthesis_step(plan, status, allow_failed_deps=allow_failed_deps)
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
                    "progress": "synthesized",
                    "task_status": status,
                }
            raise GraphInvariantViolation(
                "synthesize routed but no synthesis step is runnable"
            )
        index, step = nxt
        session.state.step_index = index
        step.metadata["status"] = StepStatus.RUNNING.value
        tid = step.resolved_task_id(index)
        synth_span = ""
        fast_context_duration_ms: int | None = None
        fast_path = emergency_synthesis
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                brief_id = str((session.state.metadata or {}).get("brief_id") or "")
                plan_id = f"plan_v{int(getattr(plan, 'plan_version', 1) or 1)}"
                evidence_ids = []
                if session.ctx.citation_manager is not None:
                    evidence_ids = [
                        str(getattr(src, "source_id", "") or "")
                        for src in list(
                            getattr(session.ctx.citation_manager, "sources", []) or []
                        )
                        if getattr(src, "source_id", None)
                    ][:40]
                synth_span = recorder.start_span(
                    "synthesis.generate",
                    phase="synthesis",
                    task_id=tid,
                    attributes={"brief_id": brief_id, "plan_id": plan_id},
                )
                recorder.emit(
                    EventType.SYNTHESIS_STARTED,
                    phase="synthesis",
                    status="start",
                    task_id=tid,
                    attributes={
                        "brief_id": brief_id,
                        "plan_id": plan_id,
                        "evidence_ids": evidence_ids,
                        "finding_ids": [
                            str(item.get("task_id") or item.get("finding_id") or "")
                            for item in list(gstate.get("findings") or [])[:24]
                            if isinstance(item, dict)
                        ],
                    },
                    input_refs=[
                        item
                        for item in [
                            (
                                {"type": "research_brief", "id": brief_id}
                                if brief_id
                                else None
                            ),
                            {"type": "research_plan", "id": plan_id},
                        ]
                        if item
                    ]
                    + [{"type": "evidence", "id": eid} for eid in evidence_ids[:12]],
                )
        except Exception:
            import logging as _log

            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        async with session.lock:
            if emergency_synthesis:
                from app.research.runtime.fast_synthesis import (
                    build_minimal_evidence_pack,
                    render_fast_partial_report,
                )

                context_started = time.perf_counter()
                context_budget_sec = max(
                    0.05,
                    float(
                        getattr(
                            self.harness.harness_config,
                            "emergency_context_budget_sec",
                            10,
                        )
                        or 10
                    ),
                )
                emergency_context_timeout = False
                try:
                    pack = await asyncio.wait_for(
                        asyncio.to_thread(
                            build_minimal_evidence_pack,
                            state=session.state,
                            graph_state=gstate,
                        ),
                        timeout=context_budget_sec,
                    )
                except asyncio.TimeoutError:
                    emergency_context_timeout = True
                    pack = {
                        "brief": {"objective": "", "dimensions": []},
                        "findings": [
                            item
                            for item in list(gstate.get("findings") or [])
                            if isinstance(item, dict)
                        ][:4],
                        "evidence_refs": [
                            str(item)
                            for item in list(gstate.get("evidence_refs") or [])
                            if item
                        ][:8],
                        "artifact_summaries": [],
                        "coverage_gaps": [],
                    }
                fast_context_duration_ms = int((time.perf_counter() - context_started) * 1000)
                reason = _resolve_emergency_reason(session, deadline_near=deadline)
                session.state.final_content = render_fast_partial_report(pack, reason=reason)
                ok = True
                if isinstance(session.state.metadata, dict):
                    session.state.metadata["emergency_synthesis"] = True
                    session.state.metadata["minimal_evidence_pack"] = pack
                    session.state.metadata["emergency_context_timeout"] = (
                        emergency_context_timeout
                    )
                    session.state.metadata["termination"] = {
                        "status": "partial",
                        "reason": reason,
                        "stage": "synthesis",
                        "research_completed": False,
                        "synthesis_attempted": True,
                        "synthesis_status": "partial_fast_path",
                        "quality_attempted": False,
                        "origin_stage": "research",
                        "detected_stage": "dispatch",
                        "cause_event_id": str(
                            (session.state.metadata or {}).get(
                                "budget_exhausted_event_id"
                            )
                            or ""
                        ),
                        "causal_chain": _termination_causal_chain(
                            reason, quality_attempted=False
                        ),
                    }
            else:
                try:
                    mgr = session.budget_manager
                    mgr.sync_from_usage(
                        session_id=session.session_id,
                        tool_calls=session.state.tool_calls_count,
                    )
                    timeout_sec = min(
                        max(10, int(self.harness.harness_config.step_timeout_sec)),
                        max(5.0, mgr.remaining_run_sec()),
                    )
                except Exception:
                    timeout_sec = max(10, int(self.harness.harness_config.step_timeout_sec))
                try:
                    ok = await asyncio.wait_for(
                        self.harness._run_single_step(
                            session.state,
                            step,
                            index,
                            session.ctx.task_query,
                            session.ctx.relative_session_dir,
                            session.ctx.uploaded_prompt,
                            session.session_id,
                            session.ctx.session_dir,
                            session.ctx.citation_manager,
                            session.ctx.idempotency,
                            session.ctx.checkpoint_store,
                        ),
                        timeout=timeout_sec,
                    )
                except asyncio.TimeoutError:
                    ok = False
                    session.state.abort_reason = (
                        session.state.abort_reason or "deadline_exceeded"
                    )
                    session.state.abort_message = (
                        session.state.abort_message or "synthesis step timeout"
                    )
                except Exception as exc:
                    from app.agent.llm_errors import LLMFailureKind, classify_llm_exception

                    provider_failure = classify_llm_exception(exc)
                    if provider_failure.kind is LLMFailureKind.UNKNOWN:
                        raise
                    ok = False
                    session.state.abort_reason = (
                        session.state.abort_reason or f"provider_{provider_failure.kind.value}"
                    )
                    session.state.abort_message = (
                        session.state.abort_message or provider_failure.message[:500]
                    )
            session.state.step_validation_results.append(
                {
                    "step_index": index,
                    "step_type": step.step_type,
                    "passed": ok,
                }
            )
            step.metadata["status"] = (
                StepStatus.DONE.value if ok else StepStatus.FAILED.value
            )
        if not ok:
            from app.agent.harness.partial_report import render_partial_report

            assessment = dict(gstate.get("progress_assessment") or {})
            partial = render_partial_report(
                state=session.state,
                abort_reason=str(session.state.abort_reason or "synthesis_failed"),
                synthesis_failed=True,
                assessment=assessment,
            )
            session.state.final_content = partial
            if isinstance(session.state.metadata, dict):
                session.state.metadata["partial_delivered"] = True
                session.state.metadata["synthesis_failed"] = True
        try:
            from app.observability import EventType, get_recorder
            from app.observability.events import new_id
            from app.observability.payload_store import get_payload_store

            recorder = get_recorder()
            if recorder.is_active:
                answer = str(session.state.final_content or "")
                answer_id = f"answer_{new_id(8)}"
                store = get_payload_store()
                ref = store.put(
                    run_id=str(gstate.get("run_id") or session.session_id),
                    artifact_type="synthesis",
                    artifact_id=answer_id,
                    payload={
                        "answer_preview": answer[:1200],
                        "word_count": len(answer.split()),
                        "task_id": tid,
                    },
                )
                brief_id = str((session.state.metadata or {}).get("brief_id") or "")
                plan_id = f"plan_v{int(getattr(plan, 'plan_version', 1) or 1)}"
                evidence_ids = []
                claim_ids = []
                if session.ctx.citation_manager is not None:
                    evidence_ids = [
                        str(getattr(src, "source_id", "") or "")
                        for src in list(
                            getattr(session.ctx.citation_manager, "sources", []) or []
                        )
                        if getattr(src, "source_id", None)
                    ][:40]
                    claim_ids = [
                        f"c{i + 1}"
                        for i, _ in enumerate(
                            list(
                                getattr(
                                    session.ctx.citation_manager, "fact_bindings", []
                                )
                                or []
                            )[:24]
                        )
                    ]
                event_type = (
                    EventType.SYNTHESIS_COMPLETED if ok else EventType.SYNTHESIS_FAILED
                )
                recorder.emit(
                    event_type,
                    phase="synthesis",
                    status="ok" if ok else "failed",
                    task_id=tid,
                    attributes={
                        "answer_id": answer_id,
                        "brief_id": brief_id,
                        "plan_id": plan_id,
                        "evidence_ids": evidence_ids,
                        "claim_ids": claim_ids,
                        "citation_ids": list(evidence_ids),
                        "answer_ref": ref.ref,
                        "answer_hash": ref.sha256,
                        "word_count": len(answer.split()),
                        "fail_reason": "" if ok else "synthesis_failed",
                        "fast_path": fast_path,
                        "context_duration_ms": fast_context_duration_ms,
                        "synthesis_status": (
                            "partial_fast_path" if fast_path else ("ok" if ok else "failed")
                        ),
                        "partial_delivered": bool(
                            isinstance(session.state.metadata, dict)
                            and session.state.metadata.get("partial_delivered")
                        ),
                    },
                    input_refs=[
                        item
                        for item in [
                            (
                                {"type": "research_brief", "id": brief_id}
                                if brief_id
                                else None
                            ),
                            {"type": "research_plan", "id": plan_id},
                        ]
                        if item
                    ],
                    output_refs=[ref.to_dict()],
                )
                if synth_span:
                    recorder.end_span(synth_span, status="ok" if ok else "failed")
                if isinstance(session.state.metadata, dict):
                    session.state.metadata["answer_id"] = answer_id
                    session.state.metadata["answer_ref"] = ref.ref
        except Exception:
            import logging as _log

            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        if isinstance(session.state.metadata, dict):
            session.state.metadata["synthesis_attempted"] = True
            session.state.metadata["synthesis_status"] = (
                "partial_fast_path" if fast_path else ("ok" if ok else "failed")
            )
        return {
            "task_status": {tid: "done" if ok else "failed"},
            "status": "partial" if fast_path else ("synthesized" if ok else "partial"),
            "progress": "synthesized",
            "replan_exhausted": True if not ok else gstate.get("replan_exhausted"),
        }

    async def node_replan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.agent.harness.guardrails import can_replan
        from app.research.planning.plan_patch import (
            apply_plan_patch,
            build_progress_patch,
        )
        from app.research.planning.policy import parse_source_policy
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        state = session.state
        assessment = dict(gstate.get("progress_assessment") or {})
        exhausted = {
            "replan_exhausted": True,
            "progress": "enough",
            "progress_assessment": {
                **assessment,
                "verdict": "enough",
                "reason": "replan_exhausted",
            },
            "replan_count": state.replan_count,
        }
        if state.plan is None or state.intent is None:
            return exhausted
        if not can_replan(state, self.harness.harness_config):
            return exhausted
        # Do not start a wave that cannot finish before the synthesis reserve.
        # Affordability must cover the complete wave, not only worker execution.
        try:
            mgr = session.budget_manager
            rows = [row for row in list(gstate.get("worker_results") or []) if isinstance(row, dict)]

            def _p95(values: list[float], default: float) -> float:
                recent = sorted(values)[-20:]
                if not recent:
                    return default
                return recent[max(0, int(len(recent) * 0.95 + 0.999) - 1)]

            execution_values = [
                int(row.get("execution_ms") or row.get("duration_ms") or 0) / 1000.0
                for row in rows
                if int(row.get("execution_ms") or row.get("duration_ms") or 0) > 0
            ]
            queue_values = [
                int(row.get("queue_ms") or 0) / 1000.0
                for row in rows
                if int(row.get("queue_ms") or 0) > 0
            ]
            execution_p95 = _p95(
                execution_values,
                float(self.harness.harness_config.step_timeout_sec),
            )
            queue_p95 = _p95(queue_values, 2.0)
            context_overhead = max(
                0.0,
                float(
                    getattr(
                        self.harness.harness_config,
                        "replan_context_overhead_sec",
                        30,
                    )
                    or 0
                ),
            )
            checkpoint_overhead = max(
                0.0,
                float(
                    getattr(
                        self.harness.harness_config,
                        "replan_checkpoint_overhead_sec",
                        2,
                    )
                    or 0
                ),
            )
            progress_overhead = min(30.0, max(5.0, execution_p95 * 0.1))
            safety_margin = min(45.0, max(10.0, execution_p95 * 0.2))
            estimated_wave = (
                context_overhead
                + queue_p95
                + execution_p95
                + progress_overhead
                + checkpoint_overhead
                + safety_margin
            )
            if (
                mgr.remaining_for_research_sec()
                < estimated_wave
            ):
                if isinstance(state.metadata, dict):
                    state.metadata["force_synthesis"] = True
                    state.metadata["replan_skipped_reason"] = (
                        "insufficient_research_time"
                    )
                    state.metadata["estimated_wave_cost_sec"] = round(estimated_wave, 3)
                return {
                    **exhausted,
                    "progress_assessment": {
                        **assessment,
                        "verdict": "enough",
                        "reason": "replan_unaffordable_force_synthesis",
                    },
                }
        except Exception:
            pass
        policy = parse_source_policy(state.intent.raw_query)
        run_budget = {}
        if isinstance(getattr(state, "metadata", None), dict):
            raw = state.metadata.get("run_budget")
            if isinstance(raw, dict):
                run_budget = raw
        max_new = int(
            run_budget.get(
                "max_plan_patch_tasks",
                getattr(self.harness.harness_config, "planner_max_plan_patch_tasks", 2)
                or 2,
            )
        )
        grant_retrieval = int(
            run_budget.get(
                "reserved_step_tool_calls",
                run_budget.get(
                    "max_step_tool_calls",
                    getattr(self.harness.harness_config, "max_step_tool_calls", 8) or 8,
                ),
            )
        )
        grant: dict[str, Any] = {}
        try:
            from app.research.planning.effort import (
                grant_on_gap,
                resolve_effective_budget,
            )

            effective = resolve_effective_budget(
                state.intent, self.harness.harness_config
            )
            grant = grant_on_gap(
                effective,
                assessment=assessment,
                run_budget=run_budget,
            )
            max_new = min(max_new, int(grant.get("max_new_tasks", max_new)))
            grant_retrieval = int(grant.get("max_retrieval_calls", grant_retrieval))
        except Exception:
            import logging as _log

            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        patch = build_progress_patch(
            state.plan,
            state.intent,
            assessment=assessment,
            worker_results=list(gstate.get("worker_results") or []),
            max_new_tasks=max_new,
        )
        for item in list(patch.get("add_tasks") or []):
            if isinstance(item, dict):
                meta = dict(item.get("metadata") or {})
                meta.setdefault("max_retrieval_calls", grant_retrieval)
                meta.setdefault("granted_on_gap", True)
                item["metadata"] = meta
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                target_gap_ids = list(
                    patch.get("target_gap_ids") or assessment.get("open_gap_ids") or []
                )
                recorder.emit(
                    EventType.REPLAN_PROPOSED,
                    phase="recover",
                    status="proposed",
                    plan_version=int(getattr(state.plan, "plan_version", 1) or 1),
                    attributes={
                        "patch_id": str(patch.get("patch_id") or ""),
                        "triggered_by": str(
                            patch.get("triggered_by")
                            or assessment.get("progress_id")
                            or ""
                        ),
                        "target_gap_ids": [str(x) for x in target_gap_ids if x],
                        "reason": str(
                            patch.get("reason")
                            or assessment.get("reason")
                            or "semantic_gap"
                        ),
                        "gaps": list(
                            assessment.get("gaps")
                            or assessment.get("missing_dimensions")
                            or assessment.get("coverage_gaps")
                            or []
                        ),
                        "added_tasks": [
                            str(item.get("task_id") or "")
                            for item in list(patch.get("add_tasks") or [])
                            if isinstance(item, dict)
                        ],
                    },
                )
        except Exception:
            import logging as _log

            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        plan, issues = apply_plan_patch(
            state.plan,
            patch,
            state.intent,
            policy=policy,
            max_new_tasks=max_new,
            max_plan_steps=self.harness.harness_config.max_plan_steps,
        )
        if issues or int(getattr(plan, "plan_version", 1) or 1) == int(
            getattr(state.plan, "plan_version", 1) or 1
        ):
            try:
                from app.observability import EventType, get_recorder

                recorder = get_recorder()
                if recorder.is_active:
                    recorder.emit(
                        EventType.REPLAN_REJECTED,
                        phase="recover",
                        status="rejected",
                        plan_version=int(getattr(state.plan, "plan_version", 1) or 1),
                        attributes={
                            "reason": str(assessment.get("reason") or "unchanged"),
                            "issues": issues,
                            "target_gap_ids": list(patch.get("target_gap_ids") or []),
                        },
                    )
            except Exception:
                pass
            return exhausted
        from_version = int(getattr(state.plan, "plan_version", 1) or 1)
        added = [
            str(item.get("task_id") or "")
            for item in list(patch.get("add_tasks") or [])
            if isinstance(item, dict)
        ]
        state.plan = plan
        state.replan_count += 1
        # 消耗 GAP reserve（不抬会话硬顶）
        if grant and isinstance(getattr(state, "metadata", None), dict):
            try:
                from app.research.planning.effort import apply_grant_to_run_budget

                state.metadata["run_budget"] = apply_grant_to_run_budget(
                    run_budget or state.metadata.get("run_budget"),
                    grant,
                    tasks_granted=len([t for t in added if t]),
                )
            except Exception:
                pass
        self.harness._report_phase(
            Phase.REPLAN,
            "done",
            state=state,
            reason=str(patch.get("reason") or "semantic_gap"),
            new_steps=len(state.plan.steps),
        )
        try:
            from app.observability import EventType, get_recorder
            from app.observability.payload_store import get_payload_store

            recorder = get_recorder()
            if recorder.is_active:
                budget = {}
                try:
                    budget = self.harness.remaining_budget(state)
                except Exception:
                    budget = {}
                patch_id = str(
                    patch.get("patch_id")
                    or f"patch_{from_version}_{state.plan.plan_version}"
                )
                store = get_payload_store()
                ref = store.put(
                    run_id=str(gstate.get("run_id") or session.session_id),
                    artifact_type="plan_patch",
                    artifact_id=patch_id,
                    payload=dict(patch),
                )
                target_gap_ids = [
                    str(x) for x in (patch.get("target_gap_ids") or []) if x
                ]
                recorder.emit(
                    EventType.REPLAN_APPLIED,
                    phase="recover",
                    status="applied",
                    plan_version=int(getattr(state.plan, "plan_version", 1) or 1),
                    attributes={
                        "patch_id": patch_id,
                        "triggered_by": str(
                            patch.get("triggered_by")
                            or assessment.get("progress_id")
                            or ""
                        ),
                        "target_gap_ids": target_gap_ids,
                        "from_plan_version": from_version,
                        "to_plan_version": int(
                            getattr(state.plan, "plan_version", 1) or 1
                        ),
                        "reason": str(
                            patch.get("reason")
                            or assessment.get("reason")
                            or "semantic_gap"
                        ),
                        "gaps": list(
                            assessment.get("gaps")
                            or assessment.get("missing_dimensions")
                            or assessment.get("coverage_gaps")
                            or []
                        ),
                        "added_tasks": [tid for tid in added if tid],
                        "removed_tasks": [],
                        "remaining_budget": budget,
                        "patch_ref": ref.ref,
                        "patch_hash": ref.sha256,
                    },
                    input_refs=[
                        {
                            "type": "progress",
                            "id": str(assessment.get("progress_id") or ""),
                        },
                        *[{"type": "gap", "id": gid} for gid in target_gap_ids],
                    ],
                    output_refs=[
                        ref.to_dict(),
                        {
                            "type": "research_plan",
                            "id": f"plan_v{int(getattr(state.plan, 'plan_version', 1) or 1)}",
                        },
                    ],
                )
        except Exception:
            import logging as _log

            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        return {
            "plan": state.plan.to_dict(),
            "plan_version": int(getattr(state.plan, "plan_version", 1) or 1),
            "task_status": task_status_map(state.plan),
            "replan_count": state.replan_count,
            "replan_exhausted": False,
            "progress": "run",
            "progress_assessment": {**assessment, "verdict": "run"},
        }

    async def node_quality_gate(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        if isinstance(session.state.metadata, dict):
            session.state.metadata["quality_attempted"] = True
            termination = session.state.metadata.get("termination")
            if isinstance(termination, dict):
                termination["quality_attempted"] = True
                termination["stage"] = "quality"
        ctx = session.ctx
        state = session.state
        citation_manager = ctx.citation_manager
        if citation_manager and state.final_content:
            cited = citation_manager.build_cited_report(state.final_content)
            state.final_content = cited
            metrics = citation_manager.compute_metrics(cited)
            state.citation_coverage_rate = metrics["citation_coverage_rate"]
            state.hallucination_rate = metrics["hallucination_rate"]
            state.evidence_source_count = metrics["registered_sources"]
            state.numeric_citation_coverage = float(
                metrics.get("numeric_citation_coverage") or 0.0
            )
            citation_manager.save_evidence_json(ctx.run_dir, run_id=session.run_id)
        outcome = self.harness.validator.validate_finalize(
            state,
            ctx.session_dir,
            citation_manager=citation_manager,
            min_citation_coverage=self.harness.harness_config.citations_min_coverage_rate,
            deliverable_dir=ctx.deliverable_dir,
        )
        await self.harness._phase_validate(
            state,
            outcome,
            step_index=state.step_index,
            scope="finalize",
        )
        passed = bool(outcome.passed or outcome.severity == "warning")
        reason = str(getattr(outcome, "reason", "") or "")
        quality_attempts = int(gstate.get("quality_attempts") or 0)
        repairable_reasons = {
            "citation_coverage_low",
            "conflict_not_disclosed",
            "unsupported_reconciled_value",
            "no_file_generated",
        }
        research_gap_reasons = {
            "no_content",
            "wrong_subagent",
            "step_validation_failed",
        }
        repairable = bool(not passed and reason in repairable_reasons)
        repair_action = ""
        if not passed:
            if repairable and quality_attempts < 1:
                repair_action = "repair"
            elif reason in research_gap_reasons:
                allowed, _why = session.budget_manager.research_allowed()
                max_replans = int(
                    (gstate.get("budget") or {}).get("max_replan_count") or 3
                )
                if allowed and int(gstate.get("replan_count") or 0) < max_replans:
                    repair_action = "replan"
            if not repair_action:
                repair_action = "partial"
        if isinstance(session.state.metadata, dict):
            session.state.metadata["quality"] = {
                "passed": passed,
                "reason": reason,
                "repairable": repairable,
                "repair_action": repair_action,
                "attempts": quality_attempts + 1,
            }
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder.emit(
                    EventType.QUALITY_EVALUATED,
                    phase="quality",
                    status="pass" if passed else "fail",
                    attributes={
                        "passed": passed,
                        "reason": reason,
                        "repairable": repairable,
                        "repair_action": repair_action,
                        "attempt": quality_attempts + 1,
                        "severity": getattr(outcome, "severity", ""),
                        "citation_coverage_rate": getattr(
                            state, "citation_coverage_rate", None
                        ),
                        "hallucination_rate": getattr(
                            state, "hallucination_rate", None
                        ),
                        "unsupported_claim_rate": getattr(
                            state, "hallucination_rate", None
                        ),
                        "conflict_disclosure_passed": (
                            True
                            if getattr(outcome, "reason", "")
                            not in {
                                "conflict_not_disclosed",
                                "unsupported_reconciled_value",
                            }
                            else False
                        ),
                        "conflict_disclosure_reason": str(
                            getattr(outcome, "reason", "") or ""
                        ),
                        "target_type": "synthesis",
                        "target_artifact_id": str(
                            (state.metadata or {}).get("answer_id") or ""
                        ),
                        "target_span_id": "",
                        "grader": "finalize_validator",
                        "grader_version": "v1",
                        "metric": "quality_gate",
                        "score": 1.0 if passed else 0.0,
                        "failure.origin_stage": "synthesis" if not passed else "",
                        "failure.detected_stage": "quality" if not passed else "",
                        "failure.cause_artifact_id": str(
                            (state.metadata or {}).get("answer_id") or ""
                        ),
                    },
                )
        except Exception:
            import logging as _log

            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        return {
            "quality_passed": passed,
            "quality_reason": reason,
            "quality_repairable": repairable,
            "quality_repair_action": repair_action,
            "quality_attempts": quality_attempts + 1,
            "final_content": state.final_content,
            "progress": "quality",
        }

    async def node_repair_synthesis(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        if session.state.plan is None:
            return {"progress": "repair_synthesis"}
        status = dict(gstate.get("task_status") or {})
        for index, step in enumerate(session.state.plan.steps):
            if step.step_type not in {"generate_markdown", "summarize", "convert_pdf"}:
                continue
            task_id = step.resolved_task_id(index)
            if status.get(task_id) in {"done", "failed", "skipped"}:
                status[task_id] = "pending"
                step.metadata["status"] = "pending"
                step.metadata["quality_repair_attempt"] = int(
                    gstate.get("quality_attempts") or 0
                )
        session.state.final_content = ""
        if isinstance(session.state.metadata, dict):
            session.state.metadata["partial_delivered"] = False
        return {
            "task_status": status,
            "status": "running",
            "final_content": "",
            "progress": "repair_synthesis",
        }

    async def node_finalize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.project import apply_graph_to_loop
        from app.research.runtime.latency import (
            critical_path_summary,
            note_final_answer,
        )

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        try:
            note_final_answer(session.state)
            if isinstance(session.state.metadata, dict):
                session.state.metadata["critical_path"] = critical_path_summary(
                    session.state.metadata
                )
        except Exception:
            pass
        if not str(session.state.final_content or "").strip() or (
            isinstance(session.state.metadata, dict)
            and session.state.metadata.get("synthesis_failed")
            and not session.state.metadata.get("partial_delivered")
        ):
            from app.agent.harness.partial_report import render_partial_report

            session.state.final_content = render_partial_report(
                state=session.state,
                abort_reason=str(
                    session.state.abort_reason or gstate.get("status") or "incomplete"
                ),
                synthesis_failed=bool(
                    isinstance(session.state.metadata, dict)
                    and session.state.metadata.get("synthesis_failed")
                ),
                assessment=dict(gstate.get("progress_assessment") or {}),
            )
            if isinstance(session.state.metadata, dict):
                session.state.metadata["partial_delivered"] = True
        success = (
            bool(gstate.get("quality_passed", True))
            and not session.state.abort_reason
            and gstate.get("status") != "partial"
        )
        if not success and isinstance(session.state.metadata, dict):
            session.state.metadata.setdefault(
                "termination",
                {
                    "status": "partial",
                    "reason": str(
                        gstate.get("quality_reason")
                        or session.state.abort_reason
                        or "incomplete"
                    ),
                    "stage": "quality" if gstate.get("quality_attempts") else "finalize",
                    "origin_stage": "synthesis" if gstate.get("quality_reason") else "run",
                    "detected_stage": "quality" if gstate.get("quality_reason") else "finalize",
                    "research_completed": False,
                    "synthesis_attempted": True,
                    "synthesis_status": "ok",
                    "quality_attempted": bool(gstate.get("quality_attempts")),
                },
            )
        result = await self.harness._phase_finalize(
            session.state,
            session.ctx.session_dir,
            success=success,
            started_at=session.ctx.run_started,
            deliverable_dir=session.ctx.deliverable_dir,
        )
        session.result = result
        if result.status in {"partial", "cancelled"}:
            graph_status = result.status
        elif result.status == "failed":
            graph_status = "aborted"
        else:
            graph_status = "completed"
        return {
            "status": graph_status,
            "final_content": session.state.final_content,
            "artifacts": list(result.artifacts),
            "progress": "done",
        }

    async def node_abort(self, gstate: dict[str, Any]) -> dict[str, Any]:
        session = get_session(str(gstate.get("run_id") or ""))
        if session is None:
            return {
                "status": "aborted",
                "abort_reason": str(gstate.get("abort_reason") or "aborted"),
                "progress": "abort",
            }
        from app.agent.harness.partial_report import render_partial_report

        session.state.final_content = render_partial_report(
            state=session.state,
            abort_reason=str(session.state.abort_reason or "aborted"),
            assessment=dict(gstate.get("progress_assessment") or {}),
        )
        if isinstance(session.state.metadata, dict):
            session.state.metadata["partial_delivered"] = True
        self.harness._report_phase(
            Phase.ABORT,
            session.state.abort_reason or "guardrail",
            state=session.state,
            tool_calls=session.state.tool_calls_count,
            abort_reason=session.state.abort_reason,
            abort_message=session.state.abort_message,
        )
        result = await self.harness._phase_finalize(
            session.state,
            session.ctx.session_dir,
            success=False,
            started_at=session.ctx.run_started,
            deliverable_dir=session.ctx.deliverable_dir,
        )
        session.result = result
        return {
            "status": "aborted",
            "abort_reason": session.state.abort_reason or "aborted",
            "artifacts": list(result.artifacts),
            "final_content": session.state.final_content,
            "progress": "abort",
        }


def _require_session(gstate: dict[str, Any]) -> RunSession:
    session = get_session(str(gstate.get("run_id") or ""))
    if session is None:
        raise RuntimeError("research graph session missing")
    return session


def _failed_worker(task_id: str, step_type: str, reason: str) -> dict[str, Any]:
    return {
        "worker_results": [
            {
                "task_id": task_id,
                "ok": False,
                "summary": reason,
                "step_type": step_type,
            }
        ],
        "task_status": {task_id: "failed"},
    }


async def _default_checkpointer():
    from app.research.runtime.checkpointer import aget_research_checkpointer

    backend = None
    path = None
    try:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        backend = getattr(cfg, "graph_checkpoint_backend", None)
        path = getattr(cfg, "graph_checkpoint_path", None) or None
    except Exception:
        pass
    return await aget_research_checkpointer(backend=backend, path=path)


async def _initial_or_resume_payload(
    graph: Any,
    payload: Any,
    config: dict[str, Any],
    ctx: Any,
) -> Any:
    """同一 thread 若仍有 next/interrupt，从 durable checkpoint 续跑。"""
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        return payload
    if snapshot is None:
        return payload
    nxt = tuple(getattr(snapshot, "next", None) or ())
    interrupts = list(getattr(snapshot, "interrupts", None) or [])
    if nxt or interrupts:
        logger.info(
            "resume research graph thread=%s next=%s restored=%s",
            (config.get("configurable") or {}).get("thread_id"),
            nxt,
            bool(getattr(ctx, "restored_full", False)),
        )
        return None
    return payload


async def _ainvoke_resilient(graph: Any, payload: Any, config: dict[str, Any]) -> Any:
    try:
        return await graph.ainvoke(payload, config)
    except Exception as exc:
        if type(exc).__name__ not in {
            "GraphInterrupt",
            "NodeInterrupt",
            "GraphBubbleUp",
        }:
            raise
        interrupts = _interrupt_from_exception(exc)
        if interrupts is not None:
            return {"__interrupt__": interrupts}
        raise


def _interrupt_from_exception(exc: BaseException) -> list[Any] | None:
    name = type(exc).__name__
    if "Interrupt" not in name and "interrupt" not in str(exc).lower():
        return None
    for attr in ("args", "interrupts"):
        value = getattr(exc, attr, None)
        if value:
            return list(value) if not isinstance(value, list) else value
    return [exc]


def _has_interrupt(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(result.get("__interrupt__"))


def _interrupt_payloads(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("__interrupt__") or []
    payloads: list[dict[str, Any]] = []
    for item in raw:
        value = getattr(item, "value", item)
        if isinstance(value, dict):
            payloads.append(value)
        elif hasattr(item, "value") and isinstance(item.value, dict):
            payloads.append(item.value)
    return payloads


def _clarification_payload(harness: Any, state: LoopState) -> dict[str, Any]:
    intent = state.intent
    allowed = ["approve", "reject"]
    if harness.harness_config.hitl_allow_edit:
        allowed.append("edit")
    return {
        "action_requests": [
            {
                "name": "task_intent",
                "args": {
                    "question": intent.clarification_question if intent else "",
                    "intent": intent.to_dict() if intent else {},
                    "suggested_deliverables": ["text", "md", "pdf"],
                },
            }
        ],
        "review_configs": [
            {"action_name": "task_intent", "allowed_decisions": allowed}
        ],
        "gate_type": "intent_clarification",
        "editable": harness.harness_config.hitl_allow_edit,
        "step_index": -1,
    }


def _plan_review_payload(harness: Any, state: LoopState) -> dict[str, Any]:
    allowed = ["approve", "reject"]
    if harness.harness_config.hitl_allow_edit:
        allowed.append("edit")
    return {
        "action_requests": [
            {
                "name": "execution_plan",
                "args": {
                    "summary": state.plan.summary if state.plan else "",
                    "steps": plan_to_editable_dict(state.plan) if state.plan else [],
                    "intent": state.intent.to_dict() if state.intent else {},
                    "intent_confidence": (
                        state.intent.intent_confidence if state.intent else 1.0
                    ),
                },
            }
        ],
        "review_configs": [
            {"action_name": "execution_plan", "allowed_decisions": allowed}
        ],
        "gate_type": "plan_review",
        "editable": harness.harness_config.hitl_allow_edit,
        "step_index": -1,
    }


def _step_gate_payload(harness: Any, step: Any, step_index: int) -> dict[str, Any]:
    allowed = ["approve", "reject"]
    if harness.harness_config.hitl_allow_edit:
        allowed.append("edit")
    return {
        "action_requests": [
            {
                "name": step.step_type,
                "args": {
                    "description": step.description,
                    "subagent": step.subagent,
                },
            }
        ],
        "review_configs": [
            {"action_name": step.step_type, "allowed_decisions": allowed}
        ],
        "step_index": step_index,
        "gate_type": "step",
        "editable": harness.harness_config.hitl_allow_edit,
    }


def _as_decisions(resume: Any) -> list[dict[str, Any]]:
    if resume is True or resume == "approve":
        return [{"type": "approve"}]
    if isinstance(resume, list):
        return [
            item if isinstance(item, dict) else {"type": str(item)} for item in resume
        ]
    if isinstance(resume, dict):
        if "type" in resume or "action" in resume:
            return [resume]
        if resume.get("decisions"):
            return list(resume["decisions"])
    return [{"type": "approve"}]


def _is_timeout(resume: Any) -> bool:
    return isinstance(resume, dict) and bool(resume.get("_timeout"))


def _rejected(resume: Any) -> bool:
    for item in _as_decisions(resume):
        if str(item.get("type") or item.get("action") or "") == "reject":
            return True
    return False
