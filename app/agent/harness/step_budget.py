"""Per-worker search, fetch, and logical tool invocation budgets.

The three resources are intentionally independent: ``batch_search(4)`` consumes
four search queries but only one tool invocation, and it must not reduce the
worker's fetch-source budget.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


class BudgetBlock(str):
    """A denial message carrying machine-readable budget metadata."""

    resource: str
    reason: str
    used: int
    limit: int

    def __new__(
        cls,
        message: str,
        *,
        resource: str,
        reason: str,
        used: int,
        limit: int,
    ) -> "BudgetBlock":
        value = super().__new__(cls, message)
        value.resource = resource
        value.reason = reason
        value.used = int(used or 0)
        value.limit = int(limit or 0)
        return value


@dataclass
class WorkerRetrievalBudget:
    search_queries_limit: int | None
    fetch_sources_limit: int | None
    tool_invocations_limit: int | None
    search_queries_used: int = 0
    fetch_sources_used: int = 0
    tool_invocations_used: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "search_queries_used": self.search_queries_used,
            "search_queries_limit": int(self.search_queries_limit or 0),
            "fetch_sources_used": self.fetch_sources_used,
            "fetch_sources_limit": int(self.fetch_sources_limit or 0),
            "tool_invocations_used": self.tool_invocations_used,
            "tool_invocations_limit": int(self.tool_invocations_limit or 0),
        }


_worker_budget: ContextVar[WorkerRetrievalBudget | None] = ContextVar(
    "harness_worker_retrieval_budget",
    default=None,
)

STOP_JSON_MESSAGE = (
    "本步搜索或抓取额度已达上限，或本轮禁止再联网。"
    "不要调用 internet_search / fetch_url / batch_search / batch_fetch。"
    "请立刻只输出结构化 JSON（ok、summary、facts、sources）。"
    "已抓取原文请用 read_artifact / read_evidence 回读。"
)


@contextmanager
def worker_retrieval_budget(
    *,
    search_queries: int | None,
    fetch_sources: int | None,
    tool_invocations: int | None,
) -> Iterator[WorkerRetrievalBudget]:
    budget = WorkerRetrievalBudget(
        search_queries_limit=None if search_queries is None else max(0, int(search_queries)),
        fetch_sources_limit=None if fetch_sources is None else max(0, int(fetch_sources)),
        tool_invocations_limit=None if tool_invocations is None else max(0, int(tool_invocations)),
    )
    token = _worker_budget.set(budget)
    try:
        yield budget
    finally:
        _worker_budget.reset(token)


def current_worker_retrieval_budget() -> WorkerRetrievalBudget | None:
    return _worker_budget.get()


def remaining_search_queries() -> int | None:
    budget = _worker_budget.get()
    if budget is None or budget.search_queries_limit is None:
        return None
    return max(0, budget.search_queries_limit - budget.search_queries_used)


def remaining_fetch_sources() -> int | None:
    budget = _worker_budget.get()
    if budget is None or budget.fetch_sources_limit is None:
        return None
    return max(0, budget.fetch_sources_limit - budget.fetch_sources_used)


def remaining_tool_invocations() -> int | None:
    budget = _worker_budget.get()
    if budget is None or budget.tool_invocations_limit is None:
        return None
    return max(0, budget.tool_invocations_limit - budget.tool_invocations_used)


def _deny(*, resource: str, reason: str, used: int, limit: int) -> str:
    from app.agent.harness.budget_events import emit_budget_denied
    from app.agent.harness.usage_tracker import (
        get_current_budget_manager,
        get_current_worker_task_id,
    )

    emit_budget_denied(
        scope="worker",
        resource=resource,
        reason=reason,
        task_id=get_current_worker_task_id(),
        used=used,
        limit=limit,
        budget_manager=get_current_budget_manager(),
    )
    return BudgetBlock(
        STOP_JSON_MESSAGE,
        resource=resource,
        reason=reason,
        used=used,
        limit=limit,
    )


def _reserve_run_tool_invocation() -> str | None:
    from app.agent.harness.usage_tracker import get_current_budget_manager

    manager = get_current_budget_manager()
    if manager is None:
        return None
    allowed, reason = manager.reserve_tool_calls(1)
    if allowed:
        return None
    return reason or "tool_call_cap"


def consume_search_queries_or_block(n: int, *, tool_name: str = "internet_search") -> str | None:
    """Consume search items and one logical tool invocation, or deny atomically."""
    _ = tool_name
    count = max(1, int(n or 1))
    budget = _worker_budget.get()
    if budget is None:
        return None
    if (
        budget.search_queries_limit is not None
        and budget.search_queries_used + count > budget.search_queries_limit
    ):
        return _deny(
            resource="search_query",
            reason="search_query_cap",
            used=budget.search_queries_used,
            limit=budget.search_queries_limit,
        )
    if (
        budget.tool_invocations_limit is not None
        and budget.tool_invocations_used + 1 > budget.tool_invocations_limit
    ):
        return _deny(
            resource="tool_call",
            reason="tool_call_cap",
            used=budget.tool_invocations_used,
            limit=budget.tool_invocations_limit,
        )

    run_reason = _reserve_run_tool_invocation()
    if run_reason:
        return _deny(
            resource="tool_call",
            reason=run_reason,
            used=budget.tool_invocations_used,
            limit=budget.tool_invocations_limit or 0,
        )
    budget.search_queries_used += count
    budget.tool_invocations_used += 1
    return None


def consume_fetch_sources_or_block(n: int, *, tool_name: str = "fetch_url") -> str | None:
    """Consume fetch items and one logical tool invocation, or deny atomically."""
    _ = tool_name
    count = max(1, int(n or 1))
    budget = _worker_budget.get()
    if budget is None:
        return None
    if (
        budget.fetch_sources_limit is not None
        and budget.fetch_sources_used + count > budget.fetch_sources_limit
    ):
        return _deny(
            resource="fetch_source",
            reason="fetch_source_cap",
            used=budget.fetch_sources_used,
            limit=budget.fetch_sources_limit,
        )
    if (
        budget.tool_invocations_limit is not None
        and budget.tool_invocations_used + 1 > budget.tool_invocations_limit
    ):
        return _deny(
            resource="tool_call",
            reason="tool_call_cap",
            used=budget.tool_invocations_used,
            limit=budget.tool_invocations_limit,
        )

    run_reason = _reserve_run_tool_invocation()
    if run_reason:
        return _deny(
            resource="tool_call",
            reason=run_reason,
            used=budget.tool_invocations_used,
            limit=budget.tool_invocations_limit or 0,
        )
    budget.fetch_sources_used += count
    budget.tool_invocations_used += 1
    return None


def consume_tool_invocations_or_block(n: int = 1) -> str | None:
    """Consume only logical invocations for tools without search/fetch semantics."""
    count = max(1, int(n or 1))
    budget = _worker_budget.get()
    if budget is None:
        return None
    if (
        budget.tool_invocations_limit is not None
        and budget.tool_invocations_used + count > budget.tool_invocations_limit
    ):
        return _deny(
            resource="tool_call",
            reason="tool_call_cap",
            used=budget.tool_invocations_used,
            limit=budget.tool_invocations_limit,
        )
    run_reason = _reserve_run_tool_invocation()
    if run_reason:
        return _deny(
            resource="tool_call",
            reason=run_reason,
            used=budget.tool_invocations_used,
            limit=budget.tool_invocations_limit or 0,
        )
    budget.tool_invocations_used += count
    return None


__all__ = [
    "WorkerRetrievalBudget",
    "BudgetBlock",
    "consume_fetch_sources_or_block",
    "consume_search_queries_or_block",
    "consume_tool_invocations_or_block",
    "current_worker_retrieval_budget",
    "remaining_fetch_sources",
    "remaining_search_queries",
    "remaining_tool_invocations",
    "worker_retrieval_budget",
]
