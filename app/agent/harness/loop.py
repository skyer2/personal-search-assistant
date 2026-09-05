"""
Agent Harness 主循环

【Phase 3】per-step 执行 + Memory recall/remember + MCP 工具上下文。
【Phase 4】harness.yml 配置 + JSONL 结构化日志 + budget 守卫。
【Phase 5】HITL interrupt_on + step gate + Command(resume) 恢复。
【Phase 6】Citation-First + HITL Edit-in-the-Loop + Dynamic Re-plan。
【Phase 7】多 Agent 编排：检索并行 fan-out、步级 checkpoint、计划绑定、工人结构化回传。
【Phase 8】混合 Planner（规则+LLM）、结构化重试、evidence digest 写报告整合。
【Phase 9】可观测性快照 + JSONL 聚合 metrics API + Eval 扩展指标（JCR/OVR/tokens_saved）。
【Phase 11】上下文分层预算、prior 步数限制、untrusted 包裹、压缩阈值可配置。
【Phase 12】Memory TTL + user_id 解析 + 结构化 MemoryRecord + recall untrusted 包裹。
【Phase 15】生产级 Memory：SQLite + Hybrid Recall + 类型化 fact + 步内增量 + 治理/审计。
【Phase 13】运行时护栏：墙钟时限、重规划上限、计划步数上限、标准化 abort_reason。
【Phase 14】结构化槽位 + 置信度 + HITL 歧义澄清 + 默认 LLM Planner + Plan 校验强化。
【Phase 19】窗口卫生、分层预算淘汰、工作笔记、证据回读、压缩保留检查。
【Phase 20】检索步直调工人；LoopState 为任务进度唯一权威并写入 checkpoint.json。
【Phase 21】Domain Harness + create_agent Leaf；删除 Main DeepAgent 二次路由。
【Phase 22】生产调度切到 Research StateGraph；while 仅作 legacy 回退。
"""

import asyncio
import copy
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.agent.harness.window_hygiene import (
    apply_checkpoint_tool_hygiene,
    parallel_graph_thread_id,
    step_graph_thread_id,
)
from app.agent.harness.working_notes import render_working_notes, write_working_notes_file
from app.agent.harness.citations import CitationManager
from app.agent.harness.compressor import ContextCompressor
from app.agent.harness.context_builder import ContextBuilder
from app.agent.harness.artifacts import ArtifactStore, get_artifact_store, set_artifact_store, reset_artifact_store
from app.agent.harness.evidence_store import EvidenceStore, get_evidence_store, set_evidence_store, reset_evidence_store
from app.agent.harness.research_brief import compile_research_brief
from app.agent.harness.worker_profiles import resolve_worker_profile, tools_for_profile
from app.agent.harness.hitl import hitl_coordinator
from app.agent.harness.planner import (
    apply_intent_clarification,
    apply_plan_edits,
    auto_resolve_clarification,
    dynamic_replan,
    plan_to_editable_dict,
    should_request_plan_review,
)
from app.agent.harness.orchestration import (
    IdempotencyRegistry,
    JSON_ONLY_FAIL_REASONS,
    RETRIEVAL_STEP_TYPES,
    StepCheckpointStore,
    attach_structured_payload,
    build_strict_json_retry_instruction,
    check_subagent_binding,
    check_unauthorized_tools,
    extract_last_assistant_text,
    find_parallel_batch,
    is_assistant_message,
    message_text,
    parse_worker_payload,
    salvage_payload_from_artifacts,
    step_idempotency_key,
    task_query_fingerprint,
    validate_structured_worker_payload,
)
from app.agent.harness.loop_state_store import deserialize_loop_state, serialize_loop_state
from app.agent.harness.worker_runtime import resolve_execute_target
from app.agent.harness.guardrails import (
    GuardrailAction,
    can_replan,
    evaluate_run_guardrails,
)
from app.agent.harness.step_budget import retrieval_budget
from app.agent.harness.observability import build_observability_snapshot
from app.agent.harness.planner_llm import build_plan_for_intent, understand_intent
from app.agent.harness.recovery import RecoveryManager
from app.agent.harness.state import (
    HarnessResult,
    LoopState,
    Phase,
    PhaseEvent,
    PlanStep,
    StepResult,
    StepStatus,
)
from app.agent.harness.validator import ResultValidator
from app.agent.memory.extractor import MemoryExtractor
from app.agent.memory.identity import (
    MemoryIdentity,
    reset_memory_identity,
    resolve_memory_identity,
    set_memory_identity,
)
from app.agent.memory.models import MemoryType, MemoryWriteRequest, WriteSource
from app.agent.memory.policy import (
    SYNTHESIS_STEP_TYPES,
    get_memory_policy,
)
from app.agent.memory.provenance import provenance_from_step
from app.agent.memory.store import MemoryStore
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.api.monitor import monitor
from app.api.trace_logger import JsonlTraceLogger, get_trace_logger
from app.api.tracing import HarnessTracer, build_run_config
from app.config.loader import HarnessConfig, get_harness_config

logger = logging.getLogger(__name__)


@dataclass
class HarnessRunContext:
    """单次 run 的进程内句柄；大对象不进 Graph checkpoint。"""

    task_query: str
    session_id: str
    run_id: str
    user_id: str
    tenant_id: str
    project_id: str
    state: LoopState
    session_dir: Path
    relative_session_dir: str
    uploaded_prompt: str
    tokens: tuple
    tracer: Any
    citation_manager: Optional[CitationManager]
    checkpoint_store: StepCheckpointStore
    idempotency: IdempotencyRegistry
    identity_token: Any
    run_started: float
    policy_token: Any = None
    restored_full: bool = False
    step_index: int = 0
    original_query: str = ""
    search_mode: str = "agent"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    context_built: bool = False
    run_dir: Path | None = None
    artifact_dir: Path | None = None
    deliverable_dir: Path | None = None


