"""Evidence-only synthesis executor with no retrieval authorization."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from app.api.tracing import build_run_config
from app.agent.harness.token_counter import estimate_tokens
from app.research.execution.llm_gateway import LLMGateway
from app.research.delivery.synthesis_context import EvidenceDigest
from app.research.runtime.worker import ResearchContext, WorkerResult
from langchain_core.messages import HumanMessage


RETRYABLE_SYNTHESIS_FAILURES = frozenset(
    {
        "provider_rate_limit",
        "provider_unavailable",
        "context_length_exceeded",
        "synthesis_timeout",
    }
)
NON_RETRYABLE_SYNTHESIS_FAILURES = frozenset(
    {"provider_auth", "provider_bad_request", "run_token_cap", "run_llm_call_cap"}
)
_OUTPUT_TOKEN_LIMITS = {"normal": 3_000, "degraded": 1_800}


class _FirstTokenCallback(BaseCallbackHandler):
    """Measure TTFT when the provider emits token callbacks during ainvoke."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.first_token_ms: int | None = None

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        if token and self.first_token_ms is None:
            self.first_token_ms = int((time.perf_counter() - self.started) * 1000)


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
    conflict_resolutions: list[dict[str, Any]] = field(default_factory=list)
    research_summary: str = ""
    evidence_digests: list[EvidenceDigest] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    worker_summaries: list[dict[str, Any]] = field(default_factory=list)
    token_budget: int = 40_000
    attempt: int = 1
    pack_tokens_estimated: int = 0


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
        self._last_ttft_ms: int | None = None
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

        prompt = self._prompt(request, context)
        attempt_metadata = self._attempt_metadata(request, prompt, model)
        try:
            raw_response = await asyncio.wait_for(
                self._invoke_raw(model=model, request=request, context=context, prompt=prompt),
                timeout=self._timeout_sec(timeout_sec),
            )
        except asyncio.TimeoutError:
            return self._result(
                started,
                ok=False,
                summary="synthesis_timeout",
                fail_reason="synthesis_timeout",
                evidence_refs=request.evidence_refs,
                metadata={**attempt_metadata, **self._failure_metadata(
                    request,
                    context,
                    model,
                    error_type="asyncio.TimeoutError",
                    error_message="synthesis timeout",
                    error_category="timeout",
                ), "ttft_ms": self._last_ttft_ms},
            )
        except Exception as exc:
            fail_reason, error_category = _failure_details(exc)
            return self._result(
                started,
                ok=False,
                summary=f"synthesis_failed:{fail_reason}",
                fail_reason=fail_reason,
                evidence_refs=request.evidence_refs,
                metadata={**attempt_metadata, **self._failure_metadata(
                    request,
                    context,
                    model,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                    error_category=error_category,
                ), "ttft_ms": self._last_ttft_ms},
            )

        response_analysis = {
            **attempt_metadata,
            **self._analyze_response(raw_response, request, context, model),
        }
        response_analysis["ttft_ms"] = self._last_ttft_ms
        content = response_analysis["cleaned_content"]
        if not content:
            return self._result(
                started,
                ok=False,
                summary="synthesis_empty_content",
                fail_reason=str(response_analysis["fail_reason"]),
                evidence_refs=request.evidence_refs,
                metadata=response_analysis,
            )
        return self._result(
            started,
            ok=True,
            summary=content[:4000],
            evidence_refs=request.evidence_refs,
            metadata=response_analysis,
        )

    async def _invoke(
        self,
        *,
        model: Any,
        request: SynthesisRequest,
        context: ResearchContext,
    ) -> str:
        return self._response_content(
            await self._invoke_raw(
                model=model,
                request=request,
                context=context,
            )
        )

    async def _invoke_raw(
        self,
        *,
        model: Any,
        request: SynthesisRequest,
        context: ResearchContext,
        prompt: str | None = None,
    ) -> Any:
        config = build_run_config(
            f"{context.session_id}:synthesis:{context.run_id}",
            metadata={
                "phase": "synthesis",
                "mode": request.mode,
                "usage_session_id": context.session_id,
            },
        )
        ttft_callback = _FirstTokenCallback()
        config.setdefault("callbacks", []).append(ttft_callback)
        gateway = LLMGateway(self.session.budget_manager)
        with gateway.execution_scope(phase="synthesis"):
            output_token_limit = _OUTPUT_TOKEN_LIMITS.get(request.mode, 3_000)
            invoke_target = model
            bind = getattr(model, "bind", None)
            if callable(bind):
                invoke_target = bind(max_tokens=output_token_limit)
            try:
                response = await gateway.ainvoke(
                    invoke_target,
                    [HumanMessage(content=prompt if prompt is not None else self._prompt(request, context))],
                    config,
                )
            finally:
                self._last_ttft_ms = ttft_callback.first_token_ms
        return response

    def _attempt_metadata(
        self, request: SynthesisRequest, prompt: str, model: Any
    ) -> dict[str, Any]:
        return {
            "attempt": request.attempt,
            "pack_tokens_estimated": request.pack_tokens_estimated,
            "prompt_chars": len(prompt),
            "digest_chars": sum(len(digest.excerpt) for digest in request.evidence_digests),
            "estimated_input_tokens": estimate_tokens(prompt),
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "finish_reason": "",
            "ttft_ms": self._last_ttft_ms,
            "model": str(getattr(model, "model_name", None) or getattr(model, "model", None) or "unknown"),
            "provider": "openai-compatible",
        }

    def _prompt(self, request: SynthesisRequest, context: ResearchContext) -> str:
        mode_instruction = (
            "先直接回答每个用户问题，再给出关键判断、综合理由和对应证据。允许基于多条证据作出有边界的 inference/forecast，并明确区分 fact、inference、forecast 和 attributed opinion。"
            if request.mode == "normal"
            else "先直接回答每个用户问题，再给出简洁判断和证据。允许基于现有证据作出有边界的 inference/forecast，明确说明覆盖不足和无法确认的部分，不得补写未证实内容；不得搜索、抓取或重新规划。"
        )
        lines = [
            f"任务：{context.query}",
            f"合成模式：{request.mode}",
            f"要求：{mode_instruction}",
            "硬性约束：只允许使用下方研究摘要和证据摘录；禁止联网、读取文件或发明新证据。",
            "冲突规则：resolved 只能采用指定 winner；expected_disagreement 必须说明口径差异；unresolved 只能披露不确定性，禁止自行选择任何一方。",
            "引用规则：正文每个含数字、金额、日期或百分比的事实句末尾必须标注证据摘录行前缀给出的 [n]；禁止使用 E 编号、artifact 编号或自造编号。",
            "交付规则：不要输出 JSON，也不要说明文件生成能力；PDF/Markdown 由运行时统一生成。",
            "输出长度：normal 不超过2500字；degraded 不超过1500字。",
        ]
        evidence_lines: list[str] = []
        for digest in request.evidence_digests:
            citation_label = (
                f"[{digest.citation_number}]"
                if digest.citation_number > 0
                else "[未编号]"
            )
            evidence_lines.append(
                f"- {citation_label}｜{digest.evidence_id}｜{digest.title}｜{digest.locator}｜{digest.excerpt}"
            )
        if not evidence_lines:
            evidence_lines.extend(f"- {item}" for item in request.evidence_refs[:80])
        conflict_lines = [
            f"- {row.get('edge_id')}｜{row.get('status')}｜blocking={bool(row.get('blocking'))}｜"
            f"criterion={row.get('criterion_id') or 'unbound'}｜{row.get('label') or ''}"
            for row in request.conflict_resolutions[:24]
            if isinstance(row, dict)
        ]
        if not conflict_lines:
            conflict_lines.extend(f"- {item}" for item in request.unresolved_conflicts[:20])
        sections = (
            ("研究摘要：", [request.research_summary] if request.research_summary else []),
            ("证据摘录：", evidence_lines),
            ("覆盖限制：", [f"- {item}" for item in request.limitations[:20]]),
            ("未解决冲突：", [f"- {item}" for item in request.unresolved_conflicts[:20]]),
            ("冲突处理契约：", conflict_lines),
            ("输出要求：", ["直接输出面向用户的报告正文；开头必须回答问题，不能以‘已有以下信息’或证据清单开头；随后解释判断依据，最后给证据和限制；引用证据对应的原始来源；不要输出 JSON。"]),
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

    def _extract_response_content(self, response: Any) -> tuple[str, bool]:
        if isinstance(response, str):
            return response, True
        if isinstance(response, dict):
            if "content" in response:
                return self._extract_response_content(response.get("content"))
            if "text" in response:
                return str(response.get("text") or ""), True
            if "summary" in response:
                return str(response.get("summary") or ""), True
            return "", False
        if isinstance(response, list):
            if not response:
                return "", True
            texts: list[str] = []
            supported = True
            for item in response:
                text, item_supported = self._extract_response_content(item)
                supported = supported and item_supported
                if text:
                    texts.append(text)
            return "\n".join(texts), supported
        content = getattr(response, "content", None)
        if content is not None:
            return self._extract_response_content(content)
        response_text = getattr(response, "text", None)
        if response_text is not None:
            return str(response_text), True
        return "", False

    @staticmethod
    def _finish_reason(response: Any) -> str:
        metadata = getattr(response, "response_metadata", None)
        if isinstance(metadata, dict) and metadata.get("finish_reason"):
            return str(metadata.get("finish_reason"))
        metadata = getattr(response, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("finish_reason"):
            return str(metadata.get("finish_reason"))
        return str(getattr(response, "finish_reason", "") or "")

    @staticmethod
    def _usage(response: Any) -> tuple[int, int]:
        usage = getattr(response, "usage_metadata", None)
        if isinstance(usage, dict):
            return int(usage.get("input_tokens") or 0), int(
                usage.get("output_tokens") or 0
            )
        usage = getattr(response, "usage", None)
        if isinstance(usage, dict):
            return int(usage.get("prompt_tokens") or 0), int(
                usage.get("completion_tokens") or 0
            )
        metadata = getattr(response, "response_metadata", None)
        token_usage = metadata.get("token_usage") if isinstance(metadata, dict) else None
        if isinstance(token_usage, dict):
            return int(token_usage.get("prompt_tokens") or 0), int(
                token_usage.get("completion_tokens") or 0
            )
        return 0, 0

    def _analyze_response(
        self,
        response: Any,
        request: SynthesisRequest,
        context: ResearchContext,
        model: Any,
    ) -> dict[str, Any]:
        raw_content, supported = self._extract_response_content(response)
        cleaned_content = self._clean_response_content(raw_content)
        if not supported:
            fail_reason = "unsupported_response_shape"
        elif not raw_content.strip():
            fail_reason = "provider_empty_content"
        elif not cleaned_content.strip():
            fail_reason = "content_removed_by_cleaner"
        else:
            fail_reason = ""
        actual_input_tokens, actual_output_tokens = self._usage(response)
        return {
            "raw_response_type": type(response).__name__,
            "raw_content_chars": len(raw_content),
            "cleaned_content": cleaned_content,
            "cleaned_content_chars": len(cleaned_content),
            "response_supported": supported,
            "fail_reason": fail_reason,
            "finish_reason": self._finish_reason(response),
            "estimated_input_tokens": self.estimate_input_tokens(request, context),
            "actual_input_tokens": actual_input_tokens,
            "actual_output_tokens": actual_output_tokens,
            "model": str(
                getattr(response, "model_name", None)
                or getattr(model, "model_name", None)
                or getattr(model, "model", None)
                or "unknown"
            ),
            "provider": "openai-compatible",
        }

    def _response_content(self, response: Any) -> str:
        raw_content, _supported = self._extract_response_content(response)
        return self._clean_response_content(raw_content)

    @staticmethod
    def _clean_response_content(content: str) -> str:
        original = content.strip()
        recovered_summaries: list[str] = []

        def collect_summary(payload: Any) -> None:
            if isinstance(payload, dict) and "ok" in payload:
                summary = str(payload.get("summary") or "").strip()
                if summary and summary not in recovered_summaries:
                    recovered_summaries.append(summary)

        decoder = json.JSONDecoder()
        cursor = 0
        while True:
            start = original.find("{", cursor)
            if start < 0:
                break
            try:
                payload, end = decoder.raw_decode(original, start)
            except json.JSONDecodeError:
                cursor = start + 1
                continue
            collect_summary(payload)
            cursor = end

        cleaned = re.sub(
            r"```json\s*.*?```",
            "",
            original,
            flags=re.DOTALL,
        )
        decoder = json.JSONDecoder()
        cursor = 0
        while True:
            start = cleaned.find("{", cursor)
            if start < 0:
                break
            try:
                payload, end = decoder.raw_decode(cleaned, start)
            except json.JSONDecodeError:
                cursor = start + 1
                continue
            if (
                isinstance(payload, dict)
                and "ok" in payload
                and ("findings" in payload or "summary" in payload)
            ):
                collect_summary(payload)
                cleaned = cleaned[:start] + cleaned[end:]
                cursor = 0
            else:
                cursor = end
        cleaned = "\n".join(
            line
            for line in cleaned.splitlines()
            if not (
                line.lstrip().startswith('{"ok":')
                and line.rstrip().endswith("}")
            )
        )
        cleaned = re.sub(
            r"^说明：当前对话环境无法直接生成或附带PDF文件.*(?:\n|$)",
            "",
            cleaned,
            flags=re.MULTILINE,
        )
        cleaned = cleaned.strip()
        if not cleaned and recovered_summaries:
            return recovered_summaries[0]
        return cleaned

    def _timeout_sec(self, requested_timeout_sec: float | None = None) -> float:
        config = self.harness.harness_config
        profile_timeout = getattr(self.session, "synthesis_timeout_sec", None)
        default_timeout_sec = (
            profile_timeout()
            if callable(profile_timeout)
            else getattr(config, "synthesis_step_timeout_sec", 0) or 60
        )
        timeout_sec = max(
            1,
            int(
                requested_timeout_sec
                if requested_timeout_sec is not None
                else default_timeout_sec
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
        result_metadata = dict(metadata or {})
        result_metadata["duration_ms"] = duration_ms
        return WorkerResult(
            ok=ok,
            task_id="synthesis",
            status="done" if ok else "failed",
            summary=summary,
            evidence_refs=list(dict.fromkeys(evidence_refs or [])),
            fail_reason=fail_reason,
            metadata=result_metadata,
            duration_ms=duration_ms,
            execution_ms=duration_ms,
        )


__all__ = [
    "NON_RETRYABLE_SYNTHESIS_FAILURES",
    "RETRYABLE_SYNTHESIS_FAILURES",
    "SynthesisExecutor",
    "SynthesisRequest",
]
