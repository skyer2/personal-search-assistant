"""步内检索预算：拦住 internet_search / fetch_url 连打，而不是等下一步才 abort。

None = 未进入 Harness 步（单测/直接调工具不拦截）。
0 = 本轮禁止再联网（structured_retry / json_only）。
正整数 = 本步剩余检索次数。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_retrieval_remaining: ContextVar[int | None] = ContextVar(
    "harness_retrieval_remaining",
    default=None,
)

RETRIEVAL_TOOLS = frozenset({"internet_search", "fetch_url"})

STOP_JSON_MESSAGE = (
    "本步检索次数已达上限，或本轮禁止再联网。"
    "不要调用 internet_search / fetch_url。"
    "请立刻只输出结构化 JSON（ok、summary、facts、sources）。"
    "已抓取原文请用 read_artifact / read_evidence 回读。"
)


@contextmanager
def retrieval_budget(remaining: int | None) -> Iterator[None]:
    token = _retrieval_remaining.set(remaining)
    try:
        yield
    finally:
        _retrieval_remaining.reset(token)


def remaining_retrieval_calls() -> int | None:
    return _retrieval_remaining.get()


def consume_retrieval_or_block(tool_name: str = "") -> str | None:
    """若应拦截本次联网，返回给模型的停止说明；否则扣一次额度。"""
    _ = tool_name
    remaining = _retrieval_remaining.get()
    if remaining is None:
        return None
    if remaining <= 0:
        return STOP_JSON_MESSAGE
    _retrieval_remaining.set(remaining - 1)
    return None
