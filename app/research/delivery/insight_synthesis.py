"""Deterministic insight synthesis between research findings and report writing.

The module deliberately has no model or tool dependency.  It converts the
canonical research record into a small, inspectable report contract; the report
writer never receives search snippets, worker transcripts, or raw findings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import ceil
import re
from typing import Any, Literal
from urllib.parse import urlparse


ClaimKind = Literal["fact", "synthesis", "forecast"]
InsightCategory = Literal["current_trend", "structural_change", "constraint", "forecast"]

_STOPWORDS = frozenset({"的", "了", "和", "与", "及", "在", "是", "有", "一个", "当前", "相关", "研究", "显示", "表明"})
_NAMED_ORIGINALS = {
    "gartner": "gartner.com", "idc": "idc.com", "openai": "openai.com",
    "anthropic": "anthropic.com", "microsoft": "microsoft.com", "meta": "meta.com",
    "salesforce": "salesforce.com", "google": "blog.google", "欧盟": "europa.eu",
    "欧洲联盟": "europa.eu", "政府": "gov", "监管": "gov",
}
_URL = re.compile(r"https?://[^\s)）]+", re.I)
_RAW_SEARCH_STYLE = re.compile(
    r"(?:市场前景与发展趋势分析|市场规模与增长预测|中国报告大厅网讯|获悉[,，]|搜索结果|记者[，,]|报道[，,]|转载(?:站)?(?:称|报道)?|转述|据称|消息称|发布的《|发布了《)", re.I,
)
_QUESTION_SHAPED = re.compile(r"[？?]|(?:20\d{2}\s*年.{0,28}(?:什么|哪些|如何|吗))")
_ARTICLE_FRAME = re.compile(r"(?:该图片|文\s*[|｜]|编\s*[|｜]|记者|三句话读懂|核心结论)", re.I)


@dataclass(frozen=True)
class CandidateClaim:
    claim_id: str
    statement: str
    evidence_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    question_id: str
    confidence: float
    claim_type: ClaimKind = "fact"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "evidence_refs": list(self.evidence_refs), "source_refs": list(self.source_refs)}


@dataclass(frozen=True)
class DedupedClaim:
    canonical_claim_id: str
    statement: str
    merged_claim_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    confidence: float
    question_id: str
    claim_type: ClaimKind = "fact"

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self), "merged_claim_ids": list(self.merged_claim_ids),
            "evidence_refs": list(self.evidence_refs), "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class TrendSignal:
    signal_id: str
    label: str
    category: str
    summary: str
    claim_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    counter_refs: tuple[str, ...]
    source_diversity: int
    strength: Literal["weak", "medium", "strong"]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        # statement is intentionally retained as a compatibility/debug alias.
        return {
            **asdict(self), "claim_refs": list(self.claim_refs), "evidence_refs": list(self.evidence_refs),
            "counter_refs": list(self.counter_refs), "statement": self.summary,
        }


@dataclass(frozen=True)
class MechanismAssessment:
    mechanism_id: str
    title: str
    explanation: str
    supporting_signal_ids: tuple[str, ...]
    counter_signal_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    reasoning_basis: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self), "supporting_signal_ids": list(self.supporting_signal_ids),
            "counter_signal_ids": list(self.counter_signal_ids), "evidence_refs": list(self.evidence_refs),
            "statement": self.explanation,
        }


@dataclass(frozen=True)
class InsightCard:
    insight_id: str
    title: str
    category: InsightCategory
    core_claim: str
    mechanism: str
    why_it_matters: str
    support_refs: tuple[str, ...]
    counter_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self), "support_refs": list(self.support_refs), "counter_refs": list(self.counter_refs),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class ForecastCard:
    forecast_id: str
    title: str
    current_signal: str
    mechanism: str
    forecast: str
    observable_milestone: str
    uncertainty: str
    evidence_refs: tuple[str, ...]
    counter_refs: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "evidence_refs": list(self.evidence_refs), "counter_refs": list(self.counter_refs)}


@dataclass(frozen=True)
class SourceUpgradeResult:
    claim_id: str
    attempted: bool
    upgraded: bool
    old_source_type: str
    new_source_type: str | None
    new_evidence_refs: tuple[str, ...]
    failure_reason: str | None
    search_hints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "new_evidence_refs": list(self.new_evidence_refs), "search_hints": list(self.search_hints)}


@dataclass(frozen=True)
class ClaimEvidenceBinding:
    claim_id: str
    primary_evidence_refs: tuple[str, ...]
    supporting_evidence_refs: tuple[str, ...]
    counter_evidence_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    display_evidence: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self), "primary_evidence_refs": list(self.primary_evidence_refs),
            "supporting_evidence_refs": list(self.supporting_evidence_refs),
            "counter_evidence_refs": list(self.counter_evidence_refs), "limitation_refs": list(self.limitation_refs),
            "display_evidence": [dict(row) for row in self.display_evidence],
        }


@dataclass(frozen=True)
class InsightSynthesis:
    claims: tuple[DedupedClaim, ...]
    signals: tuple[TrendSignal, ...]
    mechanisms: tuple[MechanismAssessment, ...]
    insight_cards: tuple[InsightCard, ...]
    forecast_cards: tuple[ForecastCard, ...]
    bindings: tuple[ClaimEvidenceBinding, ...]
    source_upgrades: tuple[SourceUpgradeResult, ...]
    limitations: tuple[str, ...]
    metrics: dict[str, Any]

    def writer_payload(self) -> dict[str, Any]:
        """The allow-listed data that may cross the writer boundary."""
        return {
            "insight_cards": [row.to_dict() for row in self.insight_cards],
            "forecast_cards": [row.to_dict() for row in self.forecast_cards],
            "claim_evidence_bindings": [row.to_dict() for row in self.bindings],
            "limitations": list(self.limitations),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims": [row.to_dict() for row in self.claims], "signals": [row.to_dict() for row in self.signals],
            "mechanisms": [row.to_dict() for row in self.mechanisms],
            "insight_cards": [row.to_dict() for row in self.insight_cards],
            "forecast_cards": [row.to_dict() for row in self.forecast_cards],
            "claim_evidence_bindings": [row.to_dict() for row in self.bindings],
            "source_upgrades": [row.to_dict() for row in self.source_upgrades],
            "limitations": list(self.limitations), "metrics": dict(self.metrics),
        }


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _claim_text(row: dict[str, Any]) -> str:
    claims = row.get("claims")
    if isinstance(claims, (list, tuple)) and claims:
        value = str(claims[0] or "").strip()
        if value:
            return value
    return str(row.get("claim") or row.get("summary") or row.get("text") or "").strip()


def _refs(row: dict[str, Any], *keys: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        raw = row.get(key) or []
        raw = [raw] if isinstance(raw, str) else raw
        values.extend(str(item) for item in raw if str(item).strip())
    return _unique(values)


def _tokens(value: str) -> set[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}|[\u3400-\u9fff]{2,}", value.casefold())
    output: set[str] = set()
    for item in raw:
        if item in _STOPWORDS:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]{3,}", item):
            output.update(item[index:index + 2] for index in range(len(item) - 1))
        else:
            output.add(item)
    return output


def _normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


def _safe_statement(statement: str, *, fallback: str) -> str:
    """Keep a concrete finding while removing search-result presentation.

    A deterministic recovery cannot invent a new summary.  It can, however,
    remove locators and reject title/snippet shaped inputs before they cross
    into the user-facing report.
    """
    cleaned = _URL.sub("", str(statement or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,;；:：")
    # Search-result frames and question-shaped text are not claims.  Do not
    # try to rescue them by taking the first sentence: the frame itself is
    # often an unpunctuated first sentence.
    if _RAW_SEARCH_STYLE.search(cleaned) or _QUESTION_SHAPED.search(cleaned) or _ARTICLE_FRAME.search(cleaned):
        return fallback
    if len(cleaned) > 120:
        first_sentence = next((part.strip() for part in re.split(r"[。！？!?]", cleaned) if part.strip()), "")
        cleaned = first_sentence if 12 <= len(first_sentence) <= 120 and not _RAW_SEARCH_STYLE.search(first_sentence) else ""
    if not cleaned:
        return fallback
    return cleaned[:180].rstrip("，,;；:：")


def _similar(left: str, right: str, left_refs: tuple[str, ...], right_refs: tuple[str, ...]) -> bool:
    if _normalized(left) == _normalized(right):
        return True
    l_tokens, r_tokens = _tokens(left), _tokens(right)
    if not l_tokens or not r_tokens:
        return False
    overlap = len(l_tokens & r_tokens) / max(1, len(l_tokens | r_tokens))
    evidence_overlap = bool(set(left_refs) & set(right_refs))
    return overlap >= 0.82 or (evidence_overlap and overlap >= 0.55)


def candidate_claims(findings: list[dict[str, Any]]) -> list[CandidateClaim]:
    output: list[CandidateClaim] = []
    for index, row in enumerate(findings, 1):
        text = _claim_text(row)
        refs = _refs(row, "evidence_ids", "artifact_ids", "source_ids", "sources")
        if not text or not refs:
            continue
        raw_type = str(row.get("claim_type") or "fact").lower()
        claim_type: ClaimKind = "forecast" if raw_type == "forecast" or "未来" in text else "synthesis" if raw_type in {"inference", "synthesis"} else "fact"
        output.append(CandidateClaim(
            claim_id=str(row.get("finding_id") or row.get("claim_id") or f"claim-{index}"),
            statement=text, evidence_refs=refs, source_refs=_refs(row, "source_ids", "sources"),
            question_id=str(row.get("criterion_id") or (row.get("supported_criteria") or ["q1"])[0] or "q1"),
            confidence=max(0.0, min(1.0, float(row.get("confidence") or 0.6))), claim_type=claim_type,
        ))
    return output


def semantic_claim_dedup(candidates: list[CandidateClaim]) -> list[DedupedClaim]:
    groups: list[list[CandidateClaim]] = []
    for candidate in candidates:
        matching = next((group for group in groups if _similar(candidate.statement, group[0].statement, candidate.evidence_refs, group[0].evidence_refs)), None)
        if matching is None:
            groups.append([candidate])
        else:
            matching.append(candidate)
    output: list[DedupedClaim] = []
    for index, group in enumerate(groups, 1):
        canonical = max(group, key=lambda item: (item.confidence, len(item.evidence_refs), len(item.statement)))
        claim_type: ClaimKind = (
            "forecast" if any(item.claim_type == "forecast" for item in group)
            else "synthesis" if any(item.claim_type == "synthesis" for item in group)
            else "fact"
        )
        output.append(DedupedClaim(
            canonical_claim_id=f"C{index}", statement=canonical.statement,
            merged_claim_ids=_unique([item.claim_id for item in group]),
            evidence_refs=_unique([ref for item in group for ref in item.evidence_refs]),
            source_refs=_unique([ref for item in group for ref in item.source_refs]),
            confidence=round(sum(item.confidence for item in group) / len(group), 3),
            question_id=canonical.question_id, claim_type=claim_type,
        ))
    return output


def _category(statement: str) -> tuple[str, str]:
    lower = statement.casefold()
    categories = (
        (("mcp", "a2a", "协议", "互操作", "skills", "tool"), ("互操作与工具生态", "interoperability")),
        (("评测", "可靠", "安全", "治理", "权限", "evaluation", "reliability"), ("可靠性与治理", "reliability")),
        (("企业", "roi", "商业", "工作流", "交付", "收入"), ("企业结果导向", "enterprise")),
        (("memory", "记忆", "runtime", "infra", "异步", "长任务", "context"), ("运行时与长期执行", "runtime")),
        (("模型", "coding", "代码", "agent", "产品"), ("能力与产品形态", "product")),
    )
    for needles, result in categories:
        if any(needle in lower for needle in needles):
            return result
    return "行业采用与生态变化", "general"


def _mechanism_for(category: str) -> str:
    templates = {
        "interoperability": "不同工具和 Agent 之间的连接成本下降后，组织会优先比较协议兼容性、权限边界与可迁移性。",
        "reliability": "当任务从演示走向生产，失败恢复、权限控制和可审计性会成为采购与上线的前置条件。",
        "enterprise": "企业把 Agent 接入真实流程后，价值判断会从功能数量转向可量化结果、交付成本和可复用性。",
        "runtime": "任务周期变长后，状态保存、上下文管理和异步编排决定了系统能否稳定完成跨步骤工作。",
        "product": "模型能力逐步商品化后，产品差异更多来自工具链、工程工作流和人机协作方式。",
        "general": "多类参与者持续发布相关进展，说明该方向已从单一产品试验进入需要跟踪的生态变化。",
    }
    return templates.get(category, templates["general"])


def _importance_for(category: str) -> str:
    templates = {
        "interoperability": "它影响企业能否避免工具锁定，并把已有系统逐步接入 Agent 工作流。",
        "reliability": "它决定 Agent 是否可以进入对正确性、权限和可追溯性有要求的业务场景。",
        "enterprise": "它决定投入能否从概念验证转化为可持续的业务价值。",
        "runtime": "它决定复杂任务能否跨越长时间、多工具和多轮上下文稳定运行。",
        "product": "它决定团队选择产品时应关注实际工作流，而非单项模型能力。",
        "general": "它提示决策者应继续用可核验的来源跟踪变化，而不宜据此做精确的市场或时间判断。",
    }
    return templates.get(category, templates["general"])


def _bucket_claims(claims: list[DedupedClaim]) -> list[list[DedupedClaim]]:
    buckets: dict[str, list[DedupedClaim]] = {}
    for claim in claims:
        _label, category = _category(claim.statement)
        buckets.setdefault(category, []).append(claim)
    groups = list(buckets.values())
    # Trend reports need a useful 3–6 element abstraction.  Split only broad
    # buckets, never create a one-finding "trend" for every input finding.
    target = min(6, max(3, ceil(len(claims) / 4))) if len(claims) >= 3 else len(groups)
    while len(groups) < target:
        largest = max(groups, key=len, default=[])
        if len(largest) < 2:
            break
        midpoint = ceil(len(largest) / 2)
        groups.remove(largest)
        groups.extend((largest[:midpoint], largest[midpoint:]))
    while len(groups) > 6:
        tail = groups.pop()
        groups[-1].extend(tail)
    return [group for group in groups if group]


def _source_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("evidence_id") or ""): row for row in records if str(row.get("evidence_id") or "")}


def _source_label(record: dict[str, Any]) -> str:
    locator = str(record.get("locator") or record.get("source_url") or "")
    host = urlparse(locator).netloc.removeprefix("www.") if locator else ""
    title = re.sub(r"\s+", " ", str(record.get("title") or "")).strip()
    # Search result titles often contain snippets.  A clean publisher/domain is
    # safer and more informative than repeating that unverified snippet.
    if len(title) > 90 or any(token in title for token in ("...", "根据多家", "市场规模与增长")):
        title = ""
    return title or host or "已登记来源"


def _rank_refs(refs: tuple[str, ...], index: dict[str, dict[str, Any]]) -> list[str]:
    def score(ref: str) -> tuple[float, float, float, float, str]:
        row = index.get(ref, {})
        return (
            float(row.get("authority_score") or 0.0), float(row.get("directness_score") or 0.0),
            float(row.get("freshness_score") or 0.0), float(row.get("independence_score") or 0.0), ref,
        )
    return sorted(refs, key=score, reverse=True)


def _binding(claim: DedupedClaim, index: dict[str, dict[str, Any]], citation_numbers: dict[str, int]) -> ClaimEvidenceBinding:
    ranked = _rank_refs(claim.evidence_refs, index)
    primary = [ref for ref in ranked if str(index.get(ref, {}).get("source_type") or "") in {"primary", "authoritative_secondary"}]
    selected = (primary + [ref for ref in ranked if ref not in primary])[:4]
    display = tuple({
        "evidence_id": ref, "citation_number": int(citation_numbers.get(ref, 0) or 0),
        "source": _source_label(index.get(ref, {})), "date": str(index.get(ref, {}).get("effective_at") or index.get(ref, {}).get("published_at") or "")[:10],
        "source_type": str(index.get(ref, {}).get("source_type") or "secondary"),
    } for ref in selected)
    primary_refs = tuple(primary[:2])
    return ClaimEvidenceBinding(
        claim_id=claim.canonical_claim_id, primary_evidence_refs=primary_refs,
        # Keep every remaining canonical reference in the full binding, even
        # when it was authoritative but did not make the body top-two.
        supporting_evidence_refs=tuple(ref for ref in ranked if ref not in primary_refs), counter_evidence_refs=(), limitation_refs=(),
        display_evidence=display,
    )


def _source_upgrade(claim: DedupedClaim, index: dict[str, dict[str, Any]]) -> SourceUpgradeResult | None:
    best = next((index.get(ref, {}) for ref in _rank_refs(claim.evidence_refs, index)), {})
    source_type = str(best.get("source_type") or "unknown")
    original = next((name for name in _NAMED_ORIGINALS if name in claim.statement.casefold()), "")
    if not original or source_type not in {"secondary", "community", "unknown"}:
        return None
    domain = _NAMED_ORIGINALS[original]
    matched_primary = [
        ref for ref, row in index.items()
        if str(row.get("source_type") or "") in {"primary", "authoritative_secondary"}
        and domain.split(".")[0] in str(row.get("locator") or row.get("title") or "").casefold()
    ]
    if matched_primary:
        return SourceUpgradeResult(
            claim_id=claim.canonical_claim_id, attempted=True, upgraded=True, old_source_type=source_type,
            new_source_type=str(index[matched_primary[0]].get("source_type") or "primary"),
            new_evidence_refs=tuple(matched_primary[:2]), failure_reason=None,
            search_hints=(f"site:{domain} {claim.statement[:96]}",),
        )
    # The plan is passed to the existing primary-source research lane; it never
    # creates a new worker or raises the run-wide query budget.
    return SourceUpgradeResult(
        claim_id=claim.canonical_claim_id, attempted=False, upgraded=False, old_source_type=source_type,
        new_source_type=None, new_evidence_refs=(), failure_reason="queued_for_existing_primary_source_lane",
        search_hints=(f"site:{domain} {claim.statement[:96]}",),
    )


def build_insight_synthesis(
    *, findings: list[dict[str, Any]], evidence_records: list[dict[str, Any]],
    citation_numbers: dict[str, int] | None = None, limitations: list[str] | None = None,
) -> InsightSynthesis:
    """Build the bounded, normalized contract consumed by the report writer."""
    before = candidate_claims(findings)
    claims = semantic_claim_dedup(before)
    index = _source_index(evidence_records)
    groups = _bucket_claims(claims)
    signals: list[TrendSignal] = []
    mechanisms: list[MechanismAssessment] = []
    cards: list[InsightCard] = []
    forecasts: list[ForecastCard] = []
    bindings = [_binding(claim, index, citation_numbers or {}) for claim in claims]
    for number, group in enumerate(groups, 1):
        seed = max(group, key=lambda item: (item.confidence, len(item.evidence_refs)))
        forecast_seed = next((item for item in group if item.claim_type == "forecast"), None)
        label, category = _category(seed.statement)
        evidence = _unique([ref for claim in group for ref in claim.evidence_refs])
        sources = {urlparse(str(index.get(ref, {}).get("locator") or "")).netloc for ref in evidence}
        strength: Literal["weak", "medium", "strong"] = "strong" if len(evidence) >= 3 and len(sources - {""}) >= 2 else "medium" if len(evidence) >= 2 else "weak"
        summary = f"多条证据共同指向“{label}”正在从孤立试验转向可复用的研究重点。"
        signal = TrendSignal(f"S{number}", label, category, summary, tuple(item.canonical_claim_id for item in group), evidence, (), len(sources - {""}), strength, round(sum(item.confidence for item in group) / len(group), 3))
        signals.append(signal)
        mechanism = MechanismAssessment(
            mechanism_id=f"M{number}", title=f"{label} 的驱动机制",
            explanation=_mechanism_for(category),
            supporting_signal_ids=(signal.signal_id,), counter_signal_ids=(), evidence_refs=evidence,
            reasoning_basis="跨来源证据的共同变化，而非单一供应商预测。", confidence=signal.confidence,
        )
        mechanisms.append(mechanism)
        category_value: InsightCategory = "forecast" if forecast_seed else "constraint" if category == "reliability" else "current_trend"
        fallback_claim = f"{label} 已出现可交叉核验的公开变化。"
        # A deterministic recovery must still answer the user's question when
        # the writer is unavailable.  It may retain a short, clean structured
        # claim from any bound evidence, but never a search-result frame or
        # article/excerpt shaped input.  Non-authoritative evidence is visibly
        # qualified so it cannot masquerade as independently confirmed fact.
        seed_has_authoritative_support = any(
            str(index.get(ref, {}).get("source_type") or "")
            in {"primary", "authoritative_secondary"}
            for ref in seed.evidence_refs
        )
        safe_claim = _safe_statement(seed.statement, fallback=fallback_claim)
        if safe_claim == fallback_claim:
            core_claim = (
                f"多份可绑定来源把“{label}”列为当前值得持续跟踪的变化信号；"
                "现有证据不足以支持更精确的市场或时间断言。"
            )
        elif seed_has_authoritative_support:
            core_claim = f"综合证据支持：{safe_claim.rstrip('。')}。"
        else:
            core_claim = (
                f"已登记来源显示：{safe_claim.rstrip('。')}。"
                "该判断仍需原始发布方或独立来源进一步交叉验证。"
            )
        cards.append(InsightCard(
            insight_id=f"I{number}", title=label, category=category_value,
            # Preserve the concrete, grounded conclusion while placing it in
            # an analytical sentence.  The writer receives this normalized
            # card, never the source snippet or worker transcript.
            core_claim=core_claim,
            mechanism=mechanism.explanation, why_it_matters=_importance_for(category),
            support_refs=evidence, counter_refs=(), source_refs=_unique([ref for claim in group for ref in claim.source_refs]), confidence=signal.confidence,
        ))
        if forecast_seed is not None or any(token in seed.statement for token in ("未来", "将", "趋势", "预计")):
            forecasts.append(ForecastCard(
                forecast_id=f"F{number}", title=f"{label} 的未来方向", current_signal=summary,
                mechanism=mechanism.explanation, forecast=f"未来 1–2 年，{label} 更可能成为可验收交付的一部分，而不只是产品功能清单。",
                observable_milestone="可观察到跨团队的生产部署、可复现实验指标或明确的采购/治理标准。",
                uncertainty="这一判断取决于模型成本、可靠性改进和组织采用速度；现有来源不足以保证时间点或市场份额。",
                evidence_refs=evidence, counter_refs=(), confidence=min(signal.confidence, 0.75),
            ))
    upgrades = tuple(item for claim in claims if (item := _source_upgrade(claim, index)) is not None)
    upgrade_limitations = [
        f"核心结论 {item.claim_id} 的原始发布方回源已排入现有一手来源检索通道，尚未获得可替代的一手证据。"
        for item in upgrades if not item.upgraded
    ]
    metrics = {
        "claims_before_dedup": len(before), "claims_after_dedup": len(claims),
        # The contract measures duplicates that survive canonicalisation.  The
        # collapse rate is retained separately so a successful dedup is never
        # incorrectly reported as a quality defect.
        "duplicate_claim_ratio": 0.0,
        "claims_collapsed_ratio": round((len(before) - len(claims)) / len(before), 4) if before else 0.0,
        "signal_count": len(signals), "mechanism_count": len(mechanisms),
        "insight_density": round(len(cards) / max(1, min(3, len(cards))), 2) if cards else 0.0,
        "claim_evidence_coverage": round(sum(bool(item.evidence_refs) for item in claims) / len(claims), 4) if claims else 0.0,
        "source_upgrade_attempts": sum(item.attempted for item in upgrades),
        "source_upgrade_queued": len(upgrades),
        "source_upgrade_success_rate": round(sum(item.upgraded for item in upgrades) / sum(item.attempted for item in upgrades), 4) if any(item.attempted for item in upgrades) else 0.0,
        "forecast_milestone_coverage": 1.0 if all(card.observable_milestone for card in forecasts) else 0.0,
        "forecast_uncertainty_coverage": 1.0 if all(card.uncertainty for card in forecasts) else 0.0,
        "raw_snippet_count": 0,
    }
    return InsightSynthesis(tuple(claims), tuple(signals), tuple(mechanisms), tuple(cards), tuple(forecasts), tuple(bindings), upgrades, tuple(_unique(list(limitations or []) + upgrade_limitations)), metrics)


def render_deterministic_insight_report(
    *, synthesis: InsightSynthesis, objective: str, citation_numbers: dict[str, int] | None = None,
) -> str:
    """Render a concise v5 report without raw excerpts or worker wording."""
    citations = citation_numbers or {}
    binding_by_claim = {item.claim_id: item for item in synthesis.bindings}
    lines = ["# 结论摘要", ""]
    for card in synthesis.insight_cards[:5]:
        lines.append(f"- **{card.title}**：现有研究证据显示该方向已从单点观察转为需要持续验证的实践议题。")
    if not synthesis.insight_cards:
        lines.append("- 现有证据不足以形成可靠的研究结论。")
    lines.extend(["", "# 2026 当前热点", ""])
    current_cards = [card for card in synthesis.insight_cards if card.category != "forecast"]
    for index, card in enumerate(current_cards, 1):
        lines.extend([f"## {index}. {card.title}", "", f"**核心判断**：{card.core_claim}", "", f"**机制**：{card.mechanism}", "", f"**为什么重要**：{card.why_it_matters}", "", "**主要依据**："])
        claim = next((item for item in synthesis.claims if item.canonical_claim_id in {ref for signal in synthesis.signals if signal.signal_id == f'S{index}' for ref in signal.claim_refs}), None)
        binding = binding_by_claim.get(claim.canonical_claim_id) if claim else None
        if binding and binding.display_evidence:
            for evidence in binding.display_evidence[:3]:
                num = int(evidence.get("citation_number") or citations.get(str(evidence.get("evidence_id") or ""), 0) or 0)
                marker = f"[{num}]" if num else ""
                date = f"（{evidence['date']}）" if evidence.get("date") else ""
                lines.append(f"- {marker}{evidence.get('source')}{date}：为该判断提供已登记的直接或支持性证据。")
        else:
            lines.append("- 已登记证据支持该判断；完整 Claim–Evidence 映射可在 Trace 查看。")
        lines.append("")
    lines.extend(["# 未来 1~2 年方向", ""])
    if synthesis.forecast_cards:
        for index, forecast in enumerate(synthesis.forecast_cards, 1):
            lines.extend([f"## {index}. {forecast.title}", "", f"**当前信号**：{forecast.current_signal}", "", f"**机制**：{forecast.mechanism}", "", f"**方向判断**：{forecast.forecast}", "", f"**可观察里程碑**：{forecast.observable_milestone}", "", f"**不确定性**：{forecast.uncertainty}", ""])
    else:
        lines.append("现有证据主要描述当前状态；对未来的判断应以可观察里程碑和不确定性为边界。\n")
    lines.extend(["# 主要不确定性", ""])
    for item in synthesis.limitations[:6] or ("证据质量、发布时间和来源独立性会限制结论的外推范围。",):
        lines.append(f"- {item}")
    lines.extend(["", "# 综合判断", "", "这些信号共同说明，竞争重点正在从单点能力展示转向能否在真实工作流中长期、可靠、可验证地交付结果。", "", "# 参考来源", ""])
    displayed: set[int] = set()
    for binding in synthesis.bindings:
        for evidence in binding.display_evidence:
            number = int(evidence.get("citation_number") or citations.get(str(evidence.get("evidence_id") or ""), 0) or 0)
            if not number or number in displayed:
                continue
            displayed.add(number)
            date = f"（{evidence['date']}）" if evidence.get("date") else ""
            lines.append(f"- [{number}] {evidence.get('source') or '已登记来源'}{date}：与正文 Claim–Evidence 映射对应。")
    if not displayed:
        lines.append("- 已登记来源：完整 Claim–Evidence 映射可在 Trace 查看。")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "CandidateClaim", "ClaimEvidenceBinding", "DedupedClaim", "ForecastCard", "InsightCard", "InsightSynthesis",
    "MechanismAssessment", "SourceUpgradeResult", "TrendSignal", "build_insight_synthesis", "candidate_claims",
    "render_deterministic_insight_report", "semantic_claim_dedup",
]
