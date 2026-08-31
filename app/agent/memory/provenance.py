"""
【Phase 18】记忆溯源与信任分级 — 防「持久化提示注入」。

深度研搜最危险的记忆失效模式：把一段不可信网页原文抽成 fact 写进长期记忆，
之后每次 recall 都会把它注入 prompt，等价于一次性注入变成永久注入。

对策是给每条记忆打信任等级 + 溯源信息，并在召回侧设准入门：
写报告这类合成步只接受 derived 以上、且带证据来源的记忆。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit


class TrustTier(str, Enum):
    """记忆可信等级（从低到高）。"""

    UNTRUSTED = "untrusted"  # 外部网页原文，未经交叉验证
    DERIVED = "derived"  # Agent 在内部系统或带引用证据上得出的结论
    TRUSTED = "trusted"  # 用户显式陈述、HITL 批准、系统种子


_TRUST_ORDER: dict[TrustTier, int] = {
    TrustTier.UNTRUSTED: 0,
    TrustTier.DERIVED: 1,
    TrustTier.TRUSTED: 2,
}

# 召回时按信任等级打折，低信任记忆即使命中也排在后面
TRUST_RECALL_WEIGHT: dict[TrustTier, float] = {
    TrustTier.UNTRUSTED: 0.6,
    TrustTier.DERIVED: 0.9,
    TrustTier.TRUSTED: 1.0,
}

# 内部可控数据源：结论可视为 derived，而非外部 untrusted
INTERNAL_STEP_TYPES = frozenset({"file_read"})
EXTERNAL_STEP_TYPES = frozenset({"network_search"})

_URL_PATTERN = re.compile(r"https?://[^\s\]\)\"'<>]+", re.IGNORECASE)
_CITE_PATTERN = re.compile(r"\[(\d+)\]")


def coerce_trust_tier(value: Any, default: TrustTier = TrustTier.DERIVED) -> TrustTier:
    if isinstance(value, TrustTier):
        return value
    try:
        return TrustTier(str(value).strip().lower())
    except (ValueError, AttributeError):
        return default


def trust_at_least(tier: Any, minimum: Any) -> bool:
    a = coerce_trust_tier(tier)
    b = coerce_trust_tier(minimum, default=TrustTier.UNTRUSTED)
    return _TRUST_ORDER[a] >= _TRUST_ORDER[b]


def tiers_at_least(minimum: Any) -> list[str]:
    """返回 >= minimum 的等级字符串列表，供 SQL IN 下推。"""
    floor = _TRUST_ORDER[coerce_trust_tier(minimum, default=TrustTier.UNTRUSTED)]
    return [t.value for t, order in _TRUST_ORDER.items() if order >= floor]


@dataclass
class Provenance:
    """一条记忆的来源证据，缺失时该记忆不得进入合成步。"""

    source_kind: str = ""  # url | sql | file | kb | user | agent
    source_urls: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    step_type: str = ""
    run_id: str = ""
    tool: str = ""
    citation_count: int = 0

    @property
    def has_evidence(self) -> bool:
        return bool(self.source_urls or self.evidence_ids or self.citation_count > 0)

    def primary_locator(self) -> str:
        if self.source_urls:
            return self.source_urls[0]
        if self.evidence_ids:
            return self.evidence_ids[0]
        return self.source_kind or self.step_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_urls": list(self.source_urls),
            "evidence_ids": list(self.evidence_ids),
            "step_type": self.step_type,
            "run_id": self.run_id,
            "tool": self.tool,
            "citation_count": self.citation_count,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "Provenance":
        if not data:
            return cls()
        return cls(
            source_kind=str(data.get("source_kind", "")),
            source_urls=[str(u) for u in (data.get("source_urls") or [])],
            evidence_ids=[str(e) for e in (data.get("evidence_ids") or [])],
            step_type=str(data.get("step_type", "")),
            run_id=str(data.get("run_id", "")),
            tool=str(data.get("tool", "")),
            citation_count=int(data.get("citation_count") or 0),
        )


def classify_trust_tier(
    *,
    write_source: Any,
    step_type: str = "",
    provenance: Optional[Provenance] = None,
) -> TrustTier:
    """根据写入来源与证据情况判定信任等级。"""
    source = getattr(write_source, "value", str(write_source)).lower()
    prov = provenance or Provenance()

    if source in {"user_explicit", "seed", "hitl"}:
        return TrustTier.TRUSTED

    effective_step = (step_type or prov.step_type or "").lower()
    if effective_step in EXTERNAL_STEP_TYPES:
        # 外部网页：即便带 URL 也只是「有出处」，不代表内容可信
        return TrustTier.UNTRUSTED
    if effective_step in INTERNAL_STEP_TYPES:
        return TrustTier.DERIVED

    # finalize 报告结论：有引用才算 derived，否则按未验证处理
    return TrustTier.DERIVED if prov.has_evidence else TrustTier.UNTRUSTED


def is_recall_eligible(
    record: Any,
    *,
    min_trust: Any = TrustTier.UNTRUSTED,
    target_step_type: str = "",
    synthesis_step_types: frozenset[str] = frozenset(),
    synthesis_min_trust: Any = TrustTier.DERIVED,
) -> bool:
    """召回准入门：合成步（写报告）对记忆的信任要求更高。"""
    tier = coerce_trust_tier(getattr(record, "trust_tier", TrustTier.DERIVED))
    if not trust_at_least(tier, min_trust):
        return False
    if target_step_type and target_step_type in synthesis_step_types:
        if not trust_at_least(tier, synthesis_min_trust):
            return False
    return True


def normalize_source_url(raw: str) -> str:
    """归一化 URL：去 fragment、去尾部标点与斜杠、小写 host。"""
    cleaned = (raw or "").strip().rstrip(".,;)]\"'")
    if not cleaned:
        return ""
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return cleaned.lower()
    if not parts.scheme:
        return cleaned.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def source_dedup_key(locator: str, *, kind: str = "url") -> str:
    """来源台账 locator 去重键（不含租户，需与 source_ledger_id 组合使用）。"""
    normalized = normalize_source_url(locator) if kind == "url" else (locator or "").strip()
    if not normalized:
        return ""
    digest = hashlib.sha1(f"{kind}:{normalized}".encode("utf-8")).hexdigest()[:20]
    return f"src_{digest}"


def source_ledger_id(
    *,
    tenant_id: str,
    user_id: str,
    project_id: str,
    locator: str,
    kind: str = "url",
) -> str:
    """租户/用户/项目作用域下的来源台账主键，避免跨租户 URL 碰撞。"""
    loc_key = source_dedup_key(locator, kind=kind)
    if not loc_key:
        return ""
    material = f"{tenant_id}:{user_id}:{project_id}:{loc_key}"
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:20]
    return f"led_{digest}"


def extract_urls(text: str, limit: int = 5) -> list[str]:
    seen: list[str] = []
    for raw in _URL_PATTERN.findall(text or ""):
        url = normalize_source_url(raw)
        if url and url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


def provenance_from_step(
    *,
    step_type: str,
    content: str,
    metadata: Optional[dict[str, Any]] = None,
    run_id: str = "",
) -> Provenance:
    """从步骤产出与 CitationManager 注册结果构造溯源信息。"""
    meta = metadata or {}
    evidence = meta.get("evidence_sources") or []
    source_urls: list[str] = []
    evidence_ids: list[str] = []
    source_kind = ""

    for item in evidence:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id", ""))
        if sid:
            evidence_ids.append(sid)
        kind = str(item.get("source_kind", ""))
        locator = str(item.get("locator", ""))
        if kind == "url" and locator:
            normalized = normalize_source_url(locator)
            if normalized and normalized not in source_urls:
                source_urls.append(normalized)
        if kind and not source_kind:
            source_kind = kind

    if not source_urls:
        source_urls = extract_urls(content)
        if source_urls and not source_kind:
            source_kind = "url"

    cite_hits = _CITE_PATTERN.findall(content or "")
    citation_count = max(len(evidence_ids), len(set(cite_hits)))

    if not source_kind:
        source_kind = {
            "file_read": "file",
            "network_search": "url",
            "finalize": "agent",
        }.get(step_type, "agent")

    return Provenance(
        source_kind=source_kind,
        source_urls=source_urls,
        evidence_ids=evidence_ids,
        step_type=step_type,
        run_id=run_id,
        citation_count=citation_count,
    )
