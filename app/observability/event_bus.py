"""Pluggable AgentEvent bus for live fanout (in-process default, optional Redis).

JSONL/OTel remain durable exporters. This bus is only for real-time delivery across
API workers when OBS_EVENT_BUS=redis and REDIS_URL is set.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Protocol


class EventBus(Protocol):
    def publish(self, channel: str, payload: dict[str, Any]) -> None: ...

    def subscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None: ...


class InProcessEventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def publish(self, channel: str, payload: dict[str, Any]) -> None:
        with self._lock:
            handlers = list(self._handlers.get(channel, []))
        for handler in handlers:
            try:
                handler(payload)
            except Exception as exc:
                print(f"[EventBus] handler failed: {exc}")

    def subscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._handlers.setdefault(channel, []).append(handler)


class RedisEventBus:
    """Best-effort Redis pub/sub. Falls back silently if redis is unavailable."""

    def __init__(self, url: str, *, channel_prefix: str = "harness:events:") -> None:
        import uuid

        self.url = url
        self.channel_prefix = channel_prefix
        self._origin = uuid.uuid4().hex
        self._local = InProcessEventBus()
        self._redis = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._subscribed: set[str] = set()
        try:
            import redis

            self._redis = redis.Redis.from_url(url, decode_responses=True)
            self._redis.ping()
        except Exception as exc:
            print(f"[EventBus] redis unavailable, using in-process only: {exc}")
            self._redis = None

    def _channel(self, channel: str) -> str:
        return f"{self.channel_prefix}{channel}"

    def publish(self, channel: str, payload: dict[str, Any]) -> None:
        body = dict(payload)
        body["_origin"] = self._origin
        self._local.publish(channel, body)
        if self._redis is None:
            return
        try:
            self._redis.publish(self._channel(channel), json.dumps(body, ensure_ascii=False, default=str))
        except Exception as exc:
            print(f"[EventBus] redis publish failed: {exc}")

    def subscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._local.subscribe(channel, handler)
        if self._redis is None:
            return
        self._subscribed.add(channel)
        if self._thread is not None and self._thread.is_alive():
            return

        def _loop() -> None:
            try:
                pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
                for name in list(self._subscribed):
                    pubsub.subscribe(self._channel(name))
                while not self._stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    raw = message.get("data")
                    try:
                        payload = json.loads(raw) if isinstance(raw, str) else {}
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("_origin") == self._origin:
                        continue
                    channel_name = str(message.get("channel") or "").replace(self.channel_prefix, "", 1)
                    self._local.publish(channel_name or channel, payload)
            except Exception as exc:
                print(f"[EventBus] redis subscribe loop stopped: {exc}")

        self._thread = threading.Thread(target=_loop, name="obs-event-bus", daemon=True)
        self._thread.start()


_BUS: EventBus | None = None


def get_event_bus() -> EventBus:
    global _BUS
    if _BUS is not None:
        return _BUS
    mode = (os.getenv("OBS_EVENT_BUS") or os.getenv("HARNESS_OBS_EVENT_BUS") or "inprocess").strip().lower()
    redis_url = (os.getenv("REDIS_URL") or os.getenv("OBS_REDIS_URL") or "").strip()
    if mode == "redis" and redis_url:
        _BUS = RedisEventBus(redis_url)
    else:
        _BUS = InProcessEventBus()
    return _BUS


def reset_event_bus(bus: EventBus | None = None) -> None:
    global _BUS
    _BUS = bus
