"""Structured invocation boundary for semantic control-plane models."""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from pydantic import TypeAdapter

from app.api.tracing import build_run_config
from app.research.execution.llm_gateway import LLMGateway


T = TypeVar("T")


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


__all__ = ["StructuredLLMGateway"]
