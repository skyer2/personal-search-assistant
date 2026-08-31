"""
Raw Artifact Store — 原文外置，Context 只保留 ref。

原则：lossy context, lossless storage。网页/SQL/文件正文可以不进窗口，
但 artifact_id / URL / path 必须保留，才能 read_artifact 按需回读。
"""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_URL_RE = re.compile(r"https?://[^\s\]\)\"'<>]+", re.IGNORECASE)

ARTIFACT_KIND_WEB = "web"
ARTIFACT_KIND_SQL = "sql"
ARTIFACT_KIND_FILE = "file"
ARTIFACT_KIND_KB = "kb"
ARTIFACT_KIND_PDF = "pdf"
ARTIFACT_KIND_TOOL = "tool"


def _next_slug(kind: str, n: int) -> str:
    return f"art-{kind}-{n}"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def infer_kind(step_type: str | None = None, locator: str = "") -> str:
    step = (step_type or "").lower()
    loc = (locator or "").lower()
    if step in {"network_search", "web"} or loc.startswith("http"):
        return ARTIFACT_KIND_WEB
    if step in {"file_read", "file"} or loc.endswith((".md", ".txt", ".csv")):
        return ARTIFACT_KIND_FILE
    if step in {"pdf"} or loc.endswith(".pdf"):
        return ARTIFACT_KIND_PDF
    return ARTIFACT_KIND_TOOL


@dataclass
class Artifact:
    artifact_id: str
    kind: str
    locator: str
    title: str = ""
    summary: str = ""
    content: str = ""
    mime: str = "text/plain"
    charset: str = "utf-8"
    content_sha256: str = ""
    char_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    step_index: int = -1
    step_type: str = ""

    def ref(self) -> str:
        return f"artifact://{self.kind}/{self.artifact_id}"

    def compact_card(self, snippet_chars: int = 280) -> dict[str, Any]:
        snippet = (self.summary or self.content or "")[:snippet_chars]
        return {
            "artifact_id": self.artifact_id,
            "ref": self.ref(),
            "kind": self.kind,
            "title": self.title or self.locator[:80],
            "locator": self.locator,
            "summary": snippet,
            "char_count": self.char_count,
        }

    def slice(self, start: int = 0, end: int | None = None) -> str:
        text = self.content or ""
        start = max(0, int(start or 0))
        if end is None:
            return text[start:]
        return text[start : max(start, int(end))]

    def search(self, query: str, *, window: int = 240, limit: int = 5) -> list[dict[str, Any]]:
        text = self.content or ""
        q = (query or "").strip()
        if not q or not text:
            return []
        hits: list[dict[str, Any]] = []
        lowered = text.lower()
        needle = q.lower()
        start = 0
        while len(hits) < limit:
            idx = lowered.find(needle, start)
            if idx < 0:
                break
            left = max(0, idx - window)
            right = min(len(text), idx + len(q) + window)
            hits.append(
                {
                    "start_offset": idx,
                    "end_offset": idx + len(q),
                    "text": text[left:right],
                }
            )
            start = idx + max(1, len(q))
        return hits

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if not include_content:
            payload.pop("content", None)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Artifact":
        row = data or {}
        content = str(row.get("content") or "")
        return cls(
            artifact_id=str(row.get("artifact_id") or ""),
            kind=str(row.get("kind") or ARTIFACT_KIND_TOOL),
            locator=str(row.get("locator") or ""),
            title=str(row.get("title") or ""),
            summary=str(row.get("summary") or ""),
            content=content,
            mime=str(row.get("mime") or "text/plain"),
            charset=str(row.get("charset") or "utf-8"),
            content_sha256=str(row.get("content_sha256") or _sha(content)),
            char_count=int(row.get("char_count") or len(content)),
            metadata=dict(row.get("metadata") or {}),
            created_at=str(row.get("created_at") or datetime.now().isoformat()),
            step_index=int(row.get("step_index") if row.get("step_index") is not None else -1),
            step_type=str(row.get("step_type") or ""),
        )


