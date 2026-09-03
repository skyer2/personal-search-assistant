"""Map internal AgentEvent attributes onto OpenTelemetry GenAI semantic conventions.

Domain code keeps AgentEvent fields. Only the OTel exporter speaks gen_ai.*.
"""

from __future__ import annotations

from typing import Any

from app.observability.context import ObservabilityContext


def _str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (str, int, float, bool)):
        return value if isinstance(value, (int, float, bool)) else str(value)
    return str(value)[:500]


def infer_provider(model: str) -> str:
    lowered = (model or "").lower()
    if "gpt" in lowered or "openai" in lowered:
        return "openai"
    if "claude" in lowered or "anthropic" in lowered:
        return "anthropic"
    if "gemini" in lowered or "google" in lowered:
        return "google"
    if "qwen" in lowered or "dashscope" in lowered:
        return "alibaba"
    if "glm" in lowered or "zhipu" in lowered:
        return "zhipu"
    if "deepseek" in lowered:
        return "deepseek"
    return "unknown"


def map_genai_attributes(
    name: str,
    attributes: dict[str, Any] | None = None,
    ctx: ObservabilityContext | None = None,
) -> dict[str, Any]:
    raw = dict(attributes or {})
    mapped: dict[str, Any] = {
        "gen_ai.agent.name": "research-agent-harness",
        "gen_ai.workflow.name": "research-harness",
    }
    if ctx is not None:
        mapped["gen_ai.conversation.id"] = ctx.session_id
        # run_id is invocation id — keep it off gen_ai.agent.id
        mapped["gen_ai.agent.version"] = ctx.git_sha or ctx.config_hash or "dev"
        mapped["agent.run_id"] = ctx.run_id
        mapped["agent.session_id"] = ctx.session_id
        mapped["agent.trace_id"] = ctx.trace_id
        if ctx.task_id:
            mapped["agent.task_id"] = ctx.task_id
        if ctx.plan_version is not None:
            mapped["agent.plan_version"] = ctx.plan_version
        if ctx.git_sha:
            mapped["agent.git_sha"] = ctx.git_sha
        if ctx.config_hash:
            mapped["agent.config_hash"] = ctx.config_hash
        if ctx.variant:
            mapped["agent.variant"] = ctx.variant

    lowered = name.lower()
    is_tool = lowered.startswith("tool.") or raw.get("tool_name")
    is_gen = lowered.startswith("gen_ai") or lowered in {"chat", "generation"}
    is_retrieval = lowered.startswith("retrieval.") or str(raw.get("tool_name") or "").lower() in {
        "internet_search",
        "web_search",
        "tavily_search",
        "search",
    }

    if is_gen:
        mapped["gen_ai.operation.name"] = "chat"
        model = str(raw.get("model") or raw.get("request_model") or "")
        if model:
            mapped["gen_ai.request.model"] = model
            mapped["gen_ai.response.model"] = str(raw.get("response_model") or model)
            mapped["gen_ai.provider.name"] = infer_provider(model)
        if raw.get("prompt_tokens") is not None:
            mapped["gen_ai.usage.input_tokens"] = int(raw.get("prompt_tokens") or 0)
        if raw.get("completion_tokens") is not None:
            mapped["gen_ai.usage.output_tokens"] = int(raw.get("completion_tokens") or 0)
        if raw.get("cache_read_tokens") is not None:
            mapped["gen_ai.usage.cache_read.input_tokens"] = int(raw.get("cache_read_tokens") or 0)
        if raw.get("finish_reason"):
            mapped["gen_ai.response.finish_reasons"] = str(raw.get("finish_reason"))

    if is_retrieval:
        mapped["gen_ai.operation.name"] = "retrieval"
        tool_name = str(raw.get("tool_name") or name.split(".", 1)[-1])
        mapped["gen_ai.tool.name"] = tool_name
        if raw.get("tool_call_id"):
            mapped["gen_ai.tool.call.id"] = str(raw.get("tool_call_id"))
    elif is_tool:
        mapped["gen_ai.operation.name"] = "execute_tool"
        tool_name = str(raw.get("tool_name") or name.split(".", 1)[-1])
        mapped["gen_ai.tool.name"] = tool_name
        if raw.get("tool_call_id"):
            mapped["gen_ai.tool.call.id"] = str(raw.get("tool_call_id"))

    if lowered in {"research.run", "run"} or lowered.startswith("research."):
        mapped["gen_ai.operation.name"] = mapped.get("gen_ai.operation.name") or "invoke_agent"

    # Preserve a small set of internal attributes without dumping prompts.
    for key in (
        "tool_name",
        "tool_call_id",
        "model",
        "worker_runtime",
        "step_type",
        "objective",
        "failure.stage",
        "failure.type",
        "fail_reason",
        "usage_missing",
        "phase",
        "prompt_template_id",
        "prompt_template_version",
        "prompt_ref",
        "output_ref",
        "input_hash",
        "output_hash",
        "temperature",
        "response_format",
        "result_count",
        "result_bytes",
    ):
        value = _str(raw.get(key))
        if value is not None:
            mapped[f"agent.{key}"] = value

    return mapped
