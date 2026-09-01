"""ResearchState SQLite checkpointer：控制流字段可跨 saver 实例恢复。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_sqlite_checkpointer_roundtrip_plan_and_progress():
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.checkpoint.memory import InMemorySaver  # noqa: F401
    except ModuleNotFoundError:
        print("[SKIP] sqlite checkpointer (langgraph not installed)")
        return

    import sqlite3

    from app.research.runtime.checkpointer import reset_checkpointer_cache, sqlite_checkpointer
    from app.research.runtime.graph import compile_research_graph, initial_graph_state

    reset_checkpointer_cache()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "graph.sqlite"
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
        result = graph.invoke(state, config=config)
        assert result.get("plan")
        assert result.get("progress_assessment") is not None
        snapshot = graph.get_state(config)
        assert snapshot.values.get("task_status")
        conn.close()

        conn2 = sqlite3.connect(str(path), check_same_thread=False)
        saver2 = SqliteSaver(conn2)
        graph2 = compile_research_graph(checkpointer=saver2)
        restored = graph2.get_state(config)
        assert restored.values.get("plan")
        assert restored.values.get("task_status") == snapshot.values.get("task_status")
        assessment = restored.values.get("progress_assessment") or {}
        assert assessment.get("verdict") in {"enough", "gap", "abort", "run"}
        conn2.close()
    reset_checkpointer_cache()
    print("[OK] sqlite checkpoint roundtrip")


def test_default_checkpointer_sqlite_helper():
    import tempfile
    from app.research.runtime.checkpointer import (
        default_research_checkpointer,
        reset_checkpointer_cache,
    )

    reset_checkpointer_cache()
    mem = default_research_checkpointer(backend="memory")
    assert mem is not None
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "g.sqlite")
        reset_checkpointer_cache()
        saver = default_research_checkpointer(backend="sqlite", path=path)
        assert saver is not None
        assert Path(path).exists()
    reset_checkpointer_cache()
    print("[OK] checkpointer helpers")


def test_async_sqlite_checkpointer_ainvoke():
    import asyncio
    import tempfile

    from app.research.runtime.checkpointer import (
        async_sqlite_checkpointer,
        reset_async_checkpointer_cache,
        reset_checkpointer_cache,
    )
    from app.research.runtime.graph import compile_research_graph, initial_graph_state

    async def _run() -> None:
        reset_checkpointer_cache()
        await reset_async_checkpointer_cache()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "async-graph.sqlite")
            saver = await async_sqlite_checkpointer(path)
            graph = compile_research_graph(checkpointer=saver)
            state = initial_graph_state(
                run_id="ackpt-1",
                session_id="ackpt-1",
                task_query="比较 Tesla 和 Figure 的差异并生成 Markdown 报告",
            )
            config = {"configurable": {"thread_id": "ackpt-1"}, "recursion_limit": 50}
            result = await graph.ainvoke(state, config=config)
            assert result.get("plan")
            snapshot = await graph.aget_state(config)
            assert snapshot.values.get("task_status")
        await reset_async_checkpointer_cache()
        reset_checkpointer_cache()

    asyncio.run(_run())
    print("[OK] async sqlite ainvoke")


if __name__ == "__main__":
    test_default_checkpointer_sqlite_helper()
    test_sqlite_checkpointer_roundtrip_plan_and_progress()
    test_async_sqlite_checkpointer_ainvoke()
    print("\n=== Checkpoint tests passed ===")
