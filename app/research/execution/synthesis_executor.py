"""Evidence-only synthesis executor with no retrieval authorization."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any

from app.agent.harness.orchestration import (
    attach_structured_payload,
    parse_worker_payload,
)
from app.agent.harness.state import StepResult
from app.agent.harness.worker_runtime import resolve_execute_target
from app.api.tracing import build_run_config
from app.research.execution.llm_gateway import LLMGateway
from app.research.execution.tool_gateway import ToolGateway
from app.research.runtime.worker import (
    ResearchContext,
    ResearchTask,
    WorkerResult,
    WorkerResultStatus,
)


def _failure_reason(exc: Exception) -> str:
    message = str(exc).lower()
    if "sensitivecontentdetected" in message or "content_filter" in message:
        return "provider_content_filter"
    if "rate limit" in message or "ratelimit" in message:
        return "provider_rate_limit"
    if "usage limit" in message or "quota" in message:
        return "provider_usage_limit"
    if "context length" in message or "context_length_exceeded" in message:
        return "context_length_exceeded"
    if "auth" in message:
        return "provider_auth"
    if "bad request" in message:
        return "provider_bad_request"
    return type(exc).__name__


class SynthesisExecutor:
    """Execute synthesis from stored evidence; retrieval is structurally denied."""

    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    async def execute(
        self,
        task: ResearchTask,
        context: ResearchContext,
    ) -> WorkerResult:
        started = time.perf_counter()
        plan = getattr(self.session.state, "plan", None)
        step_index = int(task.step_index)
        if plan is None or step_index >= len(plan.steps):
            return self._result(
                task,
                started,
                ok=False,
                status="failed",
                summary="missing_step",
                fail_reason="missing_step",
            )
        step = plan.steps[step_index]
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
            return self._result(
                task,
                started,
                ok=False,
                status="failed",
                summary=f"synthesis_worker_unavailable:{type(exc).__name__}",
                fail_reason=type(exc).__name__,
            )

        timeout_sec = self._timeout_for(step)
        try:
            result = await asyncio.wait_for(
                self._invoke(
                    task=task,
                    context=context,
                    step=step,
                    step_index=step_index,
                    execute_agent=execute_agent,
                    dispatch_mode=dispatch_mode,
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            return self._result(
                task,
                started,
                ok=False,
                status="failed",
                summary="synthesis_timeout",
                fail_reason="synthesis_timeout",
            )
        except Exception as exc:
            fail_reason = _failure_reason(exc)
            return self._result(
                task,
                started,
                ok=False,
                status="failed",
                summary=f"synthesis_failed:{fail_reason}",
                fail_reason=fail_reason,
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
        ok = bool(payload.get("ok", True))
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
            fail_reason="" if ok else str(payload.get("error_code") or "synthesis_failed"),
        )

    async def _invoke(
        self,
        *,
        task: ResearchTask,
        context: ResearchContext,
        step: Any,
        step_index: int,
        execute_agent: Any,
        dispatch_mode: str,
    ) -> StepResult:
        user_message = self.harness.context_builder.build_step_message(
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
            f"{context.session_id}:synthesis:{task.task_id}",
            metadata={
                "phase": "synthesis",
                "step_index": step_index,
                "step_type": step.step_type,
                "usage_session_id": context.session_id,
            },
        )
        gateway = LLMGateway(self.session.budget_manager)
        messages: list[Any] = []
        with ToolGateway(0).execution_scope():
            with gateway.execution_scope(
                phase="synthesis",
            ):
                async for chunk in gateway.astream(
                    execute_agent,
                    {"messages": [{"role": "user", "content": user_message}]},
                    config,
                ):
                    for node_state in chunk.values():
                        if not isinstance(node_state, dict):
                            continue
                        messages.extend(node_state.get("messages") or [])
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
                "worker_dispatch": "synthesis",
                "tools_invoked": [],
                "tool_calls": 0,
                "step_assistants_called": [step.subagent] if step.subagent else [],
            },
        )

    def _timeout_for(self, step: Any) -> float:
        config = self.harness.harness_config
        timeout_sec = max(
            10,
            int(
                getattr(config, "synthesis_step_timeout_sec", 0)
                or config.step_timeout_sec
            ),
        )
        remaining_method = getattr(self.session.budget_manager, "remaining_run_sec", None)
        if callable(remaining_method):
            remaining_sec = max(0.0, float(remaining_method()))
            if remaining_sec > 0:
                return max(10.0, min(float(timeout_sec), remaining_sec))
        return float(timeout_sec)

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


__all__ = ["SynthesisExecutor"]
