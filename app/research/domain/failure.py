"""Unified failure classification for runtime facts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    RECOVERABLE = "recoverable"
    SEMANTIC = "semantic"
    BUDGET = "budget"
    POLICY = "policy"
    CANCELLED = "cancelled"
    PROGRAMMING = "programming"


class FailureInfo(TypedDict):
    failure_class: str
    code: str
    message: str
    retryable: bool
    occurred_at: str


def failure_info(
    code: str,
    failure_class: FailureClass,
    *,
    message: str = "",
    retryable: bool = False,
    occurred_at: str = "",
) -> FailureInfo:
    return FailureInfo(
        failure_class=failure_class.value,
        code=code,
        message=message or code,
        retryable=bool(retryable and failure_class in {FailureClass.TRANSIENT, FailureClass.RECOVERABLE}),
        occurred_at=occurred_at,
    )


def classify_failure(code: str, *, message: str = "") -> FailureInfo:
    normalized = code.lower()
    if normalized in {"timeout", "step_timeout", "429", "rate_limit", "provider_rate_limit"}:
        return failure_info(code, FailureClass.TRANSIENT, message=message, retryable=True)
    if normalized in {
        "parser_failure", "invalid_json", "search_miss", "empty_result", "wrong_subagent",
        "provider_unavailable", "network_error",
    }:
        return failure_info(code, FailureClass.RECOVERABLE, message=message, retryable=True)
    if normalized in {"coverage_gap", "evidence_conflict", "unsupported_claim", "quality_failed"}:
        return failure_info(code, FailureClass.SEMANTIC, message=message)
    if normalized in {
        "budget_tokens", "research_token_cap", "budget_llm_calls", "budget_tool_calls",
        "budget_exhausted", "deadline_exceeded", "synthesis_time_reserve",
    }:
        return failure_info(code, FailureClass.BUDGET, message=message)
    if normalized in {
        "provider_policy", "provider_content_filter", "source_restricted", "safety_restriction",
        "step_rejected",
    }:
        return failure_info(code, FailureClass.POLICY, message=message)
    if normalized in {"cancelled", "user_cancelled", "interrupted"}:
        return failure_info(code, FailureClass.CANCELLED, message=message)
    if normalized in {"illegal_transition", "corrupt_state", "invalid_enum", "missing_session"}:
        return failure_info(code, FailureClass.PROGRAMMING, message=message)
    return failure_info(code, FailureClass.RECOVERABLE, message=message, retryable=True)


def failure_from_dict(raw: Any) -> FailureInfo:
    value = dict(raw) if isinstance(raw, dict) else {}
    return failure_info(
        str(value.get("code") or "unknown_failure"),
        FailureClass(str(value.get("failure_class") or FailureClass.RECOVERABLE.value)),
        message=str(value.get("message") or ""),
        retryable=bool(value.get("retryable")),
        occurred_at=str(value.get("occurred_at") or ""),
    )


__all__ = ["FailureClass", "FailureInfo", "classify_failure", "failure_from_dict", "failure_info"]
