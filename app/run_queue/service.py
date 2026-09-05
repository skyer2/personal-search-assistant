"""Durable Run Queue：SQLite 持久任务队列（P2 生产化第一步）。

目标：把「API 接受请求」与「Worker 执行研究」分离。
当前默认仍是进程内直接执行（兼容单机）；设置 RUN_QUEUE_ENABLED=1 后：

    POST /api/task → enqueue（durable row）→ 立即返回
    RunQueueWorker → claim_next → 执行 → complete/fail

多实例部署时，同一张表可换成 Redis Streams / Postgres SKIP LOCKED，
接口语义保持不变（enqueue / claim / heartbeat / complete / fail）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.observability.paths import APP_ROOT


@dataclass
class RunJob:
    job_id: str
    session_id: str
    query: str
    mode: str
    user_id: str
    tenant_id: str
    project_id: str
    status: str
    claimed_by: str | None
    created_at: str
    updated_at: str
    claimed_at: str | None = None
    heartbeat_at: str | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunQueue:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS run_jobs (
        job_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        query TEXT NOT NULL,
        mode TEXT DEFAULT 'agent',
        user_id TEXT DEFAULT 'me',
        tenant_id TEXT DEFAULT 'local',
        project_id TEXT DEFAULT 'Inbox',
        status TEXT NOT NULL,
        claimed_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        claimed_at TEXT,
        heartbeat_at TEXT,
        error TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_run_jobs_status ON run_jobs(status, created_at);
    """

    def __init__(self, path: Path | None = None):
        override = os.getenv("RUN_QUEUE_PATH")
        self.path = Path(path or override or (APP_ROOT / "output" / ".harness" / "run_queue.sqlite"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(self._SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> RunJob:
        return RunJob(
            job_id=row["job_id"],
            session_id=row["session_id"],
            query=row["query"],
            mode=row["mode"] or "agent",
            user_id=row["user_id"] or "me",
            tenant_id=row["tenant_id"] or "local",
            project_id=row["project_id"] or "Inbox",
            status=row["status"],
            claimed_by=row["claimed_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            claimed_at=row["claimed_at"],
            heartbeat_at=row["heartbeat_at"],
            error=row["error"] or "",
        )

    def enqueue(
        self,
        *,
        job_id: str,
        session_id: str,
        query: str,
        mode: str = "agent",
        user_id: str = "me",
        tenant_id: str = "local",
        project_id: str = "Inbox",
    ) -> RunJob:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO run_jobs(
                    job_id, session_id, query, mode, user_id, tenant_id, project_id,
                    status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,'pending',?,?)
                """,
                (job_id, session_id, query, mode, user_id, tenant_id, project_id, now, now),
            )
            self._conn.commit()
        return self.get_job(job_id)  # type: ignore[return-value]

    def claim_next(self, worker_id: str) -> RunJob | None:
        """FIFO 认领：单进程内锁保护；多实例部署换 SELECT ... SKIP LOCKED。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM run_jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE run_jobs
                SET status='running', claimed_by=?, claimed_at=?, heartbeat_at=?, updated_at=?
                WHERE job_id=? AND status='pending'
                """,
                (worker_id, now, now, now, row["job_id"]),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM run_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            return self._job_from_row(updated) if updated else None

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE run_jobs SET heartbeat_at=?, updated_at=? WHERE job_id=? AND claimed_by=?",
                (_utc_now(), _utc_now(), job_id, worker_id),
            )
            self._conn.commit()

    def complete(self, job_id: str, worker_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE run_jobs SET status='completed', updated_at=? WHERE job_id=? AND claimed_by=?",
                (_utc_now(), job_id, worker_id),
            )
            self._conn.commit()

    def fail(self, job_id: str, worker_id: str, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE run_jobs SET status='failed', error=?, updated_at=? WHERE job_id=? AND claimed_by=?",
                (str(error)[:2000], _utc_now(), job_id, worker_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> RunJob | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM run_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            return self._job_from_row(row) if row else None

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[RunJob]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM run_jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                rows = self._conn.execute(
                    "SELECT * FROM run_jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            return [self._job_from_row(row) for row in rows]


_QUEUE: RunQueue | None = None


def get_run_queue(path: Path | None = None) -> RunQueue:
    global _QUEUE
    if path is not None:
        if _QUEUE is not None:
            _QUEUE.close()
        _QUEUE = RunQueue(path)
        return _QUEUE
    if _QUEUE is None:
        _QUEUE = RunQueue()
    return _QUEUE


def run_queue_enabled() -> bool:
    return os.getenv("RUN_QUEUE_ENABLED", "").lower() in {"1", "true", "yes", "on"}


__all__ = ["RunJob", "RunQueue", "get_run_queue", "run_queue_enabled"]
