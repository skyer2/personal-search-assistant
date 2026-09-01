"""
Harness JSONL 结构化日志

每次 run 将阶段事件追加写入 logs/traces/{session_id}.jsonl，供离线分析与评测。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from app.config.loader import get_harness_config


class JsonlTraceLogger:
    def __init__(self, log_dir: Path, enabled: bool = True):
        self.log_dir = log_dir
        self.enabled = enabled
        self._lock = threading.Lock()
        if self.enabled:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, project_root: Path) -> "JsonlTraceLogger":
        config = get_harness_config()
        log_dir = project_root / config.jsonl_log_dir
        return cls(log_dir=log_dir, enabled=config.jsonl_log_enabled)

    def new_trace_id(self) -> str:
        return str(uuid4())

    def log_event(
        self,
        *,
        trace_id: str,
        session_id: str,
        phase: str,
        status: str,
        step_index: Optional[int] = None,
        step_type: Optional[str] = None,
        duration_ms: Optional[int] = None,
        tool_calls: Optional[int] = None,
        tokens_used: Optional[int] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self.enabled:
            return

        record: dict[str, Any] = {
            "trace_id": trace_id,
            "session_id": session_id,
            "phase": phase,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if step_index is not None:
            record["step_index"] = step_index
        if step_type is not None:
            record["step_type"] = step_type
        if duration_ms is not None:
            record["duration_ms"] = duration_ms
        if tool_calls is not None:
            record["tool_calls"] = tool_calls
        if tokens_used is not None:
            record["tokens_used"] = tokens_used
        if extra:
            record["extra"] = extra
            for key, value in extra.items():
                if key not in record:
                    record[key] = value

        path = self.log_dir / f"{session_id}.jsonl"
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")

    def log_run_summary(
        self,
        *,
        trace_id: str,
        session_id: str,
        status: str,
        duration_ms: int,
        metadata: dict[str, Any],
    ) -> None:
        self.log_event(
            trace_id=trace_id,
            session_id=session_id,
            phase="run",
            status=status,
            duration_ms=duration_ms,
            tool_calls=int(metadata.get("tool_calls_count", 0)),
            extra={"event": "run_summary", "metadata": metadata},
        )

    def read_trace(self, session_id: str) -> list[dict[str, Any]]:
        path = self.log_dir / f"{session_id}.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events


_default_logger: JsonlTraceLogger | None = None


def get_trace_logger(project_root: Path) -> JsonlTraceLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = JsonlTraceLogger.from_config(project_root)
    return _default_logger
