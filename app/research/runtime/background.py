"""Bounded, fail-open post-run work.

The answer path must not wait for telemetry exporters, memory extraction or
extended evaluation.  This helper gives those jobs a bounded lifetime and a
single place for warning-level error handling.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

logger = logging.getLogger(__name__)


def schedule_post_run(
    awaitable: Awaitable[Any],
    *,
    name: str,
    timeout_sec: float = 10.0,
) -> asyncio.Task[Any] | None:
    """Schedule an awaitable without extending the user-visible response.

    The task is intentionally fire-and-forget, but it is still bounded and
    logs failures.  A caller outside an active event loop gets a warning and
    the awaitable is closed to avoid an un-awaited coroutine warning.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        logger.warning("post-run task skipped without running loop: %s", name)
        return None

    async def _runner() -> None:
        try:
            await asyncio.wait_for(awaitable, timeout=max(0.1, float(timeout_sec)))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("post-run task timed out: %s (timeout=%ss)", name, timeout_sec)
        except Exception:
            logger.exception("post-run task failed: %s", name)

    task = loop.create_task(_runner(), name=f"post-run:{name}")

    def _done(completed: asyncio.Task[Any]) -> None:
        # Reading the exception prevents "Task exception was never retrieved"
        # if a future implementation changes the error handling above.
        if completed.cancelled():
            return
        try:
            completed.exception()
        except Exception:
            return

    task.add_done_callback(_done)
    return task


__all__ = ["schedule_post_run"]
