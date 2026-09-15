"""Structured invocation boundary for semantic control-plane models."""

from __future__ import annotations

import asyncio
import json
import os
import re
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
        config = build_run_config(phase, metadata={"phase": phase})
        # A number of OpenAI-compatible gateways accept ordinary chat but do
        # not implement tool/structured-output requests. Their native wrapper
        # can wait for a provider-side timeout before our fallback runs. Use a
        # fast JSON prompt + local schema validation by default for custom base
        # URLs; native structured output remains opt-in for compatible APIs.
        base_url = (os.getenv("OPENAI_BASE_URL") or "").strip()
        native = os.getenv("HARNESS_STRUCTURED_OUTPUT_NATIVE", "").lower() in {"1", "true", "yes", "on"}
        # Test doubles and provider wrappers may expose only
        # ``with_structured_output``; keep that contract when ordinary
        # ``ainvoke`` is unavailable.
        use_native = native or not base_url or not callable(getattr(model, "ainvoke", None))
        if use_native:
            structured_model = model.with_structured_output(TypeAdapter(schema).json_schema())
            value = await asyncio.wait_for(
                self.gateway.ainvoke(structured_model, prompt, config),
                timeout=timeout_sec,
            )
            return self._coerce(value, schema)

        json_prompt = (
            f"{prompt}\n\n只输出一个合法 JSON 对象，不要 Markdown 代码块，不要解释。"
        )
        value = await asyncio.wait_for(
            self.gateway.ainvoke(model, json_prompt, config),
            timeout=timeout_sec,
        )
        return self._coerce_json_text(value, schema)

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

    @classmethod
    def _coerce_json_text(cls, value: Any, schema: type[T]) -> T:
        if isinstance(value, dict):
            return cls._coerce(value, schema)
        raw = value if isinstance(value, str) else getattr(value, "content", None)
        if isinstance(raw, list):
            raw = "".join(
                str(item.get("text") or "")
                for item in raw
                if isinstance(item, dict) and item.get("type") in {None, "text"}
            )
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                raise TypeError("structured model returned no JSON object")
            decoded = json.loads(match.group(0))
        return cls._coerce(decoded, schema)


__all__ = [
    "StructuredLLMGateway",
    "classify_structured_error",
    "emit_semantic_fallback",
]
