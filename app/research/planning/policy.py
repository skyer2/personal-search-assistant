"""来源约束与规划分流：个人版默认 web + file。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.harness.state import TaskIntent

WEB_FORBIDDEN_MARKERS = (
    "不要联网",
    "不要搜索",
    "禁止联网",
    "不要上网",
    "不要公开资料",
    "禁止搜索",
    "不要用网络",
)
FILE_FORBIDDEN_MARKERS = ("不要读附件", "不要上传文件")

COMPARE_MARKERS = ("比较", "对比", " vs ", " VS ", "versus", "横向比较")
LANDSCAPE_MARKERS = ("竞争格局", "多维度", "综合对比")

SOURCE_TOOLS: dict[str, tuple[str, ...]] = {
    "web": ("internet_search",),
    "file": ("read_file_content",),
}

TOOL_TO_SOURCE: dict[str, str] = {
    tool: source for source, tools in SOURCE_TOOLS.items() for tool in tools
}

STEP_TO_SOURCE: dict[str, str] = {
    "network_search": "web",
    "file_read": "file",
}


@dataclass
class SourcePolicy:
    forbidden_sources: list[str] = field(default_factory=list)
    required_sources: list[str] = field(default_factory=list)

    @property
    def allowed_sources(self) -> list[str]:
        all_sources = ["web", "file"]
        forbidden = set(self.forbidden_sources)
        return [s for s in all_sources if s not in forbidden]

    def allows(self, source: str) -> bool:
        return source not in self.forbidden_sources

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "forbidden_sources": list(self.forbidden_sources),
            "required_sources": list(self.required_sources),
            "allowed_sources": list(self.allowed_sources),
        }


def parse_source_policy(query: str) -> SourcePolicy:
    q = query or ""
    forbidden: list[str] = []
    required: list[str] = []

    def _add(bucket: list[str], item: str) -> None:
        if item not in bucket:
            bucket.append(item)

    if any(m in q for m in WEB_FORBIDDEN_MARKERS):
        _add(forbidden, "web")
    if any(m in q for m in FILE_FORBIDDEN_MARKERS):
        _add(forbidden, "file")
    return SourcePolicy(forbidden_sources=forbidden, required_sources=required)


def apply_source_policy(intent: TaskIntent) -> TaskIntent:
    policy = parse_source_policy(intent.raw_query)
    intent.forbidden_sources = list(policy.forbidden_sources)
    intent.required_sources = list(policy.required_sources)
    if "web" in policy.forbidden_sources:
        intent.needs_network = False
    if "file" in policy.forbidden_sources:
        intent.needs_file_read = False
    if not intent.needs_network and not intent.needs_file_read:
        if policy.allows("web"):
            intent.needs_network = True
    return intent


def intent_allowed_sources(intent: TaskIntent) -> list[str]:
    policy = parse_source_policy(intent.raw_query)
    allowed = set(policy.allowed_sources)
    requested: list[str] = []
    if intent.needs_network:
        requested.append("web")
    if intent.needs_file_read:
        requested.append("file")
    picked = [s for s in requested if s in allowed]
    if picked:
        return picked
    if policy.required_sources:
        return [s for s in policy.required_sources if s in allowed]
    return [s for s in ("web", "file") if s in allowed]


def tools_for_sources(sources: list[str]) -> list[str]:
    tools: list[str] = []
    for source in sources:
        for tool in SOURCE_TOOLS.get(source, ()):
            if tool not in tools:
                tools.append(tool)
    return tools


def source_for_tool(tool: str) -> str | None:
    return TOOL_TO_SOURCE.get(tool)


def extract_compare_entities(query: str) -> list[str]:
    text = query or ""
    for marker in COMPARE_MARKERS:
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1]
        tail = tail.split("的")[0].split("在")[0].split("，")[0].split("。")[0]
        raw = (
            tail.replace("、", "/")
            .replace("和", "/")
            .replace("与", "/")
            .replace("，", "/")
            .replace(",", "/")
        )
        parts = [p.strip(" 的了呢吗？? ") for p in raw.split("/") if p.strip()]
        cleaned = [p for p in parts if 1 < len(p) <= 40]
        if len(cleaned) >= 2:
            return cleaned[:6]
    return []


def select_planning_mode(intent: TaskIntent, *, hybrid_enabled: bool = True) -> str:
    if not hybrid_enabled:
        sources = sum([intent.needs_network, intent.needs_file_read])
        return "direct" if sources <= 1 else "template"

    query = intent.raw_query or ""
    if any(m in query for m in COMPARE_MARKERS + LANDSCAPE_MARKERS):
        return "dynamic"
    if len(extract_compare_entities(query)) >= 2:
        return "dynamic"

    sources = sum([intent.needs_network, intent.needs_file_read])
    if sources <= 1:
        return "direct"
    return "template"
