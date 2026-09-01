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
        self.run_id: str = ctx.session_id
        self.session_id: str = ctx.session_id
        self.lock = ctx.lock
        self.result: Any = None
        max_workers = max(
            1, int(getattr(harness.harness_config, "max_parallel_workers", 3) or 3)
        )
        self.worker_sem = asyncio.Semaphore(max_workers)


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

    def compile(self, checkpointer: Any = None):
        from app.research.runtime.graph import compile_research_graph

        return compile_research_graph(
            checkpointer=checkpointer or _default_checkpointer(),
            runtime=self,
        )

    async def execute(self, ctx: Any, *, checkpointer: Any = None) -> Any:
        from langgraph.types import Command

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
        payload = empty_research_state(
            run_id=session.run_id,
            session_id=session.session_id,
            task_query=ctx.task_query,
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            max_tool_calls=self.harness.harness_config.max_tool_calls,
            max_replan_count=self.harness.harness_config.max_replan_count,
            search_mode=getattr(ctx, "search_mode", "auto") or "auto",
        )
        try:
            graph = self.compile(checkpointer=checkpointer)
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

    async def node_conversation(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.conversation.store import ConversationStore, rewrite_query

        session = _require_session(gstate)
        ctx = session.ctx
        store = ConversationStore.default(user_id=ctx.user_id or "me", project_id=ctx.project_id or "Inbox")
        thread = store.get(ctx.session_id)
        original = ctx.original_query or ctx.task_query
        resolved = rewrite_query(original, thread)
        ctx.original_query = original
        if resolved != ctx.task_query:
            ctx.task_query = resolved
        session.state.metadata["conversation_summary"] = thread.rolling_summary
        session.state.metadata["resolved_query"] = resolved
        return {
            "conversation_summary": thread.rolling_summary,
            "resolved_query": resolved,
            "progress": "conversation",
        }

    async def node_direct_answer(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.direct import compose_direct_answer, try_direct_llm
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        query = str(gstate.get("resolved_query") or session.ctx.original_query or session.ctx.task_query)
        answer = await try_direct_llm(
            query,
            conversation_summary=str(gstate.get("conversation_summary") or ""),
        )
        if not answer:
            answer = compose_direct_answer(query)
        session.state.final_content = answer
        session.state.phase = Phase.FINALIZE
        try:
            from app.api.monitor import monitor

            monitor.report_phase(
                "direct_answer",
                "done",
                session_id=session.session_id,
                search_mode="answer",
            )
        except Exception:
            pass
        return {
            "final_content": answer,
            "status": "completed",
            "quality_passed": True,
            "progress": "direct_answer",
            "plan": None,
        }

    async def node_mode_router(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.routing.mode_router import budget_for_mode, route
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        ctx = session.ctx
        personal = getattr(self.harness.harness_config, "personal_search", None) or {}
        decision = route(
            str(gstate.get("resolved_query") or ctx.task_query),
            user_mode=getattr(ctx, "search_mode", "auto") or "auto",
            conversation_summary=str(gstate.get("conversation_summary") or ""),
        )
        budget_cfg = budget_for_mode(decision.mode, personal)
        session.state.metadata["search_mode"] = decision.mode
        session.state.metadata["route_signals"] = decision.signals
        current = dict(gstate.get("budget") or {})
        current["max_tool_calls"] = int(budget_cfg["max_tool_calls"])
        current["max_replan_count"] = int(budget_cfg["max_replan_count"])
        try:
            from app.api.monitor import monitor

            monitor.report_phase(
                "mode_router",
                "done",
                session_id=session.session_id,
                search_mode=decision.mode,
                signals=decision.signals,
                user_override=decision.user_override,
            )
        except Exception:
            pass
        return {
            "search_mode": decision.mode,
            "route_signals": decision.signals,
            "budget": current,
            "progress": "routed",
        }

    async def node_quick_search(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.quick import run_quick_search

        session = _require_session(gstate)
        query = str(gstate.get("resolved_query") or session.ctx.task_query)
        try:
            cards = run_quick_search(query, max_queries=2, max_results=4)
        except Exception as exc:
            cards = []
            session.state.metadata["quick_search_error"] = str(exc)[:200]
        session.state.metadata["search_cards"] = cards
        return {"search_cards": cards, "progress": "quick_search"}

    async def node_quick_fetch(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.quick import run_quick_fetch

        session = _require_session(gstate)
        cards = list(gstate.get("search_cards") or [])
        try:
            fetched = run_quick_fetch(cards, limit=2)
        except Exception as exc:
            fetched = []
            session.state.metadata["quick_fetch_error"] = str(exc)[:200]
        session.state.metadata["fetched_pages"] = fetched
        refs = [str(item.get("artifact_id") or item.get("url") or "") for item in fetched if item]
        citation_manager = session.ctx.citation_manager
        if citation_manager is not None:
            from app.agent.harness.citations import EvidenceSource

            for i, item in enumerate(fetched or cards[:2]):
                url = str(item.get("url") or "")
                if not url:
                    continue
                citation_manager.sources.append(
                    EvidenceSource(
                        source_id=f"Q{i+1}",
                        step_index=0,
                        step_type="network_search",
                        source_kind="url",
                        locator=url,
                        excerpt=str(item.get("snippet") or "")[:400],
                        artifact_id=str(item.get("artifact_id") or ""),
                    )
                )
        return {"search_cards": cards, "evidence_refs": [r for r in refs if r], "progress": "quick_fetch"}

    async def node_quick_synthesize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.quick import compose_quick_answer

        session = _require_session(gstate)
        query = str(gstate.get("resolved_query") or session.ctx.original_query or session.ctx.task_query)
        cards = list(gstate.get("search_cards") or [])
        fetched = list((session.state.metadata or {}).get("fetched_pages") or [])
        answer = compose_quick_answer(query, cards, fetched or None)
        session.state.final_content = answer
        session.state.phase = Phase.FINALIZE
        return {
            "final_content": answer,
            "status": "completed",
            "quality_passed": True,
            "progress": "quick_synth",
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
        return {
            "intent": intent_payload,
            "brief": brief_from_intent(intent_payload),
            "needs_clarification": needs,
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
            return {
                "status": "aborted",
                "abort_reason": session.state.abort_reason or "guardrail",
                "progress": "abort",
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
        assessment = assess_progress(
            plan,
            task_status=dict(gstate.get("task_status") or {}),
            state=session.state if session is not None else None,
            worker_results=list(gstate.get("worker_results") or []),
            query=query,
            aborted=bool(
                (session is not None and session.state.abort_reason)
                or gstate.get("abort_reason")
            ),
            enabled=enabled,
            intent=session.state.intent if session is not None else gstate.get("intent"),
        )
        if session is not None:
            session.state.metadata["progress_assessment"] = assessment.to_dict()
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
        async with session.lock:
            ok = await self.harness._run_single_step(
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
        tid = step.resolved_task_id(index)
        return {
            "task_status": {tid: "done" if ok else "failed"},
            "status": "synthesized" if ok else "running",
            "progress": "synthesized",
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
        max_new = int(
            getattr(self.harness.harness_config, "planner_max_plan_patch_tasks", 2) or 2
        )
        patch = build_progress_patch(
            state.plan,
            state.intent,
            assessment=assessment,
            worker_results=list(gstate.get("worker_results") or []),
            max_new_tasks=max_new,
        )
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
            return exhausted
        state.plan = plan
        state.replan_count += 1
        self.harness._report_phase(
            Phase.REPLAN,
            "done",
            state=state,
            reason=str(patch.get("reason") or "semantic_gap"),
            new_steps=len(state.plan.steps),
        )
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
        return {
            "quality_passed": passed,
            "final_content": state.final_content,
            "progress": "quality",
        }

    async def node_finalize(self, gstate: dict[str, Any]) -> dict[str, Any]:
        from app.research.runtime.project import apply_graph_to_loop

        session = _require_session(gstate)
        apply_graph_to_loop(session.state, gstate)
        success = bool(gstate.get("quality_passed", True)) and not session.state.abort_reason
        result = await self.harness._phase_finalize(
            session.state,
            session.ctx.session_dir,
            success=success,
            started_at=session.ctx.run_started,
        )
        session.result = result
        _persist_conversation(session, result)
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
        _persist_conversation(session, result)
        return {
            "status": "aborted",
            "abort_reason": session.state.abort_reason or "aborted",
            "artifacts": list(result.artifacts),
            "progress": "abort",
        }


def _require_session(gstate: dict[str, Any]) -> RunSession:
    session = get_session(str(gstate.get("run_id") or ""))
    if session is None:
        raise RuntimeError("research graph session missing")
    return session


def _persist_conversation(session: RunSession, result: Any) -> None:
    try:
        from app.conversation.store import ConversationStore, ConversationTurn

        ctx = session.ctx
        store = ConversationStore.default(user_id=ctx.user_id or "me", project_id=ctx.project_id or "Inbox")
        user_text = ctx.original_query or ctx.task_query
        store.append_turn(ctx.session_id, ConversationTurn(role="user", content=user_text, run_id=session.run_id))
        assistant = str(getattr(result, "content", None) or session.state.final_content or "")
        sources: list[str] = []
        for card in list((session.state.metadata or {}).get("search_cards") or []):
            url = str((card or {}).get("url") or "")
            if url and url not in sources:
                sources.append(url)
        if assistant:
            store.append_turn(
                ctx.session_id,
                ConversationTurn(
                    role="assistant",
                    content=assistant[:4000],
                    run_id=session.run_id,
                    sources=sources[:12],
                ),
            )
    except Exception as exc:
        logger.warning("conversation persist skipped: %s", exc)


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


def _default_checkpointer():
    from app.research.runtime.checkpointer import default_research_checkpointer

    backend = None
    path = None
    try:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        backend = getattr(cfg, "graph_checkpoint_backend", None)
        path = getattr(cfg, "graph_checkpoint_path", None) or None
    except Exception:
        pass
    return default_research_checkpointer(backend=backend, path=path)


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
