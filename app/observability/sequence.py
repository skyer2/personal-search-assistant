"""Run-scoped monotonic sequence — shared by all child contexts of one run."""

from __future__ import annotations

import threading


class RunSequence:
    """Atomic counter. Child ObservabilityContexts must share the same instance.

    Parallel workers must not copy an integer ``seq``; they call ``next()`` on
    this object so a run's event stream is globally unique and never rewinds.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def current(self) -> int:
        with self._lock:
            return self._value
