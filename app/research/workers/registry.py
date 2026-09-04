"""
WorkerRegistry：按 step_type 直调 Leaf（个人版：Web + File + Synthesis）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

STEP_KINDS: dict[str, str] = {
    "network_search": "create_agent",
    "file_read": "create_agent",
    "research": "create_agent",
    "generate_markdown": "create_agent",
    "convert_pdf": "create_agent",
    "summarize": "create_agent",
}


class UnsupportedTaskType(KeyError):
    """Planner 产出了 WorkerRegistry 未注册的 step_type。"""


DIRECT_STEP_TYPES = frozenset(STEP_KINDS)


@dataclass
class WorkerSpec:
    step_type: str
    kind: str
    graph: Any


class WorkerRegistry:
    def __init__(self, workers: dict[str, Any] | None = None):
        self._workers: dict[str, Any] = dict(workers or {})

    def register(self, step_type: str, graph: Any) -> None:
        self._workers[step_type] = graph

    def get(self, step_type: str) -> Any | None:
        return self._workers.get(step_type)

    def as_step_map(self) -> dict[str, Any]:
        return dict(self._workers)

    def has(self, step_type: str) -> bool:
        return step_type in self._workers and self._workers[step_type] is not None


def worker_tools_for_step(step_type: str) -> list[str]:
    from app.agent.harness.worker_profiles import CONTEXT_TOOLS, resolve_worker_profile, tools_for_profile

    profile = resolve_worker_profile(step_type)
    tools = tools_for_profile(profile)
    extras = {
        "network_search": [
            "internet_search",
            "fetch_url",
            "batch_search",
            "batch_fetch",
            *CONTEXT_TOOLS,
        ],
        "file_read": ["read_file_content", *CONTEXT_TOOLS],
        "research": tools,
        "generate_markdown": ["generate_markdown", "read_file_content", *CONTEXT_TOOLS],
        "convert_pdf": ["convert_md_to_pdf", "generate_markdown", "read_file_content", *CONTEXT_TOOLS],
        "summarize": ["generate_markdown", "read_file_content", *CONTEXT_TOOLS],
    }
    return list(dict.fromkeys(extras.get(step_type, tools)))


def resolve_execute_target(
    step_type: str,
    *,
    workers: dict[str, Any] | None,
    main_agent: Any = None,
    direct_invoke: bool = True,
    profile: str = "",
) -> tuple[Any, str]:
    if not direct_invoke:
        if main_agent is not None:
            return main_agent, "main"
        raise UnsupportedTaskType(step_type)
    if workers:
        if profile and workers.get(profile) is not None:
            return workers[profile], "direct"
        if workers.get(step_type) is not None:
            return workers[step_type], "direct"
    raise UnsupportedTaskType(step_type)


def _local_file_tools() -> dict[str, Any]:
    from app.tools.markdown_tools import generate_markdown
    from app.tools.pdf_tools import convert_md_to_pdf
    from app.tools.upload_file_read_tool import read_file_content

    return {
        "read_file_content": read_file_content,
        "generate_markdown": generate_markdown,
        "convert_md_to_pdf": convert_md_to_pdf,
    }


def build_worker_registry(
    *,
    model: Any,
    checkpointer: Any = None,
    interrupt_on: Mapping[str, bool] | None = None,
    kinds: Mapping[str, str] | None = None,
    worker_model: Any = None,
) -> WorkerRegistry:
    from app.agent.subagents.network_search_agent import build_network_search_agent
    from app.research.workers.factory import create_research_worker, create_synthesis_worker
    from app.research.workers.prompts import RESEARCH_TASK_SYSTEM_PROMPT, SYNTHESIS_SYSTEM_PROMPT
    from app.agent.harness.tool_contract import wrap_tool_with_contract
    from app.agent.harness.worker_profiles import (
        PROFILE_FILE,
        PROFILE_MIXED,
        PROFILE_WEB,
        filter_tools_for_profile,
    )
    from app.tools.artifact_tools import read_artifact, read_evidence
    from app.tools.tavily_tool import internet_search
    from app.tools.fetch_url import fetch_url
    from app.tools.batch_retrieval import batch_search, batch_fetch

    kind_map = dict(STEP_KINDS)
    if kinds:
        kind_map.update(kinds)

    research_model = worker_model if worker_model is not None else model
    net = build_network_search_agent()
    files = _local_file_tools()
    synthesis_prompt = SYNTHESIS_SYSTEM_PROMPT
    hitl = dict(interrupt_on or {})

    registry = WorkerRegistry()

    def _maybe_deep(
        step_type: str,
        tools: list[Any],
        prompt: str,
        *,
        hitl_flags: Mapping[str, bool] | None = None,
        llm: Any = None,
    ) -> Any:
        active_model = llm if llm is not None else model
        kind = kind_map.get(step_type, "create_agent")
        if kind == "create_deep_agent":
            from deepagents import create_deep_agent

            return create_deep_agent(
                model=active_model,
                system_prompt=prompt,
                tools=tools,
                subagents=[],
                checkpointer=checkpointer,
                interrupt_on=dict(hitl_flags or {}),
            )
        if hitl_flags:
            return create_synthesis_worker(
                model=active_model,
                tools=tools,
                system_prompt=prompt,
                checkpointer=checkpointer,
                interrupt_on=hitl_flags,
            )
        return create_research_worker(
            model=active_model,
            tools=tools,
            system_prompt=prompt,
            checkpointer=checkpointer,
        )

    net_tools = [internet_search, fetch_url, batch_search, batch_fetch]
    context_tools = [read_artifact, read_evidence]

    def _contract(tools: list[Any], step_type: str) -> list[Any]:
        wrapped: list[Any] = []
        for tool in tools:
            if tool is None:
                continue
            name = getattr(tool, "name", "")
            if name in {"read_artifact", "read_evidence", "fetch_url", "batch_fetch"}:
                wrapped.append(tool)
            else:
                wrapped.append(wrap_tool_with_contract(tool, tool_name=name, step_type=step_type))
        return wrapped

    net_tools = _contract(net_tools, "network_search") + context_tools

    registry.register(
        "network_search",
        _maybe_deep(
            "network_search",
            net_tools,
            str(net.get("system_prompt") or ""),
            llm=research_model,
        ),
    )
    registry.register(
        PROFILE_WEB,
        _maybe_deep(
            "network_search",
            net_tools,
            str(net.get("system_prompt") or ""),
            llm=research_model,
        ),
    )

    read_tool = files.get("read_file_content")
    md_tool = files.get("generate_markdown")
    pdf_tool = files.get("convert_md_to_pdf")
    read_tools = ([read_tool] if read_tool else []) + context_tools
    md_tools = [t for t in (md_tool, read_tool) if t] + context_tools
    pdf_tools = [t for t in (pdf_tool, md_tool, read_tool) if t] + context_tools

    mixed_tools = filter_tools_for_profile(net_tools + read_tools, PROFILE_MIXED) or net_tools
    registry.register(
        "research",
        _maybe_deep("research", mixed_tools, RESEARCH_TASK_SYSTEM_PROMPT, llm=research_model),
    )
    registry.register(
        PROFILE_MIXED,
        _maybe_deep("research", mixed_tools, RESEARCH_TASK_SYSTEM_PROMPT, llm=research_model),
    )

    registry.register(
        PROFILE_FILE,
        _maybe_deep(
            "file_read",
            read_tools,
            synthesis_prompt,
            hitl_flags={"read_file_content": hitl.get("read_file_content", False)},
        ),
    )
    registry.register(
        "file_read",
        _maybe_deep(
            "file_read",
            read_tools,
            synthesis_prompt,
            hitl_flags={"read_file_content": hitl.get("read_file_content", False)},
        ),
    )
    registry.register(
        "generate_markdown",
        _maybe_deep("generate_markdown", md_tools, synthesis_prompt, hitl_flags=hitl),
    )
    registry.register(
        "summarize",
        _maybe_deep("summarize", md_tools, synthesis_prompt, hitl_flags=hitl),
    )
    registry.register(
        "convert_pdf",
        _maybe_deep("convert_pdf", pdf_tools, synthesis_prompt, hitl_flags=hitl),
    )
    return registry
