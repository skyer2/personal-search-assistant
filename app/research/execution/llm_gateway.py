"""Single authorization boundary for LLM-backed execution."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

from app.agent.harness.usage_tracker import (
    bind_budget_manager,
    get_llm_phase,
    set_llm_phase,
)

StopClass = Literal[
    "completed", "length", "content_filter", "tool_call", "provider_error", "unknown"
]


@dataclass(frozen=True)
class ModelCapabilities:
    output_limit_parameter: str | None = None
    supports_streaming: bool = False
    reasoning_model: bool = False
    supports_reasoning_effort: bool = False
    usage_field_mapping: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelCallResult:
    content: str
    stop_class: StopClass
    finish_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def model_capabilities(model: Any) -> ModelCapabilities:
    declared = getattr(model, "model_capabilities", None)
    if isinstance(declared, ModelCapabilities):
        return declared
    if isinstance(declared, dict):
        return ModelCapabilities(**{
            key: value for key, value in declared.items()
            if key in ModelCapabilities.__dataclass_fields__
        })
    # Compatibility adapter for pre-v2 LangChain bindings. New adapters must
    # declare capabilities; this branch is never used by answer-contract-v2's
    # validated-template delivery.
    if callable(getattr(model, "bind", None)):
        return ModelCapabilities(
            output_limit_parameter="max_tokens",
            supports_streaming=bool(getattr(model, "supports_synthesis_streaming", False)),
        )
    return ModelCapabilities()


def bind_output_limit(model: Any, limit: int) -> Any:
    capabilities = model_capabilities(model)
    parameter = capabilities.output_limit_parameter
    if not parameter:
        return model
    bind = getattr(model, "bind", None)
    if not callable(bind):
        raise ValueError(f"model declares {parameter!r} but has no bind capability")
    return bind(**{parameter: max(1, int(limit))})


class LLMGateway:
    def __init__(self, budget_manager: Any | None):
        self.budget_manager = budget_manager

    @contextmanager
    def execution_scope(self, *, phase: str, worker_task_id: str = "") -> Iterator[None]:
        from app.agent.harness.usage_tracker import bind_worker_budget_scope

        previous = get_llm_phase()
        set_llm_phase(phase)
        try:
            with bind_budget_manager(self.budget_manager):
                if worker_task_id:
                    with bind_worker_budget_scope(worker_task_id):
                        yield
                    return
                yield
        finally:
            set_llm_phase(previous)

    async def astream(self, target: Any, payload: Any, config: dict[str, Any] | None = None):
        if self.budget_manager is None:
            raise RuntimeError("LLM request rejected: budget manager unavailable")
        async for chunk in target.astream(payload, config=config or {}):
            yield chunk

    def invoke(self, target: Any, payload: Any, config: dict[str, Any] | None = None) -> Any:
        if self.budget_manager is None:
            raise RuntimeError("LLM request rejected: budget manager unavailable")
        return target.invoke(payload, config=config or {})

    async def ainvoke(self, target: Any, payload: Any, config: dict[str, Any] | None = None) -> Any:
        if self.budget_manager is None:
            raise RuntimeError("LLM request rejected: budget manager unavailable")
        return await target.ainvoke(payload, config=config or {})


__all__ = [
    "LLMGateway", "ModelCallResult", "ModelCapabilities", "StopClass",
    "bind_output_limit", "model_capabilities",
]
