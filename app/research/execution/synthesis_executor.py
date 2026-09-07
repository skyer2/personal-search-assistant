"""Evidence-only synthesis executor with no retrieval authorization."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.api.tracing import build_run_config
from app.research.execution.llm_gateway import LLMGateway
from app.research.execution.tool_gateway import ToolGateway
from app.research.runtime.worker import ResearchContext, WorkerResult


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


@dataclass(frozen=True)
class SynthesisRequest:
    """The only input contract for the fixed synthesis pipeline."""

    mode: str
    evidence_refs: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    unresolved_conflicts: list[str] = field(default_factory=list)
    research_summary: str = ""


class SynthesisExecutor:
    """Execute synthesis from stored evidence; retrieval is structurally denied."""

    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    async def execute(self, request: SynthesisRequest, context: ResearchContext) -> WorkerResult:
        started = time.perf_counter()
        if request.mode not in {"normal", "degraded"}:
            return self._result(
                started,
                ok=False,
                summary="invalid_synthesis_mode",
                fail_reason="invalid_synthesis_mode",
                evidence_refs=request.evidence_refs,
            )
        agent = getattr(self.harness, "agent", None)
        if agent is None:
            return self._result(
                started,
                ok=False,
                summary="synthesis_worker_unavailable",
                fail_reason="synthesis_worker_unavailable",
                evidence_refs=request.evidence_refs,
            )

        try:
            content = await asyncio.wait_for(
                self._invoke(agent=agent, request=request, context=context),
                timeout=self._timeout_sec(),
            )
        except asyncio.TimeoutError:
            return self._result(
                started,
                ok=False,
                summary="synthesis_timeout",
                fail_reason="synthesis_timeout",
                evidence_refs=request.evidence_refs,
            )
        except Exception as exc:
            fail_reason = _failure_reason(exc)
            return self._result(
                started,
                ok=False,
                summary=f"synthesis_failed:{fail_reason}",
                fail_reason=fail_reason,
                evidence_refs=request.evidence_refs,
            )

        if not content.strip():
            return self._result(
                started,
                ok=False,
                summary="synthesis_empty_content",
                fail_reason="synthesis_empty_content",
                evidence_refs=request.evidence_refs,
            )
        return self._result(
            started,
            ok=True,
            summary=content[:4000],
            evidence_refs=request.evidence_refs,
        )

    async def _invoke(
        self,
        *,
        agent: Any,
        request: SynthesisRequest,
        context: ResearchContext,
    ) -> str:
        config = build_run_config(
            f"{context.session_id}:synthesis:{context.run_id}",
            metadata={
                "phase": "synthesis",
                "mode": request.mode,
                "usage_session_id": context.session_id,
            },
        )
        gateway = LLMGateway(self.session.budget_manager)
        messages: list[Any] = []
        with ToolGateway(0).execution_scope():
            with gateway.execution_scope(phase="synthesis"):
                async for chunk in gateway.astream(
                    agent,
                    {"messages": [{"role": "user", "content": self._prompt(request, context)}]},
                    config,
                ):
                    for node_state in chunk.values():
                        if not isinstance(node_state, dict):
                            continue
                        messages.extend(node_state.get("messages") or [])
        for message in reversed(messages):
            content = str(getattr(message, "content", "") or "")
            if content.strip():
                return content
        return ""

    def _prompt(self, request: SynthesisRequest, context: ResearchContext) -> str:
        evidence = "\n".join(f"- {item}" for item in request.evidence_refs[:80])
        limitations = "\n".join(f"- {item}" for item in request.limitations[:20])
        conflicts = "\n".join(f"- {item}" for item in request.unresolved_conflicts[:20])
        mode_instruction = (
            "基于完整证据输出可靠结论。"
            if request.mode == "normal"
            else "基于现有证据输出降级结论，明确说明覆盖不足和无法确认的部分，不得补写未证实内容。"
        )
        return "\n".join(
            part
            for part in (
                f"任务：{context.query}",
                f"合成模式：{request.mode}",
                f"要求：{mode_instruction}",
                "硬性约束：只允许使用下方研究摘要和证据引用；禁止联网、读取文件或发明新证据。",
                "研究摘要：",
                request.research_summary[:80000],
                "证据引用：",
                evidence or "- 无",
                "覆盖限制：",
                limitations or "- 无",
                "未解决冲突：",
                conflicts or "- 无",
                "输出要求：直接输出面向用户的报告正文；引用证据对应的原始来源；不要输出 JSON。",
            )
            if str(part).strip()
        )

    def _timeout_sec(self) -> float:
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
        started: float,
        *,
        ok: bool,
        summary: str,
        evidence_refs: list[str] | None = None,
        fail_reason: str = "",
    ) -> WorkerResult:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return WorkerResult(
            ok=ok,
            task_id="synthesis",
            status="done" if ok else "failed",
            summary=summary,
            evidence_refs=list(dict.fromkeys(evidence_refs or [])),
            fail_reason=fail_reason,
            duration_ms=duration_ms,
            execution_ms=duration_ms,
        )


__all__ = ["SynthesisExecutor", "SynthesisRequest"]
