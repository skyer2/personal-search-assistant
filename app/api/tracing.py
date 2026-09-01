"""
Langfuse 可观测性集成

可选启用：未配置 LANGFUSE_* 环境变量时自动降级，不影响主链路执行。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import find_dotenv, load_dotenv

from app.config.loader import get_harness_config

load_dotenv(find_dotenv())

_langfuse_client: Any = None
_langfuse_handler_cls: Any = None
_enabled: Optional[bool] = None


def is_langfuse_enabled() -> bool:
    global _enabled
    if _enabled is not None:
        return _enabled
    _enabled = bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )
    return _enabled


def _get_langfuse_client() -> Any:
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client
    if not is_langfuse_enabled():
        return None
    try:
        from langfuse import Langfuse

        _langfuse_client = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        return _langfuse_client
    except Exception as exc:
        print(f"[Tracing] Langfuse client init failed: {exc}")
        return None


def _get_callback_handler_class() -> Any:
    global _langfuse_handler_cls
    if _langfuse_handler_cls is not None:
        return _langfuse_handler_cls
    try:
        from langfuse.langchain import CallbackHandler

        _langfuse_handler_cls = CallbackHandler
        return _langfuse_handler_cls
    except ImportError:
        try:
            from langfuse.callback import CallbackHandler

            _langfuse_handler_cls = CallbackHandler
            return _langfuse_handler_cls
        except ImportError as exc:
            print(f"[Tracing] Langfuse CallbackHandler unavailable: {exc}")
            return None


def create_langfuse_handler(
    session_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> Any:
    """创建 LangChain CallbackHandler；未启用时返回 None。"""
    if not is_langfuse_enabled():
        return None
    handler_cls = _get_callback_handler_class()
    if handler_cls is None:
        return None
    try:
        return handler_cls(
            session_id=session_id,
            metadata={
                "project": "research-agent-harness",
                "harness_version": "2.0",
                **(metadata or {}),
            },
        )
    except TypeError:
        return handler_cls(session_id=session_id)


def build_run_config(
    session_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构建带 Langfuse callback 与 Usage 追踪的 LangGraph 运行配置。"""
    config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
    callbacks: list[Any] = []

    harness_config = get_harness_config()
    if harness_config.langfuse_enabled:
        handler = create_langfuse_handler(session_id, metadata)
        if handler is not None:
            callbacks.append(handler)

    from app.agent.harness.usage_tracker import build_usage_callback

    usage_session_id = str((metadata or {}).get("usage_session_id") or session_id)
    usage_cb = build_usage_callback(
        usage_session_id,
        phase=(metadata or {}).get("phase", ""),
    )
    if usage_cb is not None:
        callbacks.append(usage_cb)

    if callbacks:
        config["callbacks"] = callbacks
    return config


class HarnessTracer:
    """Harness 阶段级 trace，补充 CallbackHandler 之外的宏观阶段观测。"""

    def __init__(self, session_id: str, task_query: str):
        self.session_id = session_id
        self.task_query = task_query
        self._trace: Any = None
        self._spans: dict[str, Any] = {}

    def start(self) -> None:
        client = _get_langfuse_client()
        if client is None:
            return
        try:
            self._trace = client.trace(
                name="harness_run",
                session_id=self.session_id,
                input={"query": self.task_query},
                metadata={"component": "AgentHarness"},
            )
        except Exception as exc:
            print(f"[Tracing] harness trace start failed: {exc}")

    def phase_start(self, phase: str, data: Optional[dict[str, Any]] = None) -> None:
        if self._trace is None:
            return
        try:
            self._spans[phase] = self._trace.span(
                name=phase,
                input=data or {},
                metadata={"status": "start"},
            )
        except Exception as exc:
            print(f"[Tracing] phase span start failed: {exc}")

    def phase_end(
        self,
        phase: str,
        status: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        span = self._spans.pop(phase, None)
        if span is None:
            return
        try:
            span.end(output={"status": status, **(data or {})})
        except Exception as exc:
            print(f"[Tracing] phase span end failed: {exc}")

    def finish(self, result: dict[str, Any]) -> None:
        if self._trace is None:
            return
        try:
            self._trace.update(output=result)
            client = _get_langfuse_client()
            if client is not None:
                client.flush()
        except Exception as exc:
            print(f"[Tracing] harness trace finish failed: {exc}")
