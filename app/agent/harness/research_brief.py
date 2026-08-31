"""
Research Brief — 把多轮对话压成后续 Worker / Supervisor 共用的稳定锚点。

后续步骤主要消费 Brief，而不是整段原始对话。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

_ENTITY_SPLIT = re.compile(r"[,，、/]|以及|和")
_YEAR_RANGE = re.compile(r"(20\d{2})\s*[-~到至]\s*(20\d{2}|今|现在|最新)")
_YEAR = re.compile(r"(20\d{2})")
_COMPARE = re.compile(r"(比较|对比|vs\.?|VS)", re.IGNORECASE)


@dataclass
class ResearchBrief:
    objective: str = ""
    entities: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    time_range: str = ""
    source_policy: str = ""
    deliverable: str = "text"
    citation_policy: str = "claim-evidence"
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    raw_query: str = ""

    def is_empty(self) -> bool:
        return not (self.objective or self.raw_query)

    def to_prompt(self) -> str:
        if self.is_empty():
            return ""
        lines = ["    【Research Brief — 稳定研究说明书】"]
        lines.append(f"    目标: {self.objective or self.raw_query}")
        if self.entities:
            lines.append(f"    实体: {', '.join(self.entities[:12])}")
        if self.dimensions:
            lines.append(f"    维度: {', '.join(self.dimensions[:12])}")
        if self.time_range:
            lines.append(f"    时间范围: {self.time_range}")
        if self.source_policy:
            lines.append(f"    来源策略: {self.source_policy}")
        lines.append(f"    交付物: {self.deliverable}")
        lines.append(f"    引用策略: {self.citation_policy}")
        if self.constraints:
            lines.append("    约束:")
            for item in self.constraints[:8]:
                lines.append(f"      - {item}")
        if self.success_criteria:
            lines.append("    成功标准:")
            for item in self.success_criteria[:6]:
                lines.append(f"      - {item}")
        lines.append("    后续步骤以本 Brief 为准，不要回放完整用户对话。")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str | None) -> "ResearchBrief":
        if not data:
            return cls()
        if isinstance(data, str):
            return cls(objective=data, raw_query=data)
        return cls(
            objective=str(data.get("objective") or ""),
            entities=[str(x) for x in (data.get("entities") or []) if x],
            dimensions=[str(x) for x in (data.get("dimensions") or []) if x],
            time_range=str(data.get("time_range") or ""),
            source_policy=str(data.get("source_policy") or ""),
            deliverable=str(data.get("deliverable") or "text"),
            citation_policy=str(data.get("citation_policy") or "claim-evidence"),
            constraints=[str(x) for x in (data.get("constraints") or []) if x],
            success_criteria=[str(x) for x in (data.get("success_criteria") or []) if x],
            raw_query=str(data.get("raw_query") or ""),
        )


def _split_entities(text: str) -> list[str]:
    parts = [p.strip(" 的") for p in _ENTITY_SPLIT.split(text or "") if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if 1 < len(part) <= 40 and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out[:12]


def compile_research_brief(
    *,
    task_query: str,
    intent: Any | None = None,
    plan_brief: str = "",
) -> ResearchBrief:
    """从意图 / 计划 / 用户问题编译稳定 Brief。不调用 LLM。"""
    query = (task_query or "").strip()
    summary = ""
    deliverable = "text"
    time_range = ""
    constraints: list[str] = []
    entities: list[str] = []
    dimensions: list[str] = []
    source_bits: list[str] = []
    citation_policy = "claim-evidence"

    slots = getattr(intent, "slots", None) if intent is not None else None
    if intent is not None:
        summary = str(getattr(intent, "summary", "") or "")
        deliverable = str(getattr(intent, "deliverable", "text") or "text")
        if getattr(intent, "needs_network", False):
            source_bits.append("web")
        if getattr(intent, "needs_file_read", False):
            source_bits.append("file")
        forbidden = list(getattr(intent, "forbidden_sources", None) or [])
        required = list(getattr(intent, "required_sources", None) or [])
        if forbidden:
            constraints.append("禁止来源: " + ", ".join(str(x) for x in forbidden[:8]))
        if required:
            constraints.append("必须来源: " + ", ".join(str(x) for x in required[:8]))
    if slots is not None:
        time_range = str(getattr(slots, "time_range", "") or time_range)
        topic = str(getattr(slots, "topic", "") or "")
        if topic:
            entities.extend(_split_entities(topic))
        if getattr(slots, "require_citations", False):
            citation_policy = "inline-[n]+references"
            constraints.append("正文含数字的结论必须带 [n] 引用")

    if not time_range:
        span = _YEAR_RANGE.search(query)
        if span:
            time_range = span.group(0)
        else:
            years = _YEAR.findall(query)
            if years:
                time_range = years[-1]

    if "不要使用" in query or "不得使用" in query:
        constraints.append("遵守用户对资料年份/来源的排除要求")
    if _COMPARE.search(query):
        dimensions.append("横向比较")
    for dim, keys in [
        ("市场规模", ("市场规模", "市场空间", "CAGR")),
        ("商业化", ("商业化", "量产", "交付", "营收")),
        ("技术路线", ("技术", "方案", "架构")),
        ("竞争格局", ("竞争", "对手", "格局")),
        ("风险", ("风险", "监管", "合规")),
    ]:
        if any(k in query or k in summary for k in keys) and dim not in dimensions:
            dimensions.append(dim)

    if not entities:
        entities = _split_entities(summary or query)[:8]

    objective = (plan_brief or summary or query).strip()
    success = [
        "结论可追溯到 evidence_id / artifact_id",
        "冲突数字并列保留，不自行消解",
    ]
    if deliverable in {"md", "pdf"}:
        success.append("交付物写入当前 session 工作目录")

    return ResearchBrief(
        objective=objective[:500],
        entities=entities,
        dimensions=dimensions or ["关键事实"],
        time_range=time_range,
        source_policy="+".join(source_bits) if source_bits else "web",
        deliverable=deliverable,
        citation_policy=citation_policy,
        constraints=constraints,
        success_criteria=success,
        raw_query=query[:500],
    )
