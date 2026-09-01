"""Agent Flight Recorder — 统一 Agent-native 可观测性入口。

业务代码只 emit / span 一次；JSONL、WebSocket、OTel/Langfuse、Metrics 都是 exporter。
"""

from app.observability.context import (
    ObservabilityContext,
    bind_run,
    current_context,
    reset_run,
)
from app.observability.events import (
    EVENT_VOCABULARY,
    AgentEvent,
    EventType,
    span_identity,
)
from app.observability.recorder import AgentTelemetry, get_recorder

__all__ = [
    "EVENT_VOCABULARY",
    "AgentEvent",
    "AgentTelemetry",
    "EventType",
    "ObservabilityContext",
    "bind_run",
    "current_context",
    "get_recorder",
    "reset_run",
    "span_identity",
]
