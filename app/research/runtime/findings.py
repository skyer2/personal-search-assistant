"""Validated Finding IR for worker-to-control-plane boundaries."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Finding:
    finding_id: str
    task_id: str
    subject_id: str
    dimension: str
    claim: str
    evidence_ids: list[str]
    confidence: float
    status: str
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Compatibility alias for existing coverage and report consumers.
        payload["summary"] = self.summary or self.claim
        return payload


def _safe_id(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(value or "").strip())
    return cleaned[:80] or fallback


def _evidence_ids(raw: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("evidence_ids", "artifact_ids"):
        values.extend(str(item) for item in (raw.get(key) or []) if str(item).strip())
    for key in ("artifact_id", "evidence_id"):
        value = str(raw.get(key) or "").strip()
        if value:
            values.append(value)
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return output[:12]


def normalize_finding(
    raw: Any,
    *,
    task_id: str,
    subject_id: str = "general",
    dimension: str = "general",
    index: int = 1,
) -> tuple[Finding | None, dict[str, Any]]:
    if isinstance(raw, str):
        raw = {"summary": raw}
    if not isinstance(raw, dict):
        return None, {
            "reason": "invalid_finding_type",
            "task_id": task_id,
            "index": index,
        }

    facts = raw.get("facts")
    first_fact = facts[0] if isinstance(facts, list) and facts else facts
    claim = str(raw.get("claim") or raw.get("summary") or first_fact or "").strip()
    resolved_task_id = str(raw.get("task_id") or task_id or "").strip()
    if not claim or not resolved_task_id:
        return None, {
            "reason": "missing_claim_or_task_id",
            "task_id": resolved_task_id or task_id,
            "index": index,
        }

    try:
        confidence = float(raw.get("confidence", 0.65))
    except (TypeError, ValueError):
        confidence = 0.65
    confidence = max(0.0, min(1.0, confidence))
    evidence_ids = _evidence_ids(raw)
    status = str(raw.get("status") or "").strip().lower()
    if status not in {"supported", "partial", "conflicted"}:
        status = "supported" if evidence_ids else "partial"

    finding_id = _safe_id(
        str(raw.get("finding_id") or ""), f"f_{_safe_id(resolved_task_id, 'task')}_{index}"
    )
    finding = Finding(
        finding_id=finding_id,
        task_id=resolved_task_id,
        subject_id=_safe_id(str(raw.get("subject_id") or subject_id or ""), "general"),
        dimension=str(raw.get("dimension") or dimension or "general")[:80],
        claim=claim[:1200],
        evidence_ids=evidence_ids,
        confidence=round(confidence, 3),
        status=status,
        summary=claim[:400],
    )
    return finding, {}


def normalize_findings(
    raw_findings: list[Any],
    *,
    task_id: str,
    subject_id: str = "general",
    dimension: str = "general",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_findings or [], start=1):
        finding, rejection = normalize_finding(
            raw,
            task_id=task_id,
            subject_id=subject_id,
            dimension=dimension,
            index=index,
        )
        if finding is None:
            rejected.append(rejection)
            continue
        key = f"{finding.task_id}:{finding.claim.lower()}:{finding.subject_id}:{finding.dimension}"
        if key in seen:
            rejected.append({"reason": "duplicate_finding", "task_id": finding.task_id, "index": index})
            continue
        seen.add(key)
        findings.append(finding.to_dict())
    return findings, rejected
