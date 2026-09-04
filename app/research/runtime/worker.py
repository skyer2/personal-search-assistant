"""WorkerRuntime：Research Domain 与 Agent Framework 的边界。

图节点只依赖本 Protocol。LangChain / DeepAgents 是当前实现；
DeepSeek Harness 将来作为第二个 adapter，不改 StateGraph。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    raw: Any = None
    fail_reason: str = ""


@runtime_checkable
class WorkerRuntime(Protocol):
    async def execute(self, task: ResearchTask, context: ResearchContext) -> WorkerResult:
        ...


class LangChainWorkerRuntime:
    """当前实现：隔离 child LoopState + harness._run_single_step。

    图不得再直接调用 _run_single_step。换 runtime 时只换本 adapter。
    """

    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    async def execute(self, task: ResearchTask, context: ResearchContext) -> WorkerResult:
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
            return WorkerResult(ok=False, task_id=task.task_id, fail_reason="missing_session")
        plan = session.state.plan
        if step_index >= len(plan.steps):
            return WorkerResult(ok=False, task_id=task.task_id, fail_reason="missing_step")
        step = plan.steps[step_index]

        import time

        from app.observability import EventType, get_recorder

        recorder = get_recorder()
        worker_started = time.perf_counter()
        attempt = int(step.metadata.get("attempt") or 1)
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
            )

        try:
            queue_started = time.perf_counter()
            async with session.worker_sem:
                queue_ms = int((time.perf_counter() - queue_started) * 1000)
                exec_started = time.perf_counter()
                async with session.lock:
                    child = snapshot_worker_loop_state(session.state)
                    child.step_index = step_index
                    session.state.plan.steps[step_index].metadata["status"] = StepStatus.RUNNING.value
                child_step = child.plan.steps[step_index] if child.plan is not None else step
                # 外层墙钟：含 retries；与剩余 run deadline 取 min
                try:
                    from app.agent.harness.run_budget import get_or_create_run_budget

                    mgr = get_or_create_run_budget(session.state, self.harness.harness_config)
                    mgr.sync_from_usage(
                        session_id=session.session_id,
                        tool_calls=session.state.tool_calls_count,
                    )
                    allowed, why = mgr.research_allowed()
                    remaining = mgr.remaining_run_sec()
                except Exception:
                    allowed, why = True, ""
                    remaining = float(self.harness.harness_config.step_timeout_sec)
                if not allowed:
                    async with session.lock:
                        session.state.plan.steps[step_index].metadata["status"] = StepStatus.FAILED.value
                        session.state.metadata["force_synthesis"] = True
                    return {
                        "task_status": {task.task_id: "failed"},
                        "worker_results": [
                            {
                                "task_id": task.task_id,
                                "ok": False,
                                "summary": f"budget_blocked:{why}",
                                "step_type": task.step_type,
                                "fail_reason": why or "budget_blocked",
                                "queue_ms": queue_ms,
                                "execution_ms": 0,
                            }
                        ],
                        "replan_exhausted": True,
                        "progress_assessment": {
                            "verdict": "enough",
                            "reason": "force_synthesis_budget",
                        },
                    }
                step_timeout = max(10, int(self.harness.harness_config.step_timeout_sec))
                max_retries = max(0, int(getattr(self.harness.harness_config, "max_retries", 2) or 2))
                worker_timeout = min(step_timeout * (max_retries + 1), max(5.0, remaining))
                try:
                    ok = await asyncio.wait_for(
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
                        timeout=worker_timeout,
                    )
                    fail_reason = "" if ok else "worker_failed"
                except asyncio.TimeoutError:
                    ok = False
                    fail_reason = "step_timeout"
                    result = None
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
                        session.state.plan.steps[step_index].metadata["status"] = StepStatus.FAILED.value
                        session.state.plan.steps[step_index].metadata["queue_ms"] = queue_ms
                        session.state.plan.steps[step_index].metadata["execution_ms"] = int(
                            (time.perf_counter() - exec_started) * 1000
                        )
                    return {
                        "task_status": {task.task_id: "failed"},
                        "worker_results": [
                            {
                                "task_id": task.task_id,
                                "ok": False,
                                "summary": "step_timeout",
                                "step_type": task.step_type,
                                "fail_reason": fail_reason,
                                "queue_ms": queue_ms,
                                "execution_ms": int((time.perf_counter() - exec_started) * 1000),
                            }
                        ],
                    }
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
                    session.state.plan.steps[step_index].metadata["execution_ms"] = exec_ms
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
                        session.ctx.session_dir,
                        session.ctx.task_query,
                    )
                    session.state.metadata.pop("graph_step_gated", None)

            row = worker_row(task.task_id, step, bool(ok), result)
            findings = findings_from_worker_row(row)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
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
                        "evidence_ids": evidence_ids if ok else [],
                        "gaps": list((payload or {}).get("gaps") or [])[:8],
                        "conflicts": list((payload or {}).get("conflicts") or [])[:8],
                        "confidence": (payload or {}).get("confidence"),
                        "tool_calls": int((payload or {}).get("tool_calls") or 0),
                        "search_calls": int((payload or {}).get("search_calls") or 0),
                        "tokens": int((payload or {}).get("tokens") or 0),
                        "fail_reason": "" if ok else "worker_failed",
                    },
                    input_refs=[
                        item
                        for item in [
                            {"type": "research_brief", "id": brief_id} if brief_id else None,
                            {"type": "research_plan", "id": plan_id},
                            {"type": "task", "id": task.task_id},
                        ]
                        if item
                    ],
                    output_refs=[
                        *[{"type": "finding", "id": fid} for fid in finding_ids],
                        *[{"type": "evidence", "id": eid} for eid in (evidence_ids if ok else [])],
                    ],
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
                summary=str(row.get("summary") or ""),
                findings=findings,
                evidence_refs=evidence_ids if ok else [],
                facts=list((payload or {}).get("facts") or []),
                sources=list((payload or {}).get("sources") or []),
                raw=outcome,
                fail_reason="" if ok else "worker_failed",
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - worker_started) * 1000)
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
                        "fail_reason": str(exc)[:500],
                    },
                )
                if span_key:
                    recorder.end_span(span_key, status="error", duration_ms=duration_ms)
            raise
        finally:
            if parent_ctx is not None:
                from app.observability.context import set_context

                set_context(parent_ctx)


class PlaceholderWorkerRuntime:
    """无 harness 时的图编译 / 单测工人。"""

    async def execute(self, task: ResearchTask, context: ResearchContext) -> WorkerResult:
        return WorkerResult(
            ok=True,
            task_id=task.task_id,
            summary="placeholder",
            findings=[{"task_id": task.task_id, "summary": task.objective or task.description}],
            evidence_refs=[task.task_id] if task.task_id else [],
        )
