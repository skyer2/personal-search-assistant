"""RunSummary：每次 Run 结束后的结构化结论，供后续 Run 显式继承。

原则：Continuity 通过 ContextBuilder 显式构造（读 RunSummary），
而不是通过共享文件系统 / 共享 checkpoint / 整段聊天历史隐式产生。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunSummary:
    run_id: str
    session_id: str
    query: str
    intent_summary: str = ""
    entities: list[str] = field(default_factory=list)
    conclusions: list[str] = field(default_factory=list)
    key_evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    status: str = "completed"
    created_at: str = ""
    parent_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "RunSummary":
        raw = dict(raw or {})
        return cls(
            run_id=str(raw.get("run_id") or ""),
            session_id=str(raw.get("session_id") or ""),
            query=str(raw.get("query") or ""),
            intent_summary=str(raw.get("intent_summary") or ""),
            entities=[str(x) for x in raw.get("entities") or []],
            conclusions=[str(x) for x in raw.get("conclusions") or []],
            key_evidence_refs=[str(x) for x in raw.get("key_evidence_refs") or []],
            artifact_refs=[str(x) for x in raw.get("artifact_refs") or []],
            unresolved_questions=[str(x) for x in raw.get("unresolved_questions") or []],
            status=str(raw.get("status") or "completed"),
            created_at=str(raw.get("created_at") or ""),
            parent_run_id=raw.get("parent_run_id"),
        )


def run_summary_path(run_dir: Path) -> Path:
    return Path(run_dir) / "run_summary.json"


def save_run_summary(run_dir: Path, summary: RunSummary) -> Path:
    path = run_summary_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_run_summary(run_dir: Path) -> RunSummary | None:
    path = run_summary_path(run_dir)
    if not path.exists():
        return None
    try:
        return RunSummary.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def load_session_run_summaries(
    session_dir: Path,
    *,
    limit: int = 10,
    exclude_run_id: str = "",
) -> list[RunSummary]:
    """按时间倒序读取 Session 下各 Run 的 Summary（不含当前 Run）。"""
    runs_root = Path(session_dir) / "runs"
    if not runs_root.exists():
        return []
    summaries: list[RunSummary] = []
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir():
            continue
        summary = load_run_summary(run_dir)
        if summary is None:
            continue
        if exclude_run_id and summary.run_id == exclude_run_id:
            continue
        summaries.append(summary)
    summaries.sort(key=lambda s: s.created_at, reverse=True)
    return summaries[: max(0, limit)]


__all__ = [
    "RunSummary",
    "load_run_summary",
    "load_session_run_summaries",
    "run_summary_path",
    "save_run_summary",
]
