"""RunStore service — persist-before-publish projection for the UI."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from app.observability.events import utc_now
from app.observability.paths import APP_ROOT
from app.run_store.files import list_run_output_files
from app.run_store.models import (
    ACTIVE_STATUSES,
    RECOVERABLE_ON_STARTUP,
    STATUS_AWAITING_APPROVAL,
    STATUS_CANCELLING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_PARTIAL,
    STATUS_QUEUED,
    STATUS_RECOVERABLE,
    STATUS_RUNNING,
    RunRecord,
    RunStats,
    SessionBootstrap,
    SessionRecord,
    UploadedFileRecord,
)
from app.run_store.sqlite import SqliteRunStore

_STORE: "RunStore | None" = None


def default_db_path() -> Path:
    override = os.getenv("RUN_STORE_PATH")
    if override:
        return Path(override)
    return APP_ROOT / "output" / ".harness" / "run_store.sqlite"


class RunStore:
    def __init__(self, path: Path | None = None):
        self.db = SqliteRunStore(path or default_db_path())

    def close(self) -> None:
        self.db.close()

    def ensure_session(self, session_id: str) -> SessionRecord:
        return self.ensure_session_owned(session_id)

    def ensure_session_owned(
        self,
        session_id: str,
        *,
        tenant_id: str = "local",
        user_id: str = "me",
        project_id: str = "Inbox",
    ) -> SessionRecord:
        now = utc_now()
        existing = self.get_session(session_id)
        if existing:
            self.db.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id=?",
                (now, session_id),
            )
            return SessionRecord(session_id=session_id, created_at=existing.created_at, updated_at=now)
        self.db.execute(
            """
            INSERT INTO sessions(
                session_id, created_at, updated_at, tenant_id, user_id, project_id, archived
            ) VALUES (?,?,?,?,?,?,0)
            """,
            (session_id, now, now, tenant_id, user_id, project_id),
        )
        return SessionRecord(
            session_id=session_id,
            created_at=now,
            updated_at=now,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
        )

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self.db.query_one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        return self.db.session_from_row(row) if row else None

    def create_run(
        self,
        *,
        run_id: str,
        session_id: str,
        query: str,
        mode: str = "agent",
        session_workspace: str = "",
        tenant_id: str = "local",
        user_id: str = "me",
        project_id: str = "Inbox",
    ) -> RunRecord:
        self.ensure_session_owned(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
        )
        now = utc_now()
        workspace = session_workspace or f"session_{session_id}"
        self.db.execute(
            """
            INSERT INTO runs(
                run_id, session_id, query, status, mode, created_at,
                session_workspace, last_event_seq, paused_total_ms,
                tool_calls, assistant_calls, errors, final_result, error,
                tenant_id, user_id, project_id
            ) VALUES (?,?,?,?,?,?,?,0,0,0,0,0,'','',?,?,?)
            """,
            (
                run_id,
                session_id,
                query,
                STATUS_QUEUED,
                mode,
                now,
                workspace,
                tenant_id,
                user_id,
                project_id,
            ),
        )
        record = self.get_run(run_id)
        assert record is not None
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.db.query_one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return self.db.run_from_row(row) if row else None

    def get_run_scoped(self, run_id: str, tenant_id: str) -> RunRecord | None:
        """租户边界：即使知道 run_id，跨租户也视为不可见（403 由路由层判定）。"""
        run = self.get_run(run_id)
        if run is None or run.tenant_id != tenant_id:
            return None
        return run

    def list_sessions(
        self,
        *,
        tenant_id: str = "local",
        include_archived: bool = False,
    ) -> list[SessionRecord]:
        sql = "SELECT * FROM sessions WHERE tenant_id=?"
        if not include_archived:
            sql += " AND COALESCE(archived, 0)=0"
        sql += " ORDER BY updated_at DESC"
        rows = self.db.query(sql, (tenant_id,))
        return [self.db.session_from_row(row) for row in rows]

    def set_session_archived(
        self,
        session_id: str,
        *,
        archived: bool,
        tenant_id: str | None = None,
    ) -> SessionRecord | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        if tenant_id is not None and session.tenant_id != tenant_id:
            return None
        self.db.execute(
            "UPDATE sessions SET archived=?, updated_at=? WHERE session_id=?",
            (1 if archived else 0, utc_now(), session_id),
        )
        return self.get_session(session_id)

    def delete_session(
        self,
        session_id: str,
        *,
        tenant_id: str | None = None,
        output_root: Path | None = None,
        updated_root: Path | None = None,
        traces_root: Path | None = None,
    ) -> dict[str, Any]:
        """Tombstone + 级联清理：runs / uploads / 工作区 / trace 全部按 Session 删除。"""
        session = self.get_session(session_id)
        if session is None:
            return {"deleted": False, "reason": "not_found"}
        if tenant_id is not None and session.tenant_id != tenant_id:
            return {"deleted": False, "reason": "forbidden"}
        runs = self.list_runs(session_id)
        self.db.execute("DELETE FROM runs WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM uploads WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        removed_paths: list[str] = []
        output = output_root or (APP_ROOT / "output")
        for root in (
            output / f"session_{session_id}",
            (updated_root or (APP_ROOT / "updated")) / f"session_{session_id}",
            (traces_root or (APP_ROOT / "logs" / "traces")) / f"{session_id}.jsonl",
        ):
            if root.exists():
                shutil.rmtree(root, ignore_errors=True) if root.is_dir() else root.unlink(missing_ok=True)
                removed_paths.append(str(root))
        return {
            "deleted": True,
            "session_id": session_id,
            "runs_deleted": len(runs),
            "paths_removed": removed_paths,
        }

    def delete_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        output_root: Path | None = None,
    ) -> dict[str, Any]:
        """删除单次 Run 的 Run-owned 数据（DB 行 + runs/{run_id}/ 目录）。"""
        run = self.get_run(run_id)
        if run is None:
            return {"deleted": False, "reason": "not_found"}
        if tenant_id is not None and run.tenant_id != tenant_id:
            return {"deleted": False, "reason": "forbidden"}
        self.db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        output = output_root or (APP_ROOT / "output")
        run_dir = output / f"session_{run.session_id}" / "runs" / run_id
        removed = False
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
            removed = True
        return {
            "deleted": True,
            "run_id": run_id,
            "session_id": run.session_id,
            "run_dir_removed": removed,
        }

    def list_runs(self, session_id: str) -> list[RunRecord]:
        rows = self.db.query(
            "SELECT * FROM runs WHERE session_id=? ORDER BY created_at ASC",
            (session_id,),
        )
        return [self.db.run_from_row(row) for row in rows]

    def latest_run(self, session_id: str) -> RunRecord | None:
        row = self.db.query_one(
            "SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        return self.db.run_from_row(row) if row else None

    def list_active_runs(self) -> list[RunRecord]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES | {STATUS_RECOVERABLE})
        rows = self.db.query(
            f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            tuple(ACTIVE_STATUSES | {STATUS_RECOVERABLE}),
        )
        return [self.db.run_from_row(row) for row in rows]

    def mark_running(self, run_id: str, session_workspace: str = "") -> RunRecord | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        now = utc_now()
        started = run.started_at or now
        workspace = session_workspace or run.session_workspace
        self.db.execute(
            """
            UPDATE runs SET status=?, started_at=?, session_workspace=?,
                hitl_status=NULL, pause_started_at=NULL
            WHERE run_id=?
            """,
            (STATUS_RUNNING, started, workspace, run_id),
        )
        self.ensure_session(run.session_id)
        return self.get_run(run_id)

    def mark_cancelling(self, run_id: str) -> RunRecord | None:
        return self._set_status(run_id, STATUS_CANCELLING)

    def mark_recoverable(self, run_id: str, error: str = "backend restarted") -> RunRecord | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        now = utc_now()
        self.db.execute(
            "UPDATE runs SET status=?, error=?, ended_at=COALESCE(ended_at, ?) WHERE run_id=?",
            (STATUS_RECOVERABLE, error, now, run_id),
        )
        return self.get_run(run_id)

    def complete_run(
        self,
        run_id: str,
        *,
        result: str,
        status: str = STATUS_COMPLETED,
        error: str = "",
    ) -> RunRecord | None:
        if status not in {STATUS_COMPLETED, STATUS_PARTIAL, STATUS_INTERRUPTED, STATUS_FAILED}:
            status = STATUS_COMPLETED if result else STATUS_PARTIAL
        now = utc_now()
        self.db.execute(
            """
            UPDATE runs SET status=?, final_result=?, error=?, ended_at=?,
                hitl_status=NULL, hitl_payload=NULL, pause_started_at=NULL
            WHERE run_id=?
            """,
            (status, result, error, now, run_id),
        )
        run = self.get_run(run_id)
        if run:
            self.ensure_session(run.session_id)
        return run

    def fail_run(self, run_id: str, error: str) -> RunRecord | None:
        return self.complete_run(run_id, result="", status=STATUS_FAILED, error=error)

    def interrupt_run(self, run_id: str, error: str = "cancelled") -> RunRecord | None:
        run = self.get_run(run_id)
        result = run.final_result if run else ""
        return self.complete_run(run_id, result=result, status=STATUS_INTERRUPTED, error=error)

    def set_phase(self, run_id: str, phase: str, plan_version: int | None = None) -> None:
        if plan_version is None:
            self.db.execute("UPDATE runs SET current_phase=? WHERE run_id=?", (phase, run_id))
            return
        self.db.execute(
            "UPDATE runs SET current_phase=?, plan_version=? WHERE run_id=?",
            (phase, plan_version, run_id),
        )

    def set_hitl(self, run_id: str, payload: dict[str, Any] | None, status: str = STATUS_AWAITING_APPROVAL) -> None:
        now = utc_now()
        encoded = json.dumps(payload, ensure_ascii=False) if payload else None
        hitl_status = "waiting" if payload else None
        run_status = status if payload else STATUS_RUNNING
        self.db.execute(
            """
            UPDATE runs SET status=?, hitl_status=?, hitl_payload=?,
                pause_started_at=CASE WHEN ? THEN COALESCE(pause_started_at, ?) ELSE NULL END
            WHERE run_id=?
            """,
            (run_status, hitl_status, encoded, 1 if payload else 0, now, run_id),
        )

    def clear_hitl(self, run_id: str) -> None:
        from app.run_store.models import TERMINAL_STATUSES

        run = self.get_run(run_id)
        now = utc_now()
        extra_pause = 0
        if run and run.pause_started_at:
            extra_pause = _iso_delta_ms(run.pause_started_at, now)
        next_status = STATUS_RUNNING
        if run and run.status in TERMINAL_STATUSES | {STATUS_RECOVERABLE, STATUS_CANCELLING}:
            next_status = run.status
        self.db.execute(
            """
            UPDATE runs SET status=?, hitl_status=NULL, hitl_payload=NULL,
                pause_started_at=NULL, paused_total_ms=paused_total_ms+?
            WHERE run_id=?
            """,
            (next_status, extra_pause, run_id),
        )

    def add_upload(self, session_id: str, name: str, size: int, server_path: str = "") -> UploadedFileRecord:
        self.ensure_session(session_id)
        now = utc_now()
        self.db.execute(
            "INSERT INTO uploads(session_id, name, size, uploaded_at, server_path) VALUES (?,?,?,?,?)",
            (session_id, name, int(size), now, server_path or name),
        )
        row = self.db.query_one(
            "SELECT * FROM uploads WHERE session_id=? AND name=? ORDER BY id DESC LIMIT 1",
            (session_id, name),
        )
        assert row is not None
        return self.db.upload_from_row(row)

    def list_uploads(self, session_id: str) -> list[UploadedFileRecord]:
        rows = self.db.query(
            "SELECT * FROM uploads WHERE session_id=? ORDER BY uploaded_at ASC",
            (session_id,),
        )
        return [self.db.upload_from_row(row) for row in rows]

    def on_event(self, event: Any) -> None:
        """Projection hook from Flight Recorder. Never reconstruct business state from trace."""
        run_id = getattr(event, "run_id", None)
        if not run_id or not self.get_run(run_id):
            return
        seq = int(getattr(event, "seq", 0) or 0)
        if seq:
            self.db.execute(
                "UPDATE runs SET last_event_seq=MAX(last_event_seq, ?) WHERE run_id=?",
                (seq, run_id),
            )
        event_type = str(getattr(event, "type", "") or "")
        phase = getattr(event, "phase", None)
        if event_type == "phase" and phase:
            plan_version = getattr(event, "plan_version", None)
            self.set_phase(run_id, str(phase), plan_version)
        if event_type == "tool.started":
            self.db.execute("UPDATE runs SET tool_calls=tool_calls+1 WHERE run_id=?", (run_id,))
        if event_type in {"worker.started"}:
            self.db.execute(
                "UPDATE runs SET assistant_calls=assistant_calls+1 WHERE run_id=?",
                (run_id,),
            )
        if event_type in {"tool.failed", "worker.failed", "run.failed"}:
            self.db.execute("UPDATE runs SET errors=errors+1 WHERE run_id=?", (run_id,))

    def recover_stale_runs(self, live_session_ids: set[str]) -> list[RunRecord]:
        """Backend restart: in-flight runs without a live task become recoverable."""
        recovered: list[RunRecord] = []
        for run in self.list_active_runs():
            if run.status == STATUS_AWAITING_APPROVAL:
                continue
            if run.status in RECOVERABLE_ON_STARTUP and run.session_id not in live_session_ids:
                marked = self.mark_recoverable(run.run_id)
                if marked:
                    recovered.append(marked)
        return recovered

    def bootstrap(
        self,
        session_id: str,
        *,
        output_root: Path | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> SessionBootstrap:
        session = self.get_session(session_id)
        if session is None and not self.list_runs(session_id) and not self.list_uploads(session_id):
            return SessionBootstrap(
                session_id=session_id,
                found=False,
                notice="session_not_found",
            )
        runs = self.list_runs(session_id)
        current = self.latest_run(session_id)
        hitl = None
        if current and current.hitl_payload:
            hitl = dict(current.hitl_payload)
            hitl.setdefault("session_id", session_id)
            hitl.setdefault("run_id", current.run_id)
        uploads = self.list_uploads(session_id)
        output_root = output_root or (APP_ROOT / "output")
        files = (
            list_run_output_files(output_root, session_id, current.run_id)
            if current
            else []
        )
        stats = RunStats()
        if current:
            stats = RunStats(
                tool_calls=current.tool_calls,
                assistant_calls=current.assistant_calls,
                errors=current.errors,
            )
        return SessionBootstrap(
            session_id=session_id,
            found=True,
            runs=runs,
            current_run=current,
            hitl=hitl,
            uploaded_files=uploads,
            output_files=files,
            stats=stats,
            last_event_seq=current.last_event_seq if current else 0,
            events=events or [],
        )

    def _set_status(self, run_id: str, status: str) -> RunRecord | None:
        self.db.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))
        return self.get_run(run_id)


def _iso_delta_ms(start_iso: str, end_iso: str) -> int:
    from datetime import datetime, timezone

    def _parse(value: str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    return max(0, int((_parse(end_iso) - _parse(start_iso)).total_seconds() * 1000))


def get_run_store(path: Path | None = None) -> RunStore:
    global _STORE
    if path is not None:
        _STORE = RunStore(path)
        return _STORE
    if _STORE is None:
        _STORE = RunStore()
    return _STORE


def reset_run_store() -> None:
    global _STORE
    if _STORE is not None:
        try:
            _STORE.close()
        except Exception:
            pass
    _STORE = None
