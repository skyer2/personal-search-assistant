"""JSONL journal exporter — canonical persist for local traces.

Layout (run-centric, with legacy session flat-file compatibility):

    logs/traces/
      {session_id}/
        {run_id}.jsonl
        index.jsonl          # optional session→run pointers
      {session_id}.jsonl     # legacy flat file (still readable)
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any

from app.observability.events import AgentEvent
from app.observability.retention import should_sample


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or "")) or "unknown"


class JsonlExporter:
    def __init__(self, log_dir: Path, enabled: bool = True, *, run_centric: bool = True) -> None:
        self.log_dir = log_dir
        self.enabled = enabled
        self.run_centric = run_centric
        self._lock = threading.Lock()
        if self.enabled:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def _run_path(self, session_id: str, run_id: str) -> Path:
        return self.log_dir / _safe(session_id) / f"{_safe(run_id)}.jsonl"

    def _legacy_path(self, session_id: str) -> Path:
        return self.log_dir / f"{_safe(session_id)}.jsonl"

    def export(self, event: AgentEvent) -> None:
        if not self.enabled:
            return
        if not should_sample(event.type):
            return
        line = json.dumps(event.to_jsonl_record(), ensure_ascii=False, default=str)
        with self._lock:
            if self.run_centric:
                path = self._run_path(event.session_id, event.run_id or event.session_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                index = path.parent / "index.jsonl"
                with index.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "run_id": event.run_id,
                                "session_id": event.session_id,
                                "event_id": event.event_id,
                                "type": event.type,
                                "seq": event.seq,
                                "timestamp": event.timestamp,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            else:
                with self._legacy_path(event.session_id).open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

    def _read_file(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            lines = handle
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def read_page(self, session_id: str, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Bound memory and JSON parsing to the requested tail page."""
        path = self._run_path(session_id, run_id)
        if not path.exists():
            return self.read(session_id, run_id=run_id)[-limit:]
        tail: deque[str] = deque(maxlen=max(1, limit))
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    tail.append(line)
        events: list[dict[str, Any]] = []
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def read(self, session_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        session = _safe(session_id)
        if run_id:
            records = self._read_file(self._run_path(session_id, run_id))
            if records:
                return records
            # Fallback: filter legacy flat file
            return [
                item
                for item in self._read_file(self._legacy_path(session_id))
                if str(item.get("run_id") or "") in {str(run_id), ""}
            ]

        events: list[dict[str, Any]] = []
        session_dir = self.log_dir / session
        if session_dir.is_dir():
            for path in sorted(session_dir.glob("*.jsonl")):
                if path.name == "index.jsonl":
                    continue
                events.extend(self._read_file(path))
        events.extend(self._read_file(self._legacy_path(session_id)))
        # Dedupe by event_id preferring first occurrence
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for item in events:
            key = str(item.get("event_id") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            merged.append(item)
        merged.sort(key=lambda item: (str(item.get("run_id") or ""), int(item.get("seq") or 0), str(item.get("timestamp") or "")))
        return merged

    def list_runs(self, session_id: str) -> list[str]:
        session_dir = self.log_dir / _safe(session_id)
        runs: list[str] = []
        if session_dir.is_dir():
            for path in sorted(session_dir.glob("*.jsonl")):
                if path.name == "index.jsonl":
                    continue
                runs.append(path.stem)
        return runs
