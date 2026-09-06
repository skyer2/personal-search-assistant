"""Single authorization boundary for LLM-backed execution."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from app.agent.harness.usage_tracker import (
    bind_budget_manager,
    get_llm_phase,
    set_llm_phase,
)


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
        async for chunk in target.astream(payload, config=config or {}):
            yield chunk

    async def ainvoke(self, target: Any, payload: Any, config: dict[str, Any] | None = None) -> Any:
        return await target.ainvoke(payload, config=config or {})


__all__ = ["LLMGateway"]
