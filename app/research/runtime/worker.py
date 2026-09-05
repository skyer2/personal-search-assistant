"""WorkerRuntime：Research Domain 与 Agent Framework 的边界。

图节点只依赖本 Protocol。LangChain / DeepAgents 是当前实现；
DeepSeek Harness 将来作为第二个 adapter，不改 StateGraph。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

WorkerResultStatus = Literal["done", "failed", "skipped", "blocked"]


class WorkerIdleTimeoutError(Exception):
    """Raised when a worker makes no observable progress within its lease."""


async def _run_worker_step_with_lease(
    awaitable: Any,
    *,
    child: Any,
    wall_timeout_sec: float,
    idle_timeout_sec: float,
) -> Any:
    task = asyncio.create_task(awaitable)
    started = time.perf_counter()
    last_progress = started

    def signature() -> tuple[int, ...]:
        try:
            from app.agent.harness.artifacts import get_artifact_store

            artifact_count = len(get_artifact_store())
        except Exception:
            artifact_count = 0
        return (
            int(getattr(child, "tool_calls_count", 0) or 0),
            len(getattr(child, "trace", None) or []),
            len(getattr(child, "step_results", None) or []),
            artifact_count,
        )

    last_signature = signature()
    while not task.done():
        now = time.perf_counter()
        remaining_wall = wall_timeout_sec - (now - started)
        remaining_idle = idle_timeout_sec - (now - last_progress)
        delay = min(1.0, max(0.05, min(remaining_wall, remaining_idle)))
        done, _pending = await asyncio.wait({task}, timeout=delay)
        if done:
            return task.result()
        now = time.perf_counter()
        current_signature = signature()
        if current_signature != last_signature:
            last_signature = current_signature
            last_progress = now
        if now - last_progress >= idle_timeout_sec:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise WorkerIdleTimeoutError("worker idle timeout")
        if now - started >= wall_timeout_sec:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise asyncio.TimeoutError()
    return task.result()


def salvage_worker_evidence(
    *,
    task_id: str,
    step_index: int,
    limit: int = 8,
) -> dict[str, list[Any]]:
    """Recover artifact-backed evidence when a worker misses its deadline."""
    try:
        from app.agent.harness.artifacts import get_artifact_store
        from app.research.runtime.untrusted import structured_evidence_from_artifact

        store = get_artifact_store()
        artifacts = [
            item
            for item in store.iter_artifacts()
            if item.step_index == step_index
            or str(item.metadata.get("task_id") or "") == task_id
        ][:limit]
    except Exception:
        return {"findings": [], "evidence_refs": [], "sources": []}

    findings: list[dict[str, Any]] = []
    evidence_refs: list[str] = []
    sources: list[str] = []
    for artifact in artifacts:
        evidence_refs.append(artifact.artifact_id)
        locator = artifact.locator or artifact.ref()
        if locator not in sources:
            sources.append(locator)
        evidence = structured_evidence_from_artifact(artifact)
        findings.append(
            {
                "task_id": task_id,
                "finding_id": f"salvage_{artifact.artifact_id}",
                "summary": evidence["excerpt"] or evidence["title"],
                "facts": [evidence["title"]][:1],
                "sources": [locator],
                "artifact_id": artifact.artifact_id,
                "evidence_ids": [evidence["evidence_id"]],
                "trust": evidence["trust"],
                "instruction_free": evidence["instruction_free"],
                "partial": True,
            }
        )
    return {"findings": findings, "evidence_refs": evidence_refs, "sources": sources}


@dataclass
class ResearchTask:
    """按研究目标描述的工人任务，而不是「请调用某类工具」。"""

    task_id: str
    objective: str
    step_type: str = ""
    step_index: int = 0
    description: str = ""
    subagent: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    source_policy: dict[str, Any] = field(default_factory=dict)
    plan_version: int = 1


@dataclass
class ResearchContext:
    run_id: str
    query: str
    user_id: str = "me"
    tenant_id: str = "local"
    project_id: str = "Inbox"
    session_id: str = ""


@dataclass
class WorkerResult:
    ok: bool
    task_id: str
    status: WorkerResultStatus = "done"
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    raw: Any = None
    fail_reason: str = ""
    queue_ms: int = 0
    execution_ms: int = 0
    duration_ms: int = 0


@runtime_checkable
class WorkerRuntime(Protocol):
    async def execute(
        self, task: ResearchTask, context: ResearchContext
    ) -> WorkerResult: ...


class LangChainWorkerRuntime:
    """当前实现：隔离 child LoopState + harness._run_single_step。

    图不得再直接调用 _run_single_step。换 runtime 时只换本 adapter。
    """

    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    async def execute(
        self, task: ResearchTask, context: ResearchContext
    ) -> WorkerResult:
        from app.agent.harness.state import StepStatus
        from app.research.runtime.isolation import (
            IsolatedWorkerOutcome,
            apply_isolated_outcome,
            snapshot_worker_loop_state,
        )
        from app.research.runtime.project import findings_from_worker_row
        from app.research.runtime.isolation import worker_row

        session = self.session
        step_index = int(task.step_index)
        if session is None or session.state.plan is None:
            return WorkerResult(
                ok=False,
                task_id=task.task_id,
                status="failed",
                summary="missing_session",
                fail_reason="missing_session",
            )
        plan = session.state.plan
        if step_index >= len(plan.steps):
            return WorkerResult(
                ok=False,
                task_id=task.task_id,
                status="failed",
                summary="missing_step",
                fail_reason="missing_step",
            )
        step = plan.steps[step_index]

        import time

        from app.observability import EventType, get_recorder

        recorder = get_recorder()
        worker_started = time.perf_counter()
        attempt = int(step.metadata.get("attempt") or 1)
        queue_ms = 0
        exec_ms = 0
        child = None
        salvaged: dict[str, list[Any]] = {
            "findings": [],
            "evidence_refs": [],
            "sources": [],
        }
        parent_ctx = None
        span_key = ""
        if recorder.is_active:
            from app.observability.context import bind_worker

            parent_ctx, _token = bind_worker(
                task_id=task.task_id,
                attempt=attempt,
                plan_version=int(task.plan_version or 1),
            )
            span_key = recorder.start_span(
                "worker.execute",
                phase="execute",
                task_id=task.task_id,
                attempt=attempt,
                attributes={
                    "objective": task.objective,
                    "worker_runtime": "langchain",
                    "step_type": task.step_type,
                },
            )
            recorder.emit(
                EventType.WORKER_STARTED,
                phase="execute",
                status="start",
                task_id=task.task_id,
                attempt=attempt,
                plan_version=int(task.plan_version or 1),
                attributes={
                    "objective": task.objective,
                    "worker_runtime": "langchain",
                    "step_type": task.step_type,
                    "allowed_tools": list(task.allowed_tools or []),
                },
                run_id=session.run_id,
                session_id=session.session_id,
                trace_id=str(getattr(session.state, "trace_id", "") or ""),
            )

        try:
            queue_started = time.perf_counter()
            async with session.worker_sem:
                queue_ms = int((time.perf_counter() - queue_started) * 1000)
                exec_started = time.perf_counter()
                async with session.lock:
                    child = snapshot_worker_loop_state(session.state)
                    child.step_index = step_index
                    session.state.plan.steps[step_index].metadata[
                        "status"
                    ] = StepStatus.RUNNING.value
                child_step = (
                    child.plan.steps[step_index] if child.plan is not None else step
                )
                # 外层墙钟：含 retries；与剩余 run deadline 取 min
                try:
                    mgr = session.budget_manager
                    mgr.sync_from_usage(
                        session_id=session.session_id,
                        tool_calls=session.state.tool_calls_count,
                    )
                    allowed, why = mgr.research_allowed()
                    # Research 不得侵占 synthesis 时间储备
                    remaining = mgr.remaining_for_research_sec()
                except Exception:
                    allowed, why = True, ""
                    remaining = float(self.harness.harness_config.step_timeout_sec)
                if not allowed:
                    duration_ms = int((time.perf_counter() - worker_started) * 1000)
                    async with session.lock:
                        session.state.plan.steps[step_index].metadata[
                            "status"
                        ] = StepStatus.FAILED.value
                        session.state.metadata["force_synthesis"] = True
                    if recorder.is_active:
                        recorder.emit(
                            EventType.WORKER_FAILED,
                            phase="execute",
                            status="blocked",
                            duration_ms=duration_ms,
                            task_id=task.task_id,
                            attempt=attempt,
                            plan_version=int(task.plan_version or 1),
                            attributes={
                                "objective": task.objective,
                                "step_type": task.step_type,
                                "worker_status": "blocked",
                                "fail_reason": why or "budget_blocked",
                                "queue_ms": queue_ms,
                            },
                            run_id=session.run_id,
                            session_id=session.session_id,
                            trace_id=str(getattr(session.state, "trace_id", "") or ""),
                        )
                        if span_key:
                            recorder.end_span(span_key, status="blocked", duration_ms=duration_ms)
                    return WorkerResult(
                        ok=False,
                        task_id=task.task_id,
                        status="blocked",
                        summary=f"budget_blocked:{why}",
                        fail_reason=why or "budget_blocked",
                        queue_ms=queue_ms,
                        execution_ms=0,
                        duration_ms=duration_ms,
                    )
                step_timeout = max(
                    10, int(self.harness.harness_config.step_timeout_sec)
                )
                max_retries = max(
                    0, int(getattr(self.harness.harness_config, "max_retries", 2) or 2)
                )
                # 外层墙钟：不再 * (retries+1)；retry 共享同一 deadline，避免 6min straggler
                retry_slack = 1.0 + min(0.5, 0.25 * max_retries)
                worker_timeout = min(
                    float(step_timeout) * retry_slack, max(5.0, remaining)
                )
                # 可选任务：若已 force_synthesis / enough，直接跳过
                if bool(step.metadata.get("optional")) and (
                    (
                        isinstance(session.state.metadata, dict)
                        and session.state.metadata.get("force_synthesis")
                    )
                    or str(
                        (session.state.metadata or {})
                        .get("progress_assessment", {})
                        .get("verdict")
                        or ""
                    )
                    == "enough"
                ):
                    duration_ms = int((time.perf_counter() - worker_started) * 1000)
                    async with session.lock:
                        session.state.plan.steps[step_index].metadata[
                            "status"
                        ] = "skipped"
                    if recorder.is_active:
                        recorder.emit(
                            EventType.WORKER_COMPLETED,
                            phase="execute",
                            status="skipped",
                            duration_ms=duration_ms,
                            task_id=task.task_id,
                            attempt=attempt,
                            plan_version=int(task.plan_version or 1),
                            attributes={
                                "objective": task.objective,
                                "step_type": task.step_type,
                                "worker_status": "skipped",
                                "queue_ms": queue_ms,
                            },
                            run_id=session.run_id,
                            session_id=session.session_id,
                            trace_id=str(getattr(session.state, "trace_id", "") or ""),
                        )
                        if span_key:
                            recorder.end_span(span_key, status="skipped", duration_ms=duration_ms)
                    return WorkerResult(
                        ok=True,
                        task_id=task.task_id,
                        status="skipped",
                        summary="skipped_optional_early_stop",
                        queue_ms=queue_ms,
                        execution_ms=0,
                        duration_ms=duration_ms,
                    )
                try:
                    provider_attempt = 0
                    ok = False
                    while True:
                        try:
                            ok = await _run_worker_step_with_lease(
                                self.harness._run_single_step(
                                    child,
                                    child_step,
                                    step_index,
                                    session.ctx.task_query,
                                    session.ctx.relative_session_dir,
                                    session.ctx.uploaded_prompt,
                                    session.session_id,
                                    session.ctx.session_dir,
                                    None,
                                    session.ctx.idempotency,
                                    None,
                                ),
                                child=child,
                                wall_timeout_sec=worker_timeout,
                                idle_timeout_sec=max(
                                    0.05,
                                    float(
                                        getattr(
                                            self.harness.harness_config,
                                            "worker_idle_timeout_sec",
                                            30,
                                        )
                                        or 30
                                    ),
                                ),
                            )
                            break
                        except Exception as exc:
                            from app.agent.llm_errors import (
                                classify_llm_exception,
                                failure_policy,
                                retry_delay_sec,
                            )

                            failure = classify_llm_exception(exc)
                            policy = failure_policy(failure.kind)
                            if (
                                policy.retryable
                                and provider_attempt < policy.max_attempts - 1
                            ):
                                provider_attempt += 1
                                await asyncio.sleep(
                                    min(2.0, retry_delay_sec(exc))
                                )
                                continue
                            raise
                    fail_reason = "" if ok else "worker_failed"
                except (asyncio.TimeoutError, WorkerIdleTimeoutError) as timeout_exc:
                    ok = False
                    fail_reason = (
                        "idle_timeout"
                        if isinstance(timeout_exc, WorkerIdleTimeoutError)
                        else "step_timeout"
                    )
                    exec_ms = int((time.perf_counter() - exec_started) * 1000)
                    duration_ms = int((time.perf_counter() - worker_started) * 1000)
                    salvaged = salvage_worker_evidence(
                        task_id=task.task_id,
                        step_index=step_index,
                    )
                    outcome = IsolatedWorkerOutcome(
                        step_index=step_index,
                        task_id=task.task_id,
                        ok=False,
                        result=None,
                        child_state=child,
                        fail_reason=fail_reason,
                    )
                    async with session.lock:
                        apply_isolated_outcome(session.state, outcome)
                        session.state.plan.steps[step_index].metadata[
                            "status"
                        ] = StepStatus.FAILED.value
                        session.state.plan.steps[step_index].metadata[
                            "queue_ms"
                        ] = queue_ms
                        session.state.plan.steps[step_index].metadata[
                            "execution_ms"
                        ] = int((time.perf_counter() - exec_started) * 1000)
                    if recorder.is_active:
                        recorder.emit(
                            EventType.WORKER_FAILED,
                            phase="execute",
                            status="failed",
                            duration_ms=duration_ms,
                            task_id=task.task_id,
                            attempt=attempt,
                            plan_version=int(task.plan_version or 1),
                            attributes={
                                "objective": task.objective,
                                "step_type": task.step_type,
                                "worker_status": "failed",
                                "fail_reason": fail_reason,
                                "queue_ms": queue_ms,
                                "execution_ms": exec_ms,
                                "partial_evidence_count": len(salvaged["evidence_refs"]),
                            },
                            run_id=session.run_id,
                            session_id=session.session_id,
                            trace_id=str(getattr(session.state, "trace_id", "") or ""),
                        )
                        if span_key:
                            recorder.end_span(span_key, status="failed", duration_ms=duration_ms)
                    return WorkerResult(
                        ok=False,
                        task_id=task.task_id,
                        status="failed",
                        summary=fail_reason,
                        findings=list(salvaged["findings"]),
                        evidence_refs=list(salvaged["evidence_refs"]),
                        sources=list(salvaged["sources"]),
                        raw=outcome,
                        fail_reason=fail_reason,
                        queue_ms=queue_ms,
                        execution_ms=exec_ms,
                        duration_ms=duration_ms,
                    )
                result = child.step_results[-1] if child.step_results else None
                exec_ms = int((time.perf_counter() - exec_started) * 1000)
                outcome = IsolatedWorkerOutcome(
                    step_index=step_index,
                    task_id=task.task_id,
                    ok=bool(ok),
                    result=result,
                    child_state=child,
                    fail_reason=fail_reason,
                )
                async with session.lock:
                    apply_isolated_outcome(session.state, outcome)
                    session.state.plan.steps[step_index].metadata["queue_ms"] = queue_ms
                    session.state.plan.steps[step_index].metadata[
                        "execution_ms"
                    ] = exec_ms
                    session.state.step_validation_results.append(
                        {
                            "step_index": step_index,
                            "step_type": step.step_type,
                            "passed": bool(ok),
                            "parallel": True,
                            "queue_ms": queue_ms,
                            "execution_ms": exec_ms,
                        }
                    )
                    evidence_ids: list[str] = []
                    if result is not None and session.ctx.citation_manager is not None:
                        registered = session.ctx.citation_manager.register_from_step(
                            step_index,
                            step.step_type,
                            result.content,
                            result.metadata,
                        )
                        payload = (result.metadata or {}).get("worker_payload") or {}
                        if isinstance(payload, dict):
                            session.ctx.citation_manager.bind_worker_facts(
                                step_index,
                                step.step_type,
                                list(payload.get("facts") or []),
                                list(payload.get("sources") or []),
                            )
                        evidence_ids = [
                            str(getattr(src, "source_id", "") or "")
                            for src in list(registered or [])
                            if getattr(src, "source_id", None)
                        ]
                    self.harness._refresh_working_memory(
                        session.state,
                        session.ctx.citation_manager,
                        session.ctx.run_dir,
                        session.ctx.task_query,
                    )
                    session.state.metadata.pop("graph_step_gated", None)

            row = worker_row(task.task_id, step, bool(ok), result)
            findings = findings_from_worker_row(row)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if ok:
                try:
                    from app.research.runtime.latency import note_first_evidence

                    note_first_evidence(
                        session.state,
                        evidence_count=len(findings)
                        or len(
                            list(payload.get("facts") or [])
                            if isinstance(payload, dict)
                            else []
                        ),
                    )
                except Exception:
                    pass
            duration_ms = int((time.perf_counter() - worker_started) * 1000)
            finding_ids = []
            for idx, finding in enumerate(findings):
                fid = str(finding.get("finding_id") or f"f_{task.task_id}_{idx + 1}")
                finding_ids.append(fid)
            brief_id = str((session.state.metadata or {}).get("brief_id") or "")
            plan_id = f"plan_v{int(task.plan_version or 1)}"
            if recorder.is_active:
                recorder.emit(
                    EventType.WORKER_COMPLETED if ok else EventType.WORKER_FAILED,
                    phase="execute",
                    status="ok" if ok else "failed",
                    duration_ms=duration_ms,
                    task_id=task.task_id,
                    attempt=attempt,
                    plan_version=int(task.plan_version or 1),
                    attributes={
                        "objective": task.objective,
                        "step_type": task.step_type,
                        "brief_id": brief_id,
                        "plan_id": plan_id,
                        "finding_ids": finding_ids,
                        "evidence_ids": evidence_ids,
                        "gaps": list((payload or {}).get("gaps") or [])[:8],
                        "conflicts": list((payload or {}).get("conflicts") or [])[:8],
                        "confidence": (payload or {}).get("confidence"),
                        "worker_status": "done" if ok else "failed",
                        "tool_calls": int((payload or {}).get("tool_calls") or 0),
                        "search_calls": int((payload or {}).get("search_calls") or 0),
                        "tokens": int((payload or {}).get("tokens") or 0),
                        "fail_reason": "" if ok else "worker_failed",
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
                            {"type": "task", "id": task.task_id},
                        ]
                        if item
                    ],
                    output_refs=[
                        *[{"type": "finding", "id": fid} for fid in finding_ids],
                        *[
                            {"type": "evidence", "id": eid}
                            for eid in evidence_ids
                        ],
                    ],
                    run_id=session.run_id,
                    session_id=session.session_id,
                    trace_id=str(getattr(session.state, "trace_id", "") or ""),
                )
                if span_key:
                    recorder.end_span(
                        span_key,
                        status="ok" if ok else "failed",
                        duration_ms=duration_ms,
                    )
            return WorkerResult(
                ok=bool(ok),
                task_id=task.task_id,
                status="done" if ok else "failed",
                summary=str(row.get("summary") or ""),
                findings=findings,
                evidence_refs=evidence_ids,
                facts=list((payload or {}).get("facts") or []),
                sources=list((payload or {}).get("sources") or []),
                raw=outcome,
                fail_reason="" if ok else "worker_failed",
                queue_ms=queue_ms,
                execution_ms=exec_ms,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - worker_started) * 1000)
            from app.agent.llm_errors import LLMFailureKind, classify_llm_exception

            provider_failure = classify_llm_exception(exc)
            if provider_failure.kind is not LLMFailureKind.UNKNOWN:
                if child is None:
                    child = snapshot_worker_loop_state(session.state)
                    child.step_index = step_index
                salvaged = salvage_worker_evidence(
                    task_id=task.task_id,
                    step_index=step_index,
                )
                outcome = IsolatedWorkerOutcome(
                    step_index=step_index,
                    task_id=task.task_id,
                    ok=False,
                    result=None,
                    child_state=child,
                    fail_reason=provider_failure.kind.value,
                )
                async with session.lock:
                    apply_isolated_outcome(session.state, outcome)
                    session.state.plan.steps[step_index].metadata["status"] = (
                        StepStatus.FAILED.value
                    )
                    session.state.plan.steps[step_index].metadata["queue_ms"] = queue_ms
                    session.state.plan.steps[step_index].metadata["execution_ms"] = exec_ms
            if recorder.is_active:
                recorder.emit(
                    EventType.WORKER_FAILED,
                    phase="execute",
                    status="error",
                    duration_ms=duration_ms,
                    task_id=task.task_id,
                    attempt=attempt,
                    plan_version=int(task.plan_version or 1),
                    attributes={
                        "objective": task.objective,
                        "step_type": task.step_type,
                        "fail_reason": provider_failure.kind.value
                        if provider_failure.kind is not LLMFailureKind.UNKNOWN
                        else str(exc)[:500],
                        "provider_error_kind": provider_failure.kind.value,
                        "exception_type": provider_failure.exception_type,
                        "partial_evidence_count": len(salvaged["evidence_refs"])
                        if provider_failure.kind is not LLMFailureKind.UNKNOWN
                        else 0,
                    },
                    run_id=session.run_id,
                    session_id=session.session_id,
                    trace_id=str(getattr(session.state, "trace_id", "") or ""),
                )
                if span_key:
                    recorder.end_span(span_key, status="error", duration_ms=duration_ms)
            if provider_failure.kind is not LLMFailureKind.UNKNOWN:
                return WorkerResult(
                    ok=False,
                    task_id=task.task_id,
                    status="failed",
                    summary=f"provider_{provider_failure.kind.value}",
                    findings=list(salvaged["findings"]),
                    evidence_refs=list(salvaged["evidence_refs"]),
                    sources=list(salvaged["sources"]),
                    raw=outcome,
                    fail_reason=provider_failure.kind.value,
                    queue_ms=queue_ms,
                    execution_ms=exec_ms,
                    duration_ms=duration_ms,
                )
            raise
        finally:
            if parent_ctx is not None:
                from app.observability.context import set_context

                set_context(parent_ctx)


class PlaceholderWorkerRuntime:
    """无 harness 时的图编译 / 单测工人。"""

    async def execute(
        self, task: ResearchTask, context: ResearchContext
    ) -> WorkerResult:
        return WorkerResult(
            ok=True,
            task_id=task.task_id,
            status="done",
            summary="placeholder",
            findings=[
                {"task_id": task.task_id, "summary": task.objective or task.description}
            ],
            evidence_refs=[task.task_id] if task.task_id else [],
        )
