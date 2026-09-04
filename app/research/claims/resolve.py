"""Resolve conflict edges into expected_disagreement / unresolved / resolved."""

from __future__ import annotations

from app.research.claims.models import (
    ClaimRecord,
    ClaimResolution,
    ConflictEdge,
    ReconciliationResult,
)
from app.research.claims.reconcile import detect_conflict_edges
from app.research.claims.extract import extract_claims_from_worker_results

_SCOPE_ALIASES = {
    "automotive": {"automotive", "汽车", "汽车业务"},
    "total": {"total", "总计", "整体", "全部", "gaap"},
    "verified": {"verified"},
    "pro": {"pro"},
}


def _scope_compatible(a: str, b: str) -> bool:
    sa = (a or "").lower().strip()
    sb = (b or "").lower().strip()
    if not sa or not sb:
        return True  # unknown scope → don't force unresolved solely by scope
    if sa == sb:
        return True
    for aliases in _SCOPE_ALIASES.values():
        if sa in aliases and sb in aliases:
            return True
    # different known scopes → expected disagreement (definition mismatch)
    known = {x for group in _SCOPE_ALIASES.values() for x in group}
    if sa in known and sb in known and sa != sb:
        return False
    return True


def _benchmarkish(claim: ClaimRecord) -> bool:
    blob = f"{claim.metric} {claim.text} {claim.scope}".lower()
    return any(
        tok in blob
        for tok in (
            "benchmark",
            "swe-bench",
            "swebench",
            "score",
            "accuracy",
            "pass@",
            "verified",
            "pro",
            "arena",
        )
    )


def resolve_edges(
    claims: list[ClaimRecord],
    edges: list[ConflictEdge],
) -> ReconciliationResult:
    by_id = {c.claim_id: c for c in claims}
    resolutions: list[ClaimResolution] = []
    unresolved: list[str] = []
    disclosed: list[str] = []
    resolved: list[str] = []

    for edge in edges:
        left = by_id.get(edge.left_id)
        right = by_id.get(edge.right_id)
        if left is None or right is None:
            continue

        # 1) scope / benchmark definition mismatch → expected
        if not _scope_compatible(left.scope, right.scope) or (
            _benchmarkish(left) or _benchmarkish(right)
        ):
            kind = "expected_disagreement"
            status = "disclosed"
            note = "scope_or_benchmark_mismatch"
            winner = ""
        # 2) clear authority gap → resolve toward primary
        elif abs(left.authority_score - right.authority_score) >= 0.35 and max(
            left.authority_score, right.authority_score
        ) >= 0.5:
            kind = "resolved"
            status = "resolved"
            winner = left.claim_id if left.authority_score >= right.authority_score else right.claim_id
            note = "authority_preference"
        # 3) same subject/metric/period + compatible scope + similar authority → unresolved
        elif (
            (left.subject and right.subject and left.subject.lower() == right.subject.lower())
            and left.metric == right.metric
            and (not left.period or not right.period or left.period == right.period)
            and _scope_compatible(left.scope, right.scope)
        ):
            kind = "unresolved_conflict"
            status = "unresolved"
            note = "same_scope_value_mismatch"
            winner = ""
        else:
            kind = "expected_disagreement"
            status = "disclosed"
            note = "heterogeneous_context"
            winner = ""

        label = edge.label or f"{left.text[:80]} vs {right.text[:80]}"
        res = ClaimResolution(
            edge_id=edge.edge_id,
            status=status,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            label=label,
            note=note,
            winner_id=winner,
            evidence_ids=list(dict.fromkeys(left.evidence_ids + right.evidence_ids)),
        )
        resolutions.append(res)
        if status == "unresolved":
            unresolved.append(label)
        elif status == "disclosed":
            disclosed.append(label)
        else:
            resolved.append(label)

    return ReconciliationResult(
        claims=claims,
        edges=edges,
        resolutions=resolutions,
        unresolved_labels=unresolved,
        disclosed_labels=disclosed,
        resolved_labels=resolved,
    )


def reconcile_worker_results(rows: list | None) -> ReconciliationResult:
    """End-to-end: extract → detect → resolve."""
    claims = extract_claims_from_worker_results(rows)
    edges = detect_conflict_edges(claims)
    return resolve_edges(claims, edges)
