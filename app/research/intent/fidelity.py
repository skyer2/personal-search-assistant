"""Semantic Fidelity Gate: the Brief must still be the user's question.

This gate runs between Brief compilation and planning. It fails closed: when a
required ask lost its lineage, subject or time scope, no worker may start.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.research.intent.user_ask import UserAsk, UserAskContract, extract_time_scope

_SUBJECT_MIN = 0.8


def _normalize(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _subject_present(subject: str, blob: str) -> bool:
    target = _normalize(subject)
    if not target:
        return True
    haystack = _normalize(blob)
    if target in haystack:
        return True
    # Chinese subjects may be re-segmented; accept high character overlap.
    chars = [ch for ch in target if ch.isalnum()]
    if not chars:
        return True
    hits = sum(1 for ch in chars if ch in haystack)
    return hits / len(chars) >= 0.75


def _time_scope_present(ask: UserAsk, blob: str) -> bool:
    scope = _normalize(ask.time_scope)
    if not scope:
        return True
    haystack = _normalize(blob)
    if scope in haystack:
        return True
    # A rewritten question may normalize "未来1-2年" to "未来 1~2 年".
    digits = [ch for ch in scope if ch.isdigit()]
    if digits and all(ch in haystack for ch in digits):
        return bool(extract_time_scope(blob)) or any(
            token in haystack for token in ("未来", "接下来", "今后", "截至", "当前", "最新")
        )
    return any(token in haystack for token in ("未来", "接下来", "今后", "截至", "当前", "最新"))


@dataclass(frozen=True)
class SemanticFidelityResult:
    passed: bool
    ask_coverage: float = 0.0
    subject_preservation: float = 0.0
    time_scope_preservation: float = 0.0
    orphan_questions: tuple[str, ...] = field(default_factory=tuple)
    missing_asks: tuple[str, ...] = field(default_factory=tuple)
    issues: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def score(self) -> float:
        return round(
            (self.ask_coverage + self.subject_preservation + self.time_scope_preservation) / 3.0,
            4,
        )


def evaluate_semantic_fidelity(
    contract: UserAskContract | dict[str, Any] | None,
    brief: Any,
) -> SemanticFidelityResult:
    """Check that every required ask survived Brief compilation."""
    resolved = (
        contract
        if isinstance(contract, UserAskContract)
        else UserAskContract.from_dict(contract if isinstance(contract, dict) else None)
    )
    required = list(resolved.required_asks)
    questions = list(getattr(brief, "research_questions", None) or ())
    if not required:
        return SemanticFidelityResult(True, 1.0, 1.0, 1.0)

    issues: list[str] = []
    orphans = [
        str(getattr(item, "question_id", "") or "")
        for item in questions
        if not str(getattr(item, "ask_id", "") or "").strip()
    ]
    if orphans:
        issues.append("orphan_research_question")

    by_ask: dict[str, list[Any]] = {}
    for item in questions:
        ask_id = str(getattr(item, "ask_id", "") or "")
        if ask_id:
            by_ask.setdefault(ask_id, []).append(item)

    missing: list[str] = []
    subject_ok = 0
    scope_ok = 0
    for ask in required:
        mapped = by_ask.get(ask.ask_id) or []
        if not mapped:
            missing.append(ask.ask_id)
            continue
        blob = " ".join(str(getattr(item, "text", "") or "") for item in mapped)
        if _subject_present(ask.subject, blob):
            subject_ok += 1
        else:
            issues.append(f"subject_lost:{ask.ask_id}")
        if _time_scope_present(ask, blob):
            scope_ok += 1
        else:
            issues.append(f"time_scope_lost:{ask.ask_id}")

    mapped_count = len(required) - len(missing)
    ask_coverage = mapped_count / len(required)
    subject_preservation = subject_ok / len(required)
    scope_preservation = scope_ok / len(required)
    if missing:
        issues.append("missing_required_ask")
    passed = (
        not missing
        and not orphans
        and ask_coverage >= 1.0
        and subject_preservation >= _SUBJECT_MIN
    )
    return SemanticFidelityResult(
        passed=passed,
        ask_coverage=round(ask_coverage, 4),
        subject_preservation=round(subject_preservation, 4),
        time_scope_preservation=round(scope_preservation, 4),
        orphan_questions=tuple(orphans),
        missing_asks=tuple(missing),
        issues=tuple(dict.fromkeys(issues)),
    )


__all__ = ["SemanticFidelityResult", "evaluate_semantic_fidelity"]
