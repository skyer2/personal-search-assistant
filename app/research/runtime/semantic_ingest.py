"""Canonical semantic ingest boundary for worker fan-in results."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.agent.harness.state import ExecutionPlan
from app.research.claims.extract import extract_claims_from_worker_results
from app.research.claims.models import ClaimRecord
from app.research.claims.reconcile import detect_conflict_edges
from app.research.claims.resolve import resolve_edges
from app.research.assessment.marginal_gain import assess_marginal_gain, record_semantic_wave
from app.research.coverage.assessor import assess_coverage
from app.research.coverage.compiler import compile_coverage_contract
from app.research.coverage.gaps import build_semantic_gaps
from app.research.evidence.admission import admit_evidence
from app.research.evidence.models import EvidenceRecord
from app.research.evidence.policy import registrable_domain
from app.research.planning.candidate import build_candidate_set
from app.research.spec.models import ResearchSpec


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan_from_state(state: dict[str, Any]) -> ExecutionPlan | None:
    raw = state.get("plan")
    return ExecutionPlan.from_dict(raw) if isinstance(raw, dict) and raw else None


def _task_metadata(plan: ExecutionPlan | None) -> dict[str, dict[str, Any]]:
    return {
        step.resolved_task_id(index): dict(step.metadata or {})
        for index, step in enumerate(plan.steps)
    } if plan is not None else {}


def _authority(locator: str, source_quality: str = "") -> tuple[str, float]:
    lowered = f"{locator} {source_quality}".lower()
    if any(token in lowered for token in ("primary", "official", "regulatory", "sec.gov", "arxiv.org", "/ir.", "investor", "docs.")):
        return "PRIMARY", 0.9
    if any(token in lowered for token in ("reuters", "bloomberg", "wsj", "ft.com", "nytimes", "nature.com")):
        return "HIGH_QUALITY_SECONDARY", 0.75
    return "SECONDARY", 0.55


def _source_locator(source: Any) -> str:
    if isinstance(source, dict):
        return str(source.get("url") or source.get("locator") or source.get("source") or source.get("source_id") or "")
    return str(source or "")


def _source_kind(locator: str) -> str:
    if locator.startswith("artifact:"):
        return "artifact"
    if locator.startswith("file:") or locator.startswith("local:"):
        return "file"
    if locator.startswith("evidence:"):
        return "internal"
    return "web"


def _evidence_id(task_id: str, locator: str, index: int, requested: str = "") -> str:
    if requested:
        return requested
    digest = hashlib.sha1(f"{task_id}|{locator}|{index}".encode("utf-8")).hexdigest()[:12]
    return f"evidence_{digest}"


def _build_evidence_rows(
    state: dict[str, Any], plan: ExecutionPlan | None
) -> tuple[list[EvidenceRecord], list[dict[str, Any]]]:
    records: list[EvidenceRecord] = []
    prepared_rows: list[dict[str, Any]] = []
    for raw in state.get("worker_results") or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        task_id = str(row.get("task_id") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        payload = dict(payload)
        raw_sources = list(payload.get("sources") or row.get("sources") or [])
        requested_ids = [str(item) for item in payload.get("evidence_ids") or [] if str(item).strip()]
        locators = [_source_locator(source) for source in raw_sources if _source_locator(source).strip()]
        if not locators:
            locators = [f"evidence:{requested_id}" for requested_id in requested_ids]
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
                run_id=str(state.get("run_id") or ""),
            )
            records.append(record)
            row_evidence.append(record)
        payload["evidence_ids"] = [record.evidence_id for record in row_evidence]
        row["payload"] = payload
        prepared_rows.append(row)
    return records, prepared_rows


def _enrich_claims(
    claims: list[ClaimRecord],
    metadata: dict[str, dict[str, Any]],
    admitted_ids: set[str],
) -> list[ClaimRecord]:
    enriched: list[ClaimRecord] = []
    for claim in claims:
        meta = metadata.get(claim.task_id, {})
        claim.subject_id = str(claim.subject_id or meta.get("subject_id") or claim.subject or "general")
        dimensions = [str(item) for item in meta.get("coverage_keys") or [] if str(item).strip()]
        claim.dimension_id = str(claim.dimension_id or (dimensions[0] if dimensions else "key_fact"))
        claim.evidence_ids = [item for item in claim.evidence_ids if item in admitted_ids]
        if claim.evidence_ids:
            claim.normalized_key = hashlib.sha1(
                f"{claim.subject_id}|{claim.dimension_id}|{claim.metric}|{claim.unit}|{claim.period}".encode("utf-8")
            ).hexdigest()[:12]
            enriched.append(claim)
    return enriched


def ingest_semantics(state: dict[str, Any]) -> dict[str, Any]:
    """Transform worker output into the only canonical semantic runtime state."""
    plan = _plan_from_state(state)
    task_metadata = _task_metadata(plan)
    for row in state.get("worker_results") or []:
        if isinstance(row, dict) and isinstance(row.get("task_metadata"), dict):
            task_metadata.setdefault(str(row.get("task_id") or ""), dict(row["task_metadata"]))
    spec = ResearchSpec.from_dict(state.get("research_spec"))
    evidence_rows, prepared_rows = _build_evidence_rows(state, plan)
    admission = admit_evidence(evidence_rows)
    admitted = admission.admitted
    admitted_ids = {record.evidence_id for record in admitted}

    claims = _enrich_claims(
        extract_claims_from_worker_results(prepared_rows),
        task_metadata,
        admitted_ids,
    )
    edges = detect_conflict_edges(claims)
    reconciliation = resolve_edges(claims, edges)

    candidate_update: dict[str, Any] = {}
    candidate_names: list[str] | None = None
    discovery_steps = [
        step
        for step in (plan.steps if plan else [])
        if str((step.metadata or {}).get("task_kind") or "") == "discovery"
    ]
    if discovery_steps:
        task_status = {
            task_id: task.get("execution_status", "")
            for task_id, task in dict(state.get("tasks") or {}).items()
        }
        candidate_payload = build_candidate_set(
            plan,
            worker_rows=prepared_rows,
            task_status=task_status,
            query=spec.objective,
            brief={},
        )
        candidate_update = {"candidate_set": candidate_payload}
        if candidate_payload.get("available"):
            candidate_names = [str(item) for item in candidate_payload.get("items") or []]
    else:
        existing = state.get("candidate_set") if isinstance(state.get("candidate_set"), dict) else {}
        if existing.get("available"):
            candidate_names = [str(item) for item in existing.get("items") or []]

    contract = compile_coverage_contract(spec, candidate_names=candidate_names)
    coverage = assess_coverage(
        contract,
        claims=[claim.to_dict() for claim in claims],
        evidence=[record.to_dict() for record in admitted],
        claim_conflicts=[edge.to_dict() for edge in edges],
        claim_resolutions=[row.to_dict() for row in reconciliation.resolutions],
    )
    candidate_payload = candidate_update.get("candidate_set") or (
        state.get("candidate_set") if isinstance(state.get("candidate_set"), dict) else {}
    )
    candidate_available = bool(candidate_payload.get("available"))
    gaps = build_semantic_gaps(
        coverage,
        candidate_available=candidate_available,
        discovery_required=spec.reasoning_requirements.discovery,
        previous_gaps=list((state.get("semantic_gaps") or {}).values()),
    )
    previous_coverage = state.get("coverage_state") if isinstance(state.get("coverage_state"), dict) else {}
    previous_high_quality = {
        str(row.get("evidence_id"))
        for row in state.get("evidence_records") or []
        if isinstance(row, dict) and float(row.get("authority_score") or 0.0) >= 0.7
    }
    current_high_quality = {
        record.evidence_id
        for record in admitted
        if float(record.authority_score or 0.0) >= 0.7
    }
    previous_confidence = max(
        [float(row.get("confidence") or 0.0) for row in state.get("claims") or [] if isinstance(row, dict)],
        default=0.0,
    )
    current_confidence = max([claim.confidence for claim in claims], default=0.0)
    wave_gain = record_semantic_wave(
        wave_id=max(1, int(state.get("dispatch_wave_id") or 1)),
        previous_coverage_ratio=float(previous_coverage.get("coverage_ratio") or 0.0),
        current_coverage_ratio=coverage.coverage_ratio,
        previous_covered_ids=[str(item) for item in previous_coverage.get("covered_ids") or []],
        current_covered_ids=coverage.covered_ids,
        previous_gap_ids=list((state.get("semantic_gaps") or {}).keys()),
        current_gap_ids=[gap.gap_id for gap in gaps],
        previous_conflict_ids=[str(item) for item in previous_coverage.get("conflicted_ids") or []],
        current_conflict_ids=coverage.conflicted_ids,
        previous_confidence=previous_confidence,
        current_confidence=current_confidence,
        previous_high_quality_source_ids=sorted(previous_high_quality),
        current_high_quality_source_ids=sorted(current_high_quality),
    )
    marginal = assess_marginal_gain(
        [*(state.get("semantic_wave_gains") or []), wave_gain.to_dict()],
    )
    return {
        "research_spec": spec.to_dict(),
        "coverage_contract": contract.to_dict(),
        "coverage_state": coverage.to_dict(),
        "evidence_records": [record.to_dict() for record in admitted],
        "evidence_refs": sorted(admitted_ids),
        "claims": [claim.to_dict() for claim in claims],
        "claim_conflicts": [edge.to_dict() for edge in edges],
        "claim_resolutions": [row.to_dict() for row in reconciliation.resolutions],
        "semantic_gaps": {gap.gap_id: gap.to_dict() for gap in gaps},
        "findings": [claim.to_dict() for claim in claims],
        "semantic_wave_gains": [wave_gain.to_dict()],
        "marginal_gain": marginal.to_dict(),
        "semantic_stall": 2 if marginal.stalled else 0,
        **candidate_update,
    }


__all__ = ["ingest_semantics"]