class AgentHarness:
    def __init__(
        self,
        agent: Any,
        project_root: Path,
        validator: Optional[ResultValidator] = None,
        recovery: Optional[RecoveryManager] = None,
        compressor: Optional[ContextCompressor] = None,
        context_builder: Optional[ContextBuilder] = None,
        memory: Optional[MemoryStore] = None,
        memory_extractor: Optional[MemoryExtractor] = None,
        harness_config: Optional[HarnessConfig] = None,
        trace_logger: Optional[JsonlTraceLogger] = None,
        max_retries: Optional[int] = None,
        workers: Optional[dict[str, Any]] = None,
    ):
        self.harness_config = harness_config or get_harness_config()
        self.agent = agent
        self.workers = workers or {}
        self.project_root = project_root
        self._run_checkpoint_store: Optional[StepCheckpointStore] = None
        self._run_citation_manager: Optional[CitationManager] = None
        self.validator = validator or ResultValidator()
        self.recovery = recovery or RecoveryManager()
        self.compressor = compressor or ContextCompressor(
            retention_check=self.harness_config.compression_retention_check,
            min_url_retention=self.harness_config.compression_retention_min_url,
            min_number_retention=self.harness_config.compression_retention_min_number,
            reversible=getattr(self.harness_config, "context_reversible_compression", True),
        )
        self.context_builder = context_builder or ContextBuilder.from_harness_config()
        self.memory = memory or MemoryStore()
        self.memory_extractor = memory_extractor or MemoryExtractor()
        self.trace_logger = trace_logger or get_trace_logger(project_root)
        self.max_retries = (
            max_retries
            if max_retries is not None
            else self.harness_config.max_retries
        )
        self._current_tracer: Optional[HarnessTracer] = None
        self._current_trace_id: str = ""

    def _agent_for_step(self, step: PlanStep) -> tuple[Any, str]:
        profile = resolve_worker_profile(step.step_type, step.allowed_tools)
        return resolve_execute_target(
            step.step_type,
            workers=self.workers,
            main_agent=self.agent,
            direct_invoke=getattr(self.harness_config, "direct_worker_invoke", True),
            profile=profile,
        )

    def _action_idem_key(self, state: LoopState, step: PlanStep, step_index: int) -> str:
        from app.research.idempotency import action_idempotency_key

        plan_version = int(getattr(state.plan, "plan_version", 1) or 1) if state.plan else 1
        return action_idempotency_key(
            run_id=state.run_id or state.session_id,
            plan_version=plan_version,
            task_id=step.resolved_task_id(step_index),
            action_id="execute",
        )

    def _cached_step_result(
        self,
        idempotency: Optional[IdempotencyRegistry],
        state: LoopState,
        step: PlanStep,
        step_index: int,
    ) -> tuple[Optional[StepResult], str]:
        new_key = self._action_idem_key(state, step, step_index)
        if idempotency is None:
            return None, new_key
        cached = idempotency.get(new_key)
        if cached is None:
            cached = idempotency.get(
                step_idempotency_key(state.session_id, step_index, step.step_type)
            )
        return cached, new_key

    def _graph_thread_id(
        self,
        session_id: str,
        step_index: int,
        *,
        parallel: bool = False,
        run_id: str = "",
    ) -> str:
        if not self.harness_config.context_fresh_thread_per_step:
            return session_id
        if parallel:
            return parallel_graph_thread_id(session_id, step_index, run_id)
        return step_graph_thread_id(session_id, step_index, run_id)

    def _refresh_working_memory(
        self,
        state: LoopState,
        citation_manager: Optional[CitationManager],
        run_dir: Optional[Path] = None,
        task_query: str = "",
    ) -> None:
        sources = citation_manager.sources if citation_manager is not None else []
        query = task_query or (state.intent.raw_query if state.intent else "")
        if state.intent and getattr(state.intent, "brief", None) and not state.intent.brief.is_empty():
            state.research_brief_obj = state.intent.brief
            if state.plan and not state.plan.research_brief:
                state.plan.research_brief = state.research_brief_obj.objective
        elif state.intent and (state.plan or query):
            state.research_brief_obj = compile_research_brief(
                task_query=query,
                intent=state.intent,
                plan_brief=getattr(state.plan, "research_brief", "") if state.plan else "",
            )
            if state.plan and not state.plan.research_brief:
                state.plan.research_brief = state.research_brief_obj.objective
        if self.harness_config.context_working_notes_enabled:
            state.working_notes = render_working_notes(
                task_query=query,
                step_results=state.step_results,
                evidence_sources=sources,
            )
            if run_dir is not None:
                try:
                    write_working_notes_file(run_dir, state.working_notes)
                except Exception as exc:
                    print(f"[Context] working_notes write skipped: {exc}")
        if citation_manager is not None and self.harness_config.context_evidence_lookup_enabled:
            state.evidence_lookup_block = citation_manager.build_lookup_block()
            state.evidence_lookup = citation_manager.to_dict_list()
            if run_dir is not None:
                try:
                    citation_manager.save_evidence_json(run_dir, run_id=state.run_id or None)
                except Exception as exc:
                    print(f"[Context] evidence.json write skipped: {exc}")
        try:
            # Store 自带 run-scoped 目录；persist(None) 落回构造目录，禁止写 Session 根
            get_artifact_store().persist(None)
            get_evidence_store().persist(None)
            state.obs_artifacts_stored = len(get_artifact_store())
        except Exception as exc:
            print(f"[Context] artifact/evidence persist skipped: {exc}")

    async def _hygiene_checkpoint_messages(
        self,
        config: dict[str, Any],
        state: LoopState,
        snapshot: Any = None,
        *,
        agent: Any = None,
    ) -> int:
        """把过长 tool_result 换成占位符，按 message id 写回 checkpoint，供 HITL resume 继续。"""
        if not getattr(self.harness_config, "context_clear_bulky_tool_results", True):
            return 0
        target = agent if agent is not None else self.agent
        cleared = await apply_checkpoint_tool_hygiene(target, config, snapshot=snapshot)
        if cleared:
            state.obs_tool_results_cleared += cleared
        return cleared

    def _use_graph_runtime(self) -> bool:
        if not getattr(self.harness_config, "graph_runtime_enabled", False):
            return False
        try:
            from langgraph.graph import StateGraph  # noqa: F401
        except ImportError:
            logger.warning("graph_runtime_enabled 但未安装 langgraph，回退 legacy while")
            return False
        return True

    def _bootstrap_run(
        self,
        task_query: str,
        session_id: str,
        *,
        user_id: str = "",
        tenant_id: str = "",
        project_id: str = "",
        mode: str = "agent",
        run_id: str = "",
    ) -> HarnessRunContext:
        state = LoopState(session_id=session_id, max_retries=self.max_retries)
        state.metadata["strict_validation"] = self.harness_config.validation_strict_mode
        run_started = time.perf_counter()
        state.metadata["run_started_monotonic"] = float(run_started)
        # Absolute deadline clock — create RunBudgetManager at run start (no origin drift)
        try:
            from app.agent.harness.run_budget import get_or_create_run_budget

            get_or_create_run_budget(
                state,
                self.harness_config,
                run_started=run_started,
            )
        except Exception:
            pass
        state.metadata.setdefault(
            "latency",
            {
                "first_evidence_ms": None,
                "enough_evidence_ms": None,
                "final_answer_ms": None,
                "waves": [],
            },
        )
        self._current_trace_id = self.trace_logger.new_trace_id()
        from app.observability import get_recorder
        from app.observability.events import new_id

        run_id = run_id or new_id(16)
        obs_ctx = get_recorder().start_run(
            session_id=session_id,
            run_id=run_id,
            trace_id=self._current_trace_id,
            query_preview=task_query,
        )
        self._current_trace_id = obs_ctx.trace_id
        state.run_id = obs_ctx.run_id
        state.trace_id = obs_ctx.trace_id
        state.metadata["run_id"] = obs_ctx.run_id
        state.metadata["trace_id"] = obs_ctx.trace_id
        session_dir, relative_session_dir, uploaded_prompt, tokens = (
            self._prepare_session(session_id)
        )
        # P1 FollowUpResolver：跨 Run 上下文只能通过显式 RunSummary 引用流动
        try:
            from app.research.followup import resolve_followup

            followup = resolve_followup(
                task_query,
                session_dir,
                current_run_id=str(obs_ctx.run_id),
            )
            state.metadata["followup_context"] = followup.to_dict()
        except Exception as exc:
            logger.debug("followup resolve skipped: %s", exc)
        # Run 隔离：可变执行数据全部落 runs/{run_id}/，Session 根只放共享上传资产。
        run_dir = session_dir / "runs" / str(obs_ctx.run_id)
        state_dir = run_dir / "state"
        artifact_dir = run_dir / "artifacts"
        evidence_dir = run_dir / "evidence"
        deliverable_dir = run_dir / "deliverables"
        for directory in (state_dir, artifact_dir, evidence_dir, deliverable_dir):
            directory.mkdir(parents=True, exist_ok=True)
        tracer = HarnessTracer(session_id=session_id, task_query=task_query)
        tracer.start()
        self._current_tracer = tracer
        citation_manager = CitationManager() if self.harness_config.citations_enabled else None
        checkpoint_store = StepCheckpointStore(state_dir)
        self._run_checkpoint_store = checkpoint_store
        self._run_citation_manager = citation_manager
        idempotency = IdempotencyRegistry()
        state.task_fingerprint = task_query_fingerprint(task_query)
        memory_policy = get_memory_policy()
        identity = resolve_memory_identity(
            session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        identity_token = set_memory_identity(identity)
        policy_token = None
        artifact_store = ArtifactStore(artifact_dir)
        artifact_store.load(artifact_dir)
        evidence_store = EvidenceStore(evidence_dir)
        evidence_store.load(evidence_dir)
        set_artifact_store(artifact_store)
        set_evidence_store(evidence_store)
        state.memory_user_id = identity.user_id
        state.memory_tenant_id = identity.tenant_id
        state.memory_project_id = identity.project_id
        state.memory_identity_ephemeral = identity.ephemeral
        state.memory_wrap_untrusted = memory_policy.wrap_untrusted
        state.metadata["memory_identity"] = identity.to_dict()

        restored_full = False
        step_index = 0
        persist_loop = bool(getattr(self.harness_config, "persist_loop_state", False))
        # Graph 路径只从 LangGraph SQLite 恢复；不要再 hydrate LoopState checkpoint。
        if (
            persist_loop
            and not self._use_graph_runtime()
            and self.harness_config.step_checkpoint_enabled
            and self.harness_config.resume_checkpoint
        ):
            preview = checkpoint_store.load()
            if preview and preview.get("loop_state") and self._checkpoint_matches(
                preview, state
            ):
                state, step_index, restored_full = self._hydrate_loop_checkpoint(
                    state,
                    preview,
                    idempotency,
                    citation_manager,
                )
        return HarnessRunContext(
            task_query=task_query,
            session_id=session_id,
            run_id=state.run_id,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            state=state,
            session_dir=session_dir,
            relative_session_dir=relative_session_dir,
            uploaded_prompt=uploaded_prompt,
            tokens=tokens,
            tracer=tracer,
            citation_manager=citation_manager,
            checkpoint_store=checkpoint_store,
            idempotency=idempotency,
            identity_token=identity_token,
            run_started=run_started,
            policy_token=policy_token,
            restored_full=restored_full,
            step_index=step_index,
            search_mode=mode or "agent",
            original_query=task_query,
            run_dir=run_dir,
            artifact_dir=artifact_dir,
            deliverable_dir=deliverable_dir,
        )

    def _teardown_run(self, ctx: HarnessRunContext) -> None:
        self._current_tracer = None
        self._run_checkpoint_store = None
        self._run_citation_manager = None
        reset_artifact_store()
        reset_evidence_store()
        reset_session_context(ctx.tokens[0], ctx.tokens[1])
        reset_memory_identity(ctx.identity_token)

    async def run(
        self,
        task_query: str,
        session_id: str,
        *,
        user_id: str = "me",
        tenant_id: str = "local",
        project_id: str = "Inbox",
        mode: str = "agent",
        run_id: str = "",
    ) -> HarnessResult:
        ctx = self._bootstrap_run(
            task_query,
            session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            mode=mode,
            run_id=run_id,
        )
        state = ctx.state
        _project_run_running(ctx)
        try:
            if self._use_graph_runtime():
                from app.research.runtime.runner import ResearchGraphRunner

                return await ResearchGraphRunner(self).execute(ctx)
            logger.warning(
                "graph_runtime_enabled=false：_run_legacy_loop 已弃用，仅作显式回退"
            )
            return await self._run_legacy_loop(ctx)
        except asyncio.CancelledError:
            state.abort_reason = "cancelled"
            state.abort_message = "任务被取消"
            self._report_phase(Phase.ABORT, "cancelled", state=state)
            _project_run_interrupt(state.run_id, "cancelled")
            monitor.report_task_cancelled()
            ctx.tracer.finish({"status": "cancelled"})
            raise
        except Exception as e:
            state.abort_reason = "error"
            state.abort_message = str(e)
            self._report_phase(Phase.ABORT, "error", state=state, error=str(e))
            _project_run_fail(state.run_id, str(e))
            monitor._emit("error", f"Harness 执行异常：{str(e)}")
            ctx.tracer.finish({"status": "failed", "error": str(e)})
            return HarnessResult(
                session_id=session_id,
                status="failed",
                content=state.final_content,
                trace=state.trace,
                retry_count=state.retry_count,
                metadata={
                    "error": str(e),
                    "abort_reason": state.abort_reason,
                    "abort_message": state.abort_message,
                },
            )
        finally:
            self._teardown_run(ctx)

    async def _run_legacy_loop(self, ctx: HarnessRunContext) -> HarnessResult:
        """已弃用的 while 调度。生产路径是 Research StateGraph。"""
        state = ctx.state
        task_query = ctx.task_query
        session_id = ctx.session_id
        session_dir = ctx.session_dir
        relative_session_dir = ctx.relative_session_dir
        uploaded_prompt = ctx.uploaded_prompt
        citation_manager = ctx.citation_manager
        checkpoint_store = ctx.checkpoint_store
        idempotency = ctx.idempotency
        run_started = ctx.run_started
        restored_full = ctx.restored_full
        step_index = ctx.step_index

        if restored_full:
            waiting = dict((state.metadata or {}).get("hitl_waiting") or {})
            if waiting.get("gate_type") in {"clarification", "intent_clarification"}:
                state = await self._maybe_intent_clarification(state)
            elif waiting.get("gate_type") == "plan_review":
                state = await self._maybe_plan_hitl_review(state)
            state = await self._phase_build_context(state, task_query)
            monitor.report_session_dir(str(session_dir).replace("\\", "/"))
            if waiting.get("gate_type") == "interrupt_on":
                state.metadata.pop("hitl_waiting", None)
                step_index = int(waiting.get("step_index") or step_index)
        else:
            state = await self._phase_understand(state, task_query, bool(uploaded_prompt))
            state = await self._maybe_intent_clarification(state)
            state = await self._phase_plan(state)
            state = await self._maybe_plan_hitl_review(state)
            state = await self._phase_build_context(state, task_query)
            monitor.report_session_dir(str(session_dir).replace("\\", "/"))

            if not state.plan or not state.plan.steps:
                raise RuntimeError("Harness plan is empty")

            state, step_index, _ = self._try_restore_checkpoint(
                state,
                task_query,
                checkpoint_store,
                idempotency,
                citation_manager,
            )
            if not state.resumed_from_checkpoint:
                self._save_step_checkpoint(state, session_id, 0, checkpoint_store)

        if not state.plan or not state.plan.steps:
            raise RuntimeError("Harness plan is empty")

        while step_index < len(state.plan.steps):
            step = state.plan.steps[step_index]
            if str(step.metadata.get("status") or "") == StepStatus.SKIPPED.value:
                # 预算降级 / early-stop 跳过的检索步：直接推进，让合成步继续
                step_index += 1
                continue
            state.step_index = step_index
            step.metadata["status"] = StepStatus.RUNNING.value
            if self._apply_run_guardrails(state, run_started):
                self._report_phase(
                    Phase.ABORT,
                    state.abort_reason or "guardrail",
                    state=state,
                    tool_calls=state.tool_calls_count,
                    abort_reason=state.abort_reason,
                    abort_message=state.abort_message,
                )
                break
            if str(step.metadata.get("status") or "") == StepStatus.SKIPPED.value:
                # 护栏降级把当前检索步标记为 skipped：不再执行，推进到合成步
                step_index += 1
                continue

            batch_indices = find_parallel_batch(
                state.plan.steps,
                step_index,
                enabled=(
                    self.harness_config.parallel_retrieval_enabled
                    and not (
                        self.harness_config.hitl_enabled
                        and any(
                            candidate.step_type
                            in self.harness_config.hitl_step_gate_types
                            for candidate in state.plan.steps[step_index:]
                            if candidate.metadata.get("parallel_group")
                            == step.metadata.get("parallel_group")
                        )
                    )
                ),
            )
            if len(batch_indices) >= 2:
                batch_passed = await self._run_parallel_retrieval_batch(
                    state,
                    batch_indices,
                    task_query,
                    relative_session_dir,
                    uploaded_prompt,
                    session_id,
                    session_dir,
                    citation_manager,
                    idempotency,
                    checkpoint_store,
                )
                for idx in batch_indices:
                    state.step_validation_results.append(
                        {
                            "step_index": idx,
                            "step_type": state.plan.steps[idx].step_type,
                            "passed": batch_passed,
                            "parallel": True,
                        }
                    )
                if not batch_passed:
                    if can_replan(state, self.harness_config):
                        state.plan = dynamic_replan(
                            state.plan,
                            batch_indices[-1],
                            "step_failed",
                        )
                        state.replan_count += 1
                        step_index = batch_indices[-1] + 1
                        continue
                    break
                step_index = batch_indices[-1] + 1
                continue

            step_passed = await self._run_single_step(
                state,
                step,
                step_index,
                task_query,
                relative_session_dir,
                uploaded_prompt,
                session_id,
                session_dir,
                citation_manager,
                idempotency,
                checkpoint_store,
            )
            state.step_validation_results.append(
                {
                    "step_index": step_index,
                    "step_type": step.step_type,
                    "passed": step_passed,
                }
            )
            if not step_passed:
                step.metadata["status"] = StepStatus.FAILED.value
                if can_replan(state, self.harness_config):
                    state.plan = dynamic_replan(
                        state.plan,
                        step_index,
                        "step_failed",
                    )
                    state.replan_count += 1
                    self._report_phase(
                        Phase.REPLAN,
                        "done",
                        state=state,
                        step_index=step_index,
                        reason="step_failed",
                        new_steps=len(state.plan.steps),
                    )
                    step_index += 1
                    continue
                break
            step.metadata["status"] = StepStatus.DONE.value
            step_index += 1

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
        if state.run_id:
            citation_manager.save_evidence_json(
                session_dir / "runs" / str(state.run_id),
                run_id=state.run_id,
            )

        finalize_outcome = self.validator.validate_finalize(
            state,
            session_dir,
            citation_manager=citation_manager,
            min_citation_coverage=self.harness_config.citations_min_coverage_rate,
            deliverable_dir=ctx.deliverable_dir,
        )
        state = await self._phase_validate(
            state,
            finalize_outcome,
            step_index=state.step_index,
            scope="finalize",
        )
        success = (
            (finalize_outcome.passed or finalize_outcome.severity == "warning")
            and not state.abort_reason
        )

        return await self._phase_finalize(
            state,
            session_dir,
            success=success,
            started_at=run_started,
            deliverable_dir=ctx.deliverable_dir,
        )


    async def _execute_and_validate_step(
        self,
        state: LoopState,
        step: PlanStep,
        step_index: int,
        task_query: str,
        relative_session_dir: str,
        uploaded_prompt: str,
        session_id: str,
        session_dir: Path,
        citation_manager: Optional[CitationManager],
        timeout_sec: int,
        context_builder: Optional[ContextBuilder] = None,
        run_session_id: str = "",
        json_only: bool = False,
    ) -> tuple[bool, StepResult, str]:
        """执行单步（含超时、结构化重试）并校验，不写入 state.step_results。"""
        max_attempts = (
            2
            if (
                self.harness_config.structured_output_retry
                and step.step_type in {"network_search", "research"}
            )
            else 1
        )
        extra_instruction = (
            build_strict_json_retry_instruction(step) if json_only else ""
        )
        result: Optional[StepResult] = None
        fail_reason = ""
        json_only_attempt = json_only

        for attempt in range(max_attempts):
            try:
                result = await asyncio.wait_for(
                    self._phase_execute_step(
                        state,
                        step,
                        step_index,
                        task_query,
                        relative_session_dir,
                        uploaded_prompt,
                        session_id,
                        extra_instruction=extra_instruction,
                        context_builder=context_builder,
                        run_session_id=run_session_id,
                        json_only=json_only_attempt,
                    ),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                timeout_meta: dict[str, Any] = {
                    "step_timeout": True,
                    "timeout_sec": timeout_sec,
                    "worker_dispatch": "direct",
                }
                if step.subagent:
                    timeout_meta["step_assistants_called"] = [step.subagent]
                result = StepResult(
                    step_type=step.step_type,
                    content="步骤执行超时",
                    metadata=timeout_meta,
                )

            assert result is not None
            result = self._enrich_worker_result(step, result, state)
            structured_ok, struct_reason = self._check_structured_output(step, result)
            if step.step_type in {"network_search", "research"}:
                state.obs_structured_checks += 1
                if structured_ok:
                    state.obs_structured_passes += 1
            if structured_ok or attempt >= max_attempts - 1:
                break
            if result.metadata.get("step_timeout"):
                break
            state.obs_structured_retries += 1
            extra_instruction = build_strict_json_retry_instruction(step)
            json_only_attempt = True
            self._report_phase(
                Phase.RECOVER,
                "structured_retry",
                state=state,
                step_index=step_index,
                attempt=attempt + 1,
                reason=struct_reason,
            )

        assert result is not None
        result = await self._phase_compress_step(
            state, result, step_index, citation_manager
        )
        outcome = self.validator.validate_step(step, result, session_dir, state)
        await self._phase_validate(
            state,
            outcome,
            step_index=step_index,
            scope="step",
        )
        passed = outcome.passed or outcome.severity == "warning"
        return passed, result, outcome.reason

    def _check_structured_output(self, step: PlanStep, result: StepResult) -> tuple[bool, str]:
        """【Phase 8】子 Agent 步是否满足结构化 JSON 要求。"""
        if not self.harness_config.require_structured_worker_output:
            return True, ""
        payload_raw = (result.metadata or {}).get("worker_payload") or {}
        if not isinstance(payload_raw, dict):
            return False, "invalid_structured_output"
        from app.agent.harness.orchestration import WorkerResultPayload

        payload = WorkerResultPayload(
            ok=bool(payload_raw.get("ok", True)),
            summary=str(payload_raw.get("summary", "")),
            facts=list(payload_raw.get("facts") or []),
            sources=list(payload_raw.get("sources") or []),
            findings=list(payload_raw.get("findings") or []),
            confidence=float(payload_raw.get("confidence", 1.0) or 1.0),
            error_code=str(payload_raw.get("error_code", "")),
            worker=str(payload_raw.get("worker", "")),
            step_type=str(payload_raw.get("step_type", step.step_type)),
        )
        return validate_structured_worker_payload(
            payload,
            step,
            require_json=True,
        )

    async def _run_single_step(
        self,
        state: LoopState,
        step: PlanStep,
        step_index: int,
        task_query: str,
        relative_session_dir: str,
        uploaded_prompt: str,
        session_id: str,
        session_dir: Path,
        citation_manager: Optional[CitationManager] = None,
        idempotency: Optional[IdempotencyRegistry] = None,
        checkpoint_store: Optional[StepCheckpointStore] = None,
    ) -> bool:
        cached, idem_key = self._cached_step_result(
            idempotency, state, step, step_index
        )
        if idempotency is not None:
            if cached is not None:
                state.step_results.append(cached)
                state.final_content = cached.compressed_content or cached.content
                if idem_key not in state.completed_step_keys:
                    state.completed_step_keys.append(idem_key)
                return True

        step_retry = 0
        timeout_sec = max(10, int(self.harness_config.step_timeout_sec))
        json_only = False
        while step_retry <= state.max_retries:
            passed, result, fail_reason = await self._execute_and_validate_step(
                state,
                step,
                step_index,
                task_query,
                relative_session_dir,
                uploaded_prompt,
                session_id,
                session_dir,
                citation_manager,
                timeout_sec=timeout_sec,
                run_session_id=self._graph_thread_id(
                    session_id, step_index, run_id=str(state.run_id or "")
                ),
                json_only=json_only,
            )
            if passed:
                state.step_results.append(result)
                state.final_content = result.compressed_content or result.content
                await self._maybe_remember_step(state, step, result)
                self._refresh_working_memory(
                    state,
                    citation_manager,
                    session_dir / "runs" / str(state.run_id or "") if state.run_id else session_dir,
                    task_query,
                )
                if idempotency is not None:
                    idempotency.register(idem_key, result)
                state.completed_step_keys.append(idem_key)
                self._save_step_checkpoint(
                    state,
                    session_id,
                    step_index + 1,
                    checkpoint_store,
                )
                return True

            if step_retry >= state.max_retries:
                return False

            state = await self._phase_recover(state, fail_reason, step_index)
            if (
                can_replan(state, self.harness_config)
                and not state.metadata.get("graph_runtime")
                and fail_reason
                in {"sql_empty", "search_too_short", "wrong_subagent", "step_timeout"}
            ):
                state.plan = dynamic_replan(state.plan, step_index, fail_reason)
                state.replan_count += 1
                self._report_phase(
                    Phase.REPLAN,
                    "done",
                    state=state,
                    step_index=step_index,
                    reason=fail_reason,
                    new_steps=len(state.plan.steps),
                )
            step_retry += 1
            state.retry_count += 1
            json_only = fail_reason in JSON_ONLY_FAIL_REASONS

        return False

    async def _run_parallel_retrieval_batch(
        self,
        state: LoopState,
        batch_indices: list[int],
        task_query: str,
        relative_session_dir: str,
        uploaded_prompt: str,
        session_id: str,
        session_dir: Path,
        citation_manager: Optional[CitationManager],
        idempotency: IdempotencyRegistry,
        checkpoint_store: Optional[StepCheckpointStore],
    ) -> bool:
        """【Phase 7】无依赖检索步 fan-out + join。"""
        state.phase = Phase.PARALLEL_EXECUTE
        self._report_phase(
            Phase.PARALLEL_EXECUTE,
            "start",
            state=state,
            batch_indices=batch_indices,
            batch_size=len(batch_indices),
        )
        hard_parallel = max(1, int(self.harness_config.max_parallel_workers))
        run_budget_parallel = hard_parallel
        if isinstance(getattr(state, "metadata", None), dict):
            raw_budget = state.metadata.get("run_budget")
            if isinstance(raw_budget, dict) and raw_budget.get("max_parallel_workers") is not None:
                run_budget_parallel = max(
                    1, min(hard_parallel, int(raw_budget["max_parallel_workers"]))
                )
        sem = asyncio.Semaphore(run_budget_parallel)
        timeout_sec = max(10, int(self.harness_config.step_timeout_sec))

        async def _run_one(
            idx: int,
        ) -> tuple[int, bool, Optional[StepResult], str, Optional[LoopState]]:
            step = state.plan.steps[idx]
            cached, idem_key = self._cached_step_result(idempotency, state, step, idx)
            if cached is not None:
                return idx, True, cached, "", None

            # fan-out 任务只读父状态，并在独立副本上累计 trace/counter。
            # join 阶段按 step_index 单线程合并，杜绝共享 LoopState 的竞态。
            child_state = copy.deepcopy(state)
            child_state.metadata["_parallel_child"] = True
            child_state.step_index = idx
            child_state.trace = []
            child_state.assistants_called = []
            child_state.compression_ratios = []
            child_state.tool_calls_count = 0
            child_state.obs_entity_retention_rates = []
            child_state.graph_thread_ids = []
            for field_name in (
                "obs_structured_checks",
                "obs_structured_passes",
                "obs_structured_retries",
                "obs_orchestration_violations",
                "obs_binding_violations",
                "obs_unauthorized_tool_hits",
                "obs_estimated_tokens_saved",
                "obs_step_message_tokens_peak",
                "obs_context_budget_trims",
                "obs_fresh_threads",
                "obs_retention_patches",
                "obs_tool_results_cleared",
            ):
                setattr(child_state, field_name, 0)

            try:
                async with sem:
                    passed, result, fail_reason = await self._execute_and_validate_step(
                        child_state,
                        step,
                        idx,
                        task_query,
                        relative_session_dir,
                        uploaded_prompt,
                        session_id,
                        session_dir,
                        None,
                        timeout_sec=timeout_sec,
                        context_builder=ContextBuilder.from_harness_config(),
                        run_session_id=self._graph_thread_id(
                            session_id,
                            idx,
                            parallel=True,
                            run_id=str(state.run_id or ""),
                        ),
                    )
            except Exception as exc:
                result = StepResult(
                    step_type=step.step_type,
                    content="并行步骤执行异常",
                    metadata={"parallel_error": str(exc)},
                )
                passed = False
                fail_reason = "parallel_step_error"
            return (
                idx,
                passed,
                result if passed else None,
                fail_reason,
                child_state,
            )

        raw = await asyncio.gather(
            *[_run_one(idx) for idx in batch_indices],
        )

        all_passed = True
        ordered: list[tuple[int, StepResult, Optional[LoopState]]] = []
        for item in sorted(raw, key=lambda x: x[0]):
            idx, passed, result, _reason, child_state = item
            if not passed or result is None:
                all_passed = False
                if child_state is not None:
                    self._merge_parallel_child_state(state, child_state)
                state.plan.steps[idx].metadata["status"] = StepStatus.FAILED.value
                continue
            ordered.append((idx, result, child_state))
            state.plan.steps[idx].metadata["status"] = StepStatus.DONE.value

        for idx, result, child_state in ordered:
            idem_key = self._action_idem_key(state, state.plan.steps[idx], idx)
            if child_state is not None:
                self._merge_parallel_child_state(state, child_state)
            if citation_manager is not None:
                registered = citation_manager.register_from_step(
                    idx,
                    result.step_type,
                    result.content,
                    result.metadata,
                )
                if registered:
                    evidence = [source.__dict__.copy() for source in registered]
                    result.metadata["evidence_sources"] = evidence
                    source_meta = result.metadata.setdefault("source_metadata", {})
                    if isinstance(source_meta, dict):
                        source_meta["source_ids"] = [
                            source.source_id for source in registered
                        ]
                payload = (result.metadata or {}).get("worker_payload") or {}
                if isinstance(payload, dict):
                    citation_manager.bind_worker_facts(
                        idx,
                        result.step_type,
                        list(payload.get("facts") or []),
                        list(payload.get("sources") or []),
                    )
            state.step_results.append(result)
            step = state.plan.steps[idx]
            await self._maybe_remember_step(state, step, result)
            idempotency.register(idem_key, result)
            if idem_key not in state.completed_step_keys:
                state.completed_step_keys.append(idem_key)
            for assistant in result.metadata.get("step_assistants_called") or []:
                if assistant not in state.assistants_called:
                    state.assistants_called.append(assistant)
        if ordered:
            last_result = ordered[-1][1]
            state.final_content = last_result.compressed_content or last_result.content
        self._refresh_working_memory(
            state,
            citation_manager,
            session_dir / "runs" / str(state.run_id or "") if state.run_id else session_dir,
            task_query,
        )

        if all_passed and checkpoint_store is not None:
            next_index = batch_indices[-1] + 1
            self._save_step_checkpoint(state, session_id, next_index, checkpoint_store)

        state.obs_parallel_batch_count += 1
        state.obs_parallel_steps_executed += len(batch_indices)
        self._report_phase(
            Phase.PARALLEL_EXECUTE,
            "done" if all_passed else "failed",
            state=state,
            batch_indices=batch_indices,
            passed=all_passed,
            timeout_sec=timeout_sec,
        )
        return all_passed

    @staticmethod
    def _merge_parallel_child_state(parent: LoopState, child: LoopState) -> None:
        """只合并可加和/可取最大值的执行增量，不覆盖父状态权威字段。"""
        parent.trace.extend(child.trace)
        parent.tool_calls_count += child.tool_calls_count
        parent.compression_ratios.extend(child.compression_ratios)
        for field_name in (
            "obs_structured_checks",
            "obs_structured_passes",
            "obs_structured_retries",
            "obs_orchestration_violations",
            "obs_binding_violations",
            "obs_unauthorized_tool_hits",
            "obs_estimated_tokens_saved",
            "obs_context_budget_trims",
            "obs_fresh_threads",
            "obs_retention_patches",
            "obs_tool_results_cleared",
        ):
            setattr(
                parent,
                field_name,
                getattr(parent, field_name) + getattr(child, field_name),
            )
        parent.obs_step_message_tokens_peak = max(
            parent.obs_step_message_tokens_peak,
            child.obs_step_message_tokens_peak,
        )
        parent.obs_entity_retention_rates.extend(
            getattr(child, "obs_entity_retention_rates", []) or []
        )
        parent.graph_thread_ids.extend(getattr(child, "graph_thread_ids", []) or [])

    def _enrich_worker_result(
        self,
        step: PlanStep,
        result: StepResult,
        state: LoopState,
    ) -> StepResult:
        """【Phase 7】结构化解析 + 计划绑定 / 越权工具校验。"""
        payload = parse_worker_payload(
            result.content,
            step_type=step.step_type,
            subagent=step.subagent or "",
        )
        if not (payload.facts or payload.sources or payload.findings):
            payload = salvage_payload_from_artifacts(
                payload,
                step=step,
                step_index=int(result.metadata.get("step_index") or state.step_index or 0),
            )
            if payload.facts or payload.sources or payload.findings:
                result.metadata["salvaged_from_artifacts"] = True
        attach_structured_payload(result, payload)
        self._ingest_evidence(step, result, payload, state)

        tools_invoked = list(result.metadata.get("tools_invoked") or [])
        enforce = self.harness_config.enforce_subagent_binding
        dispatch = str(result.metadata.get("worker_dispatch") or "")
        if dispatch != "direct":
            assistants_for_binding = list(
                result.metadata.get("step_assistants_called") or state.assistants_called
            )
            binding_ok, binding_reason = check_subagent_binding(
                step,
                assistants_for_binding,
                enforce=enforce,
            )
            if not binding_ok:
                result.metadata["binding_failed"] = True
                result.metadata["error_code"] = binding_reason
                payload.ok = False
                payload.error_code = binding_reason
                attach_structured_payload(result, payload)
                state.obs_binding_violations += 1
                state.obs_orchestration_violations += 1

        auth_ok, unauthorized = check_unauthorized_tools(
            step,
            tools_invoked,
            enforce=enforce,
        )
        if not auth_ok:
            result.metadata["unauthorized_tools"] = unauthorized
            payload.ok = False
            payload.error_code = "unauthorized_tool"
            attach_structured_payload(result, payload)
            state.obs_unauthorized_tool_hits += len(unauthorized)
            state.obs_orchestration_violations += 1
        elif (
            (payload.facts or payload.sources or payload.findings)
            and not result.metadata.get("step_timeout")
        ):
            payload.ok = True
            payload.error_code = ""
            attach_structured_payload(result, payload)

        struct_ok, struct_reason = validate_structured_worker_payload(
            payload,
            step,
            require_json=self.harness_config.require_structured_worker_output,
        )
        if struct_ok:
            result.metadata.pop("invalid_structured_output", None)
        else:
            result.metadata["invalid_structured_output"] = True
            result.metadata["error_code"] = struct_reason
            payload.ok = False
            attach_structured_payload(result, payload)
        return result

    def _ingest_evidence(
        self,
        step: PlanStep,
        result: StepResult,
        payload: Any,
        state: LoopState,
    ) -> None:
        store = get_evidence_store()
        artifacts = get_artifact_store()
        artifact_ids = list(getattr(payload, "artifact_ids", None) or [])
        meta = result.metadata or {}
        if meta.get("artifact_id"):
            artifact_ids.append(str(meta["artifact_id"]))
        payload_dict = {
            "facts": list(getattr(payload, "facts", None) or []),
            "sources": list(getattr(payload, "sources", None) or []),
            "findings": list(getattr(payload, "findings", None) or []),
            "conflicts": list(getattr(payload, "conflicts", None) or []),
            "confidence": getattr(payload, "confidence", 1.0),
            "evidence_ids": list(getattr(payload, "evidence_ids", None) or []),
        }
        step_index = int(meta.get("step_index") or state.step_index or 0)
        findings = store.ingest_worker_payload(
            payload_dict,
            artifact_ids=artifact_ids,
            step_index=step_index,
            step_type=step.step_type,
            artifact_store=artifacts,
        )
        eids = []
        for finding in findings:
            eids.extend(list(finding.evidence_ids or []))
        payload.evidence_ids = list(dict.fromkeys(list(payload.evidence_ids or []) + eids))
        payload.findings = [finding.to_dict() for finding in findings] or payload.findings
        attach_structured_payload(result, payload)
        if self._run_citation_manager is not None and eids:
            already = {
                str(getattr(src, "evidence_id", "") or "")
                for src in self._run_citation_manager.sources
            }
            new_spans = [
                store.spans[eid]
                for eid in eids
                if eid in store.spans and eid not in already
            ]
            if new_spans:
                self._run_citation_manager.bind_evidence_spans(new_spans, findings)
        state.obs_evidence_used_count = len(store.findings)
        state.obs_artifacts_stored = len(artifacts)

    def _checkpoint_matches(self, data: dict[str, Any] | None, state: LoopState) -> bool:
        if not data:
            return False
        if data.get("task_fingerprint") != state.task_fingerprint:
            return False
        if data.get("session_id") != state.session_id:
            return False
        return True

    def _hydrate_loop_checkpoint(
        self,
        state: LoopState,
        data: dict[str, Any],
        idempotency: IdempotencyRegistry,
        citation_manager: Optional[CitationManager],
    ) -> tuple[LoopState, int, bool]:
        state = deserialize_loop_state(data.get("loop_state") or {}, base=state)
        if citation_manager is not None:
            citation_manager.load_from_snapshot(data.get("citation_snapshot"))
            if state.evidence_lookup and not citation_manager.sources:
                citation_manager.load_from_snapshot({"sources": state.evidence_lookup})
            if citation_manager.fact_bindings:
                state.metadata["citation_fact_bindings"] = list(
                    citation_manager.fact_bindings
                )
        if not state.step_results:
            rows = data.get("step_results") or []
            state.step_results = [
                StepResult(
                    step_type=str(row.get("step_type", "")),
                    content=str(row.get("content", "")),
                    compressed_content=row.get("compressed_content"),
                    metadata=dict(row.get("metadata") or {}),
                )
                for row in rows
                if isinstance(row, dict)
            ]
        keys = list(data.get("completed_step_keys") or state.completed_step_keys)
        for key, result in zip(keys, state.step_results):
            idempotency.register(key, result)
        next_index = int(data.get("next_step_index") or state.step_index or 0)
        if state.plan:
            for idx in range(min(next_index, len(state.plan.steps))):
                state.plan.steps[idx].metadata["status"] = StepStatus.DONE.value
        return state, next_index, True

    def _try_restore_checkpoint(
        self,
        state: LoopState,
        task_query: str,
        checkpoint_store: StepCheckpointStore,
        idempotency: IdempotencyRegistry,
        citation_manager: Optional[CitationManager] = None,
    ) -> tuple[LoopState, int, bool]:
        if not (
            self.harness_config.step_checkpoint_enabled
            and self.harness_config.resume_checkpoint
        ):
            return state, 0, False

        data = checkpoint_store.load()
        if not self._checkpoint_matches(data, state):
            return state, 0, False
        assert data is not None

        if data.get("loop_state") and getattr(self.harness_config, "persist_loop_state", False):
            state, next_index, _ = self._hydrate_loop_checkpoint(
                state, data, idempotency, citation_manager
            )
            for idx in range(min(next_index, len(state.plan.steps) if state.plan else 0)):
                state.plan.steps[idx].metadata["status"] = StepStatus.DONE.value
            self._emit_checkpoint_resumed(state, data, next_index, authority="loop_state")
            self._report_phase(
                Phase.BUILD_CONTEXT,
                "checkpoint_resumed",
                state=state,
                next_step_index=next_index,
                restored_steps=len(state.step_results),
                authority="loop_state",
            )
            return state, next_index, True

        state.step_results = checkpoint_store.restore_step_results(data)
        state.assistants_called = list(data.get("assistants_called") or [])
        state.completed_step_keys = list(data.get("completed_step_keys") or [])
        idempotency.load_from_checkpoint(data, checkpoint_store)
        state.resumed_from_checkpoint = True
        next_index = int(data.get("next_step_index") or 0)
        for idx in range(min(next_index, len(state.plan.steps) if state.plan else 0)):
            state.plan.steps[idx].metadata["status"] = StepStatus.DONE.value
        self._emit_checkpoint_resumed(state, data, next_index, authority="legacy")
        self._report_phase(
            Phase.BUILD_CONTEXT,
            "checkpoint_resumed",
            state=state,
            next_step_index=next_index,
            restored_steps=len(state.step_results),
            authority="legacy",
        )
        return state, next_index, False

    def _emit_checkpoint_resumed(
        self,
        state: LoopState,
        data: dict[str, Any],
        next_index: int,
        *,
        authority: str,
    ) -> None:
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if not recorder.is_active:
                return
            completed = list(data.get("completed_step_keys") or state.completed_step_keys or [])
            pending = []
            if state.plan is not None:
                for i, step in enumerate(state.plan.steps):
                    tid = step.resolved_task_id(i)
                    if i >= next_index:
                        pending.append(tid)
            recorder.emit(
                EventType.CHECKPOINT_RESUMED,
                phase="build_context",
                status="ok",
                attributes={
                    "checkpoint_id": str(data.get("checkpoint_id") or data.get("task_fingerprint") or ""),
                    "resume_from_checkpoint_id": str(data.get("checkpoint_id") or ""),
                    "plan_version": int(getattr(state.plan, "plan_version", 1) or 1) if state.plan else None,
                    "completed_task_ids": completed[:40],
                    "pending_task_ids": pending[:40],
                    "last_seq": data.get("last_seq"),
                    "state_hash": str(data.get("state_hash") or data.get("task_fingerprint") or "")[:64],
                    "replayed_actions": int(data.get("replayed_actions") or len(state.step_results) or 0),
                    "skipped_idempotent_actions": int(data.get("skipped_idempotent_actions") or 0),
                    "authority": authority,
                },
            )
        except Exception:
            import logging as _log
            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)

    def _save_step_checkpoint(
        self,
        state: LoopState,
        session_id: str,
        next_step_index: int,
        checkpoint_store: Optional[StepCheckpointStore],
    ) -> None:
        if not self.harness_config.step_checkpoint_enabled or checkpoint_store is None:
            return
        loop_payload = None
        citation_snapshot = None
        if getattr(self.harness_config, "persist_loop_state", False):
            loop_payload = serialize_loop_state(state)
            loop_payload["step_index"] = next_step_index
            if self._run_citation_manager is not None:
                citation_snapshot = self._run_citation_manager.checkpoint_snapshot()
                loop_payload["citation_fact_bindings"] = list(
                    self._run_citation_manager.fact_bindings
                )
        checkpoint_store.save(
            session_id=session_id,
            task_fingerprint=state.task_fingerprint,
            next_step_index=next_step_index,
            step_results=state.step_results,
            assistants_called=state.assistants_called,
            completed_keys=state.completed_step_keys,
            plan_summary=state.plan.summary if state.plan else "",
            loop_state=loop_payload,
            citation_snapshot=citation_snapshot,
        )
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                pending = []
                completed = list(state.completed_step_keys or [])
                if state.plan is not None:
                    for i, step in enumerate(state.plan.steps):
                        tid = step.resolved_task_id(i)
                        if i >= next_step_index:
                            pending.append(tid)
                recorder.emit(
                    EventType.CHECKPOINT_SAVED,
                    phase="execute",
                    status="ok",
                    attributes={
                        "checkpoint_id": str(state.task_fingerprint or session_id),
                        "plan_version": int(getattr(state.plan, "plan_version", 1) or 1) if state.plan else None,
                        "completed_task_ids": completed[:40],
                        "pending_task_ids": pending[:40],
                        "last_seq": None,
                        "state_hash": str(state.task_fingerprint or "")[:64],
                        "next_step_index": next_step_index,
                    },
                )
        except Exception:
            import logging as _log
            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)

    async def _persist_hitl_waiting(
        self,
        state: LoopState,
        waiting: dict[str, Any],
    ) -> None:
        state.metadata["hitl_waiting"] = waiting
        store = self._run_checkpoint_store
        if store is None:
            return
        self._save_step_checkpoint(state, state.session_id, max(0, state.step_index), store)

    async def _clear_hitl_waiting(self, state: LoopState) -> None:
        if "hitl_waiting" in (state.metadata or {}):
            state.metadata.pop("hitl_waiting", None)
            store = self._run_checkpoint_store
            if store is not None:
                self._save_step_checkpoint(
                    state, state.session_id, max(0, state.step_index), store
                )

    def _prepare_session(self, session_id: str):
        session_dir = self.project_root / "output" / f"session_{session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)

        session_dir_str = str(session_dir).replace("\\", "/")
        relative_session_dir = str(session_dir.relative_to(self.project_root)).replace(
            "\\", "/"
        )

        updated_dir = self.project_root / "updated" / f"session_{session_id}"
        uploaded_prompt = ""
        if updated_dir.exists():
            files = [f.name for f in updated_dir.iterdir() if f.is_file()]
            if files:
                for filename in files:
                    shutil.copy2(updated_dir / filename, session_dir / filename)
                uploaded_prompt = (
                    "\n    [已上传文件] 已加载到工作目录:\n"
                    + "\n".join([f"    - {f}" for f in files])
                    + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
                )

        session_dir_token = set_session_context(session_dir_str)
        session_id_token = set_thread_context(session_id)
        return session_dir, relative_session_dir, uploaded_prompt, (
            session_dir_token,
            session_id_token,
        )

    async def _phase_understand(
        self,
        state: LoopState,
        task_query: str,
        has_uploaded_files: bool,
    ) -> LoopState:
        """【Phase 14】仅理解：规则 + 默认 LLM → TaskIntent（不含 Plan）。"""
        started = time.perf_counter()
        self._report_phase(Phase.UNDERSTAND, "start", state=state)
        state.phase = Phase.UNDERSTAND
        from app.agent.harness.usage_tracker import set_llm_phase, set_llm_session

        set_llm_session(state.session_id)
        set_llm_phase(Phase.UNDERSTAND.value)

        from app.agent.llm import compression_model

        state.intent = await understand_intent(
            task_query,
            session_id=state.session_id,
            has_uploaded_files=has_uploaded_files,
            model=compression_model,
            llm_enabled=self.harness_config.planner_llm_enabled,
            llm_min_confidence=self.harness_config.planner_llm_min_confidence,
            clarification_auto_resolve=self.harness_config.planner_clarification_auto_resolve,
        )

        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.UNDERSTAND,
            "done",
            state=state,
            duration_ms=duration,
            intent=state.intent.summary if state.intent else "",
            deliverable=state.intent.deliverable if state.intent else "",
            planner_source=state.intent.planner_source if state.intent else "rules",
            intent_confidence=state.intent.intent_confidence if state.intent else 0,
            slots=state.intent.slots.to_dict() if state.intent else {},
            needs_clarification=state.intent.needs_clarification if state.intent else False,
        )
        return state

    async def _maybe_intent_clarification(self, state: LoopState) -> LoopState:
        """【Phase 14】低置信 / 歧义 → HITL 意图澄清。"""
        if not self.harness_config.hitl_enabled:
            return state
        if not self.harness_config.planner_clarification_enabled:
            return state
        if not state.intent or not state.intent.needs_clarification:
            return state
        if state.intent.clarification_resolved:
            return state
        if self.harness_config.planner_clarification_auto_resolve:
            state.intent = auto_resolve_clarification(state.intent)
            return state

        intent = state.intent
        allowed = ["approve", "reject"]
        if self.harness_config.hitl_allow_edit:
            allowed.append("edit")

        payload = {
            "action_requests": [
                {
                    "name": "task_intent",
                    "args": {
                        "question": intent.clarification_question,
                        "intent": intent.to_dict(),
                        "suggested_deliverables": ["text", "md", "pdf"],
                    },
                }
            ],
            "review_configs": [
                {
                    "action_name": "task_intent",
                    "allowed_decisions": allowed,
                }
            ],
            "gate_type": "intent_clarification",
            "editable": self.harness_config.hitl_allow_edit,
        }
        monitor.report_hitl_interrupt(
            state.session_id,
            payload["action_requests"],
            payload["review_configs"],
            step_index=-1,
            gate_type="intent_clarification",
            editable=self.harness_config.hitl_allow_edit,
        )
        self._report_phase(
            Phase.UNDERSTAND,
            "awaiting_clarification",
            state=state,
            gate_type="intent_clarification",
        )
        await self._persist_hitl_waiting(
            state,
            {
                "gate_type": "intent_clarification",
                "step_index": -1,
                "payload": payload,
            },
        )
        try:
            decisions = await hitl_coordinator.wait_for_decisions(
                state.session_id,
                payload,
                timeout_sec=self.harness_config.hitl_timeout_sec,
            )
        except TimeoutError:
            state.intent = auto_resolve_clarification(state.intent)
            return state
        finally:
            await self._clear_hitl_waiting(state)

        state = self._apply_hitl_decisions(state, decisions, step=None, step_index=-1)
        await self._flush_hitl_memories(state)
        self._report_phase(
            Phase.UNDERSTAND,
            "clarification_resolved",
            state=state,
            gate_type="intent_clarification",
            decisions=decisions,
        )
        return state

    async def _phase_plan(self, state: LoopState) -> LoopState:
        """【Phase 14】由 TaskIntent 生成 ExecutionPlan 并校验。"""
        started = time.perf_counter()
        self._report_phase(Phase.PLAN, "start", state=state)
        state.phase = Phase.PLAN
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.PLAN.value)
        if state.intent:
            from app.agent.llm import compression_model
            from app.research.planning.compose import PlanningLimits, compose_execution_plan

            plan, issues = await compose_execution_plan(
                state.intent,
                model=compression_model,
                session_id=state.session_id,
                llm_enabled=bool(
                    self.harness_config.planner_llm_enabled
                    and getattr(self.harness_config, "planner_dynamic_lead_enabled", True)
                ),
                limits=PlanningLimits.from_config(self.harness_config),
                config=self.harness_config,
            )
            state.plan = plan
            if plan is not None:
                from app.research.planning.effort import resolve_effective_budget

                effective = resolve_effective_budget(state.intent, self.harness_config)
                state.metadata["effort_plan"] = effective.to_dict()
                state.metadata["run_budget"] = effective.as_run_budget()
                # 若 compose 已 stamp，以 plan.metadata 为准合并
                stamped = dict((getattr(plan, "metadata", None) or {}).get("effort_plan") or {})
                if stamped:
                    state.metadata["effort_plan"] = stamped
            if issues:
                state.metadata["plan_validation_issues"] = issues
        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.PLAN,
            "done",
            state=state,
            duration_ms=duration,
            steps=len(state.plan.steps) if state.plan else 0,
            summary=state.plan.summary if state.plan else "",
            plan_validation_issues=state.metadata.get("plan_validation_issues", []),
        )
        return state

    async def _maybe_plan_hitl_review(self, state: LoopState) -> LoopState:
        """【Phase 6/14】多意图 / 低置信 / 歧义 → 计划审批 + Edit-in-the-Loop。"""
        if not self.harness_config.hitl_enabled:
            return state
        if not self.harness_config.hitl_plan_review_enabled:
            return state
        if not state.intent or not state.plan:
            return state
        if not should_request_plan_review(
            state.intent,
            min_confidence=self.harness_config.planner_plan_review_min_confidence,
        ):
            return state

        allowed = ["approve", "reject"]
        if self.harness_config.hitl_allow_edit:
            allowed.append("edit")

        payload = {
            "action_requests": [
                {
                    "name": "execution_plan",
                    "args": {
                        "summary": state.plan.summary,
                        "steps": plan_to_editable_dict(state.plan),
                        "intent": state.intent.to_dict() if state.intent else {},
                        "intent_confidence": state.intent.intent_confidence if state.intent else 1.0,
                    },
                }
            ],
            "review_configs": [
                {
                    "action_name": "execution_plan",
                    "allowed_decisions": allowed,
                }
            ],
            "gate_type": "plan_review",
            "editable": self.harness_config.hitl_allow_edit,
        }
        monitor.report_hitl_interrupt(
            state.session_id,
            payload["action_requests"],
            payload["review_configs"],
            step_index=-1,
            gate_type="plan_review",
            editable=self.harness_config.hitl_allow_edit,
        )
        self._report_phase(Phase.PLAN, "awaiting_approval", state=state, gate_type="plan_review")
        await self._persist_hitl_waiting(
            state,
            {
                "gate_type": "plan_review",
                "step_index": -1,
                "payload": payload,
            },
        )
        try:
            decisions = await hitl_coordinator.wait_for_decisions(
                state.session_id,
                payload,
                timeout_sec=self.harness_config.hitl_timeout_sec,
            )
        except TimeoutError:
            return state
        finally:
            await self._clear_hitl_waiting(state)

        state = self._apply_hitl_decisions(state, decisions, step=None, step_index=-1)
        await self._flush_hitl_memories(state)
        self._report_phase(
            Phase.PLAN,
            "resumed",
            state=state,
            gate_type="plan_review",
            decisions=decisions,
        )
        return state

    def _apply_hitl_decisions(
        self,
        state: LoopState,
        decisions: list[dict[str, Any]],
        step: Optional[PlanStep],
        step_index: int,
    ) -> LoopState:
        """【Phase 6】统一处理 approve / reject / edit 决策。"""
        for decision in decisions:
            dtype = decision.get("type", "approve")
            if dtype in {"approve", "reject", "edit"}:
                pending = list(state.metadata.get("pending_hitl_memories") or [])
                gate = "step" if step is not None else "plan_or_intent"
                if dtype == "reject" and step is not None:
                    pending.append(
                        {
                            "fact": f"用户拒绝了步骤 {step.step_type}：{step.description[:80]}",
                            "gate": gate,
                            "decision": dtype,
                        }
                    )
                elif dtype == "edit":
                    pending.append(
                        {
                            "fact": f"用户编辑了{'步骤 ' + step.step_type if step else '计划/意图'}，请在后续同类任务中优先遵循该修改",
                            "gate": gate,
                            "decision": dtype,
                        }
                    )
                if pending:
                    state.metadata["pending_hitl_memories"] = pending
            if dtype == "approve":
                if state.intent and state.intent.needs_clarification:
                    state.intent.needs_clarification = False
                    state.intent.clarification_resolved = True
                continue
            if dtype == "reject":
                if step is not None:
                    step.metadata["hitl_rejected"] = True
                continue
            if dtype == "edit":
                edited = decision.get("edited_action") or {}
                if step is None and edited and state.intent and (
                    edited.get("deliverable")
                    or edited.get("slots")
                    or edited.get("intent")
                ):
                    patch = edited.get("intent") if isinstance(edited.get("intent"), dict) else edited
                    state.intent = apply_intent_clarification(state.intent, patch)
                    if state.intent:
                        plan, issues = build_plan_for_intent(state.intent)
                        state.plan = plan
                        if issues:
                            state.metadata["plan_validation_issues"] = issues
                if step is not None and edited.get("description"):
                    step.description = str(edited["description"])
                    step.metadata["hitl_edited"] = True
                if edited.get("steps") and state.plan:
                    state.plan = apply_plan_edits(state.plan, edited["steps"])
                    state.replan_count += 1
                if edited.get("replan") and self.harness_config.hitl_allow_replan and state.plan:
                    state.plan = dynamic_replan(
                        state.plan,
                        max(step_index, 0),
                        "user_replan",
                    )
                    state.replan_count += 1
                    self._report_phase(
                        Phase.REPLAN,
                        "done",
                        state=state,
                        step_index=step_index,
                        reason="user_replan",
                        new_steps=len(state.plan.steps),
                    )
        return state

    def _memory_identity(self, state: LoopState) -> MemoryIdentity:
        return MemoryIdentity(
            tenant_id=state.memory_tenant_id or "default",
            user_id=state.memory_user_id,
            project_id=getattr(state, "memory_project_id", "default") or "default",
            session_id=state.session_id,
            ephemeral=bool(getattr(state, "memory_identity_ephemeral", False)),
        )

    async def _maybe_step_recall(self, state: LoopState, step: PlanStep, task_query: str) -> None:
        """合成步二次召回：只注入高信任记忆，避免脏网页结论进入报告。"""
        policy = get_memory_policy()
        if not policy.enabled or not policy.step_recall_enabled:
            return
        if step.step_type not in SYNTHESIS_STEP_TYPES:
            return
        identity = self._memory_identity(state)
        result = await self.memory.recall_with_metrics(
            task_query,
            identity.user_id,
            identity=identity,
            top_k=policy.step_recall_top_k,
            target_step_type=step.step_type,
        )
        if result.records:
            state.memory_records = result.records
            state.memory_facts = [r.fact for r in result.records if r.fact]
            state.obs_memory_trust_filtered = result.trust_filtered

    async def _flush_hitl_memories(self, state: LoopState) -> None:
        pending = list(state.metadata.get("pending_hitl_memories") or [])
        if not pending:
            return
        state.metadata["pending_hitl_memories"] = []
        identity = self._memory_identity(state)
        writes = [
            MemoryWriteRequest(
                fact=str(item.get("fact", "")),
                memory_type=MemoryType.PROCEDURAL,
                write_source=WriteSource.HITL,
                task=state.intent.raw_query if state.intent else "",
                session_id=state.session_id,
                project_id=identity.project_id,
                confidence=0.95,
                metadata={"gate": item.get("gate", ""), "decision": item.get("decision", "")},
            )
            for item in pending
            if str(item.get("fact", "")).strip()
        ]
        if not writes:
            return
        saved = await self.memory.remember_writes(writes, user_id=identity.user_id, identity=identity)
        if saved:
            state.obs_memory_saved_count += saved

    async def _maybe_remember_step(
        self,
        state: LoopState,
        step: PlanStep,
        result: StepResult,
    ) -> None:
        """【Phase 15】检索步成功后步内增量写入；【Phase 18】必须带来源才写网页结论。"""
        policy = get_memory_policy()
        if not policy.enabled or not policy.step_incremental_enabled:
            return
        if step.step_type not in {"network_search", "file_read"}:
            return
        identity = self._memory_identity(state)
        provenance = provenance_from_step(
            step_type=step.step_type,
            content=result.content,
            metadata=result.metadata,
            run_id=state.run_id or state.session_id,
        )
        content = result.compressed_content or result.content
        writes = self.memory_extractor.extract_step_writes(
            content,
            step.step_type,
            session_id=state.session_id,
            task=state.intent.raw_query if state.intent else "",
            project_id=identity.project_id,
            provenance=provenance,
        )
        if writes and getattr(policy, "step_incremental_write_longterm", False):
            saved = await self.memory.remember_writes(
                writes,
                user_id=identity.user_id,
                identity=identity,
            )
            if saved:
                state.obs_memory_saved_count += saved
                monitor.report_phase(
                    "memory",
                    "step_incremental",
                    session_id=state.session_id,
                    count=saved,
                    step_type=step.step_type,
                    trust_tier=writes[0].resolved_trust_tier().value,
                )
        elif writes:
            monitor.report_phase(
                "memory",
                "step_incremental_deferred",
                session_id=state.session_id,
                count=len(writes),
                step_type=step.step_type,
                reason="source_ledger_only",
            )
        if provenance.source_urls:
            recorded = await self.memory.record_sources(
                provenance.source_urls,
                identity=identity,
                source_kind="url",
                quality="mixed" if step.step_type == "network_search" else "reliable",
                session_id=state.session_id,
            )
            state.obs_memory_sources_recorded += recorded

    async def _phase_build_context(self, state: LoopState, task_query: str) -> LoopState:
        started = time.perf_counter()
        self._report_phase(Phase.BUILD_CONTEXT, "start", state=state)
        state.phase = Phase.BUILD_CONTEXT
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.BUILD_CONTEXT.value)

        identity = self._memory_identity(state)
        recalled_result = await self.memory.recall_with_metrics(
            task_query,
            identity.user_id,
            identity=identity,
            top_k=self.harness_config.memory_recall_top_k,
        )
        recalled = recalled_result.records
        state.memory_records = recalled
        state.memory_facts = [r.fact for r in recalled if r.fact]
        state.memory_recalled = bool(state.memory_facts)
        state.obs_memory_recalled_count = len(state.memory_facts)
        state.obs_memory_recall_at_k = recalled_result.mean_recall_score
        state.obs_memory_embedding_used = recalled_result.embedding_used
        state.obs_memory_trust_filtered = recalled_result.trust_filtered
        state.memory_source_ledger = self.memory.list_sources(identity=identity)
        if state.memory_facts:
            monitor.report_phase(
                "memory",
                "done",
                session_id=state.session_id,
                count=len(state.memory_facts),
                source="recall",
                user_id=identity.user_id,
                project_id=identity.project_id,
                trust_filtered=recalled_result.trust_filtered,
            )

        # 显式跨 Run 上下文：注入相关历史 RunSummary（不含 raw artifact / 聊天历史）
        followup_ctx = state.metadata.get("followup_context") if isinstance(state.metadata, dict) else None
        if isinstance(followup_ctx, dict) and followup_ctx.get("context_block"):
            block = str(followup_ctx["context_block"])
            state.memory_facts = [block] + state.memory_facts
            state.obs_memory_recalled_count = len(state.memory_facts)
            monitor.report_phase(
                "memory",
                "done",
                session_id=state.session_id,
                count=1,
                source="followup_resolver",
                followup_type=str(followup_ctx.get("followup_type") or ""),
                selected_runs=list(followup_ctx.get("selected_run_ids") or []),
            )

        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.BUILD_CONTEXT,
            "done",
            state=state,
            duration_ms=duration,
            memory_count=len(recalled),
            source_ledger_count=len(state.memory_source_ledger),
        )
        return state

    async def _phase_execute_step(
        self,
        state: LoopState,
        step: PlanStep,
        step_index: int,
        task_query: str,
        relative_session_dir: str,
        uploaded_prompt: str,
        session_id: str,
        extra_instruction: str = "",
        context_builder: Optional[ContextBuilder] = None,
        run_session_id: str = "",
        json_only: bool = False,
    ) -> StepResult:
        started = time.perf_counter()
        self._report_phase(
            Phase.EXECUTE,
            "start",
            state=state,
            step_index=step_index,
            step_type=step.step_type,
            total_steps=len(state.plan.steps),
        )
        state.phase = Phase.EXECUTE
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.EXECUTE.value)

        await self._maybe_step_recall(state, step, task_query)

        if not await self._maybe_step_hitl_gate(state, step, step_index):
            duration = int((time.perf_counter() - started) * 1000)
            self._report_phase(
                Phase.EXECUTE,
                "rejected",
                state=state,
                step_index=step_index,
                step_type=step.step_type,
                duration_ms=duration,
            )
            return StepResult(
                step_type=step.step_type,
                content="用户拒绝了该步骤的人工审批，已跳过执行。",
                metadata={"hitl_rejected": True, "duration_ms": duration},
            )

        builder = context_builder or self.context_builder
        execute_agent, dispatch_mode = self._agent_for_step(step)
        user_message = builder.build_step_message(
            task_query,
            state,
            step,
            step_index,
            relative_session_dir,
            uploaded_prompt,
            enforce_binding=self.harness_config.enforce_subagent_binding,
            use_evidence_digest=self.harness_config.synthesis_use_evidence_digest,
            extra_instruction=extra_instruction,
            dispatch_mode=dispatch_mode,
        )
        step_ctx_metrics = builder.last_step_metrics
        if step_ctx_metrics is not None:
            state.obs_step_message_tokens_peak = max(
                state.obs_step_message_tokens_peak,
                step_ctx_metrics.total_tokens,
            )
            if step_ctx_metrics.layers.get("budget_trimmed") or step_ctx_metrics.evictions:
                state.obs_context_budget_trims += 1
            state.obs_evidence_retrieved_count = max(
                state.obs_evidence_retrieved_count,
                int(getattr(step_ctx_metrics, "evidence_retrieved_count", 0) or 0),
            )
            try:
                from app.observability import EventType, get_recorder

                recorder = get_recorder()
                if recorder.is_active:
                    metrics = step_ctx_metrics.to_dict() if hasattr(step_ctx_metrics, "to_dict") else {}
                    brief_id = str((state.metadata or {}).get("brief_id") or "")
                    recorder.emit(
                        EventType.CONTEXT_BUILT,
                        phase="execute",
                        status="ok",
                        task_id=step.resolved_task_id(step_index) if hasattr(step, "resolved_task_id") else None,
                        attributes={
                            "before_tokens": metrics.get("before_tokens") or metrics.get("raw_tokens"),
                            "after_tokens": metrics.get("total_tokens") or metrics.get("after_tokens"),
                            "selected_brief": brief_id,
                            "selected_evidence_count": int(
                                getattr(step_ctx_metrics, "evidence_retrieved_count", 0) or metrics.get("evidence_count") or 0
                            ),
                            "selected_memory_count": int(metrics.get("memory_count") or 0),
                            "dropped_messages": metrics.get("dropped_messages") or metrics.get("evictions") or [],
                            "dropped_tool_results": metrics.get("tool_results_cleared") or 0,
                            "compression_ratio": metrics.get("compression_ratio"),
                            "retention_check": metrics.get("retention_check") or metrics.get("layers"),
                        },
                        input_refs=[{"type": "research_brief", "id": brief_id}] if brief_id else [],
                    )
            except Exception:
                pass
            self._report_phase(
                Phase.EXECUTE,
                "context_built",
                state=state,
                step_index=step_index,
                context_metrics=step_ctx_metrics.to_dict(),
                graph_thread_id=run_session_id or session_id,
            )
        graph_thread = run_session_id or session_id
        if graph_thread != session_id:
            state.obs_fresh_threads += 1
            state.graph_thread_ids.append(graph_thread)
        config = build_run_config(
            graph_thread,
            metadata={
                "phase": "execute",
                "step_index": step_index,
                "step_type": step.step_type,
                "usage_session_id": session_id,
            },
        )
        final_content = ""
        tool_calls = 0
        retrieval_units = 0
        tools_invoked: list[str] = []
        pending_tools: dict[str, tuple[str, float]] = {}
        step_assistants: list[str] = []
        step_cap = max(0, int(getattr(self.harness_config, "max_step_tool_calls", 8) or 0))
        run_budget = {}
        if isinstance(getattr(state, "metadata", None), dict):
            raw_budget = state.metadata.get("run_budget")
            if isinstance(raw_budget, dict):
                run_budget = raw_budget
                step_cap = max(
                    0,
                    int(run_budget.get("max_step_tool_calls", step_cap) or step_cap),
                )
        step_meta = dict(getattr(step, "metadata", None) or {})
        if "max_retrieval_calls" in step_meta:
            try:
                step_cap = max(0, min(step_cap, int(step_meta["max_retrieval_calls"])))
            except (TypeError, ValueError):
                pass
        session_max = int(
            run_budget.get("max_tool_calls", self.harness_config.max_tool_calls)
            if run_budget
            else self.harness_config.max_tool_calls
        )
        session_left = max(0, int(session_max) - int(state.tool_calls_count or 0))
        if json_only:
            retrieval_remaining: int | None = 0
        elif step_cap > 0:
            retrieval_remaining = min(step_cap, session_left if session_left else step_cap)
        else:
            retrieval_remaining = None

        with retrieval_budget(retrieval_remaining):
            async for chunk in execute_agent.astream(
                {"messages": [{"role": "user", "content": user_message}]},
                config=config,
            ):
                for _node_name, node_state in chunk.items():
                    if not node_state or "messages" not in node_state:
                        continue
                    messages = node_state["messages"]
                    if not messages or not isinstance(messages, list):
                        continue
                    last_msg = messages[-1]
                    if getattr(last_msg, "tool_calls", None):
                        tool_calls += len(last_msg.tool_calls)
                        for tool_call in last_msg.tool_calls:
                            tool_name = tool_call["name"]
                            tool_args = tool_call.get("args") or {}
                            if tool_name in {"internet_search", "fetch_url"}:
                                retrieval_units += 1
                            elif tool_name == "batch_search":
                                retrieval_units += max(1, len(tool_args.get("queries") or []))
                            elif tool_name == "batch_fetch":
                                retrieval_units += max(1, len(tool_args.get("urls") or []))
                            tools_invoked.append(tool_name)
                            tool_call_id = str(tool_call.get("id") or tool_call.get("tool_call_id") or "")
                            pending_tools[tool_call_id or tool_name] = (
                                tool_name,
                                time.perf_counter(),
                            )
                            monitor.report_tool(
                                tool_name,
                                tool_call.get("args", {}),
                                tool_call_id=tool_call_id,
                            )
                    elif getattr(last_msg, "tool_call_id", None) or type(last_msg).__name__ == "ToolMessage":
                        tool_call_id = str(getattr(last_msg, "tool_call_id", "") or "")
                        tool_name, started_at = pending_tools.pop(
                            tool_call_id,
                            (str(getattr(last_msg, "name", "") or "tool"), time.perf_counter()),
                        )
                        duration_ms = int((time.perf_counter() - started_at) * 1000)
                        tool_status = str(getattr(last_msg, "status", "success") or "success")
                        failed = tool_status in {"error", "failed"}
                        content_text = message_text(last_msg)
                        result_bytes = len(content_text.encode("utf-8")) if content_text else 0
                        result_count = 0
                        document_ids: list[str] = []
                        domains: list[str] = []
                        artifact_ids: list[str] = []
                        try:
                            parsed = json.loads(content_text) if content_text.strip().startswith(("{", "[")) else None
                            if isinstance(parsed, list):
                                result_count = len(parsed)
                                for item in parsed[:20]:
                                    if isinstance(item, dict):
                                        if item.get("url"):
                                            document_ids.append(str(item.get("url")))
                                        if item.get("id"):
                                            document_ids.append(str(item.get("id")))
                            elif isinstance(parsed, dict):
                                results = parsed.get("results") or parsed.get("documents") or []
                                if isinstance(results, list):
                                    result_count = len(results)
                                    for item in results[:20]:
                                        if isinstance(item, dict) and item.get("url"):
                                            document_ids.append(str(item.get("url")))
                                artifact_ids = [str(x) for x in (parsed.get("artifact_ids") or []) if x]
                        except Exception:
                            result_count = 1 if content_text.strip() else 0
                        for url in document_ids:
                            try:
                                from urllib.parse import urlparse

                                host = urlparse(url).netloc
                                if host and host not in domains:
                                    domains.append(host)
                            except Exception:
                                pass
                        monitor.report_tool_end(
                            tool_name,
                            tool_call_id=tool_call_id,
                            duration_ms=duration_ms,
                            status="error" if failed else "ok",
                            error=content_text[:240] if failed else "",
                            result_count=result_count,
                            result_bytes=result_bytes,
                            artifact_ids=artifact_ids,
                            extra={
                                "document_ids": document_ids[:20],
                                "domains": domains[:12],
                                "top_k": result_count or None,
                            },
                        )
                    elif is_assistant_message(last_msg):
                        text = message_text(last_msg)
                        if text.strip():
                            final_content = text

        if dispatch_mode == "direct" and step.subagent:
            if step.subagent not in step_assistants:
                step_assistants.append(step.subagent)
            if step.subagent not in state.assistants_called:
                state.assistants_called.append(step.subagent)
            monitor.report_assistant(
                step.subagent,
                {"dispatch": "direct", "step_type": step.step_type},
            )

        await self._hygiene_checkpoint_messages(config, state, agent=execute_agent)
        await self._await_hitl_interrupt_resumes(
            config, state, step_index, agent=execute_agent
        )
        snapshot = await execute_agent.aget_state(config)
        if snapshot is not None:
            extracted = self._extract_final_content_from_snapshot(snapshot)
            if extracted:
                final_content = extracted

        state.tool_calls_count += tool_calls
        if isinstance(state.metadata, dict):
            state.metadata["retrieval_units_used"] = int(
                state.metadata.get("retrieval_units_used") or 0
            ) + retrieval_units
            initial_lease = int(run_budget.get("initial_retrieval_units") or 0)
            if initial_lease and state.metadata["retrieval_units_used"] >= initial_lease:
                # Soft lease exhaustion ends this wave and protects delivery;
                # Progress may still spend the separately tracked replan reserve.
                state.metadata["force_synthesis"] = True
                state.metadata["research_lease_exhausted"] = True
        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.EXECUTE,
            "done",
            state=state,
            duration_ms=duration,
            step_index=step_index,
            step_type=step.step_type,
            tool_calls=tool_calls,
        )
        return StepResult(
            step_type=step.step_type,
            content=final_content,
            metadata={
                "tool_calls": tool_calls,
                "retrieval_units": retrieval_units,
                "duration_ms": duration,
                "tools_invoked": tools_invoked,
                "step_assistants_called": step_assistants,
                "worker_dispatch": dispatch_mode,
            },
        )

    async def _maybe_step_hitl_gate(
        self,
        state: LoopState,
        step: PlanStep,
        step_index: int,
    ) -> bool:
        if not self.harness_config.hitl_enabled:
            return True
        if state.metadata.get("graph_runtime") or state.metadata.get("graph_step_gated"):
            return True
        if step.step_type not in self.harness_config.hitl_step_gate_types:
            return True

        allowed = ["approve", "reject"]
        if self.harness_config.hitl_allow_edit:
            allowed.append("edit")

        payload = {
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
                {
                    "action_name": step.step_type,
                    "allowed_decisions": allowed,
                }
            ],
            "step_index": step_index,
            "gate_type": "step",
            "editable": self.harness_config.hitl_allow_edit,
        }
        monitor.report_hitl_interrupt(
            state.session_id,
            payload["action_requests"],
            payload["review_configs"],
            step_index=step_index,
            gate_type="step",
            editable=self.harness_config.hitl_allow_edit,
        )
        self._report_phase(
            Phase.EXECUTE,
            "awaiting_approval",
            state=state,
            step_index=step_index,
            gate_type="step",
        )
        await self._persist_hitl_waiting(
            state,
            {
                "gate_type": "step",
                "step_index": step_index,
                "payload": payload,
            },
        )
        try:
            decisions = await hitl_coordinator.wait_for_decisions(
                state.session_id,
                payload,
                timeout_sec=self.harness_config.hitl_timeout_sec,
            )
        except TimeoutError:
            return False
        finally:
            await self._clear_hitl_waiting(state)

        if any(decision.get("type") == "reject" for decision in decisions):
            approved = False
        else:
            state = self._apply_hitl_decisions(state, decisions, step, step_index)
            await self._flush_hitl_memories(state)
            approved = True

        self._report_phase(
            Phase.EXECUTE,
            "resumed" if approved else "rejected",
            state=state,
            step_index=step_index,
            gate_type="step",
            decisions=decisions,
        )
        return approved

    async def _await_hitl_interrupt_resumes(
        self,
        config: dict[str, Any],
        state: LoopState,
        step_index: int,
        *,
        agent: Any = None,
    ) -> None:
        if not self.harness_config.hitl_enabled:
            return

        from langgraph.types import Command

        target = agent if agent is not None else self.agent

        while True:
            snapshot = await target.aget_state(config)
            payload = self._extract_interrupt_payload(snapshot)
            if not payload:
                break

            action_requests = payload.get("action_requests", [])
            review_configs = payload.get("review_configs", [])
            waiter_payload = {
                "action_requests": action_requests,
                "review_configs": review_configs,
                "step_index": step_index,
                "gate_type": "interrupt_on",
                "editable": self.harness_config.hitl_allow_edit,
            }
            monitor.report_hitl_interrupt(
                state.session_id,
                action_requests,
                review_configs,
                step_index=step_index,
                gate_type="interrupt_on",
            )
            self._report_phase(
                Phase.EXECUTE,
                "awaiting_approval",
                state=state,
                step_index=step_index,
                gate_type="interrupt_on",
                action_count=len(action_requests),
            )
            await self._persist_hitl_waiting(
                state,
                {
                    "gate_type": "interrupt_on",
                    "step_index": step_index,
                    "payload": waiter_payload,
                },
            )
            try:
                decisions = await hitl_coordinator.wait_for_decisions(
                    state.session_id,
                    waiter_payload,
                    timeout_sec=self.harness_config.hitl_timeout_sec,
                )
            finally:
                await self._clear_hitl_waiting(state)
            if any(d.get("type") == "edit" for d in decisions):
                state = self._apply_hitl_decisions(state, decisions, None, step_index)
                await self._flush_hitl_memories(state)
            await self._hygiene_checkpoint_messages(
                config, state, snapshot, agent=target
            )
            await target.ainvoke(
                Command(resume={"decisions": decisions}),
                config=config,
            )
            self._report_phase(
                Phase.EXECUTE,
                "resumed",
                state=state,
                step_index=step_index,
                gate_type="interrupt_on",
                decisions=decisions,
            )

    def _extract_interrupt_payload(self, snapshot: Any) -> dict[str, Any] | None:
        if snapshot is None:
            return None

        interrupts = getattr(snapshot, "interrupts", None) or ()
        if interrupts:
            first = interrupts[0]
            value = getattr(first, "value", first)
            if isinstance(value, dict) and value.get("action_requests"):
                return value

        values = getattr(snapshot, "values", None) or {}
        raw_interrupt = values.get("__interrupt__")
        if raw_interrupt:
            if isinstance(raw_interrupt, list) and raw_interrupt:
                value = getattr(raw_interrupt[0], "value", raw_interrupt[0])
                if isinstance(value, dict):
                    return value
            if isinstance(raw_interrupt, dict):
                return raw_interrupt

        return None

    def _extract_final_content_from_snapshot(self, snapshot: Any) -> str:
        values = getattr(snapshot, "values", None) or {}
        messages = values.get("messages", [])
        return extract_last_assistant_text(messages)

    @staticmethod
    def _compressed_from_worker_payload(result: StepResult) -> str:
        """工人已给出 summary+facts 时跳过再调一次压缩 LLM。"""
        payload = (result.metadata or {}).get("worker_payload") or {}
        if not isinstance(payload, dict):
            return ""
        if result.metadata.get("structured_ok") is False:
            return ""
        summary = str(payload.get("summary") or "").strip()
        facts = [str(f) for f in (payload.get("facts") or []) if f]
        sources = [str(s) for s in (payload.get("sources") or []) if s]
        findings = payload.get("findings") or []
        if not summary or not (facts or sources or findings):
            return ""
        bits = [summary]
        if facts:
            bits.append("要点: " + "; ".join(facts[:8]))
        if sources:
            bits.append("来源: " + "; ".join(sources[:8]))
        text = "\n".join(bits).strip()
        if result.step_type in {"network_search", "research"} and len(text) < 120:
            extra = " ".join(facts + sources)
            text = (text + "\n" + extra).strip()
        return text if len(text) >= 80 else ""

    async def _phase_compress_step(
        self,
        state: LoopState,
        result: StepResult,
        step_index: int,
        citation_manager: Optional[CitationManager] = None,
    ) -> StepResult:
        started = time.perf_counter()
        self._report_phase(
            Phase.COMPRESS,
            "start",
            state=state,
            step_index=step_index,
            step_type=result.step_type,
        )
        state.phase = Phase.COMPRESS
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.COMPRESS.value)

        source_meta: dict[str, Any] = {"step_index": step_index}
        if citation_manager is not None:
            registered = citation_manager.register_from_step(
                step_index,
                result.step_type,
                result.content,
                result.metadata,
            )
            source_meta["source_ids"] = [s.source_id for s in registered]
            result.metadata["evidence_sources"] = (
                citation_manager.to_dict_list()[-len(registered):]
                if registered
                else []
            )
            payload = (result.metadata or {}).get("worker_payload") or {}
            if isinstance(payload, dict):
                bound = citation_manager.bind_worker_facts(
                    step_index,
                    result.step_type,
                    list(payload.get("facts") or []),
                    list(payload.get("sources") or []),
                )
                if bound:
                    extra_ids = [s.source_id for s in bound]
                    source_meta["source_ids"] = list(source_meta.get("source_ids") or []) + extra_ids

        worker_summary = self._compressed_from_worker_payload(result)
        if worker_summary:
            result.compressed_content = worker_summary
            result.metadata["method"] = "worker_summary"
            original_chars = len(result.content or "")
            result.metadata["original_chars"] = original_chars
            result.metadata["compressed_chars"] = len(worker_summary)
            result.metadata["compression_ratio"] = (
                round(len(worker_summary) / original_chars, 3) if original_chars else 1.0
            )
            duration = int((time.perf_counter() - started) * 1000)
            self._report_phase(
                Phase.COMPRESS,
                "done",
                state=state,
                duration_ms=duration,
                step_index=step_index,
                step_type=result.step_type,
                compression_method="worker_summary",
                compression_ratio=result.metadata.get("compression_ratio"),
                evidence_sources=result.metadata.get("evidence_sources"),
            )
            try:
                from app.observability import EventType, get_recorder

                recorder = get_recorder()
                if recorder.is_active:
                    recorder.emit(
                        EventType.CONTEXT_COMPRESSED,
                        phase="compress",
                        status="ok",
                        duration_ms=duration,
                        attributes={
                            "before_tokens": result.metadata.get("original_chars"),
                            "after_tokens": result.metadata.get("compressed_chars"),
                            "compression_ratio": result.metadata.get("compression_ratio"),
                            "method": "worker_summary",
                            "selected_evidence_count": len(source_meta.get("source_ids") or []),
                        },
                    )
            except Exception:
                pass
            return result

        compressed, meta = await self.compressor.compress(
            result.content,
            result.step_type,
            source_metadata=source_meta,
            artifact_store=get_artifact_store(),
        )
        if meta.get("artifact_id"):
            result.metadata["artifact_id"] = meta["artifact_id"]
            source_meta["artifact_id"] = meta["artifact_id"]
        result.compressed_content = compressed
        result.metadata.update(meta)
        ratio = meta.get("compression_ratio")
        if isinstance(ratio, (int, float)):
            state.compression_ratios.append(float(ratio))
            original_chars = int(meta.get("original_chars") or 0)
            compressed_chars = int(meta.get("compressed_chars") or 0)
            if original_chars > compressed_chars > 0:
                state.obs_estimated_tokens_saved += max(
                    1,
                    self.compressor.estimate_tokens(result.content)
                    - self.compressor.estimate_tokens(compressed),
                )
        retention = meta.get("entity_retention")
        if isinstance(retention, (int, float)):
            state.obs_entity_retention_rates.append(float(retention))
        if meta.get("retention_patched"):
            state.obs_retention_patches += 1

        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.COMPRESS,
            "done",
            state=state,
            duration_ms=duration,
            step_index=step_index,
            step_type=result.step_type,
            compression_method=meta.get("method"),
            compression_ratio=ratio,
            evidence_sources=result.metadata.get("evidence_sources"),
        )
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder.emit(
                    EventType.CONTEXT_COMPRESSED,
                    phase="compress",
                    status="ok",
                    duration_ms=duration,
                    attributes={
                        "before_tokens": meta.get("original_chars"),
                        "after_tokens": meta.get("compressed_chars"),
                        "compression_ratio": ratio,
                        "method": meta.get("method"),
                        "selected_evidence_count": len(source_meta.get("source_ids") or []),
                        "retention_check": meta.get("entity_retention"),
                        "dropped_tool_results": meta.get("tool_results_cleared") or 0,
                    },
                )
        except Exception:
            import logging as _log
            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        return result

    async def _phase_validate(
        self,
        state: LoopState,
        outcome,
        step_index: int = 0,
        scope: str = "step",
    ) -> LoopState:
        started = time.perf_counter()
        state.phase = Phase.VALIDATE
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.VALIDATE.value)
        if outcome.passed:
            status = "done"
        elif getattr(outcome, "severity", "") == "warning":
            status = "warning"
        else:
            status = "failed"
        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.VALIDATE,
            status,
            state=state,
            duration_ms=duration,
            step_index=step_index,
            scope=scope,
            reason=outcome.reason,
            passed=outcome.passed,
        )
        return state

    async def _phase_recover(
        self,
        state: LoopState,
        reason: str,
        step_index: int,
    ) -> LoopState:
        started = time.perf_counter()
        self._report_phase(
            Phase.RECOVER,
            "start",
            state=state,
            reason=reason,
            step_index=step_index,
        )
        state.phase = Phase.RECOVER
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.RECOVER.value)
        hint = self.recovery.build_recovery_hint(reason, state)
        state.recovery_hints.append(hint)
        decision = "retry"
        lowered = str(reason or "").lower()
        if "budget" in lowered or "deadline" in lowered:
            decision = "abort"
        elif "replan" in lowered or "gap" in lowered:
            decision = "replan"
        elif "checkpoint" in lowered or "resume" in lowered:
            decision = "resume"
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder.emit(
                    EventType.RECOVERY_DECIDED,
                    phase="recover",
                    status="decided",
                    attributes={
                        "decision": decision,
                        "failure_type": reason,
                        "attempt": int(getattr(state, "retry_count", 0) or 0) + 1,
                        "previous_action": "validate_failed",
                        "remaining_budget": self.remaining_budget(state),
                        "hint": str(hint)[:240],
                    },
                )
        except Exception:
            import logging as _log
            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.RECOVER,
            "done",
            state=state,
            duration_ms=duration,
            step_index=step_index,
            hint=hint[:200],
        )
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder.emit(
                    EventType.RECOVERY_COMPLETED,
                    phase="recover",
                    status="ok",
                    duration_ms=duration,
                    attributes={"decision": decision, "failure_type": reason},
                )
        except Exception:
            import logging as _log
            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        return state

    async def _phase_finalize(
        self,
        state: LoopState,
        session_dir: Path,
        success: bool,
        started_at: float,
        deliverable_dir: Path | None = None,
    ) -> HarnessResult:
        phase_started = time.perf_counter()
        self._report_phase(Phase.FINALIZE, "start", state=state)
        state.phase = Phase.FINALIZE
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.FINALIZE.value)

        from app.agent.harness.deliverables import (
            ensure_requested_deliverables,
            session_artifact_names,
            usable_report_text,
        )

        # Run 隔离：交付物只写/只读当前 runs/{run_id}/deliverables
        deliverable_root = Path(deliverable_dir) if deliverable_dir else Path(session_dir)
        written = ensure_requested_deliverables(deliverable_root, state)
        artifacts = session_artifact_names(deliverable_root)
        # P1 RunSummary：结构化结论落盘，供后续 Run 显式继承
        try:
            from app.observability.events import utc_now
            from app.research.run_summary import RunSummary, save_run_summary

            brief = getattr(state, "research_brief_obj", None)
            entities = [str(x) for x in (getattr(brief, "entities", None) or [])][:12]
            conclusions = [
                line.lstrip("# ").strip()
                for line in str(state.final_content or "").splitlines()
                if line.strip().startswith(("-", "*", "•")) or line.startswith("## ")
            ][:8]
            gaps = []
            if isinstance(state.metadata, dict):
                assessment = state.metadata.get("progress_assessment") or {}
                if isinstance(assessment, dict):
                    gaps = [str(x) for x in assessment.get("gaps") or []][:5]
            summary = RunSummary(
                run_id=str(state.run_id or ""),
                session_id=str(state.session_id or ""),
                query=str(getattr(state.intent, "raw_query", "") or ""),
                intent_summary=str(getattr(state.intent, "summary", "") or "")[:240],
                entities=entities,
                conclusions=conclusions,
                key_evidence_refs=[
                    str(getattr(src, "source_id", "") or "")
                    for src in (self._run_citation_manager.sources if self._run_citation_manager else [])
                ][:12],
                artifact_refs=[str(name) for name in artifacts][:8],
                unresolved_questions=gaps,
                status="completed" if success else "partial",
                created_at=utc_now(),
            )
            if state.run_id:
                save_run_summary(
                    Path(session_dir) / "runs" / str(state.run_id),
                    summary,
                )
        except Exception as exc:
            logger.debug("run summary save skipped: %s", exc)
        abort_reason = str(state.abort_reason or "")
        if abort_reason:
            pdf_path = written.get("pdf")
            md_path = written.get("md")
            if pdf_path is not None:
                note = (
                    f"任务因 {abort_reason} 提前结束，已根据已有材料生成部分 PDF：{pdf_path.name}"
                )
            elif md_path is not None:
                note = (
                    f"任务因 {abort_reason} 提前结束，已写出 Markdown：{md_path.name}，但未能生成 PDF。"
                )
            else:
                note = (
                    f"任务因 {abort_reason} 提前结束，未能生成请求的文件交付物。"
                )
            if usable_report_text(state.final_content):
                if note not in state.final_content:
                    state.final_content = f"{state.final_content.rstrip()}\n\n{note}"
            else:
                state.final_content = note

        saved = 0
        policy = get_memory_policy()
        should_remember = success or policy.remember_on_partial
        identity = self._memory_identity(state)
        if should_remember and state.final_content.strip():
            provenance = provenance_from_step(
                step_type="finalize",
                content=state.final_content,
                metadata={"evidence_sources": []},
                run_id=state.run_id or state.session_id,
            )
            writes = await self.memory_extractor.extract_writes(
                state.final_content,
                max_facts=self.harness_config.memory_max_facts_per_remember,
                task=state.intent.raw_query if state.intent else "",
                topic=(state.intent.summary if state.intent else "")[:120],
                session_id=state.session_id,
                project_id=identity.project_id,
                provenance=provenance,
            )
            saved = await self.memory.remember_writes(
                writes,
                user_id=identity.user_id,
                identity=identity,
            )
        state.obs_memory_saved_count += saved
        if saved:
            monitor.report_phase(
                "memory",
                "done",
                session_id=state.session_id,
                count=saved,
                source="remember",
            )

        if policy.consolidation_enabled:
            try:
                if getattr(policy, "consolidation_durable", True):
                    self.memory.enqueue_consolidation(
                        user_id=identity.user_id, identity=identity
                    )
                    if policy.consolidation_async:
                        asyncio.create_task(self.memory.drain_jobs())
                    else:
                        await self.memory.drain_jobs()
                elif policy.consolidation_async:
                    asyncio.create_task(
                        self.memory.consolidate(user_id=identity.user_id, identity=identity)
                    )
                else:
                    await self.memory.consolidate(user_id=identity.user_id, identity=identity)
            except Exception as exc:
                print(f"[Memory] consolidation skipped: {exc}")

        if not state.final_content.strip():
            if abort_reason:
                state.final_content = (
                    f"任务因 {abort_reason} 提前结束，未能生成可展示的正文。"
                )
            else:
                state.final_content = "任务已结束，但没有可展示的正文。"
        persist_status = "success" if success else "partial"
        if abort_reason == "cancelled":
            persist_status = "interrupted"
        elif abort_reason and not success:
            persist_status = "partial"
        # Persist-before-publish：先写 RunStore，再推 WS，刷新后才能 hydrate 出结果。
        _project_run_complete(
            state.run_id,
            result=state.final_content,
            status=persist_status,
            error=state.abort_message or abort_reason,
        )
        monitor.report_task_result(
            state.final_content,
            status=persist_status if persist_status != "success" else "completed",
            run_id=str(state.run_id or ""),
            termination_reason=abort_reason,
            termination_stage="research" if abort_reason else "finalize",
        )

        duration = int((time.perf_counter() - phase_started) * 1000)
        status = "success" if success else "partial"
        total_latency_ms = int((time.perf_counter() - started_at) * 1000)
        step_passed = sum(1 for v in state.step_validation_results if v.get("passed"))
        step_total = len(state.step_validation_results)
        step_success_rate = step_passed / step_total if step_total else 0.0
        avg_compression = (
            sum(state.compression_ratios) / len(state.compression_ratios)
            if state.compression_ratios
            else 1.0
        )

        self._report_phase(
            Phase.FINALIZE,
            "done",
            state=state,
            duration_ms=duration,
            result_status=status,
            artifacts=artifacts,
        )

        obs_snapshot = build_observability_snapshot(state)
        from app.agent.harness.usage_tracker import get_usage_tracker

        usage_summary = get_usage_tracker().session_summary(state.session_id)
        try:
            state.obs_cache_read_tokens = int(
                (usage_summary.get("total") or {}).get("cache_read_tokens") or 0
            )
        except (TypeError, ValueError):
            state.obs_cache_read_tokens = 0
        result = HarnessResult(
            session_id=state.session_id,
            status=status,
            content=state.final_content,
            trace=state.trace,
            artifacts=artifacts,
            retry_count=state.retry_count,
            metadata={
                "assistants_called": state.assistants_called,
                "plan_steps": len(state.plan.steps) if state.plan else 0,
                "search_mode": str((state.metadata or {}).get("search_mode") or ""),
                "tool_calls_count": state.tool_calls_count,
                "latency_ms": total_latency_ms,
                "step_success_rate": round(step_success_rate, 3),
                "avg_compression_ratio": round(avg_compression, 3),
                "memory_recalled": state.memory_recalled,
                "memory_saved_count": state.obs_memory_saved_count,
                "memory_user_id": state.memory_user_id,
                "memory_tenant_id": state.memory_tenant_id,
                "memory_project_id": state.memory_project_id,
                "memory_identity_ephemeral": state.memory_identity_ephemeral,
                "memory_recalled_count": getattr(state, "obs_memory_recalled_count", 0),
                "memory_mean_recall_score": getattr(state, "obs_memory_recall_at_k", 0.0),
                "memory_recall_at_k": getattr(state, "obs_memory_recall_at_k", 0.0),
                "memory_embedding_used": getattr(state, "obs_memory_embedding_used", False),
                "memory_trust_filtered": getattr(state, "obs_memory_trust_filtered", 0),
                "memory_sources_recorded": getattr(state, "obs_memory_sources_recorded", 0),
                "step_validation_results": state.step_validation_results,
                "replan_count": state.replan_count,
                "citation_coverage_rate": state.citation_coverage_rate,
                "numeric_citation_coverage": getattr(state, "numeric_citation_coverage", 0.0),
                "entity_retention_avg": round(
                    (
                        sum(state.obs_entity_retention_rates)
                        / len(state.obs_entity_retention_rates)
                    )
                    if state.obs_entity_retention_rates
                    else 1.0,
                    3,
                ),
                "retention_patches": state.obs_retention_patches,
                "fresh_threads": state.obs_fresh_threads,
                "tool_results_cleared": state.obs_tool_results_cleared,
                "graph_thread_ids": list(state.graph_thread_ids),
                "hallucination_rate": state.hallucination_rate,
                "evidence_source_count": state.evidence_source_count,
                "evidence_retrieved_count": getattr(state, "obs_evidence_retrieved_count", 0),
                "evidence_used_count": getattr(state, "obs_evidence_used_count", 0),
                "artifacts_stored": getattr(state, "obs_artifacts_stored", 0),
                "cache_read_tokens": getattr(state, "obs_cache_read_tokens", 0),
                "token_budget": {
                    "model": getattr(self.harness_config, "token_budget_model", "glm-5.2"),
                    "context_window": getattr(self.harness_config, "token_context_window", 128000),
                    "stages": dict(getattr(self.harness_config, "token_stage_budgets", None) or {}),
                },
                "resumed_from_checkpoint": state.resumed_from_checkpoint,
                "completed_step_keys": state.completed_step_keys,
                "abort_reason": state.abort_reason,
                "abort_message": state.abort_message,
                "observability": obs_snapshot.to_dict(),
                "usage": usage_summary,
                "trace_id": self._current_trace_id,
                "run_id": state.run_id or state.session_id,
            },
        )
        if self._current_tracer is not None:
            self._current_tracer.finish(
                {
                    "status": result.status,
                    "retry_count": result.retry_count,
                    "artifacts": result.artifacts,
                    "metadata": result.metadata,
                }
            )
        from app.observability import get_recorder

        recorder = get_recorder()
        if recorder.is_active:
            recorder.finish_run(
                status=result.status,
                duration_ms=total_latency_ms,
                metadata=result.metadata,
                result_preview=state.final_content[:240],
            )
        else:
            self.trace_logger.log_run_summary(
                trace_id=self._current_trace_id,
                session_id=state.session_id,
                status=result.status,
                duration_ms=total_latency_ms,
                metadata=result.metadata,
            )
        return result

    def _estimate_run_tokens(self, state: LoopState) -> int:
        content_est = sum(
            self.compressor.estimate_tokens(result.content)
            for result in state.step_results
        )
        content_est += self.compressor.estimate_tokens(state.final_content)
        usage_tokens = 0
        try:
            from app.agent.harness.usage_tracker import get_usage_tracker

            summary = get_usage_tracker().session_summary(state.session_id or "")
            usage_tokens = int((summary.get("total") or {}).get("total_tokens") or 0)
            if isinstance(state.metadata, dict):
                state.metadata["llm_calls_used"] = int((summary.get("total") or {}).get("calls") or 0)
        except Exception:
            pass
        return max(content_est, usage_tokens)

    def _apply_run_guardrails(self, state: LoopState, run_started: float) -> bool:
        """【Phase 13】每步前评估护栏（三态）。

        - CONTINUE：返回 False，继续执行；
        - DEGRADE ：资源触顶 → 停止剩余检索步、标记 force_synthesis，返回 False
          （主循环会跳过已标记 skipped 的检索步，让合成步基于已有证据交付）；
        - ABORT   ：仅不可恢复错误才返回 True 并写入 abort_reason。
        """
        from app.agent.harness.run_budget import get_or_create_run_budget

        mgr = get_or_create_run_budget(
            state,
            self.harness_config,
            run_started=run_started,
        )
        mgr.sync_from_usage(session_id=state.session_id or "", tool_calls=state.tool_calls_count)
        snap = mgr.snapshot()
        if isinstance(state.metadata, dict):
            state.metadata["budget_snapshot"] = snap.to_dict()
            state.metadata["llm_calls_used"] = snap.llm_calls
            # Research 触顶：不直接 abort，标记强制进入 synthesis
            if snap.force_synthesis and not state.abort_reason:
                state.metadata["force_synthesis"] = True

        decision = evaluate_run_guardrails(
            state,
            self.harness_config,
            elapsed_sec=time.perf_counter() - run_started,
            estimated_tokens=self._estimate_run_tokens(state),
        )
        if decision.action == GuardrailAction.DEGRADE:
            # 资源耗尽 ≠ 系统失败：停止研究、保留合成交付能力
            if isinstance(state.metadata, dict):
                state.metadata["force_synthesis"] = True
                state.metadata["budget_degrade_reason"] = decision.reason
                state.metadata["budget_degrade_message"] = decision.message
            if state.plan is not None:
                for pending_step in state.plan.steps:
                    if str(pending_step.step_type or "") not in RETRIEVAL_STEP_TYPES:
                        continue
                    if str(pending_step.metadata.get("status") or "pending") not in {
                        "pending",
                        "running",
                    }:
                        continue
                    pending_step.metadata["status"] = StepStatus.SKIPPED.value
                    pending_step.metadata["skip_reason"] = f"budget_degraded:{decision.reason}"
            try:
                from app.observability import EventType, get_recorder

                recorder = get_recorder()
                if recorder.is_active:
                    recorder.emit(
                        EventType.BUDGET_EXHAUSTED,
                        phase="run",
                        status="degrade",
                        attributes={
                            "fail_reason": str(decision.reason or "budget_exhausted"),
                            "message": str(decision.message or "")[:240],
                            "decision": "degrade",
                            "remaining_budget": self.remaining_budget(state),
                            "budget_snapshot": snap.to_dict(),
                        },
                    )
            except Exception:
                import logging as _log
                _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
            return False
        if not decision.abort:
            return False
        state.abort_reason = decision.reason
        state.abort_message = decision.message
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder.emit(
                    EventType.BUDGET_EXHAUSTED,
                    phase="run",
                    status=str(decision.reason or "budget_exhausted"),
                    attributes={
                        "fail_reason": str(decision.reason or "budget_exhausted"),
                        "message": str(decision.message or "")[:240],
                        "remaining_budget": self.remaining_budget(state),
                        "budget_snapshot": snap.to_dict(),
                        "failure.origin_stage": "runtime",
                        "failure.detected_stage": "runtime",
                    },
                )
        except Exception:
            import logging as _log
            _log.getLogger("observability").debug("obs emit skipped", exc_info=True)
        return True

    def _budget_exceeded(self, state: LoopState) -> bool:
        """向后兼容：预算触顶（degrade 或 abort）均视为 exceeded。"""
        return evaluate_run_guardrails(
            state,
            self.harness_config,
            elapsed_sec=0.0,
            estimated_tokens=self._estimate_run_tokens(state),
        ).action != GuardrailAction.CONTINUE

    def remaining_budget(self, state: LoopState) -> dict[str, int]:
        run_budget = {}
        if isinstance(getattr(state, "metadata", None), dict):
            raw = state.metadata.get("run_budget")
            if isinstance(raw, dict):
                run_budget = raw
        max_tools = int(run_budget.get("max_tool_calls", self.harness_config.max_tool_calls))
        max_tokens = int(self.harness_config.max_total_tokens)
        return {
            "tool_calls": max(0, max_tools - int(state.tool_calls_count or 0)),
            "tokens": max(0, max_tokens - self._estimate_run_tokens(state)),
        }

    def _report_phase(
        self,
        phase: Phase,
        status: str,
        state: LoopState,
        **data: Any,
    ) -> None:
        event = PhaseEvent(phase=phase.value, status=status, data=data)
        if "duration_ms" in data:
            event.duration_ms = data["duration_ms"]
        state.trace.append(event)

        from app.observability import EventType, get_recorder

        recorder = get_recorder()
        task_id = data.get("task_id")
        if recorder.is_active:
            recorder.emit_phase(
                phase.value,
                status,
                task_id=task_id,
                attempt=data.get("attempt"),
                plan_version=data.get("plan_version")
                or (int(getattr(state.plan, "plan_version", 1) or 1) if state.plan else None),
                duration_ms=data.get("duration_ms"),
                attributes={k: v for k, v in data.items() if k != "status"},
            )
            if phase == Phase.PLAN and status == "done" and state.plan is not None:
                issues = list(data.get("plan_validation_issues") or [])
                plan_version = int(getattr(state.plan, "plan_version", 1) or 1)
                brief_id = str((state.metadata or {}).get("brief_id") or "")
                brief_dict = {}
                if getattr(state, "research_brief_obj", None):
                    brief_dict = dict(state.research_brief_obj or {})
                elif state.intent is not None and getattr(state.intent, "brief", None) is not None:
                    try:
                        brief_dict = state.intent.brief.to_dict()
                    except Exception:
                        brief_dict = {}
                if not brief_id and brief_dict.get("brief_id"):
                    brief_id = str(brief_dict.get("brief_id"))
                task_ids = [
                    step.resolved_task_id(i) for i, step in enumerate(state.plan.steps)
                ]
                plan_id = f"plan_v{plan_version}"
                coverage = {}
                plan_ref = ""
                plan_hash = ""
                try:
                    from app.observability.payload_store import get_payload_store
                    from app.observability.semantic import plan_brief_coverage

                    coverage = plan_brief_coverage(brief_dict, state.plan)
                    store = get_payload_store()
                    ref = store.put(
                        run_id=str(
                            getattr(state, "run_id", None)
                            or (state.metadata or {}).get("run_id")
                            or "unknown"
                        ),
                        artifact_type="research_plan",
                        artifact_id=plan_id,
                        payload={
                            "plan_version": plan_version,
                            "task_ids": task_ids,
                            "steps": [
                                {
                                    "task_id": step.resolved_task_id(i),
                                    "objective": step.objective or step.description,
                                    "step_type": step.step_type,
                                    "depends_on": list(step.depends_on or []),
                                }
                                for i, step in enumerate(state.plan.steps)
                            ],
                            "brief_coverage": coverage,
                        },
                        version=plan_version,
                    )
                    plan_ref = ref.ref
                    plan_hash = ref.sha256
                except Exception:
                    pass
                parallel_groups = 1
                try:
                    from app.research.runtime.scheduler import ready_research_steps

                    parallel_groups = max(
                        1,
                        len(
                            ready_research_steps(
                                state.plan,
                                {step.resolved_task_id(i): "pending" for i, step in enumerate(state.plan.steps)},
                            )
                        ),
                    )
                except Exception:
                    parallel_groups = 1
                recorder.emit(
                    EventType.PLAN_CREATED,
                    phase="plan",
                    status="ok",
                    plan_version=plan_version,
                    attributes={
                        "plan_id": plan_id,
                        "plan_version": plan_version,
                        "brief_id": brief_id,
                        "task_count": len(state.plan.steps),
                        "task_ids": task_ids,
                        "planning_mode": str(
                            getattr(state.intent, "planning_mode", "") or state.plan.planning_mode or ""
                        ),
                        "parallel_groups": parallel_groups,
                        "brief_coverage": coverage,
                        "plan_ref": plan_ref,
                        "plan_hash": plan_hash,
                    },
                    input_refs=[{"type": "research_brief", "id": brief_id}] if brief_id else [],
                    output_refs=[{"type": "research_plan", "id": plan_id, "ref": plan_ref, "sha256": plan_hash}],
                )
                recorder.emit(
                    EventType.PLAN_VALIDATED,
                    phase="plan",
                    status="ok" if not issues else "issues",
                    plan_version=plan_version,
                    attributes={"issue_count": len(issues), "plan_id": plan_id, "brief_id": brief_id},
                )
            return

        tracer = None if state.metadata.get("_parallel_child") else self._current_tracer
        if tracer is not None:
            if status == "start":
                tracer.phase_start(
                    phase.value,
                    data,
                    task_id=task_id,
                    attempt=data.get("attempt"),
                )
            else:
                tracer.phase_end(
                    phase.value,
                    status,
                    data,
                    task_id=task_id,
                    attempt=data.get("attempt"),
                )
        monitor_data = {k: v for k, v in data.items() if k != "status"}
        monitor.report_phase(phase.value, status, session_id=state.session_id, **monitor_data)

        log_status = status
        if status in {"done", "start"}:
            log_status = "ok" if status == "done" else "start"
        elif status in {
            "failed",
            "error",
            "cancelled",
            "budget_exceeded",
            "budget_tool_calls",
            "budget_tokens",
            "deadline_exceeded",
            "max_replan",
            "max_plan_steps",
            "guardrail",
        }:
            log_status = status

        # Legacy JsonlTraceLogger only when Flight Recorder is inactive (avoid dual-write).
        if not recorder.is_active:
            self.trace_logger.log_event(
                trace_id=self._current_trace_id,
                session_id=state.session_id,
                phase=phase.value,
                status=log_status,
                step_index=data.get("step_index"),
                step_type=data.get("step_type"),
                duration_ms=data.get("duration_ms"),
                tool_calls=data.get("tool_calls"),
                tokens_used=data.get("tokens_used"),
                extra={k: v for k, v in data.items() if k not in {
                    "step_index", "step_type", "duration_ms", "tool_calls", "tokens_used"
                }},
            )


