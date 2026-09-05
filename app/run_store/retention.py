"""Retention：按保留策略清理 Run 中间数据。

策略（对齐评审第十五节）：
- temporary / intermediate artifacts（artifacts、evidence、state）：默认 7 天
- raw trace：默认 90 天（Session 级 JSONL，由 Session 删除级联处理）
- deliverables + RunSummary：用户控制，不自动删除
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.observability.paths import APP_ROOT
from app.run_store.models import TERMINAL_STATUSES
from app.run_store.service import RunStore


@dataclass(frozen=True)
class RetentionPolicy:
    intermediate_retention_days: int = 7
    trace_retention_days: int = 90


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def apply_retention(
    store: RunStore,
    *,
    policy: RetentionPolicy | None = None,
    output_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """清理已终结 Run 中超过保留期的中间数据；保留 deliverables 与 run_summary。"""
    policy = policy or RetentionPolicy()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=policy.intermediate_retention_days)
    output = output_root or (APP_ROOT / "output")
    purged_runs = 0
    purged_dirs: list[str] = []

    sessions = store.list_sessions(include_archived=True)
    for session in sessions:
        for run in store.list_runs(session.session_id):
            if run.status not in TERMINAL_STATUSES:
                continue
            ended = _parse_iso(run.ended_at or run.created_at)
            if ended is None or ended >= cutoff:
                continue
            run_dir = output / f"session_{session.session_id}" / "runs" / run.run_id
            if not run_dir.exists():
                continue
            touched = False
            for sub in ("artifacts", "evidence", "state"):
                target = run_dir / sub
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                    purged_dirs.append(str(target))
                    touched = True
            if touched:
                purged_runs += 1

    return {
        "purged_runs": purged_runs,
        "purged_dirs": purged_dirs,
        "policy": {
            "intermediate_retention_days": policy.intermediate_retention_days,
            "trace_retention_days": policy.trace_retention_days,
        },
    }


__all__ = ["RetentionPolicy", "apply_retention"]
