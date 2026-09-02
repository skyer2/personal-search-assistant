"""Trajectory grader：required / forbidden / causal if-then / budgets。

不再把 SequenceMatcher 固定路径当作 Agent 对错。旧 helper 仍在
tests/eval/trajectory.py，仅用于 workflow regression。
"""

from __future__ import annotations

from typing import Any


def _contains(events: list[str], token: str) -> bool:
    needle = str(token or "").strip()
    if not needle:
        return True
    return any(needle == item or needle in item for item in events)


def grade_constraints(
    events: list[str],
    constraints: dict[str, Any] | None,
    *,
    counts: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    constraints = dict(constraints or {})
    counts = dict(counts or {})
    attributes = dict(attributes or {})
    missing: list[str] = []
    forbidden_hit: list[str] = []
    limit_hit: list[str] = []

    for item in constraints.get("required") or []:
        if not _contains(events, str(item)):
            missing.append(str(item))
    for item in constraints.get("forbidden") or []:
        if _contains(events, str(item)):
            forbidden_hit.append(str(item))

    cond = constraints.get("if") or {}
    then = constraints.get("then") or {}
    cond_ok = True
    for key, value in cond.items():
        if attributes.get(key) != value and counts.get(key) != value:
            cond_ok = False
            break
    if cond_ok and then:
        for item in then.get("required") or []:
            if not _contains(events, str(item)):
                missing.append(f"then:{item}")
        for item in then.get("forbidden") or []:
            if _contains(events, str(item)):
                forbidden_hit.append(f"then:{item}")

    for key, raw in (constraints.get("limits") or {}).items():
        if raw is None:
            continue
        actual = counts.get(key)
        if actual is None:
            continue
        try:
            if float(actual) > float(raw):
                limit_hit.append(f"{key}>{raw}")
        except (TypeError, ValueError):
            continue

    ok = not missing and not forbidden_hit and not limit_hit
    return {
        "ok": ok,
        "missing": missing,
        "forbidden_hit": forbidden_hit,
        "limit_hit": limit_hit,
        "score": 1.0 if ok else 0.0,
    }


def events_from_plan(plan: Any) -> list[str]:
    events = ["plan.created"]
    if plan is not None and getattr(plan, "steps", None):
        events.append("plan.validated")
        if any(getattr(s, "step_type", "") in {"generate_markdown", "summarize", "convert_pdf"} for s in plan.steps):
            events.append("synthesis.planned")
    return events
