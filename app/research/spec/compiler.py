"""Deterministic compiler from a query to a ResearchSpec."""

from __future__ import annotations

import re
from typing import Any

from app.research.spec.models import (
    Ambiguity,
    Constraint,
    DeliveryRequirements,
    EvidenceRequirements,
    FreshnessPolicy,
    InteractionRequirements,
    ReasoningRequirements,
    ResearchDimension,
    ResearchSpec,
    ResearchSubject,
    SourcePolicy,
    SuccessCriterion,
    stable_spec_id,
)

_SEPARATORS = re.compile(r"[、,，;；/]|和|与|及")
_DIMENSION_HINTS = (
    ("commercialization", ("商业化", "收入", "营收", "客户", "market", "revenue")),
    ("technology", ("技术", "模型", "产品", "technology", "architecture")),
    ("team", ("团队", "创始人", "高管", "team", "founder")),
    ("funding", ("融资", "估值", "投资", "funding", "valuation")),
    ("risk", ("风险", "合规", "监管", "risk", "compliance")),
    ("evidence", ("证据", "来源", "引用", "citation", "evidence")),
)


def _shape(query: str) -> str:
    lowered = query.lower()
    if any(word in lowered for word in ("汇总", "聚合", "筛选", "符合条件", "aggregate", "filter")):
        return "DETERMINISTIC_PIPELINE"
    if any(word in lowered for word in ("哪些", "全景", "landscape", "扫描", "候选")):
        return "DYNAMIC_DISCOVERY"
    if any(word in lowered for word in ("对比", "比较", "vs", "versus")):
        return "BREADTH_HEAVY"
    if any(word in lowered for word in ("最新", "当前", "现在", "冲突", "矛盾", "latest", "conflict")):
        return "HYBRID_CONFLICT"
    if re.search(r"\d", query) or any(
        word in lowered
        for word in ("是什么", "多少", "哪年", "哪一年", "何时", "谁", "when", "what", "who")
    ):
        return "SIMPLE_FACT"
    return "SINGLE_TOPIC_DEEP_DIVE"


def _subjects(query: str, shape: str) -> list[ResearchSubject]:
    if not query.strip():
        return []
    if shape == "BREADTH_HEAVY":
        fragments = [
            fragment.strip()
            for fragment in _SEPARATORS.split(query)
            if fragment.strip()
            and not any(word in fragment.lower() for word in ("对比", "比较", "vs", "versus"))
        ]
        if len(fragments) >= 2:
            return [
                ResearchSubject(subject_id=f"subject_{index}", name=fragment[:120])
                for index, fragment in enumerate(fragments, start=1)
            ]
    return [ResearchSubject(subject_id="subject_1", name=query[:160])]


def _dimensions(query: str, shape: str) -> list[ResearchDimension]:
    if not query.strip():
        return []
    if shape in {"BREADTH_HEAVY", "DYNAMIC_DISCOVERY"}:
        return [
            ResearchDimension("candidate_set", "候选集", "Discovery output", True),
            ResearchDimension("commercialization", "商业化", "Business viability", True),
            ResearchDimension("technology", "技术", "Technical capability", True),
        ]
    dimensions = [ResearchDimension("key_fact", "关键事实", "Query-centered fact", True)]
    if shape in {"SINGLE_TOPIC_DEEP_DIVE", "DETERMINISTIC_PIPELINE"}:
        for dimension_id, hints in _DIMENSION_HINTS:
            if any(hint.lower() in query.lower() for hint in hints):
                dimensions.append(ResearchDimension(dimension_id, hints[0], "", True))
    return dimensions[:6]


def _language_hints(query: str) -> list[str]:
    hints: list[str] = []
    if re.search(r"[\u4e00-\u9fff]", query):
        hints.append("zh")
    if re.search(r"[A-Za-z]", query):
        hints.append("en")
    if re.search(r"[\u3040-\u30ff]", query):
        hints.append("ja")
    if re.search(r"[\uac00-\ud7af]", query):
        hints.append("ko")
    if re.search(r"[\u0400-\u04ff]", query):
        hints.append("ru")
    return list(dict.fromkeys(hints))


def _source_policy(query: str, require_primary: bool) -> SourcePolicy:
    if any(marker in query.lower() for marker in ("不要联网", "不联网", "只根据内部附件", "offline only")):
        return SourcePolicy(
            allowed=["file"],
            preferred=["file"],
            forbidden=["web"],
            require_primary=False,
        )
    return SourcePolicy(
        allowed=["web"],
        preferred=["official", "primary"],
        require_primary=require_primary,
    )


