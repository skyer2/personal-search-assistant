"""SQLite query store for run-scoped events and materialized trace projections.

JSONL remains the human-readable audit export. UI reads use indexed rows and cached
projections, avoiding repeated session-wide replay.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.observability.events import AgentEvent
from app.observability.paths import traces_log_dir


class ProjectionStore:
    def __init__(self, path: Path | None = None) -> None:
        target = path or (traces_log_dir() / "trace-projections.sqlite3")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(target), check_same_thread=False)
        with self._db:
            self._db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS run_events (
                  run_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL,
                  session_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
                  PRIMARY KEY (run_id, seq, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_run_events_type ON run_events(run_id, type);
                CREATE TABLE IF NOT EXISTS run_projections (
                  run_id TEXT NOT NULL, kind TEXT NOT NULL, max_seq INTEGER NOT NULL,
                  payload TEXT NOT NULL, PRIMARY KEY(run_id, kind)
                );
            """)

    def append(self, event: AgentEvent) -> None:
        payload = json.dumps(event.to_jsonl_record(), ensure_ascii=False, default=str)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO run_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.run_id,
                    event.seq,
                    event.event_id,
                    event.session_id,
                    event.type,
                    payload,
                ),
            )
            self._db.execute(
                "DELETE FROM run_projections WHERE run_id = ?", (event.run_id,)
            )

    def events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        before_seq: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["run_id = ?", "seq > ?"]
        params: list[Any] = [run_id, after_seq]
        if before_seq is not None:
            clauses.append("seq < ?")
            params.append(before_seq)
        order = "DESC" if before_seq is not None or after_seq == 0 else "ASC"
        sql = f"SELECT payload FROM run_events WHERE {' AND '.join(clauses)} ORDER BY seq {order}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._db.execute(sql, tuple(params)).fetchall()
        values = [json.loads(row[0]) for row in rows]
        return list(reversed(values)) if order == "DESC" else values

    def max_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0] if row else 0)

    def get_projection(self, run_id: str, kind: str) -> dict[str, Any] | None:
        max_seq = self.max_seq(run_id)
        with self._lock:
            row = self._db.execute(
                "SELECT max_seq, payload FROM run_projections WHERE run_id=? AND kind=?",
                (run_id, kind),
            ).fetchone()
        return json.loads(row[1]) if row and int(row[0]) == max_seq else None

    def put_projection(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO run_projections VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    kind,
                    self.max_seq(run_id),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )


_STORE: ProjectionStore | None = None


def get_projection_store() -> ProjectionStore:
    global _STORE
    if _STORE is None:
        _STORE = ProjectionStore()
    return _STORE
