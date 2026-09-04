"""Extract structured ClaimRecords from worker fan-in results."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.research.claims.models import ClaimRecord

_METRIC = re.compile(
    r"(?P<label>收入|营收|订单|销量|估值|利润|交付|产量|revenue|arr|gmv|deliveries|shipments|score|accuracy)"
    r"(?:\s*[:：为是约]){0,4}\s*"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>亿美元|亿元|亿|万美元|万|%|美元|元|usd|m|k)?",
    re.I,
)
_YEAR = re.compile(r"\b(20\d{2})\b|FY\s*(20\d{2})|(?:日历|财年)\s*(20\d{2})", re.I)
_SCOPE = re.compile(
    r"(automotive|total|gaap|non-?gaap|verified|pro|lite|full|汽车|总计|整体|分部)",
    re.I,
)
_SUBJECT = re.compile(
    r"\b(Tesla|Figure|Unitree|OpenAI|Anthropic|Google|Waymo|Microsoft)\b"
    r"|特斯拉|小米|华为",
    re.I,
)


def _normalize_unit(unit: str, text: str = "") -> str:
    """把 亿美元 / B USD / billion 等归一，便于跨 Worker 对齐。"""
    blob = f"{unit} {text}".lower()
    u = (unit or "").lower().strip()
    if any(tok in blob for tok in ("亿美元", "billion", " bn", "bn ", " usd")) or u in {
        "b",
        "bn",
        "billion",
        "亿美元",
        "usd",
    }:
        # 文本里带 Nb / N B 也视作十亿级美元
        if "亿" in blob or "billion" in blob or re.search(r"\d+(?:\.\d+)?\s*b\b", blob) or u in {
            "b",
            "bn",
            "亿美元",
        }:
            return "usd_billion"
        if u in {"usd", "美元"} or " usd" in blob:
            return "usd"
    if u in {"亿元", "亿"} or "亿元" in blob:
        return "cny_yi"
    if u in {"%", "percent", "pct"} or "%" in blob:
        return "pct"
    return u


def _authority_score(sources: list[str], source_quality: str) -> float:
    from app.agent.harness.research_brief import PRIMARY_SOURCE_HINTS

    score = 0.0
    sq = (source_quality or "").lower()
    if sq in {"primary", "official", "regulatory"}:
        score += 0.55
    elif sq in {"secondary", "high"}:
        score += 0.25
    blob = " ".join(sources).lower()
    if any(h.lower() in blob for h in PRIMARY_SOURCE_HINTS):
        score += 0.35
    # IR / SEC / 年报 URL 即使未标 primary，也按接近官方来源加分
    if any(tok in blob for tok in ("sec.gov", "10-k", "10k", "/ir.", "ir.", "investor", "arxiv.org")):
        score += 0.35
    if any(tok in blob for tok in ("reuters", "bloomberg", "wsj", "ft.com", "nytimes")):
        score += 0.1
    if any(tok in blob for tok in ("reddit", "forum", "zhihu", "medium.com", "blog")):
        score -= 0.2
    return max(0.0, min(1.0, score))


def _claim_id(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"claim_{digest}"


def _parse_from_text(
    text: str,
    *,
    task_id: str,
    sources: list[str],
    evidence_ids: list[str],
    confidence: float,
    source_quality: str,
) -> list[ClaimRecord]:
    out: list[ClaimRecord] = []
    blob = str(text or "")
    subject_m = _SUBJECT.search(blob)
    subject = subject_m.group(0) if subject_m else ""
    period_m = _YEAR.search(blob)
    period = ""
    if period_m:
        period = next((g for g in period_m.groups() if g), "") or period_m.group(0)
    scope_m = _SCOPE.search(blob)
    scope = scope_m.group(0).lower() if scope_m else ""
    for match in _METRIC.finditer(blob):
        label = str(match.group("label") or "").lower()
        try:
            value = float(match.group("num"))
        except (TypeError, ValueError):
            continue
        if 1900 <= value <= 2100 and label not in {"revenue", "arr", "gmv", "收入", "营收"}:
            continue
        unit_raw = str(match.group("unit") or "").lower()
        # 捕获 “10B USD” 这类单位落在 num 后缀的情况
        tail = blob[match.end() : match.end() + 12]
        if not unit_raw and re.match(r"\s*[Bb]\b", tail):
            unit_raw = "b"
        unit_ctx = blob[max(0, match.start() - 12) : match.end() + 16]
        unit = _normalize_unit(unit_raw, unit_ctx)
        snippet = blob[max(0, match.start() - 40) : match.end() + 40].strip()
        cid = _claim_id(task_id, subject, label, str(value), unit, period, scope)
        out.append(
            ClaimRecord(
                claim_id=cid,
                text=snippet[:240] or f"{subject} {label}={value}{unit}".strip(),
                task_id=task_id,
                subject=subject,
                metric=label,
                value=value,
                unit=unit,
                period=period,
                scope=scope,
                sources=list(sources),
                evidence_ids=list(evidence_ids),
                confidence=confidence,
                source_quality=source_quality,
                authority_score=_authority_score(sources, source_quality),
            )
        )
    return out


def extract_claims_from_worker_results(rows: list[Any] | None) -> list[ClaimRecord]:
    """Fan-in worker_results → ClaimRecord list (cross-worker ready)."""
    claims: list[ClaimRecord] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("task_id") or "").strip()
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        sources = [str(x) for x in (payload.get("sources") or row.get("sources") or []) if x]
        evidence_ids = [str(x) for x in (payload.get("evidence_ids") or []) if x]
        try:
            confidence = float(payload.get("confidence") if payload.get("confidence") is not None else 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
        # Worker 级来源质量：优先采用 findings 中最高档（避免 summary/facts 丢 primary）
        source_quality = "unknown"
        _rank = {"primary": 3, "official": 3, "regulatory": 3, "secondary": 2, "high": 2}
        best_rank = 0
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            sq = str(finding.get("source_quality") or "").lower()
            rank = _rank.get(sq, 0)
            if rank > best_rank:
                best_rank = rank
                source_quality = sq
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            text = str(finding.get("claim") or finding.get("text") or "").strip()
            if not text:
                continue
            sq = str(finding.get("source_quality") or source_quality)
            eids = [str(x) for x in (finding.get("evidence_ids") or evidence_ids) if x]
            conf = confidence
            try:
                if finding.get("confidence") is not None:
                    conf = float(finding.get("confidence"))
            except (TypeError, ValueError):
                pass
            parsed = _parse_from_text(
                text,
                task_id=tid,
                sources=sources,
                evidence_ids=eids,
                confidence=conf,
                source_quality=sq,
            )
            if not parsed:
                cid = _claim_id(tid, text[:80])
                parsed = [
                    ClaimRecord(
                        claim_id=cid,
                        text=text[:240],
                        task_id=tid,
                        sources=sources,
                        evidence_ids=eids,
                        confidence=conf,
                        source_quality=sq,
                        authority_score=_authority_score(sources, sq),
                    )
                ]
            for claim in parsed:
                if claim.claim_id in seen:
                    continue
                seen.add(claim.claim_id)
                claims.append(claim)

        facts = [str(x) for x in (payload.get("facts") or []) if str(x).strip()]
        summary = str(payload.get("summary") or row.get("summary") or "")
        for fact in facts:
            for claim in _parse_from_text(
                fact,
                task_id=tid,
                sources=sources,
                evidence_ids=evidence_ids,
                confidence=confidence,
                source_quality=source_quality,
            ):
                if claim.claim_id in seen:
                    continue
                seen.add(claim.claim_id)
                claims.append(claim)
        if summary:
            for claim in _parse_from_text(
                summary,
                task_id=tid,
                sources=sources,
                evidence_ids=evidence_ids,
                confidence=confidence,
                source_quality=source_quality,
            ):
                if claim.claim_id in seen:
                    continue
                seen.add(claim.claim_id)
                claims.append(claim)
    return _dedupe_claims(claims)


def _dedupe_claims(claims: list[ClaimRecord]) -> list[ClaimRecord]:
    """同 task / subject / metric / period / scope / 归一单位 / 近似值 → 保留权威更高者。"""
    best: dict[str, ClaimRecord] = {}
    order: list[str] = []
    for claim in claims:
        key = "|".join(
            [
                claim.task_id,
                (claim.subject or "").lower(),
                (claim.metric or "").lower(),
                (claim.period or "").lower(),
                (claim.scope or "").lower(),
                (claim.unit or "").lower(),
                f"{float(claim.value):.4g}" if claim.value is not None else "",
            ]
        )
        prev = best.get(key)
        if prev is None:
            best[key] = claim
            order.append(key)
            continue
        if claim.authority_score > prev.authority_score or (
            claim.authority_score == prev.authority_score and claim.confidence > prev.confidence
        ):
            best[key] = claim
    return [best[k] for k in order]
