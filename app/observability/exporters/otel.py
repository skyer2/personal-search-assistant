"""OpenTelemetry tracer + optional OTLP export to Langfuse.

Parent/child MUST be established via OTel Context, not ``parent.span.id`` attributes.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from app.observability.context import current_context
from app.observability.exporters.genai import map_genai_attributes

_tracer: Any = None
_provider: Any = None
_initialized = False


def _langfuse_otlp_endpoint() -> str:
    explicit = (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("HARNESS_OTEL_OTLP_ENDPOINT") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = (os.getenv("LANGFUSE_HOST") or "").strip().rstrip("/")
    if host:
        return f"{host}/api/public/otel"
    return ""


def _langfuse_otlp_headers() -> dict[str, str]:
    raw = (os.getenv("OTEL_EXPORTER_OTLP_HEADERS") or "").strip()
    if raw:
        headers: dict[str, str] = {}
        for part in raw.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                headers[key.strip()] = value.strip()
        if headers:
            return headers
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    if public_key and secret_key:
        token = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {}


def reset_for_tests() -> None:
    global _tracer, _provider, _initialized
    _tracer = None
    _provider = None
    _initialized = False
    try:
        from opentelemetry import trace
        from opentelemetry.util._once import Once

        trace._TRACER_PROVIDER = None
        if hasattr(trace, "_TRACER_PROVIDER_SET_ONCE"):
            trace._TRACER_PROVIDER_SET_ONCE = Once()
    except Exception:
        pass


def init_otel(exporter: Any | None = None, *, force: bool = False) -> None:
    """Best-effort: 无 SDK / 无 endpoint 时静默降级。"""
    global _tracer, _provider, _initialized
    if _initialized and not force:
        return
    _initialized = True
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
    except ImportError:
        return

    resource = Resource.create(
        {
            "service.name": "research-agent-harness",
            "service.namespace": "agent.harness",
            "gen_ai.system": "research-agent-harness",
        }
    )
    provider = TracerProvider(resource=resource)
    resolved = exporter
    if resolved is None:
        endpoint = _langfuse_otlp_endpoint()
        headers = _langfuse_otlp_headers()
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                resolved = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)
            except Exception as exc:
                print(f"[Observability] OTLP exporter init failed: {exc}")
        if resolved is None and os.getenv("HARNESS_OTEL_CONSOLE") == "1":
            resolved = ConsoleSpanExporter()
    if resolved is not None:
        processor = SimpleSpanProcessor(resolved) if force else BatchSpanProcessor(resolved)
        provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    _provider = provider
    _tracer = trace.get_tracer("research-agent-harness")


def get_tracer() -> Any:
    if not _initialized:
        init_otel()
    return _tracer


def start_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    *,
    parent: Any | None = None,
    context_attrs: dict[str, Any] | None = None,
) -> Any:
    tracer = get_tracer()
    if tracer is None:
        return None
    try:
        from opentelemetry import trace

        otel_context = trace.set_span_in_context(parent) if parent is not None else None
        span = tracer.start_span(name, context=otel_context)
        merged = map_genai_attributes(name, {**(context_attrs or {}), **(attributes or {})}, current_context())
        for key, value in merged.items():
            if value is None:
                continue
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(key, value)
            else:
                span.set_attribute(key, str(value)[:500])
        return span
    except Exception as exc:
        print(f"[Observability] start_span failed: {exc}")
        return None


def end_span(span: Any, *, status: str = "ok", attributes: dict[str, Any] | None = None) -> None:
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        for key, value in (attributes or {}).items():
            if value is None:
                continue
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(key, value)
        if status in {"failed", "error", "cancelled", "fail"}:
            span.set_status(Status(StatusCode.ERROR, status))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end()
    except Exception as exc:
        print(f"[Observability] end_span failed: {exc}")


def flush_otel() -> None:
    if _provider is None:
        return
    try:
        _provider.force_flush(timeout_millis=2000)
    except Exception:
        pass
