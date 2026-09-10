"""Incremental, idempotent ingestion of Worker fan-in results."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.research.claims.extract import extract_claims_from_worker_results
from app.research.claims.reconcile import detect_conflict_edges
from app.research.claims.resolve import resolve_edges
from app.research.evidence.admission import admit_evidence
from app.research.evidence.models import EvidenceRecord
from app.research.evidence.policy import registrable_domain
from app.research.findings.compress import compress_worker_result
from app.research.runtime.task_identity import (
    normalize_search_query,
    worker_result_id,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_locator(source: Any) -> str:
    if isinstance(source, dict):
        return str(source.get("url") or source.get("locator") or source.get("source") or source.get("source_id") or "")
    return str(source or "")


def _source_kind(locator: str) -> str:
    if locator.startswith("artifact:"):
        return "artifact"
    if locator.startswith(("file:", "local:")):
        return "file"
    if locator.startswith("evidence:"):
        return "internal"
    return "web"


def _authority(locator: str, source_quality: str = "") -> tuple[str, float]:
    lowered = f"{locator} {source_quality}".lower()
    if any(token in lowered for token in ("primary", "official", "regulatory", "sec.gov", "arxiv.org", "/ir.", "investor", "docs.")):
        return "PRIMARY", 0.9
    if any(token in lowered for token in ("reuters", "bloomberg", "wsj", "ft.com", "nytimes", "nature.com")):
        return "HIGH_QUALITY_SECONDARY", 0.75
    return "SECONDARY", 0.55


def _evidence_id(task_id: str, locator: str, index: int, requested: str = "") -> str:
    if requested:
        return requested
    digest = hashlib.sha1(f"{task_id}|{locator}|{index}".encode("utf-8")).hexdigest()[:12]
    return f"evidence_{digest}"


def _prepare_evidence(rows: list[dict[str, Any]]) -> tuple[list[EvidenceRecord], list[dict[str, Any]]]:
    records: list[EvidenceRecord] = []
    prepared: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        task_id = str(row.get("task_id") or "")
        payload = dict(row.get("payload") or {})
        sources = list(payload.get("sources") or row.get("sources") or [])
        requested_ids = [str(item) for item in payload.get("evidence_ids") or [] if str(item).strip()]
        locators = [_source_locator(item) for item in sources if _source_locator(item).strip()]
        if not locators:
            locators = [f"evidence:{item}" for item in requested_ids]
        row_evidence: list[EvidenceRecord] = []
        source_quality = str(payload.get("source_quality") or "")
        for index, locator in enumerate(locators):
            requested = requested_ids[index] if index < len(requested_ids) else ""
            tier, authority = _authority(locator, source_quality)
            record = EvidenceRecord(
                evidence_id=_evidence_id(task_id, locator, index, requested),
                source_id=registrable_domain(locator) or locator,
                source_kind=_source_kind(locator),
                locator=locator,
                retrieved_at=str(payload.get("retrieved_at") or _now()),
                published_at=str(payload.get("published_at") or ""),
                effective_at=str(payload.get("effective_at") or ""),
                source_tier=tier,
                authority_score=authority,
                excerpt_ref=str(payload.get("excerpt_ref") or ""),
                artifact_ref=str(payload.get("artifact_ref") or ""),
                language=str(payload.get("language") or ""),
                task_id=task_id,
                run_id=str(row.get("run_id") or ""),
            )
            records.append(record)
            row_evidence.append(record)
        payload["evidence_ids"] = [item.evidence_id for item in row_evidence]
        row["payload"] = payload
        prepared.append(row)
    return records, prepared


def _task_metadata(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_plan = state.get("plan")
    if not isinstance(raw_plan, dict):
        return {}
    raw_steps = raw_plan.get("steps")
    steps = raw_steps if isinstance(raw_steps, list) else []
    output: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("task_id") or f"t{index}:{raw.get('step_type') or 'research'}")
        metadata = raw.get("metadata")
        output[task_id] = dict(metadata) if isinstance(metadata, dict) else {}
    return output


def ingest_new_worker_results(state: dict[str, Any]) -> dict[str, Any]:
    current_wave = max(0, int(state.get("dispatch_wave_id") or 0))
    processed = {
        str(item) for item in state.get("processed_worker_result_ids") or [] if str(item).strip()
    }
    selected: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    for raw in state.get("worker_results") or []:
        if not isinstance(raw, dict):
            continue
        result_id = worker_result_id(raw)
        if result_id in processed:
            continue
        row_wave = raw.get("dispatch_wave_id", raw.get("wave_id"))
        if row_wave is not None and int(row_wave or 0) != current_wave:
            continue
        selected.append(raw)
        selected_ids.append(result_id)

    if not selected:
        return {}

    evidence_rows, prepared_rows = _prepare_evidence(selected)
    admission = admit_evidence(evidence_rows)
    admitted_ids = {item.evidence_id for item in admission.admitted}
    metadata = _task_metadata(state)
    for row in selected:
        if isinstance(row.get("task_metadata"), dict):
            metadata.setdefault(str(row.get("task_id") or ""), dict(row["task_metadata"]))

    claims = extract_claims_from_worker_results(prepared_rows)
    for claim in claims:
        meta = metadata.get(claim.task_id, {})
        claim.subject_id = str(claim.subject_id or meta.get("subject_id") or claim.subject or "general")
        dimensions = [str(item) for item in meta.get("coverage_keys") or [] if str(item).strip()]
        claim.dimension_id = str(claim.dimension_id or (dimensions[0] if dimensions else "key_fact"))
        claim.evidence_ids = [item for item in claim.evidence_ids if item in admitted_ids]
    claims = [claim for claim in claims if claim.evidence_ids]
    edges = detect_conflict_edges(claims)
    reconciliation = resolve_edges(claims, edges)
    new_claim_ids = {claim.claim_id for claim in claims}
    new_edges = [
        edge for edge in edges
        if edge.left_id in new_claim_ids or edge.right_id in new_claim_ids
    ]
    new_resolutions = [
        row for row in reconciliation.resolutions
        if row.edge_id in {edge.edge_id for edge in new_edges}
    ]
    claims_by_task: dict[str, list[str]] = {}
    for claim in claims:
        claims_by_task.setdefault(claim.task_id, []).append(claim.claim_id)

    findings = []
    for row in prepared_rows:
        payload = dict(row.get("payload") or {})
        meta = metadata.get(str(row.get("task_id") or ""), {})
        findings.append(
            compress_worker_result(
                task_id=str(row.get("task_id") or ""),
                summary=str(row.get("summary") or payload.get("summary") or ""),
                claims=payload.get("facts") or payload.get("claims") or [],
                evidence_ids=payload.get("evidence_ids") or [],
                source_ids=payload.get("sources") or [],
                confidence=float(payload.get("confidence") or 0.0),
                unresolved_questions=payload.get("unresolved_questions") or [],
                limitations=payload.get("limitations") or [],
                wave_id=current_wave,
                supported_criteria=meta.get("target_criteria") or [],
                target_gaps=meta.get("target_gaps") or [],
                claim_ids=claims_by_task.get(str(row.get("task_id") or ""), []),
            ).to_dict()
        )

    previous_evidence = {
        str(row.get("evidence_id"))
        for row in state.get("evidence_records") or []
        if isinstance(row, dict)
    }
    previous_claims = {
        str(row.get("claim_id"))
        for row in state.get("claims") or []
        if isinstance(row, dict)
    }
    new_evidence = [item for item in admission.admitted if item.evidence_id not in previous_evidence]
    new_claims = [item for item in claims if item.claim_id not in previous_claims]
    previous_queries = [
        str(item) for item in state.get("search_query_fingerprints") or [] if str(item).strip()
    ]
    current_queries = list(previous_queries)
    for row in selected:
        payload = dict(row.get("payload") or {})
        queries = [normalize_search_query(str(item)) for item in payload.get("search_queries") or []]
        queries.append(normalize_search_query(str((row.get("task_metadata") or {}).get("objective") or row.get("summary") or "")))
        current_queries.extend(item for item in queries if item)
    unique_queries = list(dict.fromkeys(current_queries))
    duplicate_ratio = 1.0 - (len(unique_queries) / len(current_queries)) if current_queries else 0.0

    return {
        "processed_worker_result_ids": selected_ids,
        "evidence_records": [item.to_dict() for item in new_evidence],
        "evidence_refs": sorted({item.evidence_id for item in new_evidence}),
        "claims": [item.to_dict() for item in new_claims],
        "claim_conflicts": [item.to_dict() for item in new_edges],
        "claim_resolutions": [item.to_dict() for item in new_resolutions],
        "findings": findings,
        "search_query_fingerprints": unique_queries,
        "research_value_signal": {
            "new_high_quality_evidence_count": sum(
                1 for item in new_evidence if float(item.authority_score or 0.0) >= 0.7
            ),
            "new_supported_claim_count": len(new_claims),
            "duplicate_search_ratio": round(max(0.0, min(1.0, duplicate_ratio)), 4),
        },
    }


__all__ = ["ingest_new_worker_results"]
