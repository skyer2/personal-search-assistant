"""Cross-worker claim clustering and conflict edge detection."""

from __future__ import annotations

import hashlib
from itertools import combinations

from app.research.claims.models import ClaimRecord, ConflictEdge


def _edge_id(left: str, right: str) -> str:
    a, b = sorted([left, right])
    return f"cedge_{hashlib.sha1(f'{a}:{b}'.encode()).hexdigest()[:10]}"


def _same_metric_key(claim: ClaimRecord) -> str | None:
    if not claim.metric or claim.value is None:
        return None
    subject = (claim.subject or "").lower().strip()
    metric = claim.metric.lower().strip()
    unit = claim.unit.lower().strip()
    period = claim.period.lower().strip()
    # 故意不把 scope 放进 key：同 subject/metric/period 不同 scope 仍需比对
    return f"{subject}|{metric}|{unit}|{period}"


def _values_conflict(a: float, b: float) -> bool:
    base = max(abs(a), abs(b), 1e-6)
    return abs(a - b) > max(0.08 * base, 0.01)


_POSITIVE_TOKENS = (
    "yes",
    "true",
    "profitable",
    "profit",
    "increase",
    "grew",
    "growth",
    "supported",
    "leading",
    "领先",
    "盈利",
    "增长",
    "支持",
    "成立",
)
_NEGATIVE_TOKENS = (
    "no",
    "false",
    "not profitable",
    "loss",
    "loss-making",
    "decrease",
    "declined",
    "unsupported",
    "lagging",
    "亏损",
    "下滑",
    "反对",
    "不成立",
    "未盈利",
)


def _semantic_polarity(claim: ClaimRecord) -> str:
    text = f"{claim.text} {claim.metric}".lower()
    if any(token in text for token in _NEGATIVE_TOKENS):
        return "negative"
    if any(token in text for token in _POSITIVE_TOKENS):
        return "positive"
    return ""


def detect_conflict_edges(claims: list[ClaimRecord]) -> list[ConflictEdge]:
    """Global fan-in conflict detection across workers."""
    by_key: dict[str, list[ClaimRecord]] = {}
    for claim in claims:
        key = _same_metric_key(claim)
        if not key:
            continue
        by_key.setdefault(key, []).append(claim)

    edges: list[ConflictEdge] = []
    seen: set[str] = set()
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        # 跨 task 优先；同 task 内差异也可能有意义
        for left, right in combinations(group, 2):
            if left.value is None or right.value is None:
                continue
            if not _values_conflict(float(left.value), float(right.value)):
                continue
            eid = _edge_id(left.claim_id, right.claim_id)
            if eid in seen:
                continue
            seen.add(eid)
            label = (
                f"{left.subject or left.task_id}:{left.metric} "
                f"{left.value:g}{left.unit} vs {right.value:g}{right.unit}"
                f" ({left.period or '?'} / {left.scope or 'scope?'} vs {right.scope or 'scope?'})"
            )
            edges.append(
                ConflictEdge(
                    edge_id=eid,
                    left_id=left.claim_id,
                    right_id=right.claim_id,
                    kind="unresolved_conflict",
                    reason="value_mismatch",
                    label=label[:240],
                )
            )

    semantic_groups: dict[tuple[str, str], list[ClaimRecord]] = {}
    for claim in claims:
        if claim.value is not None or not claim.criterion_id:
            continue
        polarity = _semantic_polarity(claim)
        if not polarity:
            continue
        key = (claim.criterion_id, (claim.subject or claim.subject_id or "").lower())
        semantic_groups.setdefault(key, []).append(claim)

    for (_criterion_id, _subject), group in semantic_groups.items():
        if len(group) < 2:
            continue
        for left, right in combinations(group, 2):
            if _semantic_polarity(left) == _semantic_polarity(right):
                continue
            eid = _edge_id(left.claim_id, right.claim_id)
            if eid in seen:
                continue
            seen.add(eid)
            label = f"{left.subject or left.task_id}:{left.text[:80]} vs {right.text[:80]}"
            edges.append(
                ConflictEdge(
                    edge_id=eid,
                    left_id=left.claim_id,
                    right_id=right.claim_id,
                    kind="unresolved_conflict",
                    reason="semantic_polarity_mismatch",
                    label=label[:240],
                )
            )
    return edges