class ArtifactStore:
    """进程内 + session 目录持久化。Content 与 metadata 分离，避免把原文塞进 LoopState。"""

    def __init__(self, session_dir: Path | None = None):
        self.session_dir = Path(session_dir) if session_dir else None
        self._items: dict[str, Artifact] = {}
        self._counter = 0

    def __len__(self) -> int:
        return len(self._items)

    def _dir(self) -> Path | None:
        if self.session_dir is None:
            return None
        path = self.session_dir / ".harness" / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def put(
        self,
        content: str,
        *,
        kind: str = ARTIFACT_KIND_TOOL,
        locator: str = "",
        title: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
        step_index: int = -1,
        step_type: str = "",
        mime: str = "text/plain",
    ) -> Artifact:
        text = content if content is not None else ""
        digest = _sha(text)
        for existing in self._items.values():
            if existing.content_sha256 == digest and existing.locator == (locator or existing.locator):
                return existing
        self._counter += 1
        artifact = Artifact(
            artifact_id=_next_slug(kind or ARTIFACT_KIND_TOOL, self._counter),
            kind=kind or infer_kind(step_type, locator),
            locator=locator or f"step:{step_index}:{step_type or kind}",
            title=title[:200],
            summary=(summary or text[:400]).strip()[:800],
            content=text,
            mime=mime,
            content_sha256=digest,
            char_count=len(text),
            metadata=dict(metadata or {}),
            step_index=step_index,
            step_type=step_type,
        )
        self._items[artifact.artifact_id] = artifact
        self._persist_one(artifact)
        return artifact

    def put_from_tool_result(
        self,
        raw: Any,
        *,
        tool_name: str,
        step_type: str = "",
        step_index: int = -1,
        locator: str = "",
        title: str = "",
    ) -> Artifact:
        if isinstance(raw, str):
            content = raw
        else:
            try:
                content = json.dumps(raw, ensure_ascii=False, indent=2)
            except TypeError:
                content = str(raw)
        urls = _URL_RE.findall(content)
        loc = locator or (urls[0].rstrip(".,;") if urls else f"tool:{tool_name}")
        kind = infer_kind(step_type, loc)
        return self.put(
            content,
            kind=kind,
            locator=loc,
            title=title or tool_name,
            metadata={"tool_name": tool_name},
            step_index=step_index,
            step_type=step_type,
        )

    def get(self, artifact_id: str) -> Artifact | None:
        key = (artifact_id or "").strip()
        if key.startswith("artifact://"):
            key = key.rsplit("/", 1)[-1]
        item = self._items.get(key)
        if item is not None:
            return item
        loaded = self._load_one(key)
        if loaded is not None:
            self._items[loaded.artifact_id] = loaded
        return loaded

    def read(
        self,
        artifact_id: str,
        *,
        start: int = 0,
        end: int | None = None,
        query: str = "",
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        artifact = self.get(artifact_id)
        if artifact is None:
            return {"ok": False, "error_code": "artifact_not_found", "artifact_id": artifact_id}
        if query.strip():
            hits = artifact.search(query, limit=8)
            return {
                "ok": True,
                "artifact_id": artifact.artifact_id,
                "ref": artifact.ref(),
                "locator": artifact.locator,
                "title": artifact.title,
                "query": query,
                "hits": hits[:8],
            }
        text = artifact.slice(start, end)
        truncated = False
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        return {
            "ok": True,
            "artifact_id": artifact.artifact_id,
            "ref": artifact.ref(),
            "locator": artifact.locator,
            "title": artifact.title,
            "start": start,
            "end": end,
            "truncated": truncated,
            "char_count": artifact.char_count,
            "text": text,
        }

    def list_cards(self, *, limit: int = 40) -> list[dict[str, Any]]:
        return [item.compact_card() for item in list(self._items.values())[:limit]]

    def ids(self) -> list[str]:
        return list(self._items.keys())

    def checkpoint_snapshot(self) -> dict[str, Any]:
        return {
            "counter": self._counter,
            "artifacts": [item.to_dict(include_content=False) for item in self._items.values()],
        }

    def persist(self, session_dir: Path | None = None) -> Path | None:
        if session_dir is not None:
            self.session_dir = Path(session_dir)
        root = self._dir()
        if root is None:
            return None
        index_path = root / "index.json"
        index_path.write_text(
            json.dumps(self.checkpoint_snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for item in self._items.values():
            self._persist_one(item)
        return index_path

    def load(self, session_dir: Path | None = None) -> None:
        if session_dir is not None:
            self.session_dir = Path(session_dir)
        root = self._dir()
        if root is None:
            return
        index_path = root / "index.json"
        if not index_path.exists():
            return
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        self._counter = int(payload.get("counter") or 0)
        for row in payload.get("artifacts") or []:
            if not isinstance(row, dict):
                continue
            artifact = Artifact.from_dict(row)
            body = self._load_body(artifact.artifact_id)
            if body is not None:
                artifact.content = body
                artifact.char_count = len(body)
            self._items[artifact.artifact_id] = artifact

    def _persist_one(self, artifact: Artifact) -> None:
        root = self._dir()
        if root is None:
            return
        body_path = root / f"{artifact.artifact_id}.txt"
        body_path.write_text(artifact.content or "", encoding="utf-8")

    def _load_one(self, artifact_id: str) -> Artifact | None:
        root = self._dir()
        if root is None:
            return None
        body = self._load_body(artifact_id)
        if body is None:
            return None
        return Artifact(
            artifact_id=artifact_id,
            kind=artifact_id.split("-")[1] if artifact_id.startswith("art-") else ARTIFACT_KIND_TOOL,
            locator="",
            content=body,
            char_count=len(body),
            content_sha256=_sha(body),
        )

    def _load_body(self, artifact_id: str) -> str | None:
        root = self._dir()
        if root is None:
            return None
        path = root / f"{artifact_id}.txt"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")


_STORE: ContextVar[ArtifactStore | None] = ContextVar("harness_artifact_store", default=None)


def get_artifact_store() -> ArtifactStore:
    store = _STORE.get()
    if store is None:
        store = ArtifactStore()
        _STORE.set(store)
    return store


def set_artifact_store(store: ArtifactStore | None) -> None:
    _STORE.set(store)


def reset_artifact_store() -> None:
    _STORE.set(None)
