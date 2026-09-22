"""Incremental, idempotent ingestion of Worker fan-in results."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from app.research.claims.extract import extract_claims_from_worker_results
from app.research.claims.admission import ClaimDraft, admit_claim
from app.research.claims.models import ClaimRecord
from app.research.claims.reconcile import detect_conflict_edges
from app.research.claims.resolve import resolve_edges
from app.research.evidence.admission import admit_evidence
from app.research.evidence.models import EvidenceRecord
from app.research.evidence.policy import registrable_domain
from app.research.evidence.quality import score_source
from app.research.findings.compress import compress_worker_result
from app.research.findings.integrity import complete_sentence
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
    # Artifact IDs identify raw captures, never canonical evidence.  Keep an
    # explicit canonical E-id even when a model cites the artifact directly.
    if requested and not requested.casefold().startswith("art-"):
        return requested
    digest = hashlib.sha1(f"{task_id}|{locator}|{index}".encode("utf-8")).hexdigest()[:12]
    return f"evidence_{digest}"


def _runtime_artifact_metadata(
    artifact_ref: str,
    locator: str,
    evidence_ref: str = "",
) -> dict[str, Any]:
    """Resolve objective evidence metadata from runtime-owned artifact records."""
    try:
        from app.agent.harness.artifacts import get_artifact_store
        from app.agent.harness.evidence_store import get_evidence_store

        artifact_store = get_artifact_store()
        artifact = artifact_store.get(artifact_ref) if artifact_ref else None
        if artifact is None and evidence_ref:
            span = get_evidence_store().get(evidence_ref)
            artifact_id = str(getattr(span, "artifact_id", "") or "")
            artifact = artifact_store.get(artifact_id) if artifact_id else None
        if artifact is None and locator:
            for item in artifact_store.iter_artifacts():
                if item.locator == locator and item.metadata.get("published_at"):
                    return dict(item.metadata)
        return dict(artifact.metadata) if artifact is not None else {}
    except Exception:
        return {}


def _resolve_artifact_source(artifact_ref: str) -> tuple[str, dict[str, Any]]:
    """Return the original source URL for a runtime artifact when available.

    A search worker commonly returns ``art-web-*`` IDs in its structured
    payload.  Those IDs are internal captures, not sources.  Resolving them
    here keeps the ledger, source scoring and citation manager anchored on the
    fetch URL instead of on an opaque artifact handle.
    """
    if not artifact_ref:
        return "", {}
    try:
        from app.agent.harness.artifacts import get_artifact_store

        artifact = get_artifact_store().get(artifact_ref)
        if artifact is None:
            return "", {}
        return str(getattr(artifact, "locator", "") or ""), dict(
            getattr(artifact, "metadata", {}) or {}
        )
    except Exception:
        return "", {}


def _prepare_evidence(
    rows: list[dict[str, Any]],
    *,
    task_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[EvidenceRecord], list[dict[str, Any]]]:
    records: list[EvidenceRecord] = []
    prepared: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        task_id = str(row.get("task_id") or "")
        meta = dict((task_metadata or {}).get(task_id) or {})
        row_metadata = row.get("task_metadata")
        if isinstance(row_metadata, dict):
            meta.update(row_metadata)
        payload = dict(row.get("payload") or {})
        sources = list(payload.get("sources") or row.get("sources") or [])
        requested_ids = [str(item) for item in payload.get("evidence_ids") or [] if str(item).strip()]
        artifact_ids = [str(item) for item in payload.get("artifact_ids") or [] if str(item).strip()]
        locators = [_source_locator(item) for item in sources if _source_locator(item).strip()]
        if not locators:
            locators = [f"evidence:{item}" for item in requested_ids]
        row_evidence: list[EvidenceRecord] = []
        source_quality = str(payload.get("source_quality") or "")
        for index, locator in enumerate(locators):
            requested = requested_ids[index] if index < len(requested_ids) else ""
            artifact_ref = str(
                payload.get("artifact_ref")
                or (artifact_ids[index] if index < len(artifact_ids) else "")
                or (locator if locator.casefold().startswith("art-") else "")
                or ""
            )
            resolved_locator, resolved_metadata = _resolve_artifact_source(artifact_ref)
            if resolved_locator:
                locator = resolved_locator
            tier, authority = _authority(locator, source_quality)
            runtime_metadata = _runtime_artifact_metadata(
                artifact_ref,
                locator,
                requested,
            )
            runtime_metadata = {**resolved_metadata, **runtime_metadata}
            quality = score_source(
                locator,
                declared_quality=source_quality,
                published_at=str(runtime_metadata.get("published_at") or ""),
            )
            tier = "PRIMARY" if quality.source_type == "primary" else (
                "HIGH_QUALITY_SECONDARY"
                if quality.source_type == "authoritative_secondary"
                else tier
            )
            record = EvidenceRecord(
                evidence_id=_evidence_id(task_id, locator, index, requested),
                source_id=registrable_domain(locator) or locator,
                source_kind=_source_kind(locator),
                locator=locator,
                retrieved_at=str(payload.get("retrieved_at") or _now()),
                published_at=str(
                    runtime_metadata.get("published_at")
                    or ""
                ),
                effective_at=str(
                    runtime_metadata.get("effective_at")
                    or ""
                ),
                source_tier=tier,
                authority_score=max(authority, quality.authority),
                source_type=quality.source_type,
                directness_score=quality.directness,
                freshness_score=quality.freshness,
                independence_score=quality.independence,
                completeness_score=quality.completeness,
                excerpt_ref=str(payload.get("excerpt_ref") or ""),
                artifact_ref=artifact_ref,
                language=str(payload.get("language") or ""),
                task_id=task_id,
                question_id=str(payload.get("question_id") or meta.get("question_id") or ""),
                ask_id=str(payload.get("ask_id") or meta.get("ask_id") or ""),
                run_id=str(row.get("run_id") or ""),
                excerpt_quality=float(payload.get("excerpt_quality") or 0.0),
                extraction_confidence=float(payload.get("extraction_confidence") or 0.0),
            )
            records.append(record)
            row_evidence.append(record)
        payload["evidence_ids"] = [item.evidence_id for item in row_evidence]
        # Workers frequently cite artifact handles returned by a tool.  Those
        # handles are useful provenance, but they are not the canonical E-id
        # used by Claim Admission.  Normalize structured finding bindings at
        # the ledger boundary before ClaimDraft extraction so a valid finding
        # cannot be rejected merely because it used ``art-web-*``.
        by_artifact = {
            item.artifact_ref: item.evidence_id
            for item in row_evidence
            if item.artifact_ref
        }
        by_locator = {
            item.locator: item.evidence_id
            for item in row_evidence
            if item.locator
        }
        normalized_findings: list[dict[str, Any]] = []
        for raw_finding in payload.get("findings") or []:
            if not isinstance(raw_finding, dict):
                continue
            finding = dict(raw_finding)
            canonical_refs: list[str] = []
            for ref in [
                *(str(item) for item in finding.get("evidence_ids") or []),
                *(str(item) for item in finding.get("artifact_ids") or []),
                *(str(item) for item in finding.get("sources") or []),
                str(finding.get("evidence_id") or ""),
                str(finding.get("artifact_id") or ""),
                str(finding.get("source") or finding.get("locator") or ""),
            ]:
                if not ref:
                    continue
                canonical = by_artifact.get(ref) or by_locator.get(ref)
                if canonical and canonical not in canonical_refs:
                    canonical_refs.append(canonical)
            if canonical_refs:
                finding["evidence_ids"] = canonical_refs
            normalized_findings.append(finding)
        payload["findings"] = normalized_findings
        row["payload"] = payload
        prepared.append(row)
    return records, prepared


@dataclass
class ResolvedFinding:
    claim: str
    evidence_ids: list[str]
    accepted: bool
    reason: str
    raw_refs: list[str] = field(default_factory=list)


def _evidence_indexes(
    admitted: list[EvidenceRecord],
) -> tuple[dict[str, EvidenceRecord], dict[str, EvidenceRecord], dict[str, EvidenceRecord]]:
    by_id: dict[str, EvidenceRecord] = {}
    by_artifact: dict[str, EvidenceRecord] = {}
    by_locator: dict[str, EvidenceRecord] = {}
    for record in admitted:
        by_id[record.evidence_id] = record
        if record.artifact_ref:
            by_artifact.setdefault(record.artifact_ref, record)
        if record.locator:
            by_locator.setdefault(record.locator, record)
    return by_id, by_artifact, by_locator


def _finding_refs(raw_finding: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("evidence_ids", "artifact_ids", "sources"):
        values.extend(str(item) for item in raw_finding.get(key) or [] if str(item).strip())
    for key in ("evidence_id", "artifact_id", "source", "locator"):
        value = str(raw_finding.get(key) or "").strip()
        if value:
            values.append(value)
    return list(dict.fromkeys(values))


def resolve_finding_evidence_refs(
    raw_finding: dict[str, Any],
    admitted_evidence: list[EvidenceRecord],
    *,
    indexes: tuple[
        dict[str, EvidenceRecord],
        dict[str, EvidenceRecord],
        dict[str, EvidenceRecord],
    ]
    | None = None,
) -> ResolvedFinding:
    """Resolve model refs to admitted canonical evidence IDs without inventing IDs."""
    by_id, by_artifact, by_locator = indexes or _evidence_indexes(admitted_evidence)
    claim = str(raw_finding.get("claim") or raw_finding.get("summary") or "").strip()
    raw_refs = _finding_refs(raw_finding)
    if not claim:
        return ResolvedFinding(claim, [], False, "missing_claim", raw_refs)
    if not raw_refs:
        return ResolvedFinding(claim, [], False, "missing_evidence_refs", raw_refs)

    evidence_ids: list[str] = []
    for ref in raw_refs:
        record = by_id.get(ref) or by_artifact.get(ref) or by_locator.get(ref)
        if record is None:
            continue
        if record.evidence_id not in evidence_ids:
            evidence_ids.append(record.evidence_id)
    if not evidence_ids:
        return ResolvedFinding(claim, [], False, "unresolved_evidence_refs", raw_refs)
    return ResolvedFinding(claim, evidence_ids, True, "", raw_refs)


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

    metadata = _task_metadata(state)
    for row in selected:
        if isinstance(row.get("task_metadata"), dict):
            # The dispatched Step is the authoritative lineage carrier.  A
            # serialized plan can contain an older, partial metadata shape;
            # never let that stale shape erase question/ask IDs attached at
            # dispatch time.
            task_id = str(row.get("task_id") or "")
            metadata[task_id] = {
                **dict(metadata.get(task_id) or {}),
                **dict(row["task_metadata"]),
            }
    evidence_rows, prepared_rows = _prepare_evidence(selected, task_metadata=metadata)
    admission = admit_evidence(evidence_rows, require_verified_artifact=True)
    admitted_ids = {item.evidence_id for item in admission.admitted}

    claims = extract_claims_from_worker_results(prepared_rows)
    evidence_by_id = {item.evidence_id: item for item in admission.admitted}
    claim_admission_diagnostics: list[dict[str, Any]] = []
    for claim in claims:
        meta = metadata.get(claim.task_id, {})
        claim.subject_id = str(claim.subject_id or meta.get("subject_id") or claim.subject or "general")
        dimensions = [str(item) for item in meta.get("coverage_keys") or [] if str(item).strip()]
        claim.dimension_id = str(claim.dimension_id or (dimensions[0] if dimensions else "key_fact"))
        target_criteria = [
            str(item) for item in meta.get("target_criteria") or [] if str(item).strip()
        ]
        claim.criterion_id = str(
            claim.criterion_id
            or meta.get("criterion_id")
            or (target_criteria[0] if target_criteria else "")
        )
        claim.evidence_ids = [item for item in claim.evidence_ids if item in admitted_ids]
        claim.question_id = str(meta.get("question_id") or "")
        claim.ask_id = str(meta.get("ask_id") or (f"a{claim.question_id[1:]}" if claim.question_id.startswith("q") else ""))
        admission_result = admit_claim(
            ClaimDraft(
                draft_id=claim.claim_id,
                ask_id=claim.ask_id,
                question_id=claim.question_id,
                task_id=claim.task_id,
                statement=claim.text,
                claim_type=claim.claim_type,
                evidence_refs=list(claim.evidence_ids),
                provenance="worker",
                confidence=claim.confidence,
            ),
            evidence_by_id,
        )
        claim.validated = admission_result.admitted
        claim.publishability_score = admission_result.publishability_score
        claim.admission_reasons = list(admission_result.reasons)
        claim_admission_diagnostics.append({
            "claim_id": claim.claim_id,
            "task_id": claim.task_id,
            "question_id": claim.question_id,
            "status": "admitted" if admission_result.admitted else "rejected",
            **admission_result.to_dict(),
        })
    claims = [claim for claim in claims if claim.evidence_ids and claim.validated]
    existing_claims = [
        ClaimRecord.from_dict(row)
        for row in state.get("claims") or []
        if isinstance(row, dict)
    ]
    all_claims = [*existing_claims, *claims]
    edges = detect_conflict_edges(all_claims)
    reconciliation = resolve_edges(all_claims, edges)
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
    finding_diagnostics: list[dict[str, Any]] = []
    raw_finding_count = 0
    accepted_finding_count = 0
    rejected_finding_count = 0
    unresolved_evidence_ref_count = 0
    partial_fallback_finding_count = 0
    evidence_indexes = _evidence_indexes(admission.admitted)
    for row in prepared_rows:
        payload = dict(row.get("payload") or {})
        meta = metadata.get(str(row.get("task_id") or ""), {})
        row_task_id = str(row.get("task_id") or "")
        row_admitted_ids = [
            str(item)
            for item in payload.get("evidence_ids") or []
            if str(item) in admitted_ids
        ]
        payload_findings = [
            dict(item)
            for item in payload.get("findings") or []
            if isinstance(item, dict)
        ]
        raw_finding_count += len(payload_findings)
        accepted_for_row: list[dict[str, Any]] = []
        for raw_finding in payload_findings:
            claim_text = str(raw_finding.get("claim") or raw_finding.get("summary") or "")
            resolved = resolve_finding_evidence_refs(
                raw_finding,
                admission.admitted,
                indexes=evidence_indexes,
            )
            raw_claim_type = str(raw_finding.get("claim_type") or "fact")
            claim_type = raw_claim_type if raw_claim_type in {"fact", "inference", "forecast", "attributed_opinion"} else "fact"
            admission_result = admit_claim(
                ClaimDraft(
                    draft_id=str(raw_finding.get("finding_id") or f"draft_{row_task_id}"),
                    ask_id=str(meta.get("ask_id") or (f"a{str(meta.get('question_id') or '')[1:]}" if str(meta.get("question_id") or "").startswith("q") else "")),
                    question_id=str(meta.get("question_id") or ""),
                    task_id=row_task_id,
                    statement=claim_text,
                    claim_type=claim_type,  # type: ignore[arg-type]
                    evidence_refs=list(resolved.evidence_ids),
                    provenance="worker",
                    confidence=float(raw_finding.get("confidence") or payload.get("confidence") or 0.0),
                ),
                evidence_by_id,
            )
            if not admission_result.admitted:
                rejected_finding_count += 1
                finding_diagnostics.append(
                    {
                        "task_id": row_task_id,
                        "claim": claim_text,
                        "status": "rejected",
                        "reason": ",".join(admission_result.reasons),
                        "claim_admission": admission_result.to_dict(),
                    }
                )
                continue
            accepted_finding_count += 1
            accepted_for_row.append(
                {
                    **raw_finding,
                    "claim": resolved.claim,
                    "ask_id": str(meta.get("ask_id") or (f"a{str(meta.get('question_id') or '')[1:]}" if str(meta.get("question_id") or "").startswith("q") else "")),
                    "question_id": str(meta.get("question_id") or ""),
                    "validated": True,
                    "publishability_score": admission_result.publishability_score,
                    "evidence_ids": resolved.evidence_ids[:12],
                    "status": "supported",
                    "partial": False,
                    "claims": [resolved.claim],
                    "supported_criteria": list(
                        dict.fromkeys(
                            [
                                *(
                                    str(item)
                                    for item in raw_finding.get("supported_criteria") or []
                                    if str(item).strip()
                                ),
                                *(
                                    str(item)
                                    for item in meta.get("target_criteria") or []
                                    if str(item).strip()
                                ),
                            ]
                        )
                    ),
                    "target_gaps": list(
                        dict.fromkeys(
                            [
                                *(
                                    str(item)
                                    for item in raw_finding.get("target_gaps") or []
                                    if str(item).strip()
                                ),
                                *(
                                    str(item)
                                    for item in meta.get("target_gaps") or []
                                    if str(item).strip()
                                ),
                            ]
                        )
                    ),
                    "claim_ids": claims_by_task.get(row_task_id, []),
                    "wave_id": current_wave,
                }
            )

        if accepted_for_row:
            findings.extend(accepted_for_row)

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
        "finding_diagnostics": finding_diagnostics,
        "claim_admission_diagnostics": claim_admission_diagnostics,
        "salvage_evidence": [
            item
            for row in prepared_rows
            for item in list((row.get("payload") or {}).get("salvage_evidence") or [])
            if isinstance(item, dict)
        ],
        "search_query_fingerprints": unique_queries,
        "research_value_signal": {
            "new_high_quality_evidence_count": sum(
                1 for item in new_evidence if float(item.authority_score or 0.0) >= 0.7
            ),
            "new_supported_claim_count": len(new_claims),
            "duplicate_search_ratio": round(max(0.0, min(1.0, duplicate_ratio)), 4),
            "raw_finding_count": raw_finding_count,
            "accepted_finding_count": accepted_finding_count,
            "rejected_finding_count": rejected_finding_count,
            "unresolved_evidence_ref_count": unresolved_evidence_ref_count,
            "partial_fallback_finding_count": partial_fallback_finding_count,
            "claim_draft_count": len(claim_admission_diagnostics),
            "admitted_claim_count": len(new_claims),
            "rejected_claim_count": sum(1 for item in claim_admission_diagnostics if item.get("status") == "rejected"),
            "salvage_evidence_count": sum(
                len(list((row.get("payload") or {}).get("salvage_evidence") or []))
                for row in prepared_rows
            ),
        },
    }


__all__ = ["ingest_new_worker_results", "resolve_finding_evidence_refs"]
