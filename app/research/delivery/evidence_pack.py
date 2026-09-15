"""Deterministic compact evidence pack for final synthesis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Iterable

from app.agent.harness.token_counter import estimate_tokens


HARD_MAX_SYNTHESIS_INPUT_TOKENS = 30_000
NORMAL_SYNTHESIS_INPUT_TOKENS = 8_000
COMPACT_SYNTHESIS_INPUT_TOKENS = 4_000


@dataclass(frozen=True)
class EvidencePack:
    criteria: tuple[str, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    conflict_resolutions: tuple[dict[str, Any], ...] = ()
    evidence_refs: tuple[str, ...] = ()
    research_summary: str = ""
    token_budget: int = NORMAL_SYNTHESIS_INPUT_TOKENS
    estimated_tokens: int = 0
    compact: bool = False


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _claim_text(finding: dict[str, Any]) -> str:
    claims = finding.get("claims")
    if isinstance(claims, list) and claims:
        return str(claims[0] or "").strip()
    return str(
        finding.get("claim")
        or finding.get("text")
        or finding.get("summary")
        or ""
    ).strip()


def _finding_refs(finding: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("evidence_ids", "artifact_ids", "source_ids", "sources"):
        raw = finding.get(key)
        if isinstance(raw, str):
            raw = [raw]
        values.extend(str(item) for item in raw or [] if str(item).strip())
    for key in ("evidence_id", "artifact_id", "source"):
        value = str(finding.get(key) or "").strip()
        if value:
            values.append(value)
    return list(dict.fromkeys(values))


def _criteria_rows(criteria: Iterable[Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for raw in criteria or []:
        if isinstance(raw, dict):
            key = str(
                raw.get("criterion_id")
                or raw.get("coverage_id")
                or raw.get("id")
                or raw.get("key")
                or ""
            ).strip()
            text = str(
                raw.get("question")
                or raw.get("name")
                or raw.get("description")
                or raw.get("label")
                or raw.get("text")
                or key
            ).strip()
        else:
            key = str(raw or "").strip()
            text = key
        if key and (key, text) not in rows:
            rows.append((key, text))
    return rows


def _parse_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        if not match:
            return None
        try:
            return datetime.fromisoformat(match.group(0))
        except ValueError:
            return None


def _freshness_score(evidence: dict[str, Any]) -> float:
    stamp = _parse_date(evidence.get("effective_at") or evidence.get("published_at"))
    if stamp is None:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - stamp).days
    if age_days <= 365:
        return 40.0
    if age_days <= 730:
        return 25.0
    return 5.0


def _evidence_score(evidence: dict[str, Any]) -> float:
    tier = str(evidence.get("source_tier") or "").upper()
    tier_score = {
        "PRIMARY": 100.0,
        "HIGH_QUALITY_SECONDARY": 70.0,
        "SECONDARY": 40.0,
    }.get(tier, 10.0)
    authority = max(0.0, min(1.0, float(evidence.get("authority_score") or 0.0)))
    return tier_score + authority * 20.0 + _freshness_score(evidence)


def _finding_score(
    finding: dict[str, Any],
    evidence_by_ref: dict[str, dict[str, Any]],
) -> float:
    refs = _finding_refs(finding)
    evidence_score = max((_evidence_score(evidence_by_ref[ref]) for ref in refs if ref in evidence_by_ref), default=0.0)
    try:
        confidence = max(0.0, min(1.0, float(finding.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return evidence_score + confidence * 20.0 + min(10.0, len(refs) * 2.0)


def _tokens(text: str) -> set[str]:
    return {
        item
        for item in re.findall(r"[\w\u3400-\u9fff]{2,}", _normalize(text))
        if item not in {"的", "和", "与", "及", "或", "在", "是", "有"}
    }


def _infer_criterion(
    claim: str,
    criteria: list[tuple[str, str]],
) -> str:
    if len(criteria) == 1:
        return criteria[0][0]
    claim_tokens = _tokens(claim)
    best_key = ""
    best_score = 0.0
    for key, text in criteria:
        target = _tokens(text)
        if not claim_tokens or not target:
            continue
        score = len(claim_tokens & target) / max(1, len(target))
        if score > best_score:
            best_key = key
            best_score = score
    return best_key if best_score >= 0.15 else "unbound"


def _resolved_winner_findings(
    conflicts: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    claims_by_id = {str(row.get("claim_id") or ""): row for row in claims if isinstance(row, dict)}
    output: list[dict[str, Any]] = []
    for conflict in conflicts:
        if str(conflict.get("status") or "") != "resolved":
            continue
        winner = claims_by_id.get(str(conflict.get("winner_id") or ""))
        if winner is None:
            continue
        output.append(
            {
                "finding_id": f"conflict-winner:{winner.get('claim_id')}",
                "claim": str(winner.get("text") or winner.get("claim") or ""),
                "evidence_ids": [str(item) for item in winner.get("evidence_ids") or []],
                "confidence": float(winner.get("confidence") or 1.0),
                "supported_criteria": [
                    str(item)
                    for item in winner.get("supported_criteria")
                    or [winner.get("criterion_id")]
                    or []
                    if str(item).strip()
                ],
            }
        )
    return output


def _pack_estimate(
    findings: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    compact: bool,
) -> int:
    summary = "\n".join(f"- {_claim_text(item)}" for item in findings)
    evidence_rows = [
        {
            "evidence_id": row.get("evidence_id"),
            "locator": row.get("locator"),
            "published_at": row.get("published_at"),
            "source_tier": row.get("source_tier"),
            "excerpt": str(row.get("excerpt_ref") or "")[:160 if compact else 320],
        }
        for row in evidence
    ]
    payload = {"findings": summary, "evidence": evidence_rows, "conflicts": conflicts}
    return estimate_tokens(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def build_evidence_pack(
    criteria: Iterable[Any],
    findings: Iterable[Any],
    claims: Iterable[Any],
    evidence: Iterable[Any],
    conflicts: Iterable[Any],
    token_budget: int,
    *,
    compact: bool = False,
) -> EvidencePack:
    """Select criterion-balanced, evidence-backed findings within a token budget."""
    criterion_rows = _criteria_rows(criteria)
    criteria_keys = {key for key, _ in criterion_rows}
    finding_rows = [dict(row) for row in findings or [] if isinstance(row, dict)]
    claim_rows = [dict(row) for row in claims or [] if isinstance(row, dict)]
    evidence_rows = [dict(row) for row in evidence or [] if isinstance(row, dict)]
    conflict_rows = [dict(row) for row in conflicts or [] if isinstance(row, dict)]

    evidence_by_id = {
        str(row.get("evidence_id") or ""): row for row in evidence_rows if row.get("evidence_id")
    }
    evidence_by_artifact = {
        str(row.get("artifact_ref") or ""): row for row in evidence_rows if row.get("artifact_ref")
    }
    evidence_by_ref = {**evidence_by_artifact, **evidence_by_id}

    claims_by_id = {str(row.get("claim_id") or ""): row for row in claim_rows}
    claims_by_text = {_normalize(row.get("text") or row.get("claim")): row for row in claim_rows}
    claims_by_evidence: dict[str, dict[str, Any]] = {}
    for row in claim_rows:
        for evidence_id in row.get("evidence_ids") or []:
            claims_by_evidence.setdefault(str(evidence_id), row)

    finding_rows.extend(_resolved_winner_findings(conflict_rows, claim_rows))
    deduped: dict[str, dict[str, Any]] = {}
    for finding in finding_rows:
        claim = _claim_text(finding)
        if not claim:
            continue
        refs = [ref for ref in _finding_refs(finding) if ref in evidence_by_ref]
        if not refs:
            continue
        key = _normalize(claim)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = {
                **finding,
                "claim": claim,
                "evidence_ids": refs,
            }
            continue
        existing_refs = list(existing.get("evidence_ids") or [])
        existing["evidence_ids"] = list(dict.fromkeys([*existing_refs, *refs]))
        try:
            existing["confidence"] = max(
                float(existing.get("confidence") or 0.0),
                float(finding.get("confidence") or 0.0),
            )
        except (TypeError, ValueError):
            pass

    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in deduped.values():
        explicit = [
            str(item)
            for item in finding.get("supported_criteria") or []
            if str(item) in criteria_keys
        ]
        claim = _claim_text(finding)
        claim_record = claims_by_id.get(str(finding.get("claim_id") or ""))
        if claim_record is None:
            claim_record = claims_by_text.get(_normalize(claim))
        if claim_record is None:
            for ref in finding.get("evidence_ids") or []:
                claim_record = claims_by_evidence.get(str(ref))
                if claim_record is not None:
                    break
        criterion = (
            explicit[0]
            if explicit
            else str((claim_record or {}).get("criterion_id") or "")
            or _infer_criterion(claim, criterion_rows)
        )
        grouped.setdefault(criterion or "unbound", []).append(finding)

    max_per_criterion = 2 if compact else 3
    selected: list[dict[str, Any]] = []
    for rows in grouped.values():
        rows.sort(
            key=lambda item: _finding_score(item, evidence_by_ref),
            reverse=True,
        )
        selected.extend(rows[:max_per_criterion])

    selected_conflicts = [
        row
        for row in conflict_rows
        if str(row.get("status") or "") == "resolved"
        or str(row.get("kind") or "") == "expected_disagreement"
        or bool(row.get("blocking"))
    ]
    selected_evidence_ids = list(
        dict.fromkeys(
            [
                *[str(ref) for item in selected for ref in item.get("evidence_ids") or []],
                *[str(ref) for row in selected_conflicts for ref in row.get("evidence_ids") or []],
            ]
        )
    )
    selected_evidence = [evidence_by_id[item] for item in selected_evidence_ids if item in evidence_by_id]

    budget = max(1_000, min(int(token_budget or 0), HARD_MAX_SYNTHESIS_INPUT_TOKENS))
    while (
        len(selected) > 1
        and _pack_estimate(selected, selected_evidence, selected_conflicts, compact) > budget
    ):
        by_criterion: dict[str, list[dict[str, Any]]] = {}
        for item in selected:
            criterion = str((item.get("supported_criteria") or ["unbound"])[0] or "unbound")
            by_criterion.setdefault(criterion, []).append(item)
        largest = max(by_criterion.values(), key=len)
        selected = [item for item in selected if item is not largest[-1]]

    selected_refs = list(
        dict.fromkeys(
            [str(ref) for item in selected for ref in item.get("evidence_ids") or []]
        )
    )
    conflict_refs = list(
        dict.fromkeys(
            [str(ref) for row in selected_conflicts for ref in row.get("evidence_ids") or []]
        )
    )
    selected_evidence = [
        evidence_by_id[item]
        for item in [*selected_refs, *conflict_refs]
        if item in evidence_by_id
    ]
    estimated = _pack_estimate(selected, selected_evidence, selected_conflicts, compact)
    summary = "\n".join(f"- {_claim_text(item)}" for item in selected)
    return EvidencePack(
        criteria=tuple(key for key, _ in criterion_rows),
        findings=tuple(selected),
        evidence=tuple(selected_evidence),
        conflict_resolutions=tuple(selected_conflicts),
        evidence_refs=tuple(str(row.get("evidence_id") or "") for row in selected_evidence),
        research_summary=summary,
        token_budget=budget,
        estimated_tokens=estimated,
        compact=compact,
    )


__all__ = [
    "COMPACT_SYNTHESIS_INPUT_TOKENS",
    "EvidencePack",
    "HARD_MAX_SYNTHESIS_INPUT_TOKENS",
    "NORMAL_SYNTHESIS_INPUT_TOKENS",
    "build_evidence_pack",
]
