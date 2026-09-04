"""Semantic research-task priority from Planner / Research Brief.

Scheduler must not invent required/optional by list position.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.agent.harness.state import ExecutionPlan, PlanStep

_SUPPORTING = frozenset(
    {
        "supporting_context",
        "supporting",
        "background",
        "history",
        "历史",
        "辅助",
        "背景",
        "回顾",
    }
)

_DEFAULT_EVIDENCE_TARGET = {
    "independent_sources": 3,
    "max_sources": 6,
    "prefer_primary": False,
}


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def match_coverage_keys(text: str, dimensions: Iterable[str]) -> list[str]:
    blob = _norm(text)
    if not blob:
        return []
    hits: list[str] = []
    for dim in dimensions or []:
        d = str(dim or "").strip()
        if not d:
            continue
        dn = _norm(d)
        if dn and (dn in blob or blob in dn):
            hits.append(d)
            continue
        # 短关键词：维度里长度≥2 的 token 命中也算覆盖
        tokens = [t for t in re_split(d) if len(t) >= 2]
        if tokens and sum(1 for t in tokens if t.lower() in blob) >= max(1, (len(tokens) + 1) // 2):
            hits.append(d)
    return list(dict.fromkeys(hits))


def re_split(text: str) -> list[str]:
    import re

    return [t for t in re.split(r"[\s,，、/：:]+", text or "") if t]


def _brief_dimensions(plan: ExecutionPlan, intent: Any | None = None) -> list[str]:
    dims: list[str] = []
    attached = getattr(intent, "brief", None) if intent is not None else None
    if attached is not None:
        dims = [str(x) for x in (getattr(attached, "dimensions", None) or []) if str(x).strip()]
    if dims:
        return dims
    brief = getattr(plan, "research_brief", "") or ""
    # plan.research_brief 通常是一句话，维度主要靠 intent.brief
    return [str(x) for x in dims if x]


def _parse_priority(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    text = str(raw).strip().upper()
    if text in {"0", "P0", "REQUIRED", "MUST"}:
        return 0
    if text in {"1", "P1", "OPTIONAL", "NICE"}:
        return 1
    if text in {"2", "P2", "EXPLORATORY"}:
        return 2
    try:
        n = int(text)
        return 0 if n <= 0 else min(2, n)
    except (TypeError, ValueError):
        return None


def normalize_priority_meta(
    step: PlanStep,
    *,
    dimensions: Iterable[str] | None = None,
    prefer_primary: bool = False,
) -> dict[str, Any]:
    """Fill coverage_keys / required / priority / evidence_target on a research step."""
    meta = dict(step.metadata) if isinstance(step.metadata, dict) else {}
    dims = [str(x) for x in (dimensions or []) if str(x).strip()]

    keys = [str(x) for x in (meta.get("coverage_keys") or []) if str(x).strip()]
    if not keys:
        keys = match_coverage_keys(
            " ".join([step.objective or "", step.description or "", step.task_id or ""]),
            dims,
        )
    meta["coverage_keys"] = keys

    explicit_required = meta.get("required")
    explicit_optional = meta.get("optional")
    prio = _parse_priority(meta.get("priority"))

    supporting_only = bool(keys) and all(
        _norm(k) in _SUPPORTING or any(tok in _norm(k) for tok in _SUPPORTING)
        for k in keys
    )
    covers_brief = bool(keys) and not supporting_only

    if explicit_required is not None or explicit_optional is not None:
        required = bool(explicit_required) if explicit_required is not None else (not bool(explicit_optional))
    elif prio is not None:
        required = prio <= 0
    elif step.depends_on:
        required = True
    elif dims:
        required = covers_brief
    else:
        # 没有 Brief 维度时不要猜 optional：全部 required
        required = True

    if prio is None:
        prio = 0 if required else 1
    meta["required"] = bool(required)
    meta["optional"] = not bool(required)
    meta["priority"] = int(prio)

    target = dict(_DEFAULT_EVIDENCE_TARGET)
    raw_target = meta.get("evidence_target") if isinstance(meta.get("evidence_target"), dict) else {}
    if raw_target:
        try:
            target["independent_sources"] = max(
                1, int(raw_target.get("independent_sources") or target["independent_sources"])
            )
        except (TypeError, ValueError):
            pass
        try:
            target["max_sources"] = max(
                target["independent_sources"],
                int(raw_target.get("max_sources") or target["max_sources"]),
            )
        except (TypeError, ValueError):
            pass
        if raw_target.get("prefer_primary") is not None:
            target["prefer_primary"] = bool(raw_target.get("prefer_primary"))
    if prefer_primary:
        target["prefer_primary"] = True
    meta["evidence_target"] = target
    # Runtime constraint, not decorative planner metadata. One retrieval call may
    # yield multiple documents, but it cannot keep searching past max_sources.
    current_cap = meta.get("max_retrieval_calls")
    try:
        meta["max_retrieval_calls"] = min(int(current_cap), int(target["max_sources"])) if current_cap is not None else int(target["max_sources"])
    except (TypeError, ValueError):
        meta["max_retrieval_calls"] = int(target["max_sources"])
    step.metadata = meta
    return meta


def stamp_semantic_priority(
    plan: ExecutionPlan,
    *,
    intent: Any | None = None,
    dimensions: Iterable[str] | None = None,
) -> ExecutionPlan:
    """Stamp Planner semantics; never assign required by list index."""
    from app.agent.harness.orchestration import RETRIEVAL_STEP_TYPES

    dims = list(dimensions or [])
    if not dims:
        dims = _brief_dimensions(plan, intent)
    prefer_primary = False
    attached = getattr(intent, "brief", None) if intent is not None else None
    if attached is not None:
        prefer_primary = bool(getattr(attached, "prefer_primary", False))

    for step in plan.steps:
        if step.step_type not in RETRIEVAL_STEP_TYPES:
            continue
        normalize_priority_meta(step, dimensions=dims, prefer_primary=prefer_primary)
    return plan
