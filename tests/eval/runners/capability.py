"""L1.5 capability regression: semantic contracts without LLM execution."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.research.coverage.assessor import assess_coverage
from app.research.coverage.compiler import compile_coverage_contract
from app.research.planning.expansion import expand_plan
from app.research.planning.planner import plan_for_spec
from app.research.spec.compiler import compile_research_spec
from app.research.spec.models import Premise
from app.research.spec.validator import validate_research_spec
from tests.eval.metrics import TaskEvalResult
from tests.eval.runners.component import DATASETS, load_jsonl

CAPABILITY_DATASET = DATASETS / "capability_v1.jsonl"
CAPABILITIES = (
    "single_fact",
    "filtered_aggregation",
    "comparison",
    "explicit_multi_hop",
    "implicit_multi_hop",
    "long_report",
    "freshness",
    "ambiguity",
    "conflict",
    "false_premise",
    "vertical_domain",
    "long_tail",
    "multilingual",
    "strict_citation",
    "multi_turn_correction",
)


def _path(obj: Any, path: str) -> Any:
    value = obj
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise AssertionError(f"missing expectation path: {path}")
        value = value[key]
    return value


def _check_expectations(obj: Any, expectations: dict[str, Any] | None) -> list[str]:
    issues: list[str] = []
    for key, expected in (expectations or {}).items():
        try:
            actual = _path(obj, key)
        except AssertionError as error:
            issues.append(str(error))
            continue
        if key == "language_hints" and isinstance(actual, list) and isinstance(expected, list):
            comparison_ok = sorted(actual) == sorted(expected)
        else:
            comparison_ok = actual == expected
        if not comparison_ok:
            issues.append(f"{key}: expected {expected!r}, got {actual!r}")
    return issues


def _scenario_evidence(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for row in rows or []:
        item = dict(row)
        offset = item.pop("published_offset_days", None)
        if offset is not None:
            item["published_at"] = (now + timedelta(days=int(offset))).date().isoformat()
        result.append(item)
    return result


def _grade_case(case: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    issues: list[str] = []
    existing_spec = None
    if case.get("existing_spec"):
        initial = compile_research_spec(str(case["query"]))
        existing_spec = initial.to_dict()

    spec = compile_research_spec(
        str(case["query"]),
        conversation_delta=str(case.get("conversation_delta") or ""),
        existing_spec=existing_spec,
    )
    if case.get("premise"):
        spec = replace(spec, premises=[Premise(**case["premise"])])

    spec_issues = validate_research_spec(spec)
    expected_issues = list(case.get("spec_issues") or [])
    if spec_issues != expected_issues:
        issues.append(f"spec_issues: expected {expected_issues}, got {spec_issues}")
    issues.extend(_check_expectations(spec.to_dict(), case.get("spec_expect")))

    if not spec_issues:
        contract = compile_coverage_contract(spec)
        initial_units = contract.units
        coverage_expect = dict(case.get("coverage_expect") or {})
        expected_initial = int(coverage_expect.get("initial_unit_count", coverage_expect.get("unit_count", 0)) or 0)
        if expected_initial and len(initial_units) != expected_initial:
            issues.append(f"initial coverage units: expected {expected_initial}, got {len(initial_units)}")
        if coverage_expect.get("candidate_dimension") and not any(u.dimension_id == "candidate_set" for u in initial_units):
            issues.append("candidate dimension missing")
        if coverage_expect.get("freshness_required") and not all(u.freshness_policy.get("required") for u in initial_units):
            issues.append("freshness policy missing")
        if coverage_expect.get("conflict_required") and not all(u.conflict_policy.get("blocking") for u in initial_units):
            issues.append("conflict policy missing")

        if case.get("candidate_names"):
            expanded_contract = compile_coverage_contract(spec, candidate_names=list(case["candidate_names"]))
            expected_expanded = int(coverage_expect.get("expanded_unit_count", 0) or 0)
            if expected_expanded and len(expanded_contract.units) != expected_expanded:
                issues.append(f"expanded coverage units: expected {expected_expanded}, got {len(expanded_contract.units)}")
            expected_candidates = int(coverage_expect.get("expanded_candidate_dimensions", 0) or 0)
            actual_candidates = len({u.subject_id for u in expanded_contract.units if u.subject_id.startswith("candidate:")})
            if expected_candidates and actual_candidates != expected_candidates:
                issues.append(f"expanded candidates: expected {expected_candidates}, got {actual_candidates}")
            if any(u.dimension_id == "candidate_set" for u in expanded_contract.units):
                issues.append("expanded contract retained candidate_set unit")
            candidate_set = {
                "candidate_set_id": "candidate_set_regression",
                "status": "complete",
                "candidates": [{"name": name} for name in case["candidate_names"]],
            }
            expansion = expand_plan(spec, candidate_set, plan_version=1)
            if not expansion.applied:
                issues.append(f"expansion rejected: {expansion.reason}")
            if expansion.candidate_set.get("expanded_plan_version") != 2:
                issues.append("candidate expansion did not increment plan version")

        plan = plan_for_spec(spec, contract)
        task_kinds = [str((step.metadata or {}).get("task_kind") or "") for step in plan.steps]
        plan_expect = dict(case.get("plan_expect") or {})
        expected_steps = int(plan_expect.get("step_count", 0) or 0)
        if expected_steps and len(plan.steps) != expected_steps:
            issues.append(f"plan steps: expected {expected_steps}, got {len(plan.steps)}")
        expected_kinds = list(plan_expect.get("task_kinds") or [])
        if expected_kinds and task_kinds != expected_kinds:
            issues.append(f"task kinds: expected {expected_kinds}, got {task_kinds}")

        scenario = dict(case.get("coverage_scenario") or {})
        if scenario:
            state = assess_coverage(
                contract,
                claims=scenario.get("claims") or [],
                evidence=_scenario_evidence(scenario.get("evidence")),
                claim_conflicts=scenario.get("claim_conflicts") or [],
                claim_resolutions=scenario.get("claim_resolutions") or [],
            )
            actual_statuses = [unit.status for unit in state.units]
            if actual_statuses != list(scenario.get("expected_statuses") or []):
                issues.append(f"coverage statuses: expected {scenario.get('expected_statuses')}, got {actual_statuses}")

    return not issues, {"issues": issues, "spec_issues": spec_issues}


def run_capability_dry_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or CAPABILITY_DATASET)
    observed = {str(case.get("capability") or "") for case in cases}
    missing = sorted(set(CAPABILITIES) - observed)
    results: list[TaskEvalResult] = []
    for case in cases:
        ok, graded = _grade_case(case)
        results.append(
            TaskEvalResult(
                task_id=str(case.get("case_id")),
                query=str(case.get("query")),
                mode="capability",
                success=ok,
                gate_ok=ok,
                outcome_score=1.0 if ok else 0.0,
                trajectory_score=1.0 if ok else 0.0,
                grounding_score=1.0 if case.get("coverage_scenario") and ok else None,
                plan_validation_ok=ok,
                variant=str(case.get("capability")),
                metadata={
                    "capability": case.get("capability"),
                    "graded": graded,
                },
            )
        )
    if missing:
        results.append(
            TaskEvalResult(
                task_id="capability_coverage",
                query="All 15 capability classes",
                mode="capability",
                success=False,
                gate_ok=False,
                outcome_score=0.0,
                trajectory_score=0.0,
                plan_validation_ok=False,
                variant="coverage",
                metadata={"graded": {"issues": [f"missing capability: {name}" for name in missing]}},
            )
        )
    return results


__all__ = ["CAPABILITIES", "CAPABILITY_DATASET", "run_capability_dry_eval"]
