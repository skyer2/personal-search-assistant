"""Execution scope for worker tools and their independent retrieval budgets."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from app.agent.harness.step_budget import WorkerRetrievalBudget, worker_retrieval_budget


class ToolGateway:
    def __init__(
        self,
        *,
        search_queries_remaining: int | None,
        fetch_sources_remaining: int | None,
        tool_invocations_remaining: int | None,
    ):
        self.search_queries_remaining = search_queries_remaining
        self.fetch_sources_remaining = fetch_sources_remaining
        self.tool_invocations_remaining = tool_invocations_remaining

    @contextmanager
    def execution_scope(
        self,
        *,
        worker_task_id: str = "",
        step_index: int = -1,
        run_id: str = "",
        session_id: str = "",
        worker_lease_id: str = "",
    ) -> Iterator[WorkerRetrievalBudget]:
        from app.agent.harness.usage_tracker import bind_worker_execution_scope

        with worker_retrieval_budget(
            search_queries=self.search_queries_remaining,
            fetch_sources=self.fetch_sources_remaining,
            tool_invocations=self.tool_invocations_remaining,
        ) as budget:
            if not worker_task_id:
                yield budget
                return
            with bind_worker_execution_scope(
                worker_task_id,
                step_index=step_index,
                run_id=run_id,
                session_id=session_id,
                worker_lease_id=worker_lease_id,
            ):
                yield budget

    def call(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        """Invoke a tool that performs its own resource authorization."""
        return callback(*args, **kwargs)


__all__ = ["ToolGateway"]
