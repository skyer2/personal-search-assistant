"""Single authorization boundary for worker tool execution."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from app.agent.harness.step_budget import retrieval_budget


class ToolGateway:
    def __init__(self, remaining_calls: int | None):
        self.remaining_calls = remaining_calls

    @contextmanager
    def execution_scope(self) -> Iterator[None]:
        with retrieval_budget(self.remaining_calls):
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
