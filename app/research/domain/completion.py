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
class CompletionResult:
    passed: bool
    question_results: list[QuestionCompletion]
    evidence_valid: bool
    citation_valid: bool
    answer_complete: bool
    unresolved_blocking: list[str] = field(default_factory=list)
    failure_reason: str | None = None

    @property
    def outcome(self) -> str:
        if self.passed:
            return "success"
        if self.evidence_valid or any(item.answered for item in self.question_results):
            return "partial"
        return "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "question_results": [item.to_dict() for item in self.question_results],
            "evidence_valid": self.evidence_valid,
            "citation_valid": self.citation_valid,
            "answer_complete": self.answer_complete,
            "unresolved_blocking": list(self.unresolved_blocking),
            "failure_reason": self.failure_reason,
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


def evaluate_completion(
    *,
    brief: Any,
    answer_contract: dict[str, Any] | None,
    evidence_records: list[dict[str, Any]] | None,
    final_content: str = "",
    citation_valid: bool = True,
    unresolved_blocking: list[str] | None = None,
) -> CompletionResult:
    """Apply the one completion contract using only runtime-observable facts."""
    records = [row for row in (evidence_records or []) if isinstance(row, dict)]
    valid_ids = _record_ids(records)
    rows = _answer_rows(answer_contract)
    contract_objective = str((answer_contract or {}).get("objective") or "")
    if not contract_objective and isinstance((answer_contract or {}).get("final_answer"), dict):
        contract_objective = str((answer_contract or {})["final_answer"].get("objective") or "")
    questions = _questions(brief, contract_objective)
    results: list[QuestionCompletion] = []
    blocking = list(unresolved_blocking or [])
    for index, question in enumerate(questions, 1):
        qid = f"q{index}"
        row = next((item for item in rows if str(item.get("question_id") or "") == qid), None)
        if row is None and index <= len(rows):
            row = rows[index - 1]
        direct = str((row or {}).get("direct_answer") or "").strip()
        refs = [ref for ref in _refs(row or {}) if ref in valid_ids]
        blocking_gap = not refs
        answered = bool(direct) and not blocking_gap
        if not answered:
            blocking.append(f"{qid}:evidence_or_direct_answer_missing")
        results.append(QuestionCompletion(qid, answered, bool(direct), refs, blocking_gap))
    answer_complete = bool(results) and all(item.answered for item in results)
    evidence_valid = bool(records) and any(item.evidence_refs for item in results)
    passed = bool(final_content.strip()) and answer_complete and evidence_valid and bool(citation_valid) and not blocking
    reason = None if passed else (blocking[0] if blocking else "completion_contract_failed")
    return CompletionResult(passed, results, evidence_valid, bool(citation_valid), answer_complete, blocking, reason)


__all__ = ["CompletionResult", "QuestionCompletion", "evaluate_completion"]
