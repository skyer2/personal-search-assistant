"""Fail-open observability helpers: never crash business code, always log & count."""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("observability")


def safe_observe(event_name: str, fn: Callable[[], Any]) -> Any:
    """Execute an observability emit function; swallow exceptions but log and count."""
    try:
        return fn()
    except Exception:
        logger.exception("observability emit failed: %s", event_name)
        try:
            from app.observability.metrics import get_metrics

            get_metrics().inc("harness.observability.emit_failed")
        except Exception:
            pass
    return None
