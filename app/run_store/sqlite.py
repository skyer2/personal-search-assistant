"""SQLite persistence for the RunStore projection."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.run_store.models import RunRecord, SessionRecord, UploadedFileRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    current_phase TEXT,
    plan_version INTEGER,
    final_result TEXT,
    error TEXT,
    session_workspace TEXT,
    last_event_seq INTEGER DEFAULT 0,
    hitl_status TEXT,
    hitl_payload TEXT,
    paused_total_ms INTEGER DEFAULT 0,
    pause_started_at TEXT,
    tool_calls INTEGER DEFAULT 0,
    assistant_calls INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    server_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_uploads_session ON uploads(session_id, uploaded_at);
"""

_MIGRATIONS = {
    "sessions": {
        "tenant_id": "ALTER TABLE sessions ADD COLUMN tenant_id TEXT DEFAULT 'local'",
        "user_id": "ALTER TABLE sessions ADD COLUMN user_id TEXT DEFAULT 'me'",
        "project_id": "ALTER TABLE sessions ADD COLUMN project_id TEXT DEFAULT 'Inbox'",
        "archived": "ALTER TABLE sessions ADD COLUMN archived INTEGER DEFAULT 0",
    },
    "runs": {
        "tenant_id": "ALTER TABLE runs ADD COLUMN tenant_id TEXT DEFAULT 'local'",
        "user_id": "ALTER TABLE runs ADD COLUMN user_id TEXT DEFAULT 'me'",
        "project_id": "ALTER TABLE runs ADD COLUMN project_id TEXT DEFAULT 'Inbox'",
    },
}


class SqliteRunStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """向前兼容：旧库缺列时补齐（tenant / archive 所有权字段）。"""
        for table, columns in _MIGRATIONS.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            for column, ddl in columns.items():
                if column not in existing:
                    self._conn.execute(ddl)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @staticmethod
    def session_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=str(row["tenant_id"] or "local"),
            user_id=str(row["user_id"] or "me"),
            project_id=str(row["project_id"] or "Inbox"),
            archived=bool(row["archived"]),
        )

    @staticmethod
    def run_from_row(row: sqlite3.Row) -> RunRecord:
        payload = row["hitl_payload"]
        hitl = None
        if payload:
            try:
                hitl = json.loads(payload)
            except json.JSONDecodeError:
                hitl = None
        return RunRecord(
            run_id=row["run_id"],
            session_id=row["session_id"],
            query=row["query"],
            status=row["status"],
            created_at=row["created_at"],
            mode=row["mode"] or "agent",
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            current_phase=row["current_phase"],
            plan_version=row["plan_version"],
            final_result=row["final_result"] or "",
            error=row["error"] or "",
            session_workspace=row["session_workspace"] or "",
            last_event_seq=int(row["last_event_seq"] or 0),
            hitl_status=row["hitl_status"],
            hitl_payload=hitl,
            paused_total_ms=int(row["paused_total_ms"] or 0),
            pause_started_at=row["pause_started_at"],
            tool_calls=int(row["tool_calls"] or 0),
            assistant_calls=int(row["assistant_calls"] or 0),
            errors=int(row["errors"] or 0),
            tenant_id=str(row["tenant_id"] or "local"),
            user_id=str(row["user_id"] or "me"),
            project_id=str(row["project_id"] or "Inbox"),
        )

    @staticmethod
    def upload_from_row(row: sqlite3.Row) -> UploadedFileRecord:
        return UploadedFileRecord(
            id=row["id"],
            session_id=row["session_id"],
            name=row["name"],
            size=int(row["size"] or 0),
            uploaded_at=row["uploaded_at"],
            server_path=row["server_path"] or "",
        )
