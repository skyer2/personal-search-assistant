"""Outcome / gate 分离：hard constraint 才决定 success。"""

from __future__ import annotations

from typing import Any


HARD_GATES = {
    "timeout",
    "unsafe",
    "invalid_schema",
    "wrong_final_answer",
    "unsupported_claim",
    "budget_exhausted",
}


def grade_gates(
    *,
    harness_status: str = "",
    abort_reason: str = "",
    constraint_ok: bool = True,
    plan_ok: bool = True,
    requested: list[str] | None = None,
    outcome_wrong: bool = False,
    unsupported: bool = False,
) -> dict[str, Any]:
    failures: list[str] = []
    requested = list(requested or [])
    status = str(harness_status or "")
    if status in {"timeout", "cancelled"} or abort_reason in {"deadline_exceeded"}:
        failures.append("timeout")
    if abort_reason in {"budget_exceeded", "budget_tool_calls", "budget_tokens"}:
        failures.append("budget_exhausted")
    if not plan_ok:
        failures.append("invalid_schema")
    if not constraint_ok:
        failures.append("constraint")
    if outcome_wrong:
        failures.append("wrong_final_answer")
    if unsupported:
        failures.append("unsupported_claim")
    if requested:
        failures = [item for item in failures if item in requested or item in HARD_GATES]
    return {
        "ok": not failures,
        "failures": failures,
    }


def score_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None
