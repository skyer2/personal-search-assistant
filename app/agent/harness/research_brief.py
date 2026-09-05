"""
Research Brief — Task Understanding 的稳定中间表示（IR），不是 Intent，也不是 Plan。

分层：
  User Query
    → Task Understanding（规则硬约束 + LLM 语义补全）
    → Research Brief / ResearchSpec   ← 本模块
    → Planner（Objective DAG）
    → Harness Runtime Policy（budget / retry / concurrency）

Brief 回答：What exactly does success mean?
Planner 回答：How should the research be decomposed?
Intent/Routing 只回答：这是哪类任务？（对本系统价值有限）

后续 Plan / Progress / Worker 主要消费 Brief，而不是整段原始对话。
Quick / direct 路径可不编译完整 Brief。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

_ENTITY_SPLIT = re.compile(r"[,，、/]|以及|和")
_YEAR_RANGE = re.compile(r"(20\d{2})\s*[-~到至]\s*(20\d{2}|今|现在|最新)")
_YEAR = re.compile(r"(20\d{2})")
_COMPARE = re.compile(r"(比较|对比|vs\.?|VS|versus)", re.IGNORECASE)
_DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|gov|edu|io|cn)\b", re.I)
_ENTITY_STOP = {
    "进度",
    "动态",
    "报告",
    "资料",
    "差异",
    "比较",
    "对比",
    "趋势",
    "搜索",
    "检索",
    "查询",
    "生成",
    "输出",
    "markdown",
    "pdf",
}

PRIMARY_MARKERS = ("官方", "一手", "白皮书", "官网", "原论文", "primary", "first-party")
RECENT_MARKERS = ("最新", "今天", "现在", "刚刚", "实时")
THOROUGH_MARKERS = ("多维度", "综合", "全面", "深入", "官方", "白皮书", "对照")
PRIMARY_SOURCE_HINTS = (
    ".gov",
    "gov.",
    "官方",
    "白皮书",
    "arxiv.org",
    "ieee.org",
    "nature.com",
    "sec.gov",
    "investor",
    "/ir/",
    "白皮",
    "官网",
)


@dataclass
class ResearchSubject:
    """One independent research subject and its lexical aliases."""

    canonical: str
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchBrief:
    """Research Spec / Task Understanding IR（语义合同）。"""

    objective: str = ""
    entities: list[str] = field(default_factory=list)
    subjects: list[ResearchSubject] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    time_range: str = ""
    source_policy: str = ""
    deliverable: str = "text"
    delivery_requirements: list[str] = field(default_factory=list)
    citation_policy: str = "claim-evidence"
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    raw_query: str = ""
    depth: str = "standard"  # shallow | standard | thorough
    freshness: str = "any"  # any | recent | 约束原文
    prefer_primary: bool = False
    preferred_domains: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.objective or self.raw_query)

    def to_prompt(self) -> str:
        if self.is_empty():
            return ""
        lines = ["    【Research Brief — Task Understanding IR】"]
        lines.append(f"    目标: {self.objective or self.raw_query}")
        if self.entities:
            lines.append(f"    实体: {', '.join(self.entities[:12])}")
        if self.subjects:
            lines.append(
                "    独立主题: "
                + "; ".join(
                    f"{subject.canonical} ({', '.join(subject.aliases)})"
                    if subject.aliases else subject.canonical
                    for subject in self.subjects[:8]
                )
            )
        if self.dimensions:
            lines.append(f"    维度: {', '.join(self.dimensions[:12])}")
        if self.time_range:
            lines.append(f"    时间范围: {self.time_range}")
        lines.append(f"    深度: {self.depth}")
        lines.append(f"    新鲜度: {self.freshness}")
        if self.source_policy:
            lines.append(f"    来源策略: {self.source_policy}")
        if self.prefer_primary:
            lines.append("    来源偏好: 优先官方/一手")
        if self.preferred_domains:
            lines.append(f"    提示域名: {', '.join(self.preferred_domains[:8])}")
        lines.append(f"    交付物: {self.deliverable}")
        if self.delivery_requirements:
            lines.append(f"    交付要求: {', '.join(self.delivery_requirements[:6])}")
        lines.append(f"    引用策略: {self.citation_policy}")
        if self.constraints:
            lines.append("    约束:")
            for item in self.constraints[:8]:
                lines.append(f"      - {item}")
        if self.success_criteria:
            lines.append("    成功标准:")
            for item in self.success_criteria[:6]:
                lines.append(f"      - {item}")
        if self.ambiguities:
            lines.append("    歧义/待澄清:")
            for item in self.ambiguities[:4]:
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
        depth = str(data.get("depth") or "standard")
        if depth not in {"shallow", "standard", "thorough"}:
            depth = "standard"
        freshness = str(data.get("freshness") or "any") or "any"
        subjects = []
        for raw in data.get("subjects") or []:
            if isinstance(raw, str) and raw.strip():
                subjects.append(ResearchSubject(canonical=raw.strip()))
            elif isinstance(raw, dict) and str(raw.get("canonical") or "").strip():
                subjects.append(ResearchSubject(
                    canonical=str(raw["canonical"]).strip(),
                    aliases=[str(x) for x in (raw.get("aliases") or []) if x],
                ))
        return cls(
            objective=str(data.get("objective") or ""),
            entities=[str(x) for x in (data.get("entities") or []) if x],
            subjects=subjects,
            dimensions=[str(x) for x in (data.get("dimensions") or []) if x],
            time_range=str(data.get("time_range") or ""),
            source_policy=str(data.get("source_policy") or ""),
            deliverable=str(data.get("deliverable") or "text"),
            delivery_requirements=[str(x) for x in (data.get("delivery_requirements") or []) if x],
            citation_policy=str(data.get("citation_policy") or "claim-evidence"),
            constraints=[str(x) for x in (data.get("constraints") or []) if x],
            success_criteria=[str(x) for x in (data.get("success_criteria") or []) if x],
            ambiguities=[str(x) for x in (data.get("ambiguities") or []) if x],
            raw_query=str(data.get("raw_query") or ""),
            depth=depth,
            freshness=freshness,
            prefer_primary=bool(data.get("prefer_primary", False)),
            preferred_domains=[str(x) for x in (data.get("preferred_domains") or []) if x],
        )


def _split_entities(text: str) -> list[str]:
    parts = [p.strip(" 的了呢吗？? ") for p in _ENTITY_SPLIT.split(text or "") if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.lower()
        if key in _ENTITY_STOP or part in _ENTITY_STOP:
            continue
        if 1 < len(part) <= 40 and key not in seen:
            seen.add(key)
            out.append(part)
    return out[:12]


def _infer_depth(query: str, *, deliverable: str, entity_count: int, dimensions: list[str]) -> str:
    q = query or ""
    # Output formatting belongs to DeliveryEffort, never ResearchEffort.
    if entity_count >= 2 or _COMPARE.search(q):
        return "thorough"
    if any(m in q for m in THOROUGH_MARKERS) or len(dimensions) >= 2:
        return "thorough"
    if len(q.strip()) <= 24:
        return "shallow"
    return "standard"


def _infer_freshness(query: str, time_range: str) -> str:
    q = query or ""
    if any(m in q for m in RECENT_MARKERS):
        return "recent"
    if "以前" in q or "之前" in q or "不得使用" in q or "不要使用" in q:
        return (time_range or q)[:80] or "constrained"
    if time_range:
        return time_range
    return "any"


def _infer_prefer_primary(query: str) -> bool:
    q = (query or "").lower()
    return any(m.lower() in q or m in (query or "") for m in PRIMARY_MARKERS)


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
    subjects: list[ResearchSubject] = []
    dimensions: list[str] = []
    source_bits: list[str] = []
    citation_policy = "claim-evidence"
    preferred_domains = [m.group(0).lower() for m in _DOMAIN.finditer(query)]

    slots = getattr(intent, "slots", None) if intent is not None else None
    existing = getattr(intent, "brief", None) if intent is not None else None
    if isinstance(existing, ResearchBrief) and not existing.is_empty() and existing.entities:
        entities = list(existing.entities)
        subjects = list(existing.subjects)
        dimensions = list(existing.dimensions)
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
        if topic and not entities:
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

    try:
        from app.research.planning.policy import extract_compare_entities

        compared = extract_compare_entities(query)
    except Exception:
        compared = []
    if not compared and "核对" in query and "与" in query:
        comparison = query.split("核对", 1)[1].split("，", 1)[0]
        left, right = comparison.split("与", 1)
        left_name = left.strip().split()[-1] if left.strip() else ""
        right_name = right.strip().split()[0] if right.strip() else ""
        if left_name and right_name:
            compared = [left_name, right_name]
    if compared:
        entities = compared

    if _COMPARE.search(query) and "横向比较" not in dimensions:
        dimensions.append("横向比较")
    for dim, keys in [
        ("市场规模", ("市场规模", "市场空间", "CAGR")),
        ("商业化", ("商业化", "量产", "交付", "营收", "订单")),
        ("技术路线", ("技术", "方案", "架构")),
        ("竞争格局", ("竞争", "对手", "格局")),
        ("风险", ("风险", "监管", "合规")),
        ("监管", ("监管", "合规", "牌照")),
    ]:
        if any(k in query or k in summary for k in keys) and dim not in dimensions:
            dimensions.append(dim)

    if not entities:
        entities = _split_entities(summary or query)[:8]

    # Backward-compatible normalization. Explicit subjects win; otherwise a
    # comparison has independent subjects while ordinary topic strings are
    # aliases of one canonical subject (rather than accidental fan-out).
    if not subjects:
        if compared:
            subjects = [ResearchSubject(canonical=item) for item in entities]
        elif entities:
            subjects = [ResearchSubject(canonical=entities[0], aliases=entities[1:])]

    prefer_primary = _infer_prefer_primary(query)
    if prefer_primary:
        constraints.append("优先官方/一手来源")

    depth = _infer_depth(
        query,
        deliverable=deliverable,
        entity_count=len(subjects),
        dimensions=dimensions,
    )
    freshness = _infer_freshness(query, time_range)

    if plan_brief:
        objective = plan_brief.strip()
    elif summary and not summary.startswith("搜索任务"):
        objective = summary.strip()
    else:
        objective = query
    success = [
        "结论可追溯到 evidence_id / artifact_id",
        "冲突数字并列保留，不自行消解",
    ]
    delivery_requirements = []
    if deliverable in {"md", "pdf"}:
        delivery_requirements.append("交付物写入当前 session 工作目录")
    if prefer_primary:
        success.append("关键结论尽量落到官方或一手来源")
    if freshness == "recent" or (time_range and time_range[:4].isdigit()):
        success.append("证据年份覆盖用户关心的时间范围")

    ambiguities: list[str] = []
    if intent is not None:
        for flag in list(getattr(intent, "ambiguity_flags", None) or [])[:6]:
            ambiguities.append(str(flag))
        q = getattr(intent, "clarification_question", "") or ""
        if q and q not in ambiguities:
            ambiguities.append(str(q)[:160])

    if source_bits:
        source_policy = "+".join(source_bits)
    else:
        forbidden = (
            set(str(x) for x in (getattr(intent, "forbidden_sources", None) or []))
            if intent is not None
            else set()
        )
        if "web" in forbidden and "file" not in forbidden:
            source_policy = "file"
        else:
            source_policy = "web"

    return ResearchBrief(
        objective=objective[:500],
        entities=entities,
        subjects=subjects,
        dimensions=dimensions or ["关键事实"],
        time_range=time_range,
        source_policy=source_policy,
        deliverable=deliverable,
        delivery_requirements=delivery_requirements,
        citation_policy=citation_policy,
        constraints=constraints,
        success_criteria=success,
        ambiguities=ambiguities[:6],
        raw_query=query[:500],
        depth=depth,
        freshness=freshness,
        prefer_primary=prefer_primary,
        preferred_domains=preferred_domains[:8],
    )


def attach_brief(intent: Any, *, plan_brief: str = "") -> Any:
    """把编译后的 Brief 写回 Intent；供 planner / compose 调用。"""
    if intent is None:
        return intent
    query = str(getattr(intent, "raw_query", "") or "")
    intent.brief = compile_research_brief(
        task_query=query,
        intent=intent,
        plan_brief=plan_brief,
    )
    return intent


def brief_of(intent: Any | None, *, query: str = "", plan_brief: str = "") -> ResearchBrief:
    existing = getattr(intent, "brief", None) if intent is not None else None
    if isinstance(existing, ResearchBrief) and not existing.is_empty():
        return existing
    return compile_research_brief(
        task_query=query or str(getattr(intent, "raw_query", "") or ""),
        intent=intent,
        plan_brief=plan_brief,
    )
