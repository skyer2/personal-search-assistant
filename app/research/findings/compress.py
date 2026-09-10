"""Deterministic worker-result compression for Supervisor input."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from app.research.findings.models import ResearchFinding


def _finding_id(task_id: str, summary: str, evidence: tuple[str, ...], wave_id: int) -> str:
    payload = "|".join([task_id, str(wave_id), summary, *evidence])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"finding_{digest}"


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, dict):
        text = str(value.get("claim") or value.get("text") or value.get("summary") or "").strip()
        return (text,) if text else ()
    return tuple(str(item).strip() for item in value or [] if str(item).strip())


def compress_worker_result(
    *, task_id: str, summary: str, claims: Iterable[Any] | None = None,
    evidence_ids: Iterable[Any] | None = None, source_ids: Iterable[Any] | None = None,
    confidence: float = 0.0, unresolved_questions: Iterable[Any] | None = None,
    limitations: Iterable[Any] | None = None, wave_id: int = 0,
    supported_criteria: Iterable[Any] | None = None, target_gaps: Iterable[Any] | None = None,
    claim_ids: Iterable[Any] | None = None,
) -> ResearchFinding:
    clean_summary = str(summary or "").strip() or "No worker summary was produced."
    evidence = tuple(dict.fromkeys(str(item) for item in evidence_ids or [] if str(item).strip()))
    sources = tuple(dict.fromkeys(str(item) for item in source_ids or [] if str(item).strip()))
    clean_claims = tuple(dict.fromkeys(_strings(claims)))
    finding_limitations = tuple(dict.fromkeys(_strings(limitations)))
    if not evidence:
        finding_limitations = (*finding_limitations, "no_admitted_evidence")
    return ResearchFinding(
            finding_id=_finding_id(str(task_id or ""), clean_summary, evidence, int(wave_id or 0)),
            task_id=str(task_id or ""), wave_id=max(0, int(wave_id or 0)),
            supported_criteria=tuple(dict.fromkeys(_strings(supported_criteria))),
            target_gaps=tuple(dict.fromkeys(_strings(target_gaps))),
            claim_ids=tuple(dict.fromkeys(_strings(claim_ids))),
            summary=clean_summary, claims=clean_claims, evidence_ids=evidence,
        source_ids=sources, confidence=max(0.0, min(1.0, float(confidence or 0.0))),
        unresolved_questions=tuple(dict.fromkeys(_strings(unresolved_questions))),
        limitations=finding_limitations,
    )


def compress_worker_results(rows: Iterable[dict[str, Any]]) -> list[ResearchFinding]:
    findings: dict[str, ResearchFinding] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        finding = compress_worker_result(
            task_id=str(raw.get("task_id") or ""),
            summary=str(raw.get("summary") or payload.get("summary") or ""),
            claims=payload.get("claims") or payload.get("facts") or raw.get("claims") or raw.get("facts") or [],
            evidence_ids=payload.get("evidence_ids") or raw.get("evidence_ids") or [],
            source_ids=payload.get("sources") or raw.get("sources") or [],
            confidence=float(payload.get("confidence") or raw.get("confidence") or 0.0),
            unresolved_questions=payload.get("unresolved_questions") or [],
            limitations=payload.get("limitations") or [],
            wave_id=int(raw.get("dispatch_wave_id") or raw.get("wave_id") or 0),
            supported_criteria=(raw.get("task_metadata") or {}).get("target_criteria") or [],
            target_gaps=(raw.get("task_metadata") or {}).get("target_gaps") or [],
            claim_ids=payload.get("claim_ids") or [],
        )
        findings[finding.finding_id] = finding
    return list(findings.values())


__all__ = ["compress_worker_result", "compress_worker_results"]
