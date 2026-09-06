"""Single authorization boundary for worker tool execution."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from app.agent.harness.step_budget import retrieval_budget


class ToolGateway:
    def __init__(self, remaining_calls: int | None):
        self.remaining_calls = remaining_calls

    @contextmanager
    def execution_scope(self) -> Iterator[None]:
        with retrieval_budget(self.remaining_calls):
            yield


__all__ = ["ToolGateway"]
