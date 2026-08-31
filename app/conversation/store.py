"""Conversation Store — L0 会话上下文（不是长期 Memory）。

最近 4～8 turn + rolling summary，按 thread / user / project 隔离。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

Role = Literal["user", "assistant"]

DEFAULT_MAX_TURNS = 8
SUMMARY_KEEP_TURNS = 4
FOLLOW_UP_MARKERS = ("呢", "那", "这个", "那个", "还有", "继续", "然后", "同上")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConversationTurn:
    role: Role
    content: str
    run_id: str | None = None
    sources: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "ConversationTurn":
        raw = dict(data or {})
        role = str(raw.get("role") or "user")
        if role not in {"user", "assistant"}:
            role = "user"
        return cls(
            role=role,  # type: ignore[arg-type]
            content=str(raw.get("content") or ""),
            run_id=str(raw["run_id"]) if raw.get("run_id") else None,
            sources=[str(x) for x in (raw.get("sources") or []) if x],
            timestamp=str(raw.get("timestamp") or _now()),
        )


@dataclass
class ConversationThread:
    thread_id: str
    project_id: str = "Inbox"
    user_id: str = "me"
    turns: list[ConversationTurn] = field(default_factory=list)
    rolling_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "turns": [t.to_dict() for t in self.turns],
            "rolling_summary": self.rolling_summary,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ConversationThread":
        raw = dict(data or {})
        return cls(
            thread_id=str(raw.get("thread_id") or ""),
            project_id=str(raw.get("project_id") or "Inbox"),
            user_id=str(raw.get("user_id") or "me"),
            turns=[ConversationTurn.from_dict(t) for t in (raw.get("turns") or []) if t],
            rolling_summary=str(raw.get("rolling_summary") or ""),
        )


def looks_like_follow_up(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    if any(m in q for m in FOLLOW_UP_MARKERS):
        return True
    if len(q) <= 16 and q.endswith(("？", "?", "吗", "呢")):
        return True
    return False


def rewrite_query(query: str, thread: ConversationThread | None) -> str:
    """把短追问接到上一轮主题。Conversation ≠ Memory。"""
    q = (query or "").strip()
    if not thread or not thread.turns or not looks_like_follow_up(q):
        return q
    last_user = next((t.content for t in reversed(thread.turns) if t.role == "user"), "")
    last_assistant = next((t.content for t in reversed(thread.turns) if t.role == "assistant"), "")
    summary = (thread.rolling_summary or last_user or "").strip()
    parts = []
    if summary:
        parts.append(f"此前主题：{summary[:240]}")
    if last_user and last_user != summary:
        parts.append(f"上一轮问题：{last_user[:160]}")
    if last_assistant:
        parts.append(f"上一轮要点：{last_assistant[:200]}")
    parts.append(f"当前追问：{q}")
    return "\n".join(parts)


def _compress_turns(old_turns: list[ConversationTurn]) -> str:
    lines: list[str] = []
    for turn in old_turns:
        prefix = "用户" if turn.role == "user" else "助手"
        lines.append(f"{prefix}：{turn.content[:180]}")
    text = "；".join(lines)
    return re.sub(r"\s+", " ", text).strip()[:400]


class ConversationStore:
    def __init__(self, root: Path, *, user_id: str = "me", project_id: str = "Inbox"):
        self.user_id = user_id or "me"
        self.project_id = project_id or "Inbox"
        self.root = Path(root) / self.user_id / self.project_id
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def default(cls, *, user_id: str = "me", project_id: str = "Inbox") -> "ConversationStore":
        return cls(Path("output") / ".conversations", user_id=user_id, project_id=project_id)

    def _path(self, thread_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", thread_id or "default")[:80]
        return self.root / f"{safe}.json"

    def get(self, thread_id: str) -> ConversationThread:
        path = self._path(thread_id)
        if not path.exists():
            return ConversationThread(
                thread_id=thread_id,
                project_id=self.project_id,
                user_id=self.user_id,
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        thread = ConversationThread.from_dict(data)
        thread.thread_id = thread_id
        thread.project_id = self.project_id
        thread.user_id = self.user_id
        return thread

    def save(self, thread: ConversationThread) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(thread.thread_id).write_text(
            json.dumps(thread.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def append_turn(self, thread_id: str, turn: ConversationTurn, *, max_turns: int = DEFAULT_MAX_TURNS) -> ConversationThread:
        thread = self.get(thread_id)
        thread.turns.append(turn)
        overflow = len(thread.turns) - max_turns
        if overflow > 0:
            evicted = thread.turns[:overflow]
            thread.turns = thread.turns[overflow:]
            extra = _compress_turns(evicted)
            if extra:
                prev = thread.rolling_summary.strip()
                thread.rolling_summary = (f"{prev}；{extra}" if prev else extra)[:600]
        self.save(thread)
        return thread

    def get_context(
        self,
        thread_id: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> tuple[list[ConversationTurn], str]:
        thread = self.get(thread_id)
        return list(thread.turns[-max_turns:]), thread.rolling_summary

    def rewrite(self, thread_id: str, query: str) -> str:
        return rewrite_query(query, self.get(thread_id))
