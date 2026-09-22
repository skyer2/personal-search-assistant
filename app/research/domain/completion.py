"""Single deterministic completion contract for Deep Research runs.

This module is deliberately independent from Coverage, Answerability and
Synthesis.  Those components provide diagnostics; this contract alone decides
whether the delivered report answered the user's key questions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class QuestionCompletion:
    question_id: str
    answered: bool
    direct_answer_present: bool
    evidence_refs: list[str] = field(default_factory=list)
    blocking_gap: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PartialContract:
    """Strict floor for a PARTIAL delivery.

    Evidence merely existing is not enough: at least one required user ask must
    be answered with grounded claims, acceptable sources and passing relevance.
    """

    answered_required_asks: int = 0
    grounded_claim_count: int = 0
    source_quality_pass: bool = False
    relevance_pass: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.answered_required_asks >= 1
            and self.grounded_claim_count >= 1
            and self.source_quality_pass
            and self.relevance_pass
        )

    @property
    def failure_reason(self) -> str:
        if self.answered_required_asks < 1:
            return "partial_contract:no_answered_required_ask"
        if self.grounded_claim_count < 1:
            return "partial_contract:no_grounded_claim"
        if not self.source_quality_pass:
            return "partial_contract:source_quality_failed"
        if not self.relevance_pass:
            return "partial_contract:relevance_failed"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "passed": self.passed,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class CompletionResult:
    passed: bool
    question_results: list[QuestionCompletion]
    evidence_valid: bool
    citation_valid: bool
    answer_complete: bool
    unresolved_blocking: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    partial_contract: PartialContract = field(default_factory=PartialContract)
    ask_results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if self.passed:
            return "success"
        return "partial" if self.partial_contract.passed else "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "question_results": [item.to_dict() for item in self.question_results],
            "ask_results": list(self.ask_results),
            "evidence_valid": self.evidence_valid,
            "citation_valid": self.citation_valid,
            "answer_complete": self.answer_complete,
            "unresolved_blocking": list(self.unresolved_blocking),
            "failure_reason": self.failure_reason,
            "partial_contract": self.partial_contract.to_dict(),
            "outcome": self.outcome,
        }


def _questions(brief: Any, objective: str = "") -> list[str]:
    if isinstance(brief, dict):
        values = brief.get("key_questions") or brief.get("questions") or []
    else:
        values = getattr(brief, "key_questions", None) or getattr(brief, "questions", None) or []
    questions = [str(item).strip() for item in values if str(item).strip()]
    return questions or ([str(objective).strip()] if str(objective).strip() else [])


def _answer_rows(answer_contract: Any) -> list[dict[str, Any]]:
    if not isinstance(answer_contract, dict):
        return []
    raw = answer_contract.get("answers") or answer_contract.get("question_answers") or []
    # Deterministic recovery in pre-v1.1 snapshots nested the typed answer
    # under ``final_answer``.  Accept that persisted shape so a provider-empty
    # synthesis cannot turn already grounded evidence into a false failure.
    if not raw and isinstance(answer_contract.get("final_answer"), dict):
        nested = answer_contract["final_answer"]
        raw = nested.get("answers") or nested.get("question_answers") or []
    return [item for item in raw if isinstance(item, dict)]


# A recovered answer was assembled by the runtime from existing claims, not
# written by the synthesis model. It can be delivered, but never as SUCCESS.
_RECOVERY_MODES = frozenset({"evidence_bound_recovery", "deterministic_recovery"})


def _recovered(answer_contract: Any) -> bool:
    row = answer_contract if isinstance(answer_contract, dict) else {}
    nested = row.get("final_answer") if isinstance(row.get("final_answer"), dict) else {}
    mode = str(nested.get("synthesis_mode") or row.get("synthesis_mode") or "")
    return mode in _RECOVERY_MODES


def _record_ids(records: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for row in records:
        for key in ("evidence_id", "artifact_ref", "artifact_id", "source_id", "source", "url", "source_url"):
            value = str(row.get(key) or "").strip()
            if value:
                ids.add(value)
    return ids


def _refs(row: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("evidence_refs", "evidence_ids", "finding_refs", "artifact_ids", "source_ids"):
        values = row.get(key) or []
        values = [values] if isinstance(values, str) else values
        refs.extend(str(value).strip() for value in values if str(value).strip())
    return list(dict.fromkeys(refs))


_REFUSAL_MARKERS = (
    "当前证据不足",
    "无法可靠回答",
    "无法形成可靠综合判断",
)


def _is_refusal(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value) and any(marker in value for marker in _REFUSAL_MARKERS)


def _ask_ids(brief: Any) -> list[tuple[int, str, bool]]:
    """Return (question_index, ask_id, required) triples for the brief."""
    resolver = getattr(brief, "ask_id_for_question_index", None)
    asks = list(getattr(brief, "user_asks", None) or ())
    required_by_id = {
        str(getattr(item, "ask_id", "") or ""): bool(getattr(item, "required", True))
        for item in asks
    }
    questions = _questions(brief)
    output: list[tuple[int, str, bool]] = []
    for index in range(1, len(questions) + 1):
        ask_id = str(resolver(index) or "") if callable(resolver) else ""
        output.append((index, ask_id, required_by_id.get(ask_id, True)))
    return output


def evaluate_completion(
    *,
    brief: Any,
    answer_contract: dict[str, Any] | None,
    evidence_records: list[dict[str, Any]] | None,
    final_content: str = "",
    citation_valid: bool = True,
    unresolved_blocking: list[str] | None = None,
    coverage: dict[str, Any] | None = None,
    broken_evidence_count: int = 0,
    minimum_high_authority_ratio: float = 0.0,
    require_authoritative_per_question: bool = False,
    source_quality_pass: bool = True,
    relevance_pass: bool = True,
    relevance_partial_pass: bool | None = None,
    source_quality_partial_pass: bool | None = None,
) -> CompletionResult:
    """Apply the one completion contract using only runtime-observable facts."""
    records = [row for row in (evidence_records or []) if isinstance(row, dict)]
    valid_ids = _record_ids(records)
    records_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        for key in ("evidence_id", "artifact_ref", "artifact_id", "source_id", "source", "url", "source_url"):
            value = str(record.get(key) or "").strip()
            if value:
                records_by_id[value] = record
    rows = _answer_rows(answer_contract)
    contract_objective = str((answer_contract or {}).get("objective") or "")
    if not contract_objective and isinstance((answer_contract or {}).get("final_answer"), dict):
        contract_objective = str((answer_contract or {})["final_answer"].get("objective") or "")
    questions = _questions(brief, contract_objective)
    lineage = {index: (ask_id, required) for index, ask_id, required in _ask_ids(brief)}
    results: list[QuestionCompletion] = []
    blocking = list(unresolved_blocking or [])
    coverage_rows = [
        row for row in (coverage or {}).get("key_question_coverage") or []
        if isinstance(row, dict)
    ]
    for row in coverage_rows:
        if bool(row.get("blocking")) or str(row.get("status") or "") == "uncovered":
            blocking.append(f"{row.get('question_id') or 'question'}:blocking_coverage_gap")
    if broken_evidence_count > 0:
        blocking.append("broken_evidence_present")
    if minimum_high_authority_ratio > 0:
        high = sum(
            1
            for row in records
            if str(row.get("source_tier") or "").upper() in {"PRIMARY", "HIGH_QUALITY_SECONDARY"}
            or float(row.get("authority_score") or 0.0) >= 0.75
        )
        if not records or high / len(records) < minimum_high_authority_ratio:
            blocking.append("minimum_source_quality_not_met")
    answered_asks: dict[str, bool] = {}
    grounded_claims = 0
    for index, question in enumerate(questions, 1):
        qid = f"q{index}"
        row = next((item for item in rows if str(item.get("question_id") or "") == qid), None)
        if row is None and index <= len(rows):
            row = rows[index - 1]
        direct = str((row or {}).get("direct_answer") or "").strip()
        refs = [ref for ref in _refs(row or {}) if ref in valid_ids]
        authoritative = any(
            str(records_by_id[ref].get("source_tier") or "").upper()
            in {"PRIMARY", "HIGH_QUALITY_SECONDARY"}
            or float(records_by_id[ref].get("authority_score") or 0.0) >= 0.75
            for ref in refs
            if ref in records_by_id
        )
        blocking_gap = not refs or (
            require_authoritative_per_question and not authoritative
        )
        # A refusal sentence is not a direct answer, even when evidence exists.
        grounded = bool(direct) and not _is_refusal(direct) and bool(refs)
        answered = grounded and not blocking_gap
        if grounded:
            grounded_claims += 1
        if not answered:
            blocking.append(
                f"{qid}:authoritative_evidence_missing"
                if refs and require_authoritative_per_question and not authoritative
                else f"{qid}:evidence_or_direct_answer_missing"
            )
        ask_id, required = lineage.get(index, ("", True))
        if ask_id:
            answered_asks[ask_id] = (
                answered_asks.get(ask_id, False) or (grounded and required)
            )
        results.append(QuestionCompletion(qid, answered, bool(direct), refs, blocking_gap))
    answer_complete = bool(results) and all(item.answered for item in results)
    evidence_valid = bool(records) and any(item.evidence_refs for item in results)
    blocking = list(dict.fromkeys(blocking))
    answered_required = sum(1 for value in answered_asks.values() if value)
    if not answered_asks:
        # Pre-lineage snapshots: fall back to answered question count.
        answered_required = sum(1 for item in results if item.answered)
    partial = PartialContract(
        answered_required_asks=answered_required,
        grounded_claim_count=grounded_claims,
        source_quality_pass=bool(
            source_quality_pass
            if source_quality_partial_pass is None
            else source_quality_partial_pass
        ),
        relevance_pass=bool(
            relevance_pass if relevance_partial_pass is None else relevance_partial_pass
        ),
    )
    recovered = _recovered(answer_contract)
    passed = (
        bool(final_content.strip())
        and answer_complete
        and evidence_valid
        and bool(citation_valid)
        and not blocking
        and bool(source_quality_pass)
        and bool(relevance_pass)
        and not recovered
    )
    reason = None
    if not passed:
        reason = blocking[0] if blocking else "completion_contract_failed"
        if not source_quality_pass:
            reason = "source_quality_failed"
        elif not relevance_pass:
            reason = "answer_relevance_failed"
        elif recovered:
            reason = "synthesis_recovered_not_model_written"
    return CompletionResult(
        passed,
        results,
        evidence_valid,
        bool(citation_valid),
        answer_complete,
        blocking,
        reason,
        partial,
        [
            {"ask_id": ask_id, "answered": value}
            for ask_id, value in answered_asks.items()
        ],
    )


__all__ = [
    "CompletionResult",
    "PartialContract",
    "QuestionCompletion",
    "evaluate_completion",
]
