"""Single authorization boundary for worker tool execution."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from app.agent.harness.step_budget import retrieval_budget


class ToolGateway:
    def __init__(self, remaining_calls: int | None):
        self.remaining_calls = remaining_calls

    @contextmanager
    def execution_scope(
        self,
        *,
        worker_task_id: str = "",
        step_index: int = -1,
        run_id: str = "",
        session_id: str = "",
    ) -> Iterator[None]:
        from app.agent.harness.usage_tracker import bind_worker_execution_scope

        with retrieval_budget(self.remaining_calls):
            if not worker_task_id:
                yield
                return
            with bind_worker_execution_scope(
                worker_task_id,
                step_index=step_index,
                run_id=run_id,
                session_id=session_id,
            ):
                yield

    def authorize(self, count: int = 1) -> None:
        if self.remaining_calls is None:
            return
        if int(count) < 1 or self.remaining_calls < int(count):
            raise PermissionError("tool request rejected: retrieval quota unavailable")

    def call(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        self.authorize()
        return callback(*args, **kwargs)


__all__ = ["ToolGateway"]
