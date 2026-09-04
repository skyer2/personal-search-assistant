"""Run-level Search/Fetch cache + single-flight.

Eliminates duplicate Tavily queries and URL fetches across parallel workers.
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

T = TypeVar("T")

_LOCK = threading.RLock()
_SEARCH: dict[str, tuple[float, Any]] = {}
_FETCH: dict[str, tuple[float, Any]] = {}
_INFLIGHT_SEARCH: dict[str, Future] = {}
_INFLIGHT_FETCH: dict[str, Future] = {}

# Soft TTL — Deep Research runs are minutes, not hours
_DEFAULT_TTL_SEC = 900.0
_MAX_ENTRIES = 256


def normalize_query(query: str) -> str:
    return " ".join(str(query or "").lower().split())


def canonical_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _key(*parts: str) -> str:
    blob = "|".join(parts)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _evict(store: dict[str, tuple[float, Any]]) -> None:
    if len(store) <= _MAX_ENTRIES:
        return
    # Drop oldest half
    ordered = sorted(store.items(), key=lambda kv: kv[1][0])
    for k, _ in ordered[: max(1, len(ordered) // 2)]:
        store.pop(k, None)


def _get_fresh(store: dict[str, tuple[float, Any]], key: str, ttl: float) -> Any | None:
    row = store.get(key)
    if not row:
        return None
    ts, value = row
    if time.monotonic() - ts > ttl:
        store.pop(key, None)
        return None
    return value


def cached_call(
    *,
    kind: str,
    cache_key: str,
    producer: Callable[[], T],
    ttl_sec: float = _DEFAULT_TTL_SEC,
) -> T:
    """Return cached value or run producer under single-flight."""
    store = _SEARCH if kind == "search" else _FETCH
    inflight = _INFLIGHT_SEARCH if kind == "search" else _INFLIGHT_FETCH

    with _LOCK:
        hit = _get_fresh(store, cache_key, ttl_sec)
        if hit is not None:
            return hit  # type: ignore[return-value]
        existing = inflight.get(cache_key)
        if existing is not None:
            fut: Future = existing
        else:
            fut = Future()
            inflight[cache_key] = fut
            existing = None

    if existing is not None:
        return fut.result()  # type: ignore[return-value]

    try:
        value = producer()
        with _LOCK:
            store[cache_key] = (time.monotonic(), value)
            _evict(store)
            fut.set_result(value)
        return value
    except Exception as exc:
        with _LOCK:
            fut.set_exception(exc)
        raise
    finally:
        with _LOCK:
            inflight.pop(cache_key, None)


def search_cache_key(query: str, topic: str, max_results: int) -> str:
    return _key("search", normalize_query(query), str(topic), str(max_results))


def fetch_cache_key(url: str, max_chars: int) -> str:
    return _key("fetch", canonical_url(url), str(max_chars))


def clear_retrieval_cache() -> None:
    with _LOCK:
        _SEARCH.clear()
        _FETCH.clear()
        _INFLIGHT_SEARCH.clear()
        _INFLIGHT_FETCH.clear()
