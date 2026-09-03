"""Semantic payload store: events keep refs/hashes; full artifacts live on disk."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.observability.paths import traces_log_dir


@dataclass
class SemanticRef:
    type: str
    id: str
    version: int = 1
    ref: str = ""
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {k: v for k, v in payload.items() if v not in ("", None)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SemanticRef":
        data = dict(raw or {})
        return cls(
            type=str(data.get("type") or ""),
            id=str(data.get("id") or ""),
            version=int(data.get("version") or 1),
            ref=str(data.get("ref") or ""),
            sha256=str(data.get("sha256") or ""),
        )


def payload_sha256(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or ""))
    return cleaned[:80] or "unknown"


class SemanticPayloadStore:
    """Persists sanitized structured payloads under logs/traces/payloads/{run_id}/."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (traces_log_dir() / "payloads")
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def _run_dir(self, run_id: str) -> Path:
        path = self.root / _safe_segment(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _next_name(self, run_id: str, artifact_type: str) -> str:
        key = f"{run_id}:{artifact_type}"
        with self._lock:
            self._counters[key] = int(self._counters.get(key) or 0) + 1
            index = self._counters[key]
        return f"{_safe_segment(artifact_type)}_{index:03d}.json"

    def put(
        self,
        *,
        run_id: str,
        artifact_type: str,
        artifact_id: str,
        payload: dict[str, Any] | list[Any],
        version: int = 1,
        filename: str | None = None,
    ) -> SemanticRef:
        name = filename or self._next_name(run_id, artifact_type)
        path = self._run_dir(run_id) / name
        body = {
            "type": artifact_type,
            "id": artifact_id,
            "version": int(version or 1),
            "payload": payload,
        }
        digest = payload_sha256(body["payload"])
        body["sha256"] = digest
        with self._lock:
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        rel = f"payloads/{_safe_segment(run_id)}/{name}"
        return SemanticRef(
            type=artifact_type,
            id=artifact_id,
            version=int(version or 1),
            ref=rel,
            sha256=digest,
        )

    def get(self, ref: str) -> dict[str, Any] | None:
        text = str(ref or "").strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_absolute():
            # refs are relative to traces log dir
            path = traces_log_dir() / text
            if not path.exists():
                path = self.root.parent / text if self.root.name == "payloads" else self.root / text
        if not path.exists():
            # allow bare filename under any run dir
            for candidate in self.root.glob(f"*/{Path(text).name}"):
                path = candidate
                break
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None


_STORE: SemanticPayloadStore | None = None


def get_payload_store() -> SemanticPayloadStore:
    global _STORE
    if _STORE is None:
        _STORE = SemanticPayloadStore()
    return _STORE


def reset_payload_store(store: SemanticPayloadStore | None = None) -> None:
    global _STORE
    _STORE = store
