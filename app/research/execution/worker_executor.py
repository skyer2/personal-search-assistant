"""Task executor for the graph research worker node."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any

from app.agent.harness.orchestration import (
    SYNTHESIS_STEP_TYPES,
    attach_structured_payload,
    parse_worker_payload,
)
from app.agent.harness.state import StepResult
from app.agent.harness.run_budget import BudgetReservationError
from app.agent.harness.worker_runtime import resolve_execute_target
from app.api.tracing import build_run_config
from app.research.execution.llm_gateway import LLMGateway
from app.research.execution.tool_gateway import ToolGateway
from app.research.runtime.worker import (
    ResearchContext,
    ResearchTask,
    WorkerResult,
    WorkerResultStatus,
    salvage_worker_evidence,
)
from app.observability.semantic_events import worker_event_attributes


class WorkerExecutorV2:
    """Execute one immutable task through a leaf worker and typed gateways."""

    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    async def execute(
        self,
        task: ResearchTask,
        context: ResearchContext,
    ) -> WorkerResult:
        started = time.perf_counter()
        queue_started = time.perf_counter()
        step_index = int(task.step_index)
        plan = getattr(self.session.state, "plan", None)
        if plan is None or step_index >= len(plan.steps):
            return self._result(task, started, ok=False, status="failed", summary="missing_step")
        step = plan.steps[step_index]
        if bool(step.metadata.get("optional")) and bool(getattr(self.session, "wave_early_stop", False)):
            try:
                from app.observability import EventType, get_recorder

                recorder = get_recorder()
                if recorder.is_active:
                    span_key = recorder.start_span(
                        "worker.execute_v2",
                        phase="execute",
                        task_id=task.task_id,
                        plan_version=task.plan_version,
                        attempt=task.attempt,
                        attributes={"objective": task.objective, "worker_status": "skipped"},
                    )
                    recorder.emit(
                        EventType.WORKER_STARTED,
                        phase="execute",
                        status="start",
                        task_id=task.task_id,
                        plan_version=task.plan_version,
                        attempt=task.attempt,
                        attributes={"objective": task.objective, "worker_status": "skipped"},
                        run_id=context.run_id,
                        session_id=context.session_id,
                    )
                    recorder.emit(
                        EventType.WORKER_COMPLETED,
                        phase="execute",
                        status="skipped",
                        task_id=task.task_id,
                        plan_version=task.plan_version,
                        attempt=task.attempt,
                        attributes={
                            "objective": task.objective,
                            "worker_status": "skipped",
                            "fail_reason": "optional_wave_coverage_complete",
                        },
                        run_id=context.run_id,
                        session_id=context.session_id,
                    )
                    recorder.end_span(span_key, status="skipped")
            except Exception:
                pass
            return self._result(
                task,
                started,
                ok=True,
                status="skipped",
                summary="skipped_optional_wave_coverage_complete",
                fail_reason="optional_wave_coverage_complete",
                queue_ms=int((time.perf_counter() - queue_started) * 1000),
            )
        worker_result: WorkerResult | None = None
        worker_ok: bool | None = None
        simple_fact = bool(step.metadata.get("simple_fact_fast_path"))
        execute_agent = None
        dispatch_mode = "direct_search"
        if not simple_fact:
            try:
                execute_agent, dispatch_mode = resolve_execute_target(
                    task.step_type,
                    workers=getattr(self.harness, "workers", None),
                    main_agent=getattr(self.harness, "agent", None),
                    direct_invoke=bool(
                        getattr(self.harness.harness_config, "direct_worker_invoke", True)
                    ),
                    profile=step.subagent or "",
                )
            except Exception as exc:
                worker_ok = False
                return self._result(
                    task,
                    started,
                    ok=False,
                    status="failed",
                    summary=f"worker_unavailable:{type(exc).__name__}",
                    fail_reason=type(exc).__name__,
                )
        reserve_worker_lease = getattr(
            self.session.budget_manager, "reserve_worker_lease", None
        )
        if not callable(reserve_worker_lease):
            worker_ok = False
            worker_result = self._result(
                task,
                started,
                ok=False,
            status="blocked",
                summary="budget_blocked:budget_manager_unavailable",
                fail_reason="budget_manager_unavailable",
            )
        active_wave_size = getattr(self.session, "active_wave_size", None)
        if active_wave_size is None:
            resolve_parallel = getattr(self.session, "_resolve_max_workers", None)
            active_wave_size = resolve_parallel() if callable(resolve_parallel) else 3
        parallel_workers = max(1, int(active_wave_size))
        lease_id, block_reason = reserve_worker_lease(
            task.task_id,
            max_llm_calls=int(step.metadata.get("max_llm_calls") or 0) or None,
            token_ceiling=int(step.metadata.get("token_ceiling") or 0) or None,
            parallel_workers=parallel_workers,
        )
        queue_ms = int((time.perf_counter() - queue_started) * 1000)
        if not lease_id:
            worker_ok = False
            return self._result(
                task,
                started,
                ok=False,
                status="blocked",
                summary=f"budget_blocked:{block_reason}",
                fail_reason=block_reason,
                queue_ms=queue_ms,
            )

        recorder_span = ""
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            worker_attributes = worker_event_attributes(
                objective=task.objective,
                step_type=task.step_type,
                worker_runtime="v2",
                search_mode="agent",
                task_shape="simple_fact" if simple_fact else "",
                execution_path="fast_path" if simple_fact else "harness",
                dispatch_wave_id=task.dispatch_wave_id,
            )
            if recorder.is_active:
                try:
                    recorder_span = recorder.start_span(
                        "worker.execute_v2",
                        phase="execute",
                        task_id=task.task_id,
                        plan_version=task.plan_version,
                        attempt=task.attempt,
                        attributes=worker_attributes,
                    )
                except Exception as exc:
                    self._emit_observability_error(
                        recorder,
                        context,
                        operation="start_span",
                        error=exc,
                    )
                try:
                    recorder.emit(
                        EventType.WORKER_STARTED,
                        phase="execute",
                        status="start",
                        task_id=task.task_id,
                        plan_version=task.plan_version,
                        attempt=task.attempt,
                        attributes=worker_attributes,
                        run_id=context.run_id,
                        session_id=context.session_id,
                    )
                except Exception as exc:
                    self._emit_observability_error(
                        recorder,
                        context,
                        operation="worker_started",
                        error=exc,
                    )
        except Exception:
            recorder_span = ""

        tool_usage = {"tool_calls": 0, "tools_invoked": []}
        try:
            timeout_sec = self._timeout_for(step)
            invoke_leaf = (
                self._invoke_simple_fact
                if simple_fact
                else self._invoke_leaf
            )
            result = await asyncio.wait_for(
                invoke_leaf(
                    task=task,
                    context=context,
                    step=step,
                    step_index=step_index,
                    execute_agent=execute_agent,
                    dispatch_mode=dispatch_mode,
                    tool_usage=tool_usage,
                ),
                timeout=timeout_sec,
            )
            result = self.harness._enrich_worker_result(step, result, self.session.state)
            payload = result.metadata.get("worker_payload")
            if not isinstance(payload, dict):
                structured = parse_worker_payload(
                    result.content,
                    step_type=step.step_type,
                    subagent=step.subagent or "",
                )
                attach_structured_payload(result, structured)
                payload = asdict(structured)
            citation_manager = getattr(self.session.ctx, "citation_manager", None)
            if citation_manager is not None:
                registered = citation_manager.register_from_step(
                    step_index,
                    step.step_type,
                    result.content,
                    result.metadata,
                )
                if registered:
                    result.metadata["evidence_sources"] = [
                        source.__dict__.copy() for source in registered
                    ]
                citation_manager.bind_worker_facts(
                    step_index,
                    step.step_type,
                    list(payload.get("facts") or []),
                    list(payload.get("sources") or []),
                )
            ok = bool(payload.get("ok", True))
            worker_ok = ok
            evidence_refs = list(payload.get("evidence_ids") or []) + list(
                payload.get("artifact_ids") or []
            )
            return self._result(
                task,
                started,
                ok=ok,
                status="done" if ok else "failed",
                summary=str(payload.get("summary") or result.content)[:4000],
                findings=list(payload.get("findings") or []),
                evidence_refs=list(dict.fromkeys(evidence_refs)),
                facts=list(payload.get("facts") or []),
                sources=list(payload.get("sources") or []),
                raw=result,
                fail_reason="" if ok else str(payload.get("error_code") or "worker_failed"),
                metrics=self._worker_metrics(context, task, tool_usage),
                queue_ms=queue_ms,
            )
        except BudgetReservationError as exc:
            worker_ok = False
            reason = str(getattr(exc, "reason", "") or "budget_tokens")
            recovered = self._salvage_or_fail(
                task,
                context,
                step,
                step_index,
                started,
                cause=f"budget_blocked:{reason}",
                fail_reason=reason,
                status="blocked",
                ok=False,
                queue_ms=queue_ms,
            )
            worker_ok = recovered.ok
            worker_result = recovered
            return recovered
        except asyncio.TimeoutError:
            worker_ok = False
            recovered = self._salvage_or_fail(
                task,
                context,
                step,
                step_index,
                started,
                cause="worker_timeout",
                fail_reason="worker_timeout",
                status="failed",
                ok=False,
                queue_ms=queue_ms,
            )
            worker_ok = recovered.ok
            worker_result = recovered
            return recovered
        except Exception as exc:
            worker_ok = False
            reason = type(exc).__name__
            recovered = self._salvage_or_fail(
                task,
                context,
                step,
                step_index,
                started,
                cause=f"worker_exception:{reason}:{exc}",
                fail_reason=reason,
                status="failed",
                ok=False,
                queue_ms=queue_ms,
            )
            worker_ok = recovered.ok
            worker_result = recovered
            return recovered
        finally:
            self._sync_tool_usage(tool_usage)
            if lease_id:
                try:
                    release_worker_lease = getattr(
                        self.session.budget_manager, "release_worker_lease", None
                    )
                    if callable(release_worker_lease):
                        release_worker_lease(lease_id)
                except Exception:
                    pass
            try:
                from app.observability import EventType, get_recorder
                from app.research.domain.task_state import worker_result_lifecycle

                recorder = get_recorder()
                if recorder.is_active:
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    if worker_result is None:
                        execution_status = "failed"
                        result_status = "none"
                        stop_reason = "none"
                    elif worker_result.ok:
                        execution_status = "succeeded"
                        result_status = "complete"
                        stop_reason = "none"
                    elif worker_result.status == "skipped":
                        execution_status = "skipped"
                        result_status = "none"
                        stop_reason = "none"
                    else:
                        lifecycle_status, lifecycle_result, lifecycle_stop, _ = worker_result_lifecycle(worker_result)
                        execution_status = lifecycle_status.value
                        result_status = lifecycle_result.value
                        stop_reason = lifecycle_stop.value
                    recorder.emit(
                        EventType.WORKER_COMPLETED if worker_ok else EventType.WORKER_FAILED,
                        phase="execute",
                        status=execution_status,
                        task_id=task.task_id,
                        plan_version=task.plan_version,
                        attempt=task.attempt,
                        duration_ms=duration_ms,
                        attributes=worker_event_attributes(
                            objective=task.objective,
                            step_type=task.step_type,
                            worker_runtime="v2",
                            search_mode="agent",
                            task_shape="simple_fact" if simple_fact else "",
                            execution_path="fast_path" if simple_fact else "harness",
                            dispatch_wave_id=task.dispatch_wave_id,
                            execution_status=execution_status,
                            result_status=result_status,
                            stop_reason=stop_reason,
                            fail_reason=(
                                worker_result.fail_reason
                                if worker_result is not None
                                else "worker_result_missing"
                            ),
                            evidence_ids=(
                                worker_result.evidence_refs if worker_result is not None else []
                            ),
                            finding_ids=(
                                [
                                    str(item.get("finding_id") or "")
                                    for item in worker_result.findings
                                    if isinstance(item, dict)
                                ]
                                if worker_result is not None
                                else []
                            ),
                            tool_calls=int(tool_usage.get("tool_calls") or 0),
                            duration_ms=duration_ms,
                            metrics=worker_result.metrics if worker_result is not None else {},
                        ),
                        run_id=context.run_id,
                        session_id=context.session_id,
                    )
                    if recorder_span:
                        recorder.end_span(
                            recorder_span,
                            status="ok" if worker_ok else "failed",
                            duration_ms=duration_ms,
                        )
            except Exception:
                pass

    def _emit_observability_error(
        self,
        recorder: Any,
        context: ResearchContext,
        *,
        operation: str,
        error: Exception,
    ) -> None:
        try:
            recorder.emit(
                "observability.internal_error",
                phase="execute",
                status="warning",
                attributes={
                    "operation": operation,
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                    "run_id": context.run_id,
                    "session_id": context.session_id,
                },
            )
        except Exception:
            return

    async def _invoke_leaf(
        self,
        *,
        task: ResearchTask,
        context: ResearchContext,
        step: Any,
        step_index: int,
        execute_agent: Any,
        dispatch_mode: str,
        tool_usage: dict[str, Any],
    ) -> StepResult:
        self._record_direct_assistant(step)
        builder = self.harness.context_builder
        user_message = builder.build_step_message(
            context.query,
            self.session.state,
            step,
            step_index,
            self.session.ctx.relative_session_dir,
            self.session.ctx.uploaded_prompt,
            enforce_binding=self.harness.harness_config.enforce_subagent_binding,
            use_evidence_digest=self.harness.harness_config.synthesis_use_evidence_digest,
            dispatch_mode=dispatch_mode,
        )
        config = build_run_config(
            f"{context.session_id}:worker:{task.task_id}",
            metadata={
                "phase": "execute",
                "step_index": step_index,
                "step_type": step.step_type,
                "usage_session_id": context.session_id,
            },
        )
        gateway = LLMGateway(self.session.budget_manager)
        tool_limit = self._remaining_tool_calls()
        step_search_cap = int(step.metadata.get("max_search_calls") or 0)
        if step_search_cap > 0 and tool_limit is not None:
            tool_limit = min(tool_limit, step_search_cap)
        tool_gateway = ToolGateway(tool_limit)
        messages: list[Any] = []
        tools_invoked: list[str] = []
        tool_call_ids: set[tuple[str, str]] = set()
        try:
            with tool_gateway.execution_scope(
                worker_task_id=task.task_id,
                step_index=step_index,
                run_id=context.run_id,
                session_id=context.session_id,
            ):
                with gateway.execution_scope(
                    phase="execute",
                    worker_task_id=task.task_id,
                ):
                    async for chunk in gateway.astream(
                        execute_agent,
                        {"messages": [{"role": "user", "content": user_message}]},
                        config,
                    ):
                        for node_state in chunk.values():
                            if not isinstance(node_state, dict):
                                continue
                            for message in list(node_state.get("messages") or []):
                                messages.append(message)
                                for tool_call in getattr(message, "tool_calls", None) or []:
                                    call_name = str(tool_call.get("name") or "")
                                    call_id = str(
                                        tool_call.get("id")
                                        or tool_call.get("tool_call_id")
                                        or f"{call_name}:{len(tool_call_ids) + 1}"
                                    )
                                    identity = (call_id, call_name)
                                    if identity in tool_call_ids:
                                        continue
                                    tool_call_ids.add(identity)
                                    tools_invoked.append(call_name)
        finally:
            tool_usage["tool_calls"] = len(tool_call_ids)
            tool_usage["tools_invoked"] = tools_invoked
        content = ""
        for message in reversed(messages):
            content = str(getattr(message, "content", "") or "")
            if content.strip():
                break
        return StepResult(
            step_type=step.step_type,
            content=content,
            metadata={
                "step_index": step_index,
                "task_id": task.task_id,
                "worker_dispatch": "direct",
                "tools_invoked": tools_invoked,
                "tool_calls": len(tool_call_ids),
                "step_assistants_called": [step.subagent] if step.subagent else [],
                "duration_ms": 0,
            },
        )

    async def _invoke_simple_fact(
        self,
        *,
        task: ResearchTask,
        context: ResearchContext,
        step: Any,
        step_index: int,
        execute_agent: Any,
        dispatch_mode: str,
        tool_usage: dict[str, Any],
    ) -> StepResult:
        """Run one authorized provider search without an LLM worker."""
        _ = (execute_agent, dispatch_mode)
        from app.agent.harness.citations import (
            EvidenceSource,
            SourceTier,
            classify_source_tier,
        )
        from app.research.evidence.policy import SIMPLE_FACT_EVIDENCE_POLICY
        from app.tools.tavily_tool import internet_search

        tool_gateway = ToolGateway(self._remaining_tool_calls())
        responses: list[dict[str, Any]] = []
        queries: list[str] = [context.query]
        try:
            with tool_gateway.execution_scope(
                worker_task_id=task.task_id,
                step_index=step_index,
                run_id=context.run_id,
                session_id=context.session_id,
            ):
                first_response = tool_gateway.call(
                    internet_search.invoke,
                    {
                        "query": context.query,
                        "topic": "general",
                        "max_results": 2,
                        "include_raw_content": True,
                    },
                )
                if isinstance(first_response, dict):
                    responses.append(first_response)
                first_results = [
                    item
                    for item in (first_response.get("results") or [])
                    if isinstance(item, dict) and str(item.get("url") or "").strip()
                ] if isinstance(first_response, dict) else []
                provisional_sources = [
                    EvidenceSource(
                        source_id=f"candidate-{index}",
                        step_index=step_index,
                        step_type=step.step_type,
                        source_kind="url",
                        locator=str(item.get("url")),
                        excerpt="",
                    )
                    for index, item in enumerate(first_results)
                ]
                needs_followup = not SIMPLE_FACT_EVIDENCE_POLICY.is_sufficient(
                    provisional_sources
                )
                if needs_followup:
                    import re

                    normalized_query = re.sub(r"\s+", " ", context.query).strip(" ?？")
                    normalized_query = re.sub(
                        r"(?<=[A-Za-z0-9])\s+(?=[Vv]\d+\b)",
                        "-",
                        normalized_query,
                    )
                    followup_query = (
                        f"{normalized_query} 官方"
                        if any("\u4e00" <= char <= "\u9fff" for char in context.query)
                        else f"{normalized_query} official source"
                    )
                    queries.append(followup_query)
                    second_response = tool_gateway.call(
                        internet_search.invoke,
                        {
                            "query": followup_query,
                            "topic": "general",
                            "max_results": 5,
                            "include_raw_content": True,
                        },
                    )
                    if isinstance(second_response, dict):
                        responses.append(second_response)
        finally:
            tool_usage["tool_calls"] = len(queries)
            tool_usage["tools_invoked"] = ["internet_search"] * len(queries)

        if not responses:
            raise RuntimeError("simple_fact_search_invalid_response")

        seen_urls: set[str] = set()
        all_results = []
        for response in responses:
            for item in response.get("results") or []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                all_results.append(item)
        ranked_results = sorted(
            all_results,
            key=lambda item: {
                SourceTier.PRIMARY.value: 0,
                SourceTier.HIGH_QUALITY_SECONDARY.value: 1,
                SourceTier.COMMUNITY.value: 2,
                SourceTier.UNKNOWN.value: 3,
            }.get(classify_source_tier(str(item.get("url") or "")), 4),
        )
        results = [
            item
            for item in ranked_results
        ][:4]
        facts = [
            str(item.get("content") or item.get("raw_content") or item.get("title") or "").strip()
            for item in results
        ]
        facts = [fact for fact in facts if fact]
        sources = [str(item.get("url")) for item in results]
        summary = str(results[0].get("title") or next(iter(facts), "")).strip()
        payload = {
            "ok": bool(results and facts),
            "summary": summary,
            "facts": facts,
            "sources": sources,
            "findings": [
                {
                    "task_id": task.task_id,
                    "claim": fact,
                    "evidence_ids": [source],
                    "sources": [source],
                }
                for fact, source in zip(facts, sources)
            ],
            "artifact_ids": [],
            "error_code": "",
            "worker": "simple_fact_search",
            "step_type": step.step_type,
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return StepResult(
            step_type=step.step_type,
            content=content,
            metadata={
                "step_index": step_index,
                "task_id": task.task_id,
                "worker_dispatch": "simple_fact_fast_path",
                "tools_invoked": ["internet_search"],
                "tool_calls": len(queries),
                "step_assistants_called": [],
                "duration_ms": 0,
                "worker_payload": payload,
            },
        )

    def _remaining_tool_calls(self) -> int | None:
        config = self.harness.harness_config
        step_cap = int(getattr(config, "max_step_tool_calls", 8) or 0)
        run_cap = int(getattr(config, "max_tool_calls", 20) or 0)
        used = int(getattr(self.session.state, "tool_calls_count", 0) or 0)
        if step_cap <= 0:
            return None
        return max(0, min(step_cap, run_cap - used))

    def _timeout_for(self, step: Any) -> float:
        config = self.harness.harness_config
        timeout_sec = max(10, int(config.step_timeout_sec))
        if str(step.step_type) in SYNTHESIS_STEP_TYPES:
            synthesis_timeout = int(
                getattr(config, "synthesis_step_timeout_sec", 0) or 0
            )
            timeout_sec = max(timeout_sec, synthesis_timeout)
        remaining_method = getattr(self.session.budget_manager, "remaining_run_sec", None)
        if callable(remaining_method):
            remaining_sec = max(0.0, float(remaining_method()))
            if remaining_sec > 0:
                return max(10.0, min(float(timeout_sec), remaining_sec))
        return float(timeout_sec)

    def _record_direct_assistant(self, step: Any) -> None:
        assistant = str(step.subagent or "")
        if not assistant:
            return
        assistants_called = getattr(self.session.state, "assistants_called", None)
        if isinstance(assistants_called, list) and assistant not in assistants_called:
            assistants_called.append(assistant)

    def _sync_tool_usage(self, metadata: dict[str, Any]) -> None:
        observed = int(
            metadata.get("tool_calls")
            or len(metadata.get("tools_invoked") or [])
            or 0
        )
        state = self.session.state
        current = int(getattr(state, "tool_calls_count", 0) or 0)
        state.tool_calls_count = max(current, observed)
        snapshot_method = getattr(self.session.budget_manager, "snapshot", None)
        if not callable(snapshot_method):
            return
        try:
            snapshot = snapshot_method()
            state.tool_calls_count = max(
                int(state.tool_calls_count),
                int(getattr(snapshot, "tool_calls", 0) or 0),
            )
        except Exception:
            return

    def _worker_metrics(
        self,
        context: ResearchContext,
        task: ResearchTask,
        tool_usage: dict[str, Any],
        *,
        fail_reason: str = "",
        artifact_count: int | None = None,
    ) -> dict[str, Any]:
        tools_invoked = [str(item) for item in tool_usage.get("tools_invoked") or []]
        llm_calls = 0
        input_tokens = 0
        output_tokens = 0
        cache_hits = 0
        try:
            from app.agent.harness.usage_tracker import get_usage_tracker

            records = get_usage_tracker().session_summary(context.session_id).get("records") or []
            for raw_record in records:
                record = raw_record if isinstance(raw_record, dict) else {}
                if str((record.get("extra") or {}).get("worker_task_id") or "") != task.task_id:
                    continue
                llm_calls += 1
                input_tokens += int(record.get("prompt_tokens") or 0)
                output_tokens += int(record.get("completion_tokens") or 0)
                if int(record.get("cache_read_tokens") or 0) > 0:
                    cache_hits += 1
        except Exception:
            pass
        if artifact_count is None:
            try:
                from app.agent.harness.artifacts import get_artifact_store

                artifact_count = sum(
                    1
                    for item in get_artifact_store().iter_artifacts()
                    if str(item.metadata.get("run_id") or "") == context.run_id
                    and str(item.metadata.get("task_id") or "") == task.task_id
                )
            except Exception:
                artifact_count = 0
        return {
            "llm_calls": llm_calls,
            "search_calls": sum(
                1
                for name in tools_invoked
                if any(token in name.lower() for token in ("search", "tavily", "bocha"))
            ),
            "fetch_calls": sum(
                1
                for name in tools_invoked
                if any(token in name.lower() for token in ("fetch", "read_url"))
            ),
            "tool_calls": int(tool_usage.get("tool_calls") or len(tools_invoked) or 0),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_hits": cache_hits,
            "artifact_count": int(artifact_count or 0),
            "fail_reason": fail_reason,
        }

    def _salvage_or_fail(
        self,
        task: ResearchTask,
        context: ResearchContext,
        step: Any,
        step_index: int,
        started: float,
        *,
        cause: str,
        fail_reason: str,
        status: WorkerResultStatus,
        ok: bool,
        queue_ms: int = 0,
    ) -> WorkerResult:
        salvaged = salvage_worker_evidence(
            run_id=context.run_id,
            task_id=task.task_id,
            step_index=step_index,
        )
        findings = list(salvaged.get("findings") or [])
        evidence_refs = list(salvaged.get("evidence_refs") or [])
        sources = list(salvaged.get("sources") or [])
        facts = list(salvaged.get("facts") or [])
        candidates = list(salvaged.get("candidates") or [])
        if not findings and not evidence_refs and not sources and not facts and not candidates:
            return self._result(
                task,
                started,
                ok=ok,
                status=status,
                summary=cause,
                fail_reason=fail_reason,
            )

        payload = {
            "ok": False,
            "summary": f"{cause}; recovered evidence from existing artifacts",
            "facts": facts,
            "sources": sources,
            "findings": findings,
            "artifact_ids": evidence_refs,
            "candidates": candidates,
            "error_code": fail_reason,
            "worker": step.subagent or "",
            "step_type": step.step_type,
        }
        raw = StepResult(
            step_type=step.step_type,
            content="",
            metadata={
                "step_index": step_index,
                "task_id": task.task_id,
                "worker_runtime": "v2",
                "recovered_from_artifacts": True,
                "failure_before_recovery": cause,
                "worker_payload": payload,
                "structured_ok": False,
            },
        )
        return self._result(
            task,
            started,
            ok=False,
            status="partial",
            summary=payload["summary"],
            findings=findings,
            evidence_refs=evidence_refs,
            facts=facts,
            sources=sources,
                candidates=candidates,
                raw=raw,
                fail_reason=fail_reason,
                metrics=self._worker_metrics(
                    context,
                    task,
                    {"tool_calls": 0, "tools_invoked": []},
                    fail_reason=fail_reason,
                    artifact_count=len(evidence_refs),
                ),
                queue_ms=queue_ms,
            )

    def _result(
        self,
        task: ResearchTask,
        started: float,
        *,
        ok: bool,
        status: WorkerResultStatus,
        summary: str = "",
        findings: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
        facts: list[str] | None = None,
        sources: list[str] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        raw: Any = None,
        fail_reason: str = "",
        metrics: dict[str, Any] | None = None,
        queue_ms: int = 0,
    ) -> WorkerResult:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return WorkerResult(
            ok=ok,
            task_id=task.task_id,
            status=status,
            summary=summary,
            findings=findings or [],
            evidence_refs=evidence_refs or [],
            facts=facts or [],
            sources=sources or [],
            candidates=candidates or [],
            raw=raw,
            fail_reason=fail_reason,
            metrics=metrics
            or {
                "llm_calls": 0,
                "search_calls": 0,
                "fetch_calls": 0,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hits": 0,
                "artifact_count": 0,
                "fail_reason": fail_reason,
            },
            duration_ms=duration_ms,
            execution_ms=max(0, duration_ms - max(0, queue_ms)),
            queue_ms=queue_ms,
        )


__all__ = ["WorkerExecutorV2"]
