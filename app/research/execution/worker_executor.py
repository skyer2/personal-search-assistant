"""Task executor that bypasses the legacy per-step control loop."""

from __future__ import annotations

import asyncio
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
    salvage_worker_evidence,
)


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
        step_index = int(task.step_index)
        plan = getattr(self.session.state, "plan", None)
        if plan is None or step_index >= len(plan.steps):
            return self._result(task, started, ok=False, status="failed", summary="missing_step")
        step = plan.steps[step_index]
        worker_ok: bool | None = None
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
            return self._result(
                task,
                started,
                ok=False,
                status="blocked",
                summary="budget_blocked:budget_manager_unavailable",
                fail_reason="budget_manager_unavailable",
            )
        resolve_parallel = getattr(self.session, "_resolve_max_workers", None)
        parallel_workers = int(resolve_parallel() if callable(resolve_parallel) else 3)
        lease_id, block_reason = reserve_worker_lease(
            task.task_id,
            parallel_workers=parallel_workers,
        )
        if not lease_id:
            worker_ok = False
            return self._result(
                task,
                started,
                ok=False,
                status="blocked",
                summary=f"budget_blocked:{block_reason}",
                fail_reason=block_reason,
            )

        recorder_span = ""
        try:
            from app.observability import EventType, get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                recorder_span = recorder.start_span(
                    "worker.execute_v2",
                    phase="execute",
                    task_id=task.task_id,
                    plan_version=task.plan_version,
                    attributes={
                        "objective": task.objective,
                        "worker_runtime": "v2",
                        "step_type": task.step_type,
                    },
                )
                recorder.emit(
                    EventType.WORKER_STARTED,
                    phase="execute",
                    status="start",
                    task_id=task.task_id,
                    plan_version=task.plan_version,
                    attributes={"worker_runtime": "v2", "step_type": task.step_type},
                    run_id=context.run_id,
                    session_id=context.session_id,
                )
        except Exception:
            recorder_span = ""

        tool_usage = {"tool_calls": 0, "tools_invoked": []}
        try:
            timeout_sec = self._timeout_for(step)
            result = await asyncio.wait_for(
                self._invoke_leaf(
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
            )
        except BudgetReservationError as exc:
            worker_ok = False
            reason = str(getattr(exc, "reason", "") or "budget_tokens")
            recovered = self._salvage_or_fail(
                task,
                step,
                step_index,
                started,
                cause=f"budget_blocked:{reason}",
                fail_reason=reason,
                status="blocked",
                ok=False,
            )
            worker_ok = recovered.ok
            return recovered
        except asyncio.TimeoutError:
            worker_ok = False
            recovered = self._salvage_or_fail(
                task,
                step,
                step_index,
                started,
                cause="worker_timeout",
                fail_reason="worker_timeout",
                status="failed",
                ok=False,
            )
            worker_ok = recovered.ok
            return recovered
        except Exception as exc:
            worker_ok = False
            reason = type(exc).__name__
            recovered = self._salvage_or_fail(
                task,
                step,
                step_index,
                started,
                cause=f"worker_exception:{reason}:{exc}",
                fail_reason=reason,
                status="failed",
                ok=False,
            )
            worker_ok = recovered.ok
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

                recorder = get_recorder()
                if recorder.is_active:
                    recorder.emit(
                        EventType.WORKER_COMPLETED if worker_ok else EventType.WORKER_FAILED,
                        phase="execute",
                        status="ok" if worker_ok else "failed",
                        task_id=task.task_id,
                        plan_version=task.plan_version,
                        attributes={"worker_runtime": "v2"},
                        run_id=context.run_id,
                        session_id=context.session_id,
                    )
                    if recorder_span:
                        recorder.end_span(
                            recorder_span,
                            status="ok" if worker_ok else "failed",
                            duration_ms=int((time.perf_counter() - started) * 1000),
                        )
            except Exception:
                pass

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
        tool_gateway = ToolGateway(self._remaining_tool_calls())
        messages: list[Any] = []
        tools_invoked: list[str] = []
        tool_call_ids: set[tuple[str, str]] = set()
        try:
            with tool_gateway.execution_scope():
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

    def _salvage_or_fail(
        self,
        task: ResearchTask,
        step: Any,
        step_index: int,
        started: float,
        *,
        cause: str,
        fail_reason: str,
        status: str,
        ok: bool,
    ) -> WorkerResult:
        salvaged = salvage_worker_evidence(task_id=task.task_id, step_index=step_index)
        findings = list(salvaged.get("findings") or [])
        evidence_refs = list(salvaged.get("evidence_refs") or [])
        sources = list(salvaged.get("sources") or [])
        facts = list(salvaged.get("facts") or [])
        if not findings and not evidence_refs and not sources and not facts:
            return self._result(
                task,
                started,
                ok=ok,
                status=status,
                summary=cause,
                fail_reason=fail_reason,
            )

        payload = {
            "ok": True,
            "summary": f"{cause}; recovered evidence from existing artifacts",
            "facts": facts,
            "sources": sources,
            "findings": findings,
            "artifact_ids": evidence_refs,
            "error_code": "",
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
                "structured_ok": True,
            },
        )
        return self._result(
            task,
            started,
            ok=True,
            status="done",
            summary=payload["summary"],
            findings=findings,
            evidence_refs=evidence_refs,
            facts=facts,
            sources=sources,
            raw=raw,
            fail_reason="",
        )

    def _result(
        self,
        task: ResearchTask,
        started: float,
        *,
        ok: bool,
        status: str,
        summary: str = "",
        findings: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
        facts: list[str] | None = None,
        sources: list[str] | None = None,
        raw: Any = None,
        fail_reason: str = "",
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
            raw=raw,
            fail_reason=fail_reason,
            duration_ms=duration_ms,
            execution_ms=duration_ms,
        )


__all__ = ["WorkerExecutorV2"]