def compile_research_spec(
    query: str,
    *,
    conversation_delta: str = "",
    existing_spec: dict[str, Any] | None = None,
) -> ResearchSpec:
    objective = re.sub(r"\s+", " ", " ".join(part for part in (query, conversation_delta) if part).strip())
    shape = _shape(objective)
    subjects = _subjects(objective, shape)
    dimensions = _dimensions(objective, shape)
    lowered = objective.lower()
    reasoning = ReasoningRequirements(
        lookup=shape == "SIMPLE_FACT",
        filter=any(word in lowered for word in ("符合", "筛选", "filter")),
        aggregate=any(word in lowered for word in ("汇总", "聚合", "aggregate")),
        compare=shape == "BREADTH_HEAVY",
        multi_hop=any(word in lowered for word in ("为什么", "原因", "影响", "多跳", "why")),
        discovery=shape in {"BREADTH_HEAVY", "DYNAMIC_DISCOVERY"},
        synthesis=True,
    )
    evidence = EvidenceRequirements(
        min_independent_sources=1 if shape == "SIMPLE_FACT" else 2,
        prefer_primary=True,
        claim_level_citation=True,
        conflict_resolution_required=shape == "HYBRID_CONFLICT",
        freshness_required=any(word in lowered for word in ("最新", "当前", "现在", "latest")),
        source_diversity_required=shape != "SIMPLE_FACT",
    )
    interaction = InteractionRequirements(supports_followup_delta=bool(conversation_delta.strip()))
    delivery = DeliveryRequirements(
        format="pdf" if "pdf" in lowered else "markdown",
        depth=(
            "brief"
            if shape == "SIMPLE_FACT"
            else "long"
            if any(word in lowered for word in ("长报告", "深度报告", "long report"))
            else "standard"
        ),
    )
    criteria = [
        SuccessCriterion("criterion_evidence", "Every required coverage unit has admitted evidence.", True),
        SuccessCriterion("criterion_citation", "Every final claim has an existing evidence citation.", True),
    ]
    if shape in {"BREADTH_HEAVY", "DYNAMIC_DISCOVERY"}:
        criteria.append(
            SuccessCriterion("criterion_candidates", "Discovery produces an explicit candidate set.", True)
        )
    constraints = [Constraint("constraint_scope", "Do not expand beyond the requested research objective.", True)]
    ambiguities = [] if objective else [Ambiguity("ambiguity_empty", "Query is empty", True)]
    if existing_spec:
        previous = ResearchSpec.from_dict(existing_spec)
        return ResearchSpec(
            spec_id=previous.spec_id,
            version=previous.version + 1,
            objective=objective or previous.objective,
            subjects=subjects or previous.subjects,
            dimensions=dimensions or previous.dimensions,
            reasoning_requirements=reasoning,
            evidence_requirements=evidence,
            interaction_requirements=interaction,
            delivery_requirements=delivery,
            constraints=constraints,
            premises=previous.premises,
            ambiguities=ambiguities or previous.ambiguities,
            success_criteria=criteria,
            task_shape=shape,
            freshness=FreshnessPolicy(
                required=evidence.freshness_required,
                max_age_days=365 if evidence.freshness_required else None,
            ),
            source_policy=(
                _source_policy(objective, evidence.prefer_primary)
                if any(marker in objective.lower() for marker in ("不要联网", "不联网", "只根据内部附件", "offline only"))
                else previous.source_policy
            ),
            assumptions=previous.assumptions,
            language_hints=sorted(set(previous.language_hints) | set(_language_hints(objective))),
        )
    return ResearchSpec(
        spec_id=stable_spec_id(objective),
        version=1,
        objective=objective,
        subjects=subjects,
        dimensions=dimensions,
        reasoning_requirements=reasoning,
        evidence_requirements=evidence,
        interaction_requirements=interaction,
        delivery_requirements=delivery,
        constraints=constraints,
        ambiguities=ambiguities,
        success_criteria=criteria,
        task_shape=shape,
        freshness=FreshnessPolicy(
            required=evidence.freshness_required,
            max_age_days=365 if evidence.freshness_required else None,
        ),
        source_policy=_source_policy(objective, evidence.prefer_primary),
        language_hints=_language_hints(objective),
    )


__all__ = ["compile_research_spec"]
