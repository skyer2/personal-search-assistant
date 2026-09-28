"""Deterministic compiler from a query to a ResearchSpec."""

from __future__ import annotations

import re
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.research.intent.user_ask import compile_user_ask_contract

from app.research.spec.models import (
    ANSWER_POLICY_VERSION,
    ANSWER_SCHEMA_VERSION,
    AnswerSpec,
    AskSpec,
    Ambiguity,
    Constraint,
    DeliveryRequirements,
    EvidenceRequirements,
    FieldRequirement,
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

_NUMBER_WORDS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _target_units(text: str, kind: str) -> int:
    if kind not in {"recommendation", "comparison"}:
        return 1
    match = re.search(r"(?<!\d)(\d{1,2})\s*(?:家|个|款|种|项|tools?|companies?)", text, re.IGNORECASE)
    if match:
        return max(1, min(20, int(match.group(1))))
    match = re.search(r"([一两二三四五六七八九十])\s*(?:家|个|款|种|项)", text)
    if match:
        return _NUMBER_WORDS[match.group(1)]
    return 5 if kind == "recommendation" else 1


def _field_requirements(kind: str) -> tuple[FieldRequirement, ...]:
    rows: dict[str, tuple[tuple[str, str, str, bool], ...]] = {
        "fact": (("answer", "fact", "direct_fact", False), ("scope", "scope", "explicit_scope", False)),
        "recommendation": (
            ("company", "entity", "identity", False),
            ("product", "fact", "product_fact", False),
            ("why_interesting", "inference", "grounded_reason", False),
            ("limitations", "limitation", "explicit_boundary", True),
        ),
        "comparison": (
            ("subjects", "entity_list", "identity", False),
            ("dimensions", "comparison", "per_subject_evidence", False),
            ("conditional_judgment", "inference", "grounded_reason", False),
        ),
        "explanation": (
            ("explanation", "inference", "causal_evidence", False),
            ("boundary", "limitation", "causality_boundary", False),
        ),
        "current_state": (
            ("status", "fact", "current_fact", False),
            ("as_of", "datetime", "explicit_scope", False),
        ),
        "forecast": (
            ("current_signal", "fact", "current_fact", False),
            ("mechanism", "inference", "grounded_reason", False),
            ("direction", "forecast", "grounded_forecast", False),
            ("milestone", "forecast", "observable_milestone", False),
            ("uncertainty", "limitation", "explicit_boundary", False),
        ),
    }
    return tuple(FieldRequirement(*row) for row in rows[kind])


def _compile_answer_spec(
    objective: str,
    *,
    spec_id: str,
    revision: int,
    as_of: datetime,
    timezone: str,
    explicit_subjects: tuple[str, ...] = (),
) -> AnswerSpec:
    contract = compile_user_ask_contract(objective)
    asks: list[AskSpec] = []
    assumptions: list[str] = []
    for index, user_ask in enumerate(contract.asks, 1):
        kind = user_ask.ask_type
        target = _target_units(user_ask.text, kind)
        partial_allowed = not any(token in user_ask.text for token in ("必须完整", "否则不要", "不要部分", "all or nothing"))
        entity_scope: dict[str, Any] = {"subject": user_ask.subject} if user_ask.subject else {"type": "unspecified"}
        if kind == "comparison" and len(explicit_subjects) >= 2:
            entity_scope = {"subjects": list(explicit_subjects)}
        if kind == "recommendation" and any(token in objective.casefold() for token in ("ai初创", "ai 创业", "ai startup")):
            entity_scope = {"type": "ai_startup", "geography": "global"}
        time_scope = (
            {"mode": "explicit", "value": user_ask.time_scope}
            if user_ask.time_scope
            else {"mode": "current" if kind in {"current_state", "recommendation", "forecast"} else "unspecified"}
        )
        asks.append(AskSpec(
            ask_id=user_ask.ask_id,
            question_id=f"q{index}",
            original_text=user_ask.text,
            kind=kind,
            required=user_ask.required,
            entity_scope=entity_scope,
            time_scope=time_scope,
            required_fields=_field_requirements(kind),
            target_units=target,
            min_partial_units=1,
            max_units=target,
            partial_allowed=partial_allowed,
            selection_criteria=(
                ("product_difference", "practical_value", "verified_progress")
                if kind == "recommendation"
                else ("user_requested_dimensions",) if kind == "comparison" else ()
            ),
        ))
        if kind == "recommendation" and target == 5 and not re.search(r"\d|[一两二三四五六七八九十]\s*(?:家|个|款|种|项)", user_ask.text):
            assumptions.append("用户未指定数量，默认交付5个经验证对象")
    if not asks and objective:
        raise ValueError("contract_invalid:no_asks")
    return AnswerSpec(
        schema_version=ANSWER_SCHEMA_VERSION,
        spec_id=spec_id,
        revision=revision,
        objective=objective,
        as_of=as_of.isoformat(),
        timezone=timezone,
        asks=tuple(asks),
        assumptions=tuple(assumptions),
        policy_version=ANSWER_POLICY_VERSION,
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
    if shape == "DYNAMIC_DISCOVERY":
        lowered = query.lower()
        domain = "AI startup" if ("ai" in lowered or "人工智能" in query) else "general landscape"
        geography = "China" if any(marker in query for marker in ("国内", "中国")) else "global"
        return [
            ResearchSubject(
                subject_id=f"company_landscape:{domain.lower().replace(' ', '_')}:{geography.lower()}",
                name=f"{geography} {domain}" if geography == "China" else f"{domain} candidates",
                subject_type="company_landscape",
                domain=domain,
                geography=geography,
            )
        ]
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
        dimensions = [
            ResearchDimension("candidate_set", "候选集", "Discovery output", True),
            ResearchDimension("commercialization", "商业化", "Business viability", True),
            ResearchDimension("technology", "技术", "Technical capability", True),
            ResearchDimension("team", "团队", "Founder and execution team", True),
            ResearchDimension("funding", "融资", "Funding and investor signal", True),
            ResearchDimension("market_position", "市场位置", "Market position and competition", True),
            ResearchDimension("career_opportunity", "职业机会", "Career growth and joining value", True),
            ResearchDimension("risk", "风险", "Business, technology, and policy risk", True),
        ]
        if shape == "BREADTH_HEAVY":
            dimensions = [dimension for dimension in dimensions if dimension.dimension_id != "candidate_set"]
        return dimensions
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
    as_of: datetime | None = None,
    timezone: str = "Asia/Shanghai",
    engine_version: str | None = None,
    explicit_subjects: tuple[str, ...] = (),
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
    now = as_of or datetime.now(ZoneInfo(timezone))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(timezone))
    spec_id = stable_spec_id(objective)
    revision = 1
    selected_engine = str(
        engine_version or os.getenv("HARNESS_RESEARCH_ENGINE_VERSION", "legacy_v1")
    ).strip()
    if selected_engine not in {"legacy_v1", "answer_contract_v2"}:
        raise ValueError(f"unsupported research engine: {selected_engine}")
    if existing_spec:
        previous = ResearchSpec.from_dict(existing_spec)
        spec_id = previous.spec_id
        revision = previous.version + 1
        return ResearchSpec(
            spec_id=spec_id,
            version=revision,
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
            answer_spec=_compile_answer_spec(
                objective or previous.objective,
                spec_id=spec_id,
                revision=revision,
                as_of=now,
                timezone=timezone,
                explicit_subjects=explicit_subjects,
            ),
            engine_version=selected_engine,
        )
    return ResearchSpec(
        spec_id=spec_id,
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
        answer_spec=_compile_answer_spec(
            objective,
            spec_id=spec_id,
            revision=revision,
            as_of=now,
            timezone=timezone,
            explicit_subjects=explicit_subjects,
        ),
        engine_version=selected_engine,
    )


__all__ = ["compile_research_spec"]
