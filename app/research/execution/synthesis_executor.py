"""Evidence-only synthesis executor with no retrieval authorization."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.api.tracing import build_run_config
from app.agent.harness.token_counter import estimate_tokens
from app.research.execution.llm_gateway import LLMGateway
from app.research.delivery.synthesis_context import EvidenceDigest
from app.research.runtime.worker import ResearchContext, WorkerResult
from langchain_core.messages import HumanMessage


RETRYABLE_SYNTHESIS_FAILURES = frozenset(
    {"provider_rate_limit", "provider_unavailable", "context_length_exceeded"}
)
NON_RETRYABLE_SYNTHESIS_FAILURES = frozenset(
    {"provider_auth", "provider_bad_request", "run_token_cap", "run_llm_call_cap"}
)


def _failure_details(exc: Exception) -> tuple[str, str]:
    message = str(exc).lower()
    if "run_token_cap" in message:
        return "run_token_cap", "budget_denied"
    if "run_llm_call_cap" in message:
        return "run_llm_call_cap", "budget_denied"
    if isinstance(exc, (TypeError, ValueError)) and any(
        token in message for token in ("invalid input", "expected", "input type", "message")
    ):
        return "local_validation", "local_validation"
    if "sensitivecontentdetected" in message or "content_filter" in message:
        return "provider_content_filter", "content_filter"
    if "rate limit" in message or "ratelimit" in message:
        return "provider_rate_limit", "rate_limit"
    if "usage limit" in message or "quota" in message:
        return "provider_usage_limit", "provider_quota"
    if "auth" in message or "401" in message or "permission" in message:
        return "provider_auth", "auth"
    if "bad request" in message or "400" in message:
        return "provider_bad_request", "provider_bad_request"
    if "context length" in message or "context_length_exceeded" in message:
        return "context_length_exceeded", "context_length"
    if "unavailable" in message or "connection" in message or "503" in message:
        return "provider_unavailable", "connection"
    if "stream" in message or "incomplete" in message:
        return "stream_error", "stream_error"
    return "unknown_provider_error", "unknown"


def _failure_reason(exc: Exception) -> str:
    return _failure_details(exc)[0]


@dataclass(frozen=True)
class SynthesisRequest:
    """The only input contract for the fixed synthesis pipeline."""

    mode: str
    evidence_refs: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    unresolved_conflicts: list[str] = field(default_factory=list)
    research_summary: str = ""
    evidence_digests: list[EvidenceDigest] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    worker_summaries: list[dict[str, Any]] = field(default_factory=list)
    token_budget: int = 40_000


class SynthesisExecutor:
    """Execute synthesis from stored evidence; retrieval is structurally denied."""

    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    async def execute(
        self,
        request: SynthesisRequest,
        context: ResearchContext,
        *,
        timeout_sec: float | None = None,
    ) -> WorkerResult:
        started = time.perf_counter()
        if request.mode not in {"normal", "degraded"}:
            return self._result(
                started,
                ok=False,
                summary="invalid_synthesis_mode",
                fail_reason="invalid_synthesis_mode",
                evidence_refs=request.evidence_refs,
            )
        model = getattr(self.harness, "synthesis_model", None)
        if model is None:
            return self._result(
                started,
                ok=False,
                summary="synthesis_worker_unavailable",
                fail_reason="synthesis_worker_unavailable",
                evidence_refs=request.evidence_refs,
            )

        try:
            content = await asyncio.wait_for(
                self._invoke(model=model, request=request, context=context),
                timeout=self._timeout_sec(timeout_sec),
            )
        except asyncio.TimeoutError:
            return self._result(
                started,
                ok=False,
                summary="synthesis_timeout",
                fail_reason="synthesis_timeout",
                evidence_refs=request.evidence_refs,
                metadata=self._failure_metadata(
                    request,
                    context,
                    model,
                    error_type="asyncio.TimeoutError",
                    error_message="synthesis timeout",
                    error_category="timeout",
                ),
            )
        except Exception as exc:
            fail_reason, error_category = _failure_details(exc)
            return self._result(
                started,
                ok=False,
                summary=f"synthesis_failed:{fail_reason}",
                fail_reason=fail_reason,
                evidence_refs=request.evidence_refs,
                metadata=self._failure_metadata(
                    request,
                    context,
                    model,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                    error_category=error_category,
                ),
            )

        if not content.strip():
            return self._result(
                started,
                ok=False,
                summary="synthesis_empty_content",
                fail_reason="empty_content",
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
        model: Any,
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
        with gateway.execution_scope(phase="synthesis"):
            response = await gateway.ainvoke(
                model,
                [HumanMessage(content=self._prompt(request, context))],
                config,
            )
        return self._response_content(response)

    def _prompt(self, request: SynthesisRequest, context: ResearchContext) -> str:
        mode_instruction = (
            "基于完整证据输出可靠结论。"
            if request.mode == "normal"
            else "基于现有证据输出降级结论，明确说明覆盖不足和无法确认的部分，不得补写未证实内容。"
        )
        lines = [
            f"任务：{context.query}",
            f"合成模式：{request.mode}",
            f"要求：{mode_instruction}",
            "硬性约束：只允许使用下方研究摘要和证据摘录；禁止联网、读取文件或发明新证据。",
        ]
        evidence_lines: list[str] = []
        for digest in request.evidence_digests:
            evidence_lines.append(
                f"- {digest.evidence_id}｜{digest.title}｜{digest.locator}｜{digest.excerpt}"
            )
        if not evidence_lines:
            evidence_lines.extend(f"- {item}" for item in request.evidence_refs[:80])
        sections = (
            ("研究摘要：", [request.research_summary] if request.research_summary else []),
            ("证据摘录：", evidence_lines),
            ("覆盖限制：", [f"- {item}" for item in request.limitations[:20]]),
            ("未解决冲突：", [f"- {item}" for item in request.unresolved_conflicts[:20]]),
            ("输出要求：", ["直接输出面向用户的报告正文；引用证据对应的原始来源；不要输出 JSON。"]),
        )
        budget = max(1_000, request.token_budget)
        for header, section_lines in sections:
            candidate = [*lines, header]
            for line in section_lines:
                candidate.append(line)
                if estimate_tokens("\n".join(candidate)) > budget:
                    candidate.pop()
                    break
            if len(candidate) > len(lines):
                lines = candidate
        return "\n".join(line for line in lines if str(line).strip())

    def estimate_input_tokens(self, request: SynthesisRequest, context: ResearchContext) -> int:
        return estimate_tokens(self._prompt(request, context))

    def _response_content(self, response: Any) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            content = response.get("content")
            return str(content or "") if content is not None else ""
        if isinstance(response, list):
            return self._response_content(response[-1]) if response else ""
        return str(getattr(response, "content", "") or "")

    def _timeout_sec(self, requested_timeout_sec: float | None = None) -> float:
        config = self.harness.harness_config
        timeout_sec = max(
            1,
            int(
                requested_timeout_sec
                if requested_timeout_sec is not None
                else getattr(config, "synthesis_step_timeout_sec", 0)
                or 60
            ),
        )
        remaining_method = getattr(self.session.budget_manager, "remaining_run_sec", None)
        if callable(remaining_method):
            remaining_sec = max(0.0, float(remaining_method()))
            return min(float(timeout_sec), remaining_sec)
        return float(timeout_sec)

    def _failure_metadata(
        self,
        request: SynthesisRequest,
        context: ResearchContext,
        model: Any,
        *,
        error_type: str,
        error_message: str,
        error_category: str,
    ) -> dict[str, Any]:
        snapshot_method = getattr(self.session.budget_manager, "snapshot", None)
        snapshot = snapshot_method() if callable(snapshot_method) else None
        return {
            "error": {
                "type": error_type,
                "message": error_message,
                "category": error_category,
            },
            "model": str(
                getattr(model, "model_name", None)
                or getattr(model, "model", None)
                or "unknown"
            ),
            "provider": "openai-compatible",
            "mode": request.mode,
            "estimated_input_tokens": self.estimate_input_tokens(request, context),
            "remaining_run_tokens": max(
                0,
                int(getattr(snapshot, "token_limit", 0) or 0)
                - int(getattr(snapshot, "used_tokens", 0) or 0),
            ),
            "remaining_run_sec": max(
                0.0, float(getattr(snapshot, "remaining_run_sec", 0.0) or 0.0)
            ),
            "fallback": "deterministic_partial",
        }

    def _result(
        self,
        started: float,
        *,
        ok: bool,
        summary: str,
        evidence_refs: list[str] | None = None,
        fail_reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> WorkerResult:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return WorkerResult(
            ok=ok,
            task_id="synthesis",
            status="done" if ok else "failed",
            summary=summary,
            evidence_refs=list(dict.fromkeys(evidence_refs or [])),
            fail_reason=fail_reason,
            metadata=dict(metadata or {}),
            duration_ms=duration_ms,
            execution_ms=duration_ms,
        )


__all__ = [
    "NON_RETRYABLE_SYNTHESIS_FAILURES",
    "RETRYABLE_SYNTHESIS_FAILURES",
    "SynthesisExecutor",
    "SynthesisRequest",
]
