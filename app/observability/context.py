"""Run-scoped observability identity (session / run / trace / span)."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from app.observability.events import new_id

_current: ContextVar["ObservabilityContext | None"] = ContextVar(
    "agent_obs_context",
    default=None,
)


@dataclass
class ObservabilityContext:
    session_id: str
    run_id: str
    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    root_span_id: str | None = None
    seq: int = 0
    plan_version: int | None = None
    task_id: str | None = None
    attempt: int | None = None
    git_sha: str = ""
    config_hash: str = ""
    variant: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def child(self, **overrides: Any) -> "ObservabilityContext":
        payload = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "root_span_id": self.root_span_id,
            "seq": self.seq,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "git_sha": self.git_sha,
            "config_hash": self.config_hash,
            "variant": self.variant,
            "extra": dict(self.extra),
        }
        payload.update({k: v for k, v in overrides.items() if v is not None})
        return ObservabilityContext(**payload)


def current_context() -> ObservabilityContext | None:
    return _current.get()


def bind_run(
    *,
    session_id: str,
    run_id: str | None = None,
    trace_id: str | None = None,
    git_sha: str = "",
    config_hash: str = "",
    variant: str = "",
) -> tuple[ObservabilityContext, Token]:
    ctx = ObservabilityContext(
        session_id=session_id,
        run_id=run_id or session_id,
        trace_id=trace_id or new_id(32),
        git_sha=git_sha,
        config_hash=config_hash,
        variant=variant,
    )
    token = _current.set(ctx)
    return ctx, token


def reset_run(token: Token | None) -> None:
    if token is not None:
        _current.reset(token)
    else:
        _current.set(None)


def set_context(ctx: ObservabilityContext) -> Token:
    return _current.set(ctx)


def bind_worker(
    *,
    task_id: str,
    attempt: int | None = None,
    plan_version: int | None = None,
) -> tuple[ObservabilityContext | None, Token | None]:
    """并行 Worker 各自一份 context，避免抢占父 run 的 span_id。"""
    parent = current_context()
    if parent is None:
        return None, None
    child = parent.child(
        task_id=task_id,
        attempt=attempt if attempt is not None else parent.attempt,
        plan_version=plan_version if plan_version is not None else parent.plan_version,
        parent_span_id=parent.root_span_id or parent.span_id,
        span_id=new_id(),
    )
    token = set_context(child)
    return parent, token