def _project_run_running(ctx: HarnessRunContext) -> None:
    try:
        from app.run_store import get_run_store

        workspace = ""
        if getattr(ctx, "session_dir", None):
            workspace = str(ctx.session_dir)
        get_run_store().mark_running(ctx.run_id, session_workspace=workspace)
    except Exception as exc:
        logger.warning("RunStore mark_running skipped: %s", exc)


def _project_run_complete(run_id: str, *, result: str, status: str, error: str = "") -> None:
    if not run_id:
        return
    try:
        from app.run_store import (
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_INTERRUPTED,
            STATUS_PARTIAL,
            get_run_store,
        )

        mapped = {
            "success": STATUS_COMPLETED,
            "completed": STATUS_COMPLETED,
            "partial": STATUS_PARTIAL,
            "interrupted": STATUS_INTERRUPTED,
            "failed": STATUS_FAILED,
            "cancelled": STATUS_INTERRUPTED,
        }.get(status, STATUS_COMPLETED)
        get_run_store().complete_run(run_id, result=result, status=mapped, error=error)
    except Exception as exc:
        logger.warning("RunStore complete skipped: %s", exc)


def _project_run_fail(run_id: str, error: str) -> None:
    if not run_id:
        return
    try:
        from app.run_store import get_run_store

        get_run_store().fail_run(run_id, error)
    except Exception as exc:
        logger.warning("RunStore fail skipped: %s", exc)


def _project_run_interrupt(run_id: str, error: str = "cancelled") -> None:
    if not run_id:
        return
    try:
        from app.run_store import get_run_store

        get_run_store().interrupt_run(run_id, error=error)
    except Exception as exc:
        logger.warning("RunStore interrupt skipped: %s", exc)
