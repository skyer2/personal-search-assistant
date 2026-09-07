"""History deletion must cascade beyond the UI run row."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import memory_routes, session_routes
from app.agent.memory.backend.sqlite_backend import SqliteMemoryBackend
from app.agent.memory.identity import MemoryIdentity
from app.agent.memory.models import MemoryType, MemoryWriteRequest, WriteSource
from app.agent.memory.policy import MemoryPolicy
from app.agent.memory.provenance import Provenance
from app.agent.memory.store import MemoryStore
from app.observability.events import AgentEvent, EventType, utc_now
from app.observability.projection_store import ProjectionStore
from app.run_store.deletion import delete_run_cascade, delete_session_cascade
from app.run_store.service import RunStore


def _memory_policy() -> MemoryPolicy:
    return MemoryPolicy(
        provider="sqlite",
        embedding_enabled=False,
        source_ledger_enabled=True,
        consolidation_enabled=False,
        utility_gate_enabled=False,
        min_fact_chars=1,
        project_scope_enabled=True,
    )


def _memory_store(tmp_path: Path) -> MemoryStore:
    backend = SqliteMemoryBackend(tmp_path / "memory.sqlite", _memory_policy())
    return MemoryStore(backend=backend, policy=_memory_policy())


def _write_checkpoint(path: Path, run_id: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )
            """
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, '', 'cp1', NULL, NULL, X'00', NULL)",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO writes VALUES (?, '', 'cp1', 'task', 0, 'state', NULL, NULL)",
            (run_id,),
        )


def _write_trace(root: Path, session_id: str, run_id: str) -> None:
    event = AgentEvent(
        event_id=f"{run_id}-event",
        trace_id=f"{run_id}-trace",
        span_id=f"{run_id}-span",
        run_id=run_id,
        session_id=session_id,
        seq=1,
        timestamp=utc_now(),
        type=EventType.RUN_STARTED,
    )
    projection = ProjectionStore(root / "trace-projections.sqlite3")
    projection.append(event)
    run_dir = root / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{run_id}.jsonl").write_text(
        json.dumps(event.to_jsonl_record()) + "\n",
        encoding="utf-8",
    )
    payload_dir = root / "payloads" / run_id
    payload_dir.mkdir(parents=True)
    (payload_dir / "brief.json").write_text("{}", encoding="utf-8")


async def test_delete_run_cascades_trace_checkpoint_and_memory(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "run_store.sqlite")
    store.ensure_session_owned("s1", tenant_id="local", user_id="me", project_id="Inbox")
    store.create_run(
        run_id="r1",
        session_id="s1",
        query="DeepSeek 创始人",
        tenant_id="local",
        user_id="me",
        project_id="Inbox",
    )
    output = tmp_path / "output"
    run_dir = output / "session_s1" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "run_summary.json").write_text("{}", encoding="utf-8")
    traces = tmp_path / "traces"
    checkpoint = tmp_path / "checkpoints.sqlite"
    _write_trace(traces, "s1", "r1")
    _write_checkpoint(checkpoint, "r1")

    memory = _memory_store(tmp_path)
    identity = MemoryIdentity(
        tenant_id="local",
        user_id="me",
        project_id="Inbox",
        session_id="s1",
    )
    await memory.remember_writes(
        [
            MemoryWriteRequest(
                fact="DeepSeek 创始人是梁文锋",
                memory_type=MemoryType.SEMANTIC,
                write_source=WriteSource.STEP_INCREMENTAL,
                project_id="Inbox",
                session_id="s1",
                provenance=Provenance(run_id="r1", step_type="network_search"),
            )
        ],
        user_id="me",
        identity=identity,
    )
    await memory.record_sources(
        ["https://example.com/deepseek"],
        identity=identity,
        session_id="s1",
        run_id="r1",
    )

    result = await delete_run_cascade(
        store,
        "r1",
        tenant_id="local",
        output_root=output,
        traces_root=traces,
        checkpoint_path=checkpoint,
        memory_store=memory,
    )

    assert result["deleted"] is True
    assert result["memory_deleted"] == 2
    assert store.get_run("r1") is None
    assert not run_dir.exists()
    assert not (traces / "s1" / "r1.jsonl").exists()
    assert not (traces / "payloads" / "r1").exists()
    with sqlite3.connect(checkpoint) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
    records = memory.list_records("me", tenant_id="local", project_id="Inbox")
    assert records == []
    assert memory.list_sources(identity=identity) == []


