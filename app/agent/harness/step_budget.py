"""Per-worker search, fetch, and logical tool invocation budgets.

The three resources are intentionally independent: ``batch_search(4)`` consumes
four search queries but only one tool invocation, and it must not reduce the
worker's fetch-source budget.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time
from typing import Iterator


class BudgetBlock(str):
    """A denial message carrying machine-readable budget metadata."""

    scope: str
    resource: str
    reason: str
    used: int
    reserved: int
    limit: int
    is_soft_finalization: bool

    def __new__(
        cls,
        message: str,
        *,
        resource: str,
        reason: str,
        used: int,
        reserved: int = 0,
        limit: int,
    ) -> "BudgetBlock":
        value = super().__new__(cls, message)
        value.scope = "worker"
        value.resource = resource
        value.reason = reason
        value.used = int(used or 0)
        value.reserved = int(reserved or 0)
        value.limit = int(limit or 0)
        value.is_soft_finalization = reason in {
            "soft_deadline_finalize", "soft_budget_finalize"
        }
        return value


@dataclass
class WorkerRetrievalBudget:
    search_queries_limit: int | None
    fetch_sources_limit: int | None
    tool_invocations_limit: int | None
    search_queries_used: int = 0
    fetch_sources_used: int = 0
    tool_invocations_used: int = 0
    soft_deadline_at: float | None = None
    finalization_reason: str = ""
    admitted_evidence_count: int = 0

    @property
    def retrieval_count(self) -> int:
        return self.search_queries_used + self.fetch_sources_used

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

FINALIZE_JSON_MESSAGE = (
    "本步已进入 Finalization Mode：预算或时间接近上限。"
    "不要再调用 internet_search / fetch_url / batch_search / batch_fetch。"
    "请立即基于已抓取内容输出结构化 JSON（ok、summary、facts、sources、findings、evidence_ids、stop_reason）。"
    "如需核对原文，只能使用 read_artifact / read_evidence。"
)


@contextmanager
def worker_retrieval_budget(
    *,
    search_queries: int | None,
    fetch_sources: int | None,
    tool_invocations: int | None,
    soft_deadline_at: float | None = None,
) -> Iterator[WorkerRetrievalBudget]:
    budget = WorkerRetrievalBudget(
        search_queries_limit=None if search_queries is None else max(0, int(search_queries)),
        fetch_sources_limit=None if fetch_sources is None else max(0, int(fetch_sources)),
        tool_invocations_limit=None if tool_invocations is None else max(0, int(tool_invocations)),
        soft_deadline_at=soft_deadline_at,
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


def _deny(*, resource: str, reason: str, used: int, limit: int) -> BudgetBlock:
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


def _soft_finalize(*, resource: str, reason: str, used: int, limit: int) -> BudgetBlock:
    from app.agent.harness.budget_events import emit_budget_decided
    from app.agent.harness.usage_tracker import (
        get_current_budget_manager,
        get_current_worker_lease_id,
        get_current_worker_task_id,
    )

    task_id = get_current_worker_task_id()
    budget = _worker_budget.get()
    if budget is not None:
        budget.finalization_reason = reason
    emit_budget_decided(
        scope="worker",
        resource=resource,
        reason=reason,
        task_id=task_id,
        worker_lease_id=get_current_worker_lease_id(),
        used=used,
        limit=limit,
        budget_manager=get_current_budget_manager(),
    )
    return BudgetBlock(
        FINALIZE_JSON_MESSAGE,
        resource=resource,
        reason=reason,
        used=used,
        limit=limit,
    )


def _soft_finalization_block(resource: str) -> BudgetBlock | None:
    budget = _worker_budget.get()
    if budget is None:
        return None
    if budget.finalization_reason:
        return BudgetBlock(
            FINALIZE_JSON_MESSAGE,
            resource=resource,
            reason=budget.finalization_reason,
            used=0,
            limit=0,
        )

    return trigger_worker_soft_finalization_block(resource)


def _existing_worker_evidence_count() -> int:
    """Count already stored artifacts/spans belonging to the active worker."""
    from app.agent.harness.usage_tracker import (
        get_current_worker_run_id,
        get_current_worker_task_id,
    )

    task_id = get_current_worker_task_id()
    run_id = get_current_worker_run_id()
    if not task_id or not run_id:
        return 0
    from app.agent.harness.artifacts import get_artifact_store
    from app.agent.harness.evidence_store import get_evidence_store

    artifacts = [
        item for item in get_artifact_store().iter_artifacts()
        if str(item.metadata.get("task_id") or "") == task_id
        and str(item.metadata.get("run_id") or "") == run_id
    ]
    artifact_ids = {item.artifact_id for item in artifacts}
    spans = [
        span for span in get_evidence_store().spans.values()
        if span.artifact_id in artifact_ids
        or (
            str(span.metadata.get("task_id") or "") == task_id
            and str(span.metadata.get("run_id") or "") == run_id
        )
    ]
    return len(artifacts) + len(spans)


def trigger_worker_soft_finalization_block(
    resource: str = "token",
    *,
    projected_tokens: int = 0,
    projected_llm_calls: int = 0,
) -> BudgetBlock | None:
    """Evaluate and arm finalization before another retrieval or LLM round."""
    budget = _worker_budget.get()
    if budget is None or budget.finalization_reason:
        return None

    # Before the first admitted search/fetch, a soft stop cannot preempt
    # retrieval. Hard worker/run caps and cancellation still apply at the
    # resource reservation boundary.
    if budget.retrieval_count == 0 and budget.admitted_evidence_count == 0:
        budget.admitted_evidence_count = _existing_worker_evidence_count()
    if budget.retrieval_count == 0 and budget.admitted_evidence_count == 0:
        return None

    if budget.soft_deadline_at is not None and time.monotonic() >= budget.soft_deadline_at:
        return _soft_finalize(
            resource=resource,
            reason="soft_deadline_finalize",
            used=0,
            limit=0,
        )

    from app.agent.harness.usage_tracker import (
        get_current_budget_manager,
        get_current_worker_task_id,
    )

    manager = get_current_budget_manager()
    task_id = get_current_worker_task_id()
    if manager is None or not task_id:
        return None
    snapshot_method = getattr(manager, "worker_lease_snapshot", None)
    if not callable(snapshot_method):
        return None
    snapshot = dict(snapshot_method(task_id) or {})
    tokens_used = int(snapshot.get("tokens_used") or 0) + int(
        snapshot.get("in_flight_tokens") or 0
    ) + max(0, int(projected_tokens or 0))
    token_limit = int(snapshot.get("token_limit") or 0)
    if token_limit > 0:
        token_threshold = max(1, int(token_limit * 0.8))
        if projected_tokens > 0:
            projected_threshold = token_limit - 2 * int(projected_tokens)
            token_threshold = min(
                token_threshold,
                max(1, int(token_limit * 0.4), projected_threshold),
            )
        if tokens_used >= token_threshold:
            return _soft_finalize(
                resource="token",
                reason="soft_budget_finalize",
                used=tokens_used,
                limit=token_limit,
            )

    llm_calls_used = int(snapshot.get("llm_calls_used") or 0) + max(
        0,
        int(projected_llm_calls or 0),
    )
    llm_calls_limit = int(snapshot.get("llm_calls_limit") or 0)
    if llm_calls_limit > 0:
        threshold = max(1, int((llm_calls_limit * 4 + 4) / 5), llm_calls_limit - 2)
        if projected_llm_calls > 0:
            projected_calls = int(projected_llm_calls)
            threshold = min(
                threshold,
                max(
                    1,
                    int(llm_calls_limit * 0.5),
                    llm_calls_limit - 2 * projected_calls,
                ),
            )
        if llm_calls_used >= threshold:
            return _soft_finalize(
                resource="llm_call",
                reason="soft_budget_finalize",
                used=llm_calls_used,
                limit=llm_calls_limit,
            )
    return None


def arm_worker_soft_finalization(
    *,
    projected_tokens: int = 0,
    projected_llm_calls: int = 0,
) -> str:
    """Arm finalization before or after an LLM usage boundary."""
    budget = _worker_budget.get()
    if budget is not None and budget.finalization_reason:
        return budget.finalization_reason

    block = trigger_worker_soft_finalization_block(
        "token",
        projected_tokens=projected_tokens,
        projected_llm_calls=projected_llm_calls,
    )
    return str(getattr(block, "reason", "") or "")


def _reserve_run_tool_invocation() -> str | None:
    from app.agent.harness.usage_tracker import get_current_budget_manager

    manager = get_current_budget_manager()
    if manager is None:
        return None
    allowed, reason = manager.reserve_tool_calls(1)
    if allowed:
        return None
    return reason or "tool_call_cap"


def consume_search_queries_or_block(n: int, *, tool_name: str = "internet_search") -> BudgetBlock | None:
    """Consume search items and one logical tool invocation, or deny atomically."""
    _ = tool_name
    count = max(1, int(n or 1))
    budget = _worker_budget.get()
    if budget is None:
        return None
    soft_block = _soft_finalization_block("search_query")
    if soft_block is not None:
        return soft_block
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


def consume_fetch_sources_or_block(n: int, *, tool_name: str = "fetch_url") -> BudgetBlock | None:
    """Consume fetch items and one logical tool invocation, or deny atomically."""
    _ = tool_name
    count = max(1, int(n or 1))
    budget = _worker_budget.get()
    if budget is None:
        return None
    soft_block = _soft_finalization_block("fetch_source")
    if soft_block is not None:
        return soft_block
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


def consume_tool_invocations_or_block(n: int = 1) -> BudgetBlock | None:
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
    "arm_worker_soft_finalization",
    "WorkerRetrievalBudget",
    "BudgetBlock",
    "FINALIZE_JSON_MESSAGE",
    "consume_fetch_sources_or_block",
    "consume_search_queries_or_block",
    "consume_tool_invocations_or_block",
    "current_worker_retrieval_budget",
    "remaining_fetch_sources",
    "remaining_search_queries",
    "remaining_tool_invocations",
    "worker_retrieval_budget",
]
