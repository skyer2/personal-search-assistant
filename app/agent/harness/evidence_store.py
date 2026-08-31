"""
Evidence Span Store — Claim → EvidenceSpan → Artifact 的可回读证据链。

Worker 只把 claim + evidence_id 放进 LLM context；Synthesizer 不确定时
调用 read_evidence(E27) 取原始 span，而不是把全部 digest 塞进窗口。
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.agent.harness.artifacts import Artifact, ArtifactStore, get_artifact_store

_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]{2,}")


@dataclass
class EvidenceSpan:
    evidence_id: str
    artifact_id: str
    locator: str
    start_offset: int
    end_offset: int
    text: str
    source_kind: str = "text"
    source_quality: str = "unknown"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    step_index: int = -1
    step_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EvidenceSpan":
        row = data or {}
        return cls(
            evidence_id=str(row.get("evidence_id") or ""),
            artifact_id=str(row.get("artifact_id") or ""),
            locator=str(row.get("locator") or ""),
            start_offset=int(row.get("start_offset") or 0),
            end_offset=int(row.get("end_offset") or 0),
            text=str(row.get("text") or ""),
            source_kind=str(row.get("source_kind") or "text"),
            source_quality=str(row.get("source_quality") or "unknown"),
            timestamp=str(row.get("timestamp") or datetime.now().isoformat()),
            step_index=int(row.get("step_index") if row.get("step_index") is not None else -1),
            step_type=str(row.get("step_type") or ""),
            metadata=dict(row.get("metadata") or {}),
        )


@dataclass
class Finding:
    claim_id: str
    claim: str
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    freshness: str = ""
    source_quality: str = "unknown"
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Finding":
        row = data or {}
        return cls(
            claim_id=str(row.get("claim_id") or ""),
            claim=str(row.get("claim") or ""),
            evidence_ids=[str(x) for x in (row.get("evidence_ids") or [])],
            confidence=float(row.get("confidence") or 1.0),
            freshness=str(row.get("freshness") or ""),
            source_quality=str(row.get("source_quality") or "unknown"),
            conflicts=[str(x) for x in (row.get("conflicts") or [])],
        )


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _overlap_score(query: str, target: str) -> float:
    q = _tokens(query)
    t = _tokens(target)
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


class EvidenceStore:
    def __init__(self, session_dir: Path | None = None):
        self.session_dir = Path(session_dir) if session_dir else None
        self.spans: dict[str, EvidenceSpan] = {}
        self.findings: dict[str, Finding] = {}
        self.conflicts: list[dict[str, Any]] = []
        self._span_counter = 0
        self._claim_counter = 0

    def __len__(self) -> int:
        return len(self.spans)

    def add_span(
        self,
        text: str,
        *,
        artifact_id: str = "",
        locator: str = "",
        start_offset: int = 0,
        end_offset: int | None = None,
        source_kind: str = "text",
        source_quality: str = "unknown",
        step_index: int = -1,
        step_type: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceSpan:
        body = (text or "").strip()
        if not body:
            raise ValueError("empty evidence span")
        end = int(end_offset) if end_offset is not None else start_offset + len(body)
        for existing in self.spans.values():
            if (
                existing.artifact_id == artifact_id
                and existing.locator == locator
                and existing.text == body
            ):
                return existing
        self._span_counter += 1
        span = EvidenceSpan(
            evidence_id=f"E{self._span_counter}",
            artifact_id=artifact_id,
            locator=locator,
            start_offset=int(start_offset or 0),
            end_offset=end,
            text=body[:4000],
            source_kind=source_kind,
            source_quality=source_quality,
            step_index=step_index,
            step_type=step_type,
            metadata=dict(metadata or {}),
        )
        self.spans[span.evidence_id] = span
        return span

    def add_finding(
        self,
        claim: str,
        *,
        evidence_ids: list[str] | None = None,
        confidence: float = 1.0,
        freshness: str = "",
        source_quality: str = "unknown",
        conflicts: list[str] | None = None,
        claim_id: str = "",
    ) -> Finding:
        text = (claim or "").strip()
        if not text:
            raise ValueError("empty claim")
        if not claim_id:
            self._claim_counter += 1
            claim_id = f"C{self._claim_counter}"
        finding = Finding(
            claim_id=claim_id,
            claim=text,
            evidence_ids=[str(x) for x in (evidence_ids or []) if x],
            confidence=float(confidence or 0.0),
            freshness=freshness,
            source_quality=source_quality,
            conflicts=list(conflicts or []),
        )
        self.findings[finding.claim_id] = finding
        return finding

    def bind_fact(
        self,
        fact: str,
        *,
        artifact: Artifact | None = None,
        locator: str = "",
        source_kind: str = "text",
        step_index: int = -1,
        step_type: str = "",
        confidence: float = 1.0,
    ) -> tuple[Finding, EvidenceSpan]:
        text = (fact or "").strip()
        start = 0
        end = len(text)
        artifact_id = ""
        loc = locator
        if artifact is not None:
            artifact_id = artifact.artifact_id
            loc = loc or artifact.locator
            haystack = artifact.content or ""
            needle = text[:80]
            idx = haystack.find(needle) if needle else -1
            if idx < 0 and needle:
                idx = haystack.lower().find(needle.lower())
            if idx >= 0:
                start = idx
                end = idx + len(text)
                span_text = haystack[max(0, idx - 40) : min(len(haystack), idx + max(len(text), 160))]
            else:
                span_text = text
        else:
            span_text = text
        span = self.add_span(
            span_text,
            artifact_id=artifact_id,
            locator=loc or f"step:{step_index}:{step_type}",
            start_offset=start,
            end_offset=end,
            source_kind=source_kind,
            step_index=step_index,
            step_type=step_type,
        )
        finding = self.add_finding(
            text,
            evidence_ids=[span.evidence_id],
            confidence=confidence,
            source_quality=span.source_quality,
        )
        return finding, span

    def ingest_worker_payload(
        self,
        payload: dict[str, Any],
        *,
        artifact_ids: list[str] | None = None,
        step_index: int = -1,
        step_type: str = "",
        artifact_store: ArtifactStore | None = None,
    ) -> list[Finding]:
        store = artifact_store or get_artifact_store()
        artifacts = [store.get(aid) for aid in (artifact_ids or [])]
        artifacts = [a for a in artifacts if a is not None]
        primary = artifacts[0] if artifacts else None
        findings: list[Finding] = []

        raw_findings = payload.get("findings") if isinstance(payload, dict) else None
        if isinstance(raw_findings, list) and raw_findings:
            for item in raw_findings[:20]:
                if isinstance(item, str):
                    finding, _ = self.bind_fact(
                        item,
                        artifact=primary,
                        source_kind=_kind_from_step(step_type),
                        step_index=step_index,
                        step_type=step_type,
                    )
                    findings.append(finding)
                    continue
                if not isinstance(item, dict):
                    continue
                claim = str(item.get("claim") or item.get("text") or "").strip()
                if not claim:
                    continue
                ids = [str(x) for x in (item.get("evidence_ids") or []) if x]
                if not ids:
                    finding, _ = self.bind_fact(
                        claim,
                        artifact=primary,
                        locator=str(item.get("locator") or ""),
                        source_kind=_kind_from_step(step_type),
                        step_index=step_index,
                        step_type=step_type,
                        confidence=float(item.get("confidence") or 1.0),
                    )
                    findings.append(finding)
                else:
                    findings.append(
                        self.add_finding(
                            claim,
                            evidence_ids=ids,
                            confidence=float(item.get("confidence") or 1.0),
                            freshness=str(item.get("freshness") or ""),
                            source_quality=str(item.get("source_quality") or "unknown"),
                            conflicts=[str(x) for x in (item.get("conflicts") or [])],
                            claim_id=str(item.get("claim_id") or ""),
                        )
                    )
            self._record_conflicts(payload)
            return findings

        facts = [str(f) for f in (payload.get("facts") or []) if str(f).strip()]
        sources = [str(s) for s in (payload.get("sources") or []) if str(s).strip()]
        for i, fact in enumerate(facts[:12]):
            locator = sources[i] if i < len(sources) else (sources[0] if sources else "")
            art = None
            if locator:
                art = next((a for a in artifacts if locator in (a.locator, a.title)), primary)
            else:
                art = primary
            finding, _ = self.bind_fact(
                fact,
                artifact=art,
                locator=locator,
                source_kind=_kind_from_step(step_type),
                step_index=step_index,
                step_type=step_type,
                confidence=float(payload.get("confidence") or 1.0),
            )
            findings.append(finding)
        self._record_conflicts(payload)
        return findings

    def _record_conflicts(self, payload: dict[str, Any]) -> None:
        for item in payload.get("conflicts") or []:
            if isinstance(item, dict):
                self.conflicts.append(dict(item))
            elif str(item).strip():
                self.conflicts.append({"text": str(item).strip()})

    def get(self, evidence_id: str) -> EvidenceSpan | None:
        key = (evidence_id or "").strip()
        if key.startswith("evidence://"):
            key = key.rsplit("/", 1)[-1]
        return self.spans.get(key)

    def read(
        self,
        evidence_id: str,
        *,
        include_artifact: bool = False,
        max_chars: int = 2000,
    ) -> dict[str, Any]:
        span = self.get(evidence_id)
        if span is None:
            return {"ok": False, "error_code": "evidence_not_found", "evidence_id": evidence_id}
        payload: dict[str, Any] = {
            "ok": True,
            "evidence_id": span.evidence_id,
            "artifact_id": span.artifact_id,
            "locator": span.locator,
            "start_offset": span.start_offset,
            "end_offset": span.end_offset,
            "source_kind": span.source_kind,
            "source_quality": span.source_quality,
            "text": span.text[:max_chars],
        }
        if include_artifact and span.artifact_id:
            from app.agent.harness.artifacts import get_artifact_store

            extra = get_artifact_store().read(
                span.artifact_id,
                start=span.start_offset,
                end=max(span.end_offset, span.start_offset + 400),
                max_chars=max_chars,
            )
            payload["artifact"] = extra
        return payload

    def retrieve(
        self,
        query: str,
        *,
        section: str = "",
        max_items: int = 12,
        min_score: float = 0.08,
    ) -> list[EvidenceSpan]:
        needle = " ".join(part for part in [query, section] if part).strip()
        if not needle:
            return list(self.spans.values())[:max_items]
        scored: list[tuple[float, EvidenceSpan]] = []
        for span in self.spans.values():
            blob = f"{span.text} {span.locator} {span.metadata}"
            score = _overlap_score(needle, blob)
            finding_bonus = 0.0
            for finding in self.findings.values():
                if span.evidence_id in finding.evidence_ids:
                    finding_bonus = max(finding_bonus, _overlap_score(needle, finding.claim))
            score = max(score, finding_bonus)
            if score >= min_score:
                scored.append((score, span))
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored:
            return [span for _, span in scored[:max_items]]
        return list(self.spans.values())[:max_items]

    def retrieve_findings(self, query: str, *, max_items: int = 10) -> list[Finding]:
        if not self.findings:
            return []
        scored = [
            (_overlap_score(query, finding.claim), finding)
            for finding in self.findings.values()
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        picked = [finding for score, finding in scored if score > 0][:max_items]
        return picked or list(self.findings.values())[:max_items]

    def lookup_block(
        self,
        *,
        query: str = "",
        max_items: int = 12,
        excerpt_chars: int = 360,
    ) -> str:
        spans = self.retrieve(query, max_items=max_items) if query else list(self.spans.values())[:max_items]
        if not spans:
            return ""
        lines = [
            "    【可回读证据 — 按 evidence_id 核对，勿使用未列出的精确数字】",
            "    需要原文时调用 read_evidence(evidence_id) 或 read_artifact(artifact_id)。",
        ]
        for i, span in enumerate(spans, 1):
            excerpt = (span.text or "")[:excerpt_chars]
            lines.append(
                f"  [{i}] {span.evidence_id} artifact={span.artifact_id or '-'} "
                f"({span.source_kind}) {span.locator}"
            )
            if excerpt:
                lines.append(f"      {excerpt}")
        if self.conflicts:
            lines.append("    【冲突证据 — 不得自行消解，须并列保留】")
            for item in self.conflicts[:8]:
                lines.append(f"      - {item}")
        return "\n".join(lines)

    def checkpoint_snapshot(self) -> dict[str, Any]:
        return {
            "span_counter": self._span_counter,
            "claim_counter": self._claim_counter,
            "spans": [span.to_dict() for span in self.spans.values()],
            "findings": [finding.to_dict() for finding in self.findings.values()],
            "conflicts": list(self.conflicts),
        }

    def load_snapshot(self, payload: dict[str, Any] | None) -> None:
        if not payload:
            return
        self.spans = {}
        for row in payload.get("spans") or []:
            if isinstance(row, dict) and row.get("evidence_id"):
                span = EvidenceSpan.from_dict(row)
                self.spans[span.evidence_id] = span
        self.findings = {}
        for row in payload.get("findings") or []:
            if isinstance(row, dict) and (row.get("claim_id") or row.get("claim")):
                finding = Finding.from_dict(row)
                self.findings[finding.claim_id] = finding
        self.conflicts = [dict(x) if isinstance(x, dict) else {"text": str(x)} for x in (payload.get("conflicts") or [])]
        self._span_counter = int(payload.get("span_counter") or len(self.spans))
        self._claim_counter = int(payload.get("claim_counter") or len(self.findings))

    def persist(self, session_dir: Path | None = None) -> Path | None:
        if session_dir is not None:
            self.session_dir = Path(session_dir)
        if self.session_dir is None:
            return None
        path = self.session_dir / ".harness" / "evidence_store.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.checkpoint_snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, session_dir: Path | None = None) -> None:
        if session_dir is not None:
            self.session_dir = Path(session_dir)
        if self.session_dir is None:
            return
        path = self.session_dir / ".harness" / "evidence_store.json"
        if not path.exists():
            return
        try:
            self.load_snapshot(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return


def _kind_from_step(step_type: str) -> str:
    return {
        "network_search": "url",
        "network_search": "url",
        "file_read": "file",
        "file_read": "file",
        "research": "url",
    }.get(step_type, "text")


_STORE: ContextVar[EvidenceStore | None] = ContextVar("harness_evidence_store", default=None)


def get_evidence_store() -> EvidenceStore:
    store = _STORE.get()
    if store is None:
        store = EvidenceStore()
        _STORE.set(store)
    return store


def set_evidence_store(store: EvidenceStore | None) -> None:
    _STORE.set(store)


def reset_evidence_store() -> None:
    _STORE.set(None)