async def test_delete_session_cascades_all_runs_and_memory(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "run_store.sqlite")
    store.ensure_session_owned("s1", tenant_id="local", user_id="me", project_id="Inbox")
    for run_id in ("r1", "r2"):
        store.create_run(
            run_id=run_id,
            session_id="s1",
            query=f"query {run_id}",
            tenant_id="local",
            user_id="me",
            project_id="Inbox",
        )
    traces = tmp_path / "traces"
    checkpoint = tmp_path / "checkpoints.sqlite"
    for run_id in ("r1", "r2"):
        _write_trace(traces, "s1", run_id)
        _write_checkpoint(checkpoint, run_id)
    memory = _memory_store(tmp_path)
    identity = MemoryIdentity(
        tenant_id="local",
        user_id="me",
        project_id="Inbox",
        session_id="s1",
    )
    await memory.remember_writes(
        [
            MemoryWriteRequest(
                fact="用户偏好 PDF 交付",
                memory_type=MemoryType.PREFERENCE,
                write_source=WriteSource.USER_EXPLICIT,
                project_id="Inbox",
                session_id="s1",
            )
        ],
        user_id="me",
        identity=identity,
    )

    result = await delete_session_cascade(
        store,
        "s1",
        tenant_id="local",
        output_root=tmp_path / "output",
        updated_root=tmp_path / "updated",
        traces_root=traces,
        checkpoint_path=checkpoint,
        memory_store=memory,
    )

    assert result["deleted"] is True
    assert result["runs_deleted"] == 2
    assert result["memory_deleted"] == 1
    assert store.get_session("s1") is None
    assert not (traces / "s1").exists()
    with sqlite3.connect(checkpoint) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    assert memory.list_records("me", tenant_id="local", project_id="Inbox") == []


async def test_delete_run_api_end_to_end(tmp_path: Path, monkeypatch) -> None:
    store = RunStore(tmp_path / "run_store.sqlite")
    store.ensure_session_owned("s1", tenant_id="local", user_id="me", project_id="Inbox")
    store.create_run(
        run_id="r1",
        session_id="s1",
        query="DeepSeek 创始人",
        tenant_id="local",
        user_id="me",
        project_id="Inbox",
    )
    traces = tmp_path / "traces"
    checkpoint = tmp_path / "checkpoints.sqlite"
    _write_trace(traces, "s1", "r1")
    _write_checkpoint(checkpoint, "r1")
    memory = _memory_store(tmp_path)
    identity = MemoryIdentity(
        tenant_id="local",
        user_id="me",
        project_id="Inbox",
        session_id="s1",
    )
    await memory.remember_writes(
        [
            MemoryWriteRequest(
                fact="DeepSeek 创始人是梁文锋",
                memory_type=MemoryType.SEMANTIC,
                write_source=WriteSource.STEP_INCREMENTAL,
                project_id="Inbox",
                session_id="s1",
                provenance=Provenance(run_id="r1"),
            )
        ],
        user_id="me",
        identity=identity,
    )

    monkeypatch.setattr(session_routes, "get_run_store", lambda: store)
    monkeypatch.setattr(session_routes, "get_memory_store", lambda: memory)
    monkeypatch.setattr(memory_routes, "get_memory_store", lambda: memory)
    monkeypatch.setattr(session_routes, "_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(session_routes, "_UPDATED_DIR", tmp_path / "updated")
    monkeypatch.setattr(session_routes, "_TRACES_DIR", traces)
    monkeypatch.setenv("HARNESS_GRAPH_CHECKPOINT", str(checkpoint))
    app = FastAPI()
    app.include_router(session_routes.router)
    app.include_router(memory_routes.router)
    client = TestClient(app)

    before = client.get("/api/memory/records?tenant_id=local&user_id=me&project_id=Inbox")
    assert before.status_code == 200
    assert before.json()["total"] == 1

    response = client.delete("/api/runs/r1?tenant_id=local")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert response.json()["checkpoint"]["checkpoints_deleted"] == 1

    after = client.get("/api/memory/records?tenant_id=local&user_id=me&project_id=Inbox")
    assert after.status_code == 200
    assert after.json()["total"] == 0
    assert not (traces / "s1" / "r1.jsonl").exists()
    assert store.get_run("r1") is None
