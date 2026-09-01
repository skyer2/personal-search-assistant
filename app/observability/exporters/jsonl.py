"""JSONL journal exporter — canonical persist for local traces."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.observability.events import AgentEvent


class JsonlExporter:
    def __init__(self, log_dir: Path, enabled: bool = True) -> None:
        self.log_dir = log_dir
        self.enabled = enabled
        self._lock = threading.Lock()
        if self.enabled:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def export(self, event: AgentEvent) -> None:
        if not self.enabled:
            return
        path = self.log_dir / f"{event.session_id}.jsonl"
        line = json.dumps(event.to_jsonl_record(), ensure_ascii=False, default=str)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read(self, session_id: str) -> list[dict[str, Any]]:
        path = self.log_dir / f"{session_id}.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
