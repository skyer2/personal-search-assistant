"""Durable ResearchState checkpointer：默认异步 SQLite 文件，失败回退内存。

生产路径是 graph.ainvoke()，必须用 AsyncSqliteSaver。
同步 SqliteSaver 只留给 graph.invoke() 单测。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SQLITE_SAVER: Any = None
_SQLITE_CONN: sqlite3.Connection | None = None
_SQLITE_PATH: str | None = None

_ASYNC_SAVER: Any = None
_ASYNC_CONN: Any = None
_ASYNC_PATH: str | None = None
_ASYNC_LOOP_ID: int | None = None

DEFAULT_CHECKPOINT_PATH = "output/.harness/graph_checkpoints.sqlite"


def memory_checkpointer():
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


def default_research_checkpointer(
    *,
    backend: str | None = None,
    path: str | Path | None = None,
):
    """同步 helper：memory 或 SqliteSaver（仅 sync invoke 测试）。

    生产 ainvoke 请用 aget_research_checkpointer。
    """
    chosen = (
        backend
        or os.getenv("HARNESS_GRAPH_CHECKPOINT_BACKEND")
        or "sqlite"
    ).strip().lower()
    if chosen in {"memory", "inmemory", "mem", "none"}:
        return memory_checkpointer()
    try:
        return sqlite_checkpointer(path)
    except Exception as exc:
        logger.warning("sqlite checkpointer unavailable (%s); falling back to InMemorySaver", exc)
        return memory_checkpointer()


async def aget_research_checkpointer(
    *,
    backend: str | None = None,
    path: str | Path | None = None,
):
    """生产默认：AsyncSqliteSaver；失败回退 InMemorySaver。"""
    chosen = (
        backend
        or os.getenv("HARNESS_GRAPH_CHECKPOINT_BACKEND")
        or "sqlite"
    ).strip().lower()
    if chosen in {"memory", "inmemory", "mem", "none"}:
        return memory_checkpointer()
    try:
        return await async_sqlite_checkpointer(path)
    except Exception as exc:
        logger.warning(
            "async sqlite checkpointer unavailable (%s); falling back to InMemorySaver",
            exc,
        )
        return memory_checkpointer()


def sqlite_checkpointer(path: str | Path | None = None):
    """同步 SqliteSaver：只给 graph.invoke() 用。"""
    global _SQLITE_SAVER, _SQLITE_CONN, _SQLITE_PATH
    from langgraph.checkpoint.sqlite import SqliteSaver

    resolved = str(
        path
        or os.getenv("HARNESS_GRAPH_CHECKPOINT")
        or DEFAULT_CHECKPOINT_PATH
    )
    if _SQLITE_SAVER is not None and _SQLITE_PATH == resolved and _SQLITE_CONN is not None:
        return _SQLITE_SAVER
    target = Path(resolved)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    if hasattr(saver, "setup"):
        saver.setup()
    _SQLITE_CONN = conn
    _SQLITE_SAVER = saver
    _SQLITE_PATH = resolved
    return saver


async def async_sqlite_checkpointer(path: str | Path | None = None):
    """给 graph.ainvoke() / aget_state() 用的 AsyncSqliteSaver。"""
    global _ASYNC_SAVER, _ASYNC_CONN, _ASYNC_PATH, _ASYNC_LOOP_ID
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    resolved = str(
        path
        or os.getenv("HARNESS_GRAPH_CHECKPOINT")
        or DEFAULT_CHECKPOINT_PATH
    )
    loop_id = id(asyncio.get_running_loop())
    if (
        _ASYNC_SAVER is not None
        and _ASYNC_PATH == resolved
        and _ASYNC_CONN is not None
        and _ASYNC_LOOP_ID == loop_id
    ):
        return _ASYNC_SAVER

    if _ASYNC_CONN is not None:
        try:
            await _ASYNC_CONN.close()
        except Exception:
            pass
        _ASYNC_SAVER = None
        _ASYNC_CONN = None

    target = Path(resolved)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(target))
    await conn.execute("PRAGMA journal_mode=WAL")
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    _ASYNC_CONN = conn
    _ASYNC_SAVER = saver
    _ASYNC_PATH = resolved
    _ASYNC_LOOP_ID = loop_id
    return saver


def reset_checkpointer_cache() -> None:
    global _SQLITE_SAVER, _SQLITE_CONN, _SQLITE_PATH
    global _ASYNC_SAVER, _ASYNC_CONN, _ASYNC_PATH, _ASYNC_LOOP_ID
    if _SQLITE_CONN is not None:
        try:
            _SQLITE_CONN.close()
        except Exception:
            pass
    _SQLITE_SAVER = None
    _SQLITE_CONN = None
    _SQLITE_PATH = None
    _ASYNC_SAVER = None
    _ASYNC_CONN = None
    _ASYNC_PATH = None
    _ASYNC_LOOP_ID = None


async def reset_async_checkpointer_cache() -> None:
    global _ASYNC_SAVER, _ASYNC_CONN, _ASYNC_PATH, _ASYNC_LOOP_ID
    if _ASYNC_CONN is not None:
        try:
            await _ASYNC_CONN.close()
        except Exception:
            pass
    _ASYNC_SAVER = None
    _ASYNC_CONN = None
    _ASYNC_PATH = None
    _ASYNC_LOOP_ID = None
