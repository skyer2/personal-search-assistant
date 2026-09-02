"""Durable Run / Session projection — UI truth, not workflow truth.

四种事实源不要混：
- Workflow execution  → LangGraph ResearchState checkpoint
- Run / UI projection → 本模块 SQLite RunStore
- Agent history       → AgentEvent journal / JSONL
- Raw data            → Artifact / Evidence store
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 一次 Agent 执行的生命周期。active_tasks 只是进程内 execution cache。
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_CANCELLING = "cancelling"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"
STATUS_RECOVERABLE = "recoverable"
STATUS_INTERRUPTED = "interrupted"

ACTIVE_STATUSES = frozenset(
    {
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_AWAITING_APPROVAL,
        STATUS_CANCELLING,
    }
)
TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_PARTIAL,
        STATUS_INTERRUPTED,
    }
)
RECOVERABLE_ON_STARTUP = frozenset(
    {
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_CANCELLING,
    }
)


@dataclass
class SessionRecord:
    session_id: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    query: str
    status: str
    created_at: str
    mode: str = "agent"
    started_at: str | None = None
    ended_at: str | None = None
    current_phase: str | None = None
    plan_version: int | None = None
    final_result: str = ""
    error: str = ""
    session_workspace: str = ""
    last_event_seq: int = 0
    hitl_status: str | None = None
    hitl_payload: dict[str, Any] | None = None
    paused_total_ms: int = 0
    pause_started_at: str | None = None
    tool_calls: int = 0
    assistant_calls: int = 0
    errors: int = 0

    def elapsed_ms(self, now_iso: str | None = None) -> int:
        if not self.started_at:
            return 0
        from datetime import datetime, timezone

        def _parse(value: str) -> datetime:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        start = _parse(self.started_at)
        if self.ended_at:
            end = _parse(self.ended_at)
        elif self.pause_started_at:
            end = _parse(self.pause_started_at)
        elif now_iso:
            end = _parse(now_iso)
        else:
            end = datetime.now(timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        raw = int((end - start).total_seconds() * 1000)
        return max(0, raw - int(self.paused_total_ms or 0))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["elapsed_ms"] = self.elapsed_ms()
        return payload


@dataclass
class UploadedFileRecord:
    session_id: str
    name: str
    size: int
    uploaded_at: str
    server_path: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "uploaded_at": self.uploaded_at,
            "server_file_id": self.id,
            "server_path": self.server_path,
        }


@dataclass
class RunStats:
    tool_calls: int = 0
    assistant_calls: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionBootstrap:
    session_id: str
    found: bool
    runs: list[RunRecord] = field(default_factory=list)
    current_run: RunRecord | None = None
    hitl: dict[str, Any] | None = None
    uploaded_files: list[UploadedFileRecord] = field(default_factory=list)
    output_files: list[dict[str, Any]] = field(default_factory=list)
    stats: RunStats = field(default_factory=RunStats)
    last_event_seq: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    notice: str = ""

    def to_dict(self) -> dict[str, Any]:
        current = self.current_run.to_dict() if self.current_run else None
        return {
            "found": self.found,
            "session_id": self.session_id,
            "runs": [item.to_dict() for item in self.runs],
            "current_run": current,
            "hitl": self.hitl,
            "uploaded_files": [item.to_dict() for item in self.uploaded_files],
            "output_files": self.output_files,
            "stats": self.stats.to_dict(),
            "last_event_seq": self.last_event_seq,
            "events": self.events,
            "notice": self.notice,
        }
