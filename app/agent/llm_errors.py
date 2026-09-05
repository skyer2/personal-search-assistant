"""Unified classification for LLM provider failures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LLMFailureKind(str, Enum):
    RATE_LIMIT = "rate_limit"
    USAGE_LIMIT = "usage_limit"
    SERVER_ERROR = "server_error"
    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    CONTENT_FILTER = "content_filter"
    CONTEXT_LENGTH = "context_length"
    INVALID_REQUEST = "invalid_request"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LLMFailure:
    kind: LLMFailureKind
    message: str
    exception_type: str


@dataclass(frozen=True)
class LLMFailurePolicy:
    retryable: bool
    max_attempts: int
    action: str
    backoff_base_sec: float = 0.5


_FAILURE_POLICIES: dict[LLMFailureKind, LLMFailurePolicy] = {
    LLMFailureKind.RATE_LIMIT: LLMFailurePolicy(True, 2, "respect_retry_after"),
    LLMFailureKind.SERVER_ERROR: LLMFailurePolicy(True, 2, "backoff_with_jitter"),
    LLMFailureKind.CONNECTION_ERROR: LLMFailurePolicy(True, 2, "retry"),
    LLMFailureKind.TIMEOUT: LLMFailurePolicy(True, 1, "retry_or_degrade"),
    LLMFailureKind.USAGE_LIMIT: LLMFailurePolicy(False, 0, "stop_research_partial"),
    LLMFailureKind.CONTENT_FILTER: LLMFailurePolicy(False, 0, "reduce_context"),
    LLMFailureKind.CONTEXT_LENGTH: LLMFailurePolicy(False, 0, "compact_context"),
    LLMFailureKind.INVALID_REQUEST: LLMFailurePolicy(False, 0, "fail_node"),
    LLMFailureKind.BAD_REQUEST: LLMFailurePolicy(False, 0, "fail_node"),
    LLMFailureKind.AUTH: LLMFailurePolicy(False, 0, "fail_dependency"),
    LLMFailureKind.UNKNOWN: LLMFailurePolicy(False, 0, "fail_fast"),
}


def failure_policy(kind: LLMFailureKind) -> LLMFailurePolicy:
    return _FAILURE_POLICIES.get(kind, _FAILURE_POLICIES[LLMFailureKind.UNKNOWN])


def retry_delay_sec(exc: BaseException) -> float:
    retry_after = getattr(exc, "retry_after", None)
    headers = getattr(exc, "response_headers", None) or getattr(exc, "headers", None)
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    if isinstance(headers, dict):
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    return 0.5


def classify_llm_exception(exc: BaseException) -> LLMFailure:
    message = str(exc)
    lowered = message.lower()
    raw_status = getattr(exc, "status_code", None)
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None
    exception_type = type(exc).__name__

    if "sensitivecontentdetected" in lowered or "content_filter" in lowered or "content filter" in lowered:
        kind = LLMFailureKind.CONTENT_FILTER
    elif (
        status == 429
        or "rate limit" in lowered
        or "rate_limit" in lowered
        or "usage limit" in lowered
        or "usage_limit" in lowered
        or "quota" in lowered
        or "exceeds your plan" in lowered
        or "upgrade your plan" in lowered
    ):
        kind = (
            LLMFailureKind.USAGE_LIMIT
            if (
                "usage limit" in lowered
                or "usage_limit" in lowered
                or "quota" in lowered
                or "exceeds your plan" in lowered
                or "upgrade your plan" in lowered
            )
            else LLMFailureKind.RATE_LIMIT
        )
    elif status in {500, 502, 503, 504} or "server error" in lowered or "overloaded" in lowered:
        kind = LLMFailureKind.SERVER_ERROR
    elif (
        "connection" in lowered
        or "connection reset" in lowered
        or "connection refused" in lowered
        or exception_type.endswith("ConnectionError")
    ):
        kind = LLMFailureKind.CONNECTION_ERROR
    elif status == 401 or status == 403 or "authentication" in lowered or "permission" in lowered:
        kind = LLMFailureKind.AUTH
    elif "context_length" in lowered or "maximum context" in lowered or "token limit" in lowered:
        kind = LLMFailureKind.CONTEXT_LENGTH
    elif "timeout" in lowered or "timed out" in lowered or exception_type.endswith("TimeoutError"):
        kind = LLMFailureKind.TIMEOUT
    elif status == 400 or exception_type.endswith("BadRequestError"):
        kind = LLMFailureKind.INVALID_REQUEST
    else:
        kind = LLMFailureKind.UNKNOWN
    return LLMFailure(kind=kind, message=message, exception_type=exception_type)
