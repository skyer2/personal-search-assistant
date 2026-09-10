"""Strict, deduplicated, token-bounded synthesis input context."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.agent.harness.token_counter import estimate_tokens, get_token_counter


@dataclass(frozen=True)
class EvidenceDigest:
    evidence_id: str
    title: str
    locator: str
    excerpt: str
    supported_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class SynthesisContext:
    evidence_refs: tuple[str, ...] = ()
    evidence_digests: tuple[EvidenceDigest, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    worker_summaries: tuple[dict[str, Any], ...] = ()
    semantic_gaps: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    unresolved_conflicts: tuple[str, ...] = ()
    token_budget: int = 40_000
    compacted: bool = False

    def research_summary(self) -> str:
        lines: list[str] = []
        for finding in self.findings:
            claim = str(finding.get("claim") or finding.get("summary") or "").strip()
            if not claim:
                continue
            lines.append(f"- {claim}")
        for row in self.worker_summaries:
            summary = str(row.get("summary") or "").strip()
            if summary:
                lines.append(f"- [{row.get('task_id') or 'worker'}] {summary}")
        lines.extend(f"- 尚未覆盖：{gap}" for gap in self.semantic_gaps if str(gap).strip())
        return "\n".join(lines)


class SynthesisContextBuilder:
    """Reduce graph state to a deterministic synthesis input contract."""

    def __init__(self, harness: Any, session: Any):
        self.harness = harness
        self.session = session

    def build(
        self,
        gstate: dict[str, Any],
        *,
        limitations: list[str] | None = None,
        unresolved_conflicts: list[str] | None = None,
        compact: bool = False,
    ) -> SynthesisContext:
        refs = self._evidence_refs(gstate)
        findings = self._findings(gstate, refs)
        base_budget = int(get_token_counter().budget_for_stage("synthesis", fallback=40_000))
        context = SynthesisContext(
            evidence_refs=tuple(refs),
            evidence_digests=tuple(self._digests(refs, findings, compact)),
            findings=tuple(findings),
            worker_summaries=tuple(self._worker_summaries(gstate, compact)),
            semantic_gaps=tuple(self._semantic_gaps(gstate)),
            limitations=tuple(self._strings(limitations)),
            unresolved_conflicts=tuple(self._strings(unresolved_conflicts)),
            token_budget=max(1_000, base_budget // 2 if compact else base_budget),
            compacted=compact,
        )
        return self._fit(context)

    def compact(self, context: SynthesisContext) -> SynthesisContext:
        if context.compacted:
            return context
        digests = tuple(
            EvidenceDigest(
                item.evidence_id,
                item.title[:120],
                item.locator[:240],
                item.excerpt[:180],
                item.supported_claims[:2],
            )
            for item in context.evidence_digests
        )
        return replace(
            context,
            evidence_digests=digests,
            findings=context.findings[:40],
            worker_summaries=context.worker_summaries[:24],
            semantic_gaps=context.semantic_gaps[:12],
            limitations=context.limitations[:12],
            unresolved_conflicts=context.unresolved_conflicts[:12],
            token_budget=max(1_000, context.token_budget // 2),
            compacted=True,
        )

    @staticmethod
    def _strings(values: list[str] | None) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(item) for item in values or [] if str(item).strip()))

    @staticmethod
    def _evidence_refs(gstate: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        for item in gstate.get("evidence_refs") or []:
            value = str(item or "").strip()
            if value.startswith("artifact://"):
                value = value.rsplit("/", 1)[-1]
            if value and value not in refs:
                refs.append(value)
        return refs

    def _findings(
        self,
        gstate: dict[str, Any],
        refs: list[str],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        allowed = set(refs)
        for raw in gstate.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            claim = str(raw.get("claim") or raw.get("summary") or "").strip()
            evidence_ids = [
                str(item).strip()
                for item in raw.get("evidence_ids") or []
                if str(item).strip()
            ]
            if not claim or (allowed and not any(item in allowed for item in evidence_ids)):
                continue
            fingerprint = f"{claim.casefold()}:{'|'.join(sorted(evidence_ids))}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            output.append({**raw, "claim": claim[:1200], "evidence_ids": evidence_ids[:12]})
        return output[:120]

    def _worker_summaries(
        self,
        gstate: dict[str, Any],
        compact: bool,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in gstate.get("worker_results") or []:
            if not isinstance(raw, dict):
                continue
            summary = str(raw.get("summary") or "").strip()
            if not summary:
                continue
            task_id = str(raw.get("task_id") or "").strip()
            key = (task_id, summary.casefold()[:240])
            if key in seen:
                continue
            seen.add(key)
            output.append({"task_id": task_id, "summary": summary[:600 if compact else 1200]})
            if len(output) >= (24 if compact else 80):
                break
        return output

    @staticmethod
    def _semantic_gaps(gstate: dict[str, Any]) -> list[str]:
        judgement = gstate.get("coverage_judgement")
        if not isinstance(judgement, dict):
            return []
        return [
            str(item)
            for item in judgement.get("missing") or []
            if str(item).strip()
        ][:24]

    def _digests(
        self,
        refs: list[str],
        findings: list[dict[str, Any]],
        compact: bool,
    ) -> list[EvidenceDigest]:
        claims: dict[str, list[str]] = {}
        for finding in findings:
            claim = str(finding.get("claim") or finding.get("summary") or "").strip()
            for evidence_id in finding.get("evidence_ids") or []:
                values = claims.setdefault(str(evidence_id), [])
                if claim and claim not in values:
                    values.append(claim)

        by_id: dict[str, EvidenceDigest] = {}
        by_locator: dict[str, str] = {}
        for evidence_id in refs:
            digest = self._artifact_digest(evidence_id, claims.get(evidence_id, []), compact)
            if digest is None:
                digest = self._citation_digest(evidence_id, claims.get(evidence_id, []), compact)
            if digest is None:
                continue
            locator = digest.locator.casefold()
            existing_id = by_locator.get(locator)
            if existing_id:
                existing = by_id[existing_id]
                merged = list(dict.fromkeys([*existing.supported_claims, *digest.supported_claims]))
                by_id[existing_id] = EvidenceDigest(
                    existing.evidence_id,
                    existing.title or digest.title,
                    existing.locator,
                    existing.excerpt or digest.excerpt,
                    tuple(merged[:6]),
                )
                continue
            by_id[evidence_id] = digest
            if locator:
                by_locator[locator] = evidence_id
        return list(by_id.values())

    def _artifact_digest(
        self,
        evidence_id: str,
        claims: list[str],
        compact: bool,
    ) -> EvidenceDigest | None:
        try:
            from app.agent.harness.artifacts import get_artifact_store

            artifact = get_artifact_store().get(evidence_id)
        except Exception:
            artifact = None
        if artifact is None:
            return None
        return EvidenceDigest(
            artifact.artifact_id,
            (artifact.title or artifact.locator or artifact.artifact_id)[:120],
            artifact.locator[:240],
            (artifact.summary or artifact.content or "")[:240 if compact else 500],
            tuple(claims[:3 if compact else 6]),
        )

    def _citation_digest(
        self,
        evidence_id: str,
        claims: list[str],
        compact: bool,
    ) -> EvidenceDigest | None:
        manager = getattr(self.session.ctx, "citation_manager", None)
        sources = list(getattr(manager, "sources", None) or []) if manager is not None else []
        for source in sources:
            source_id = str(getattr(source, "source_id", "") or "")
            artifact_id = str(getattr(source, "artifact_id", "") or "")
            if evidence_id not in {source_id, artifact_id}:
                continue
            locator = str(getattr(source, "locator", "") or source_id)
            return EvidenceDigest(
                source_id or evidence_id,
                locator[:120],
                locator[:240],
                str(getattr(source, "excerpt", "") or "")[:240 if compact else 500],
                tuple(claims[:3 if compact else 6]),
            )
        return None

    def _fit(self, context: SynthesisContext) -> SynthesisContext:
        while context.findings and estimate_tokens(context.research_summary()) > context.token_budget:
            findings = context.findings[: max(1, len(context.findings) // 2)]
            context = replace(context, findings=findings)
            if len(findings) == 1:
                break
        return context


__all__ = ["EvidenceDigest", "SynthesisContext", "SynthesisContextBuilder"]
