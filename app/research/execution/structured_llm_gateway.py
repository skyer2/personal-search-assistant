"""Structured invocation boundary for semantic control-plane models."""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypeVar

from pydantic import TypeAdapter

from app.api.tracing import build_run_config
from app.research.execution.llm_gateway import LLMGateway


T = TypeVar("T")


def classify_structured_error(exc: Exception) -> str:
    from app.agent.harness.run_budget import BudgetReservationError

    message = str(exc).lower()
    if isinstance(exc, BudgetReservationError) or "budget" in message:
        return "budget"
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in message or "timed out" in message:
        return "timeout"
    if "validation" in message or type(exc).__name__ == "ValidationError":
        return "validation"
    if any(token in message for token in ("connection", "http", "api", "provider", "rate limit")):
        return "provider"
    if isinstance(exc, (TypeError, ValueError)):
        return "structured_output"
    return "unknown"


def emit_semantic_fallback(
    *,
    phase: str,
    component: str,
    fallback: str,
    exc: Exception,
    schema: type[Any],
    model: Any,
    started: float,
) -> None:
    try:
        from app.observability import EventType, get_recorder

        recorder = get_recorder()
        if not recorder.is_active:
            return
        model_name = str(
            getattr(model, "model_name", None)
            or getattr(model, "model", None)
            or "unknown"
        )
        recorder.emit(
            EventType.SEMANTIC_FALLBACK,
            phase=phase,
            status="fallback",
            duration_ms=int((time.perf_counter() - started) * 1000),
            attributes={
                "phase": phase,
                "component": component,
                "fallback": fallback,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "error_category": classify_structured_error(exc),
                "model": model_name,
                "provider": "openai-compatible",
                "schema": schema.__name__,
            },
        )
    except Exception:
        return


class StructuredLLMGateway:
    """Invoke a raw ChatModel with a provider-supported structured schema."""

    def __init__(self, budget_manager: Any | None):
        self.gateway = LLMGateway(budget_manager)

    async def ainvoke(
        self,
        *,
        model: Any,
        schema: type[T],
        prompt: str,
        phase: str,
        timeout_sec: float,
    ) -> T:
        if model is None:
            raise ValueError("structured model unavailable")
        if timeout_sec <= 0:
            raise ValueError("structured model timeout must be positive")
        structured_model = model.with_structured_output(TypeAdapter(schema).json_schema())
        config = build_run_config(phase, metadata={"phase": phase})
        value = await asyncio.wait_for(
            self.gateway.ainvoke(structured_model, prompt, config),
            timeout=timeout_sec,
        )
        return self._coerce(value, schema)

    @staticmethod
    def _coerce(value: Any, schema: type[T]) -> T:
        if isinstance(value, schema):
            return value
        if isinstance(value, dict):
            from_dict = getattr(schema, "from_dict", None)
            if callable(from_dict):
                return from_dict(value)
            model_validate = getattr(schema, "model_validate", None)
            if callable(model_validate):
                return model_validate(value)
        raise TypeError("structured model returned an unsupported value")


__all__ = [
    "StructuredLLMGateway",
    "classify_structured_error",
    "emit_semantic_fallback",
]
