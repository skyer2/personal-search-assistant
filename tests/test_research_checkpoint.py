"""ResearchState SQLite checkpointer round-trip tests."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_sqlite_checkpointer_roundtrip_plan_and_progress(tmp_path: Path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    from app.research.domain.task_state import task_execution_projection
    from app.research.runtime.checkpointer import reset_checkpointer_cache
    from app.research.runtime.graph import compile_research_graph, initial_graph_state

    reset_checkpointer_cache()
    path = tmp_path / "graph.sqlite"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    graph = compile_research_graph(checkpointer=saver)
    state = initial_graph_state(
        run_id="ckpt-1",
        session_id="ckpt-1",
        task_query="比较 Tesla 和 Figure 的差异并生成 Markdown 报告",
    )
    config = {"configurable": {"thread_id": "ckpt-1"}, "recursion_limit": 50}
    try:
        result = graph.invoke(state, config=config)
        assert result.get("plan")
        assert result.get("progress_assessment") is not None
        snapshot = graph.get_state(config)
        task_status = task_execution_projection(snapshot.values.get("tasks"))
        assert task_status
    finally:
        conn.close()
        reset_checkpointer_cache()

    conn2 = sqlite3.connect(str(path), check_same_thread=False)
    saver2 = SqliteSaver(conn2)
    saver2.setup()
    graph2 = compile_research_graph(checkpointer=saver2)
    try:
        restored = graph2.get_state(config)
        assert restored.values.get("plan")
        assert task_execution_projection(restored.values.get("tasks")) == task_status
        assessment = restored.values.get("progress_assessment") or {}
        assert assessment.get("status") in {"sufficient", "gap", "unknown"}
    finally:
        conn2.close()
        reset_checkpointer_cache()


def test_default_checkpointer_sqlite_helper(tmp_path: Path):
    from app.research.runtime.checkpointer import (
        default_research_checkpointer,
        reset_checkpointer_cache,
    )

    reset_checkpointer_cache()
    mem = default_research_checkpointer(backend="memory")
    assert mem is not None
    path = str(tmp_path / "g.sqlite")
    try:
        saver = default_research_checkpointer(backend="sqlite", path=path)
        assert saver is not None
        assert Path(path).exists()
    finally:
        reset_checkpointer_cache()


def test_async_sqlite_checkpointer_ainvoke(tmp_path: Path):
    from app.research.domain.task_state import task_execution_projection
    from app.research.runtime.checkpointer import (
        async_sqlite_checkpointer,
        reset_async_checkpointer_cache,
        reset_checkpointer_cache,
    )
    from app.research.runtime.graph import compile_research_graph, initial_graph_state

    async def _run() -> None:
        reset_checkpointer_cache()
        await reset_async_checkpointer_cache()
        path = str(tmp_path / "async-graph.sqlite")
        saver = await async_sqlite_checkpointer(path)
        graph = compile_research_graph(checkpointer=saver)
        state = initial_graph_state(
            run_id="ackpt-1",
            session_id="ackpt-1",
            task_query="比较 Tesla 和 Figure 的差异并生成 Markdown 报告",
        )
        config = {"configurable": {"thread_id": "ackpt-1"}, "recursion_limit": 50}
        try:
            result = await graph.ainvoke(state, config=config)
            assert result.get("plan")
            snapshot = await graph.aget_state(config)
            assert task_execution_projection(snapshot.values.get("tasks"))
        finally:
            await reset_async_checkpointer_cache()
            reset_checkpointer_cache()

    asyncio.run(_run())
