"""Unified classification for LLM provider failures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LLMFailureKind(str, Enum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONTENT_FILTER = "content_filter"
    CONTEXT_LENGTH = "context_length"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LLMFailure:
    kind: LLMFailureKind
    message: str
    exception_type: str


def classify_llm_exception(exc: BaseException) -> LLMFailure:
    message = str(exc)
    lowered = message.lower()
    status = getattr(exc, "status_code", None)
    exception_type = type(exc).__name__

    if "sensitivecontentdetected" in lowered or "content_filter" in lowered or "content filter" in lowered:
        kind = LLMFailureKind.CONTENT_FILTER
    elif status == 429 or "rate limit" in lowered or "rate_limit" in lowered:
        kind = LLMFailureKind.RATE_LIMIT
    elif status == 401 or status == 403 or "authentication" in lowered or "permission" in lowered:
        kind = LLMFailureKind.AUTH
    elif "context_length" in lowered or "maximum context" in lowered or "token limit" in lowered:
        kind = LLMFailureKind.CONTEXT_LENGTH
    elif "timeout" in lowered or "timed out" in lowered or exception_type.endswith("TimeoutError"):
        kind = LLMFailureKind.TIMEOUT
    elif status == 400 or exception_type.endswith("BadRequestError"):
        kind = LLMFailureKind.BAD_REQUEST
    else:
        kind = LLMFailureKind.UNKNOWN
    return LLMFailure(kind=kind, message=message, exception_type=exception_type)
