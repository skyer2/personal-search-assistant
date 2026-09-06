"""Explicit worker activity tracking for idle-timeout decisions."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from contextvars import ContextVar


@dataclass
class OperationState:
    operation_id: str
    name: str
    started_at: float


@dataclass
class WorkerActivityTracker:
    worker_id: str = ""
    started_at: float = field(default_factory=time.perf_counter)
    last_progress_at: float = field(default_factory=time.perf_counter)
    events: list[dict[str, Any]] = field(default_factory=list)
    tools_completed: int = 0
    artifacts_written: int = 0
    findings_emitted: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _operations: dict[str, OperationState] = field(default_factory=dict, repr=False)
    _next_operation: int = 0

    def begin_operation(self, name: str) -> str:
        now = time.perf_counter()
        with self._lock:
            self._next_operation += 1
            operation_id = f"{name}:{self._next_operation}"
            self._operations[operation_id] = OperationState(operation_id, name, now)
            self.last_progress_at = now
            self._record("OPERATION_STARTED", name, operation_id)
            return operation_id

    def end_operation(self, operation_id: str, *, status: str = "ok") -> None:
        now = time.perf_counter()
        with self._lock:
            operation = self._operations.pop(operation_id, None)
            if operation is None:
                return
            self.last_progress_at = now
            if operation.name.startswith("tool."):
                self.tools_completed += 1
            self._record(
                "OPERATION_COMPLETED",
                operation.name,
                operation_id,
                status=status,
                duration_ms=int((now - operation.started_at) * 1000),
            )

    def heartbeat(
        self,
        *,
        event: str = "HEARTBEAT",
        current_operation: str = "",
        **details: Any,
    ) -> None:
        now = time.perf_counter()
        with self._lock:
            self.last_progress_at = now
            self._record(event, current_operation or "worker", "", **details)

    def artifact_written(self) -> None:
        with self._lock:
            self.artifacts_written += 1
            self.last_progress_at = time.perf_counter()
            self._record("ARTIFACT_WRITTEN", "artifact")

    def finding_emitted(self) -> None:
        with self._lock:
            self.findings_emitted += 1
            self.last_progress_at = time.perf_counter()
            self._record("FINDING_EMITTED", "finding")

    def has_in_flight_operations(self) -> bool:
        with self._lock:
            return bool(self._operations)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "worker_id": self.worker_id,
                "in_flight": bool(self._operations),
                "in_flight_operations": [
                    {
                        "name": item.name,
                        "started_at": item.started_at,
                        "duration_ms": int((time.perf_counter() - item.started_at) * 1000),
                    }
                    for item in self._operations.values()
                ],
                "last_progress_at": self.last_progress_at,
                "tools_completed": self.tools_completed,
                "artifacts_written": self.artifacts_written,
                "findings_emitted": self.findings_emitted,
            }

    def _record(self, event: str, operation: str, operation_id: str = "", **details: Any) -> None:
        self.events.append(
            {
                "event": event,
                "operation": operation,
                "operation_id": operation_id,
                "at": time.perf_counter(),
                **details,
            }
        )
        self.events = self.events[-128:]


_current_tracker: ContextVar[WorkerActivityTracker | None] = ContextVar(
    "worker_activity_tracker", default=None
)


def get_current_worker_activity() -> WorkerActivityTracker | None:
    tracker = _current_tracker.get()
    return tracker if isinstance(tracker, WorkerActivityTracker) else None


def set_current_worker_activity(tracker: WorkerActivityTracker | None) -> Any:
    return _current_tracker.set(tracker)


def reset_current_worker_activity(token: Any) -> None:
    _current_tracker.reset(token)


@contextmanager
def tracked_worker_operation(name: str) -> Iterator[WorkerActivityTracker | None]:
    tracker = get_current_worker_activity()
    operation_id = tracker.begin_operation(name) if tracker is not None else ""
    try:
        yield tracker
    finally:
        if tracker is not None and operation_id:
            tracker.end_operation(operation_id)
