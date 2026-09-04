"""把 AgentHarness 领域服务接到 StateGraph：图是调度权威。"""

from __future__ import annotations

import asyncio
import logging
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
        persist_loop = bool(getattr(self.harness.harness_config, "persist_loop_state", False))
        # LoopState checkpoint 不再作为 Graph 的种子。仅当显式打开旧 persist 时保留 HITL 桥。
        if persist_loop and ctx.restored_full:
            waiting = dict((session.state.metadata or {}).get("hitl_waiting") or {})
            gate = str(waiting.get("gate_type") or "")
            if gate in {"clarification", "intent_clarification"}:
                session.state = await self.harness._maybe_intent_clarification(session.state)
            elif gate == "plan_review":
                session.state = await self.harness._maybe_plan_hitl_review(session.state)
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
        try:
            graph = self.compile(
                checkpointer=checkpointer or await _default_checkpointer(),
                profile=profile,
            )
            result = await _ainvoke_resilient(
                graph, await _initial_or_resume_payload(graph, payload, config, ctx), config
            )
            while _has_interrupt(result):
                resume_value = await self._bridge_interrupts(result, session)
                result = await _ainvoke_resilient(
                    graph, Command(resume=resume_value), config
                )
            if session.result is not None:
                return session.result
            return await self._complete_from_graph(session, result)
        finally:
            drop_session(session.run_id)

    async def _bridge_interrupts(self, result: dict[str, Any], session: RunSession) -> Any:
        """图内 interrupt() 暂停后，用现有 coordinator 等前端 POST /resume。"""
        from app.agent.harness.hitl import hitl_coordinator
        from app.api.monitor import monitor

        payloads = _interrupt_payloads(result)
        if not payloads:
            return True
        item = payloads[0]
        coordinator_payload = dict(item.get("coordinator_payload") or item)
        gate_type = str(
            coordinator_payload.get("gate_type")
            or item.get("kind")
            or "step"
        )
        step_index = int(coordinator_payload.get("step_index", item.get("step_index", -1)))
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

    async def _complete_from_graph(self, session: RunSession, graph_result: dict[str, Any]) -> Any:
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
            citation_manager.save_evidence_json(ctx.session_dir)

        finalize_outcome = self.harness.validator.validate_finalize(
            state,
            ctx.session_dir,
            citation_manager=citation_manager,
            min_citation_coverage=self.harness.harness_config.citations_min_coverage_rate,
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
        )
        session.result = result
        return result

    # --- nodes ---

    async def node_vanilla_agent(self, gstate: dict[str, Any]) -> dict[str, Any]:
        """对照实验：单 Worker + search/fetch/file，不跑 Brief/Plan/Progress。"""
        from app.agent.harness.state import ExecutionPlan, PlanStep
        from app.research.runtime.isolation import worker_row
        from app.research.runtime.project import apply_graph_to_loop
        from app.research.runtime.worker import LangChainWorkerRuntime, ResearchContext, ResearchTask

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
            bits = [str(item.get("summary") or "") for item in result.findings if isinstance(item, dict)]
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
                "payload": {"summary": result.summary, "facts": result.facts, "sources": result.sources},
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
            session.state = await self.harness._phase_understand(
                state,
                ctx.task_query,
                bool(ctx.uploaded_prompt),
            )
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
                        "planner_source": getattr(intent_obj, "planner_source", None) if intent_obj else None,
                        "intent_confidence": getattr(intent_obj, "intent_confidence", None) if intent_obj else None,
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

            session.state.plan = ExecutionPlan.from_dict(gstate["plan"])
            plan = session.state.plan
        else:
            session.state = await self.harness._phase_plan(session.state)
            if session.state.plan is not None:
                session.state.plan = annotate_plan_tasks(session.state.plan)
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
            session.state = await self.harness._phase_build_context(
                session.state, ctx.task_query
            )
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

        session = get_session(str(gstate.get("run_id") or ""))
        if session is None:
            return {"progress": "dispatch"}
        apply_graph_to_loop(session.state, gstate)
        if session.state.plan is None:
            return {"progress": "dispatch"}
        if self.harness._apply_run_guardrails(session.state, session.ctx.run_started):
            # 若仍可合成，优先走 synthesis 而不是直接 abort dump
            if isinstance(session.state.metadata, dict) and session.state.metadata.get("force_synthesis"):
                return {
                    "replan_exhausted": True,
                    "progress": "enough",
                    "progress_assessment": {
                        "verdict": "enough",
                        "reason": "force_synthesis_budget",
                    },
                    "status": "running",
                }
            return {
                "status": "aborted",
                "abort_reason": session.state.abort_reason or "guardrail",
                "progress": "abort",
            }
        # Research 触顶但未超 hard：阻止再开 research，推进 synthesis
        if isinstance(session.state.metadata, dict) and session.state.metadata.get("force_synthesis"):
            return {
                "replan_exhausted": True,
                "progress": "enough",
                "progress_assessment": {
                    "verdict": "enough",
                    "reason": "force_synthesis_budget",
                },
            }
        return {
            "replan_count": int(gstate.get("replan_count") or session.state.replan_count),
            "progress": "dispatch",
        }

    async def node_research_worker(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from langgraph.types import interrupt

        from app.research.runtime.isolation import worker_row
        from app.research.runtime.project import apply_graph_to_loop
        from app.research.runtime.worker import LangChainWorkerRuntime, ResearchContext, ResearchTask

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
                    "worker_results": [{
                        "task_id": task_id,
                        "ok": False,
                        "summary": "step_rejected",
                        "step_type": step.step_type,
                    }],
                }
            session.state = self.harness._apply_hitl_decisions(
                session.state, _as_decisions(resume), step, step_index
            )
            await self.harness._flush_hitl_memories(session.state)
            session.state.metadata["graph_step_gated"] = True
        runtime = LangChainWorkerRuntime(self.harness, session)
        result = await runtime.execute(
            ResearchTask(
                task_id=task_id,
                objective=str(step.objective or step.description or ""),
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
        outcome = result.raw
        tid = step.resolved_task_id(step_index)
        row = worker_row(tid, step, result.ok, getattr(outcome, "result", None)) if outcome is not None else {
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
        return {
            "worker_results": [row],
            "task_status": {tid: "done" if result.ok else "failed"},
            "evidence_refs": result.evidence_refs or ([tid] if result.ok else []),
            "findings": result.findings,
        }

    async def node_progress(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.planning.progress import assess_progress
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
                session.state.metadata["claim_reconciliation"] = reconciliation.to_dict()
        except Exception:
            reconciliation = None
        assessment = assess_progress(
            plan,
            task_status=dict(gstate.get("task_status") or {}),
            state=session.state if session is not None else None,
            worker_results=worker_rows,
            query=query,
            aborted=bool(
                (session is not None and session.state.abort_reason)
                or gstate.get("abort_reason")
            ),
            enabled=enabled,
            intent=session.state.intent if session is not None else gstate.get("intent"),
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
        if session is not None:
            session.state.metadata["progress_assessment"] = assessment.to_dict()
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
                run_id = str(gstate.get("run_id") or (session.session_id if session else "unknown"))
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
                    },
                    input_refs=[
                        item
                        for item in [
                            {"type": "research_brief", "id": brief_id} if brief_id else None,
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
            "progress": "progress_eval",
        }
        if assessment.verdict == "abort":
            payload["status"] = "aborted"
            payload["abort_reason"] = assessment.reason or "aborted"
        return payload

    async def node_synthesize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.scheduler import next_synthesis_step
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        plan = session.state.plan
        if plan is None:
            return {"status": "synthesized", "progress": "synthesized"}
        status = task_status_map(plan)
        nxt = next_synthesis_step(plan, status)
        if nxt is None:
            return {"status": "synthesized", "progress": "synthesized", "task_status": status}
        index, step = nxt
        session.state.step_index = index
        step.metadata["status"] = StepStatus.RUNNING.value
        tid = step.resolved_task_id(index)
        synth_span = ""
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
                        for src in list(getattr(session.ctx.citation_manager, "sources", []) or [])
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
                            {"type": "research_brief", "id": brief_id} if brief_id else None,
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
            try:
                from app.agent.harness.run_budget import get_or_create_run_budget

                mgr = get_or_create_run_budget(session.state, self.harness.harness_config)
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
                session.state.abort_reason = session.state.abort_reason or "deadline_exceeded"
                session.state.abort_message = session.state.abort_message or "synthesis step timeout"
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
                        for src in list(getattr(session.ctx.citation_manager, "sources", []) or [])
                        if getattr(src, "source_id", None)
                    ][:40]
                    claim_ids = [
                        f"c{i + 1}"
                        for i, _ in enumerate(
                            list(getattr(session.ctx.citation_manager, "fact_bindings", []) or [])[:24]
                        )
                    ]
                event_type = EventType.SYNTHESIS_COMPLETED if ok else EventType.SYNTHESIS_FAILED
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
                        "partial_delivered": bool(
                            isinstance(session.state.metadata, dict)
                            and session.state.metadata.get("partial_delivered")
                        ),
                    },
                    input_refs=[
                        item
                        for item in [
                            {"type": "research_brief", "id": brief_id} if brief_id else None,
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
        return {
            "task_status": {tid: "done" if ok else "failed"},
            "status": "synthesized" if ok else "partial",
            "progress": "synthesized",
            "replan_exhausted": True if not ok else gstate.get("replan_exhausted"),
        }

    async def node_replan(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.agent.harness.guardrails import can_replan
        from app.research.planning.plan_patch import apply_plan_patch, build_progress_patch
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
        policy = parse_source_policy(state.intent.raw_query)
        run_budget = {}
        if isinstance(getattr(state, "metadata", None), dict):
            raw = state.metadata.get("run_budget")
            if isinstance(raw, dict):
                run_budget = raw
        max_new = int(
            run_budget.get(
                "max_plan_patch_tasks",
                getattr(self.harness.harness_config, "planner_max_plan_patch_tasks", 2) or 2,
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
            from app.research.planning.effort import grant_on_gap, resolve_effective_budget

            effective = resolve_effective_budget(state.intent, self.harness.harness_config)
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
                target_gap_ids = list(patch.get("target_gap_ids") or assessment.get("open_gap_ids") or [])
                recorder.emit(
                    EventType.REPLAN_PROPOSED,
                    phase="recover",
                    status="proposed",
                    plan_version=int(getattr(state.plan, "plan_version", 1) or 1),
                    attributes={
                        "patch_id": str(patch.get("patch_id") or ""),
                        "triggered_by": str(patch.get("triggered_by") or assessment.get("progress_id") or ""),
                        "target_gap_ids": [str(x) for x in target_gap_ids if x],
                        "reason": str(patch.get("reason") or assessment.get("reason") or "semantic_gap"),
                        "gaps": list(assessment.get("gaps") or assessment.get("missing_dimensions") or assessment.get("coverage_gaps") or []),
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
                patch_id = str(patch.get("patch_id") or f"patch_{from_version}_{state.plan.plan_version}")
                store = get_payload_store()
                ref = store.put(
                    run_id=str(gstate.get("run_id") or session.session_id),
                    artifact_type="plan_patch",
                    artifact_id=patch_id,
                    payload=dict(patch),
                )
                target_gap_ids = [str(x) for x in (patch.get("target_gap_ids") or []) if x]
                recorder.emit(
                    EventType.REPLAN_APPLIED,
                    phase="recover",
                    status="applied",
                    plan_version=int(getattr(state.plan, "plan_version", 1) or 1),
                    attributes={
                        "patch_id": patch_id,
                        "triggered_by": str(patch.get("triggered_by") or assessment.get("progress_id") or ""),
                        "target_gap_ids": target_gap_ids,
                        "from_plan_version": from_version,
                        "to_plan_version": int(getattr(state.plan, "plan_version", 1) or 1),
                        "reason": str(patch.get("reason") or assessment.get("reason") or "semantic_gap"),
                        "gaps": list(assessment.get("gaps") or assessment.get("missing_dimensions") or assessment.get("coverage_gaps") or []),
                        "added_tasks": [tid for tid in added if tid],
                        "removed_tasks": [],
                        "remaining_budget": budget,
                        "patch_ref": ref.ref,
                        "patch_hash": ref.sha256,
                    },
                    input_refs=[
                        {"type": "progress", "id": str(assessment.get("progress_id") or "")},
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
            citation_manager.save_evidence_json(ctx.session_dir)
        outcome = self.harness.validator.validate_finalize(
            state,
            ctx.session_dir,
            citation_manager=citation_manager,
            min_citation_coverage=self.harness.harness_config.citations_min_coverage_rate,
        )
        await self.harness._phase_validate(
            state,
            outcome,
            step_index=state.step_index,
            scope="finalize",
        )
        passed = bool(outcome.passed or outcome.severity == "warning")
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
                        "severity": getattr(outcome, "severity", ""),
                        "citation_coverage_rate": getattr(state, "citation_coverage_rate", None),
                        "hallucination_rate": getattr(state, "hallucination_rate", None),
                        "unsupported_claim_rate": getattr(state, "hallucination_rate", None),
                        "conflict_disclosure_passed": (
                            True
                            if getattr(outcome, "reason", "")
                            not in {"conflict_not_disclosed", "unsupported_reconciled_value"}
                            else False
                        ),
                        "conflict_disclosure_reason": str(getattr(outcome, "reason", "") or ""),
                        "target_type": "synthesis",
                        "target_artifact_id": str((state.metadata or {}).get("answer_id") or ""),
                        "target_span_id": "",
                        "grader": "finalize_validator",
                        "grader_version": "v1",
                        "metric": "quality_gate",
                        "score": 1.0 if passed else 0.0,
                        "failure.origin_stage": "synthesis" if not passed else "",
                        "failure.detected_stage": "quality" if not passed else "",
                        "failure.cause_artifact_id": str((state.metadata or {}).get("answer_id") or ""),
                    },
                )
        except Exception:
            import logging as _log
            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        return {
            "quality_passed": passed,
            "final_content": state.final_content,
            "progress": "quality",
        }

    async def node_finalize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        if not str(session.state.final_content or "").strip() or (
            isinstance(session.state.metadata, dict)
            and session.state.metadata.get("synthesis_failed")
            and not session.state.metadata.get("partial_delivered")
        ):
            from app.agent.harness.partial_report import render_partial_report

            session.state.final_content = render_partial_report(
                state=session.state,
                abort_reason=str(session.state.abort_reason or gstate.get("status") or "incomplete"),
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
        result = await self.harness._phase_finalize(
            session.state,
            session.ctx.session_dir,
            success=success,
            started_at=session.ctx.run_started,
        )
        session.result = result
        return {
            "status": "completed" if result.status != "failed" else "aborted",
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
        "worker_results": [{
            "task_id": task_id,
            "ok": False,
            "summary": reason,
            "step_type": step_type,
        }],
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
    payload: dict[str, Any],
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
        if type(exc).__name__ not in {"GraphInterrupt", "NodeInterrupt", "GraphBubbleUp"}:
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
        "review_configs": [{"action_name": "task_intent", "allowed_decisions": allowed}],
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
                    "intent_confidence": state.intent.intent_confidence if state.intent else 1.0,
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
        return [item if isinstance(item, dict) else {"type": str(item)} for item in resume]
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
