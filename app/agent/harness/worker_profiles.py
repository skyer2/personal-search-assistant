"""
Worker Profiles — 个人搜索助手：Web / File / Mixed / Synthesis。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

WEB_TOOLS = ("internet_search",)
FILE_READ_TOOLS = ("read_file_content",)
CONTEXT_TOOLS = ("read_artifact", "read_evidence")
SYNTHESIS_EXTRA = ("generate_markdown",)

PROFILE_WEB = "web_researcher"
PROFILE_FILE = "file_researcher"
PROFILE_MIXED = "mixed_researcher"
PROFILE_SYNTHESIS = "synthesis_editor"


@dataclass(frozen=True)
class WorkerProfile:
    name: str
    tools: tuple[str, ...]
    step_types: tuple[str, ...]


PROFILES: dict[str, WorkerProfile] = {
    PROFILE_WEB: WorkerProfile(
        name=PROFILE_WEB,
        tools=WEB_TOOLS + CONTEXT_TOOLS,
        step_types=("network_search", "research"),
    ),
    PROFILE_FILE: WorkerProfile(
        name=PROFILE_FILE,
        tools=FILE_READ_TOOLS + CONTEXT_TOOLS,
        step_types=("file_read", "research"),
    ),
    PROFILE_MIXED: WorkerProfile(
        name=PROFILE_MIXED,
        tools=WEB_TOOLS + FILE_READ_TOOLS + CONTEXT_TOOLS,
        step_types=("research",),
    ),
    PROFILE_SYNTHESIS: WorkerProfile(
        name=PROFILE_SYNTHESIS,
        tools=("generate_markdown", "read_file_content") + CONTEXT_TOOLS,
        step_types=("generate_markdown", "summarize", "convert_pdf"),
    ),
}


def _has_any(tools: set[str], group: Iterable[str]) -> bool:
    return any(name in tools for name in group)


def resolve_worker_profile(
    step_type: str,
    allowed_tools: Iterable[str] | None = None,
) -> str:
    kind = (step_type or "").strip().lower()
    if kind == "network_search":
        return PROFILE_WEB
    if kind == "file_read":
        return PROFILE_FILE
    if kind in {"generate_markdown", "summarize", "convert_pdf"}:
        return PROFILE_SYNTHESIS
    tools = {str(t) for t in (allowed_tools or []) if t}
    if not tools:
        return PROFILE_MIXED if kind == "research" else PROFILE_MIXED
    flags = [
        _has_any(tools, WEB_TOOLS),
        _has_any(tools, FILE_READ_TOOLS),
    ]
    if sum(1 for x in flags if x) <= 1:
        if flags[0]:
            return PROFILE_WEB
        if flags[1]:
            return PROFILE_FILE
    return PROFILE_MIXED


def tools_for_profile(profile: str) -> list[str]:
    spec = PROFILES.get(profile)
    if spec is None:
        spec = PROFILES[PROFILE_MIXED]
    return list(spec.tools)


def filter_tools_for_profile(tools: list[Any], profile: str) -> list[Any]:
    allowed = set(tools_for_profile(profile))
    filtered = [tool for tool in tools if getattr(tool, "name", "") in allowed]
    return filtered or list(tools)
