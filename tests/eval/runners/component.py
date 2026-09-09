"""L1 Agent Component Eval：Brief / Coverage / Supervisor / Evidence。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.research.brief.compiler import compile_structured_brief
from app.research.brief.models import FastPathEligibility
from app.research.coverage.judge import CoverageJudgement, judge_coverage
from app.research.supervisor.agent import SupervisorAgent
from tests.eval.graders.evidence import grade_evidence_case
from tests.eval.metrics import TaskEvalResult

DATASETS = Path(__file__).resolve().parents[1] / "datasets"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def _result(case: dict[str, Any], layer: str, graded: dict[str, Any]) -> TaskEvalResult:
    taxonomy = dict(case.get("taxonomy") or {})
    return TaskEvalResult(
        task_id=str(case.get("case_id") or case.get("id")),
        query=str(case.get("query") or case.get("answer") or layer),
        mode="component",
        success=bool(graded.get("ok")),
        gate_ok=bool(graded.get("ok")),
        outcome_score=1.0 if graded.get("ok") else 0.0,
        trajectory_score=1.0 if graded.get("ok") else 0.0,
        grounding_score=graded.get("grounding_score"),
        failure_stage="" if graded.get("ok") else str(taxonomy.get("stage") or layer),
        failure_type="" if graded.get("ok") else str(taxonomy.get("type") or layer),
        plan_validation_ok=bool(graded.get("ok")),
        variant=layer,
        metadata={"layer": layer, "graded": graded, "category": case.get("category")},
    )


def run_brief_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or DATASETS / "brief_v1.jsonl")
    results: list[TaskEvalResult] = []
    for case in cases:
        brief = compile_structured_brief(str(case["query"]))
        eligibility = FastPathEligibility.from_brief(brief)
        expect = dict(case.get("expected") or {})
        issues: list[str] = []
        checks = {
            "user_intent": brief.user_intent == expect.get("user_intent"),
            "fast_path": eligibility.eligible is bool(expect.get("fast_path")),
            "primary_required": brief.source_requirements.primary_required is bool(expect.get("primary_required", False)),
            "freshness_required": brief.freshness_requirements.required is bool(expect.get("freshness_required", False)),
            "deliverable_format": brief.deliverable.format == expect.get("deliverable_format", brief.deliverable.format),
            "deliverable_depth": brief.deliverable.depth == expect.get("deliverable_depth", brief.deliverable.depth),
            "explicit_subjects": list(brief.explicit_subjects) == list(expect.get("explicit_subjects") or brief.explicit_subjects),
            "min_independent_sources": brief.source_requirements.min_independent_sources == int(
                expect.get("min_independent_sources", brief.source_requirements.min_independent_sources)
            ),
        }
        issues = [name for name, passed in checks.items() if not passed]
        graded = {
            "ok": not issues,
            "issues": issues,
            "brief_id": brief.brief_id,
            "user_intent": brief.user_intent,
            "fast_path": eligibility.eligible,
        }
        results.append(_result(case, "brief", graded))
    return results


def run_coverage_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or DATASETS / "coverage_v1.jsonl")
    results: list[TaskEvalResult] = []
    for case in cases:
        brief = compile_structured_brief(str(case["query"]))
        judgement = judge_coverage(
            brief,
            list(case.get("findings") or []),
            claim_conflicts=list(case.get("claim_conflicts") or []),
        )
        expect = dict(case.get("expected") or {})
        issues: list[str] = []
        if judgement.status != expect.get("status"):
            issues.append(f"status:{judgement.status}!={expect.get('status')}")
        if judgement.sufficient is not bool(expect.get("sufficient")):
            issues.append("sufficient_mismatch")
        if "missing_count" in expect and len(judgement.missing) != int(expect["missing_count"]):
            issues.append("missing_count_mismatch")
        if "conflict_count" in expect and len(judgement.conflicts) != int(expect["conflict_count"]):
            issues.append("conflict_count_mismatch")
        if expect.get("has_weak_claims") and not judgement.weak_claims:
            issues.append("weak_claims_missing")
        if expect.get("has_next_questions") and not judgement.recommended_next_questions:
            issues.append("next_questions_missing")
        graded = {
            "ok": not issues,
            "issues": issues,
            "status": judgement.status,
            "sufficient": judgement.sufficient,
            "missing": list(judgement.missing),
            "recommended_next_questions": list(judgement.recommended_next_questions),
        }
        results.append(_result(case, "coverage", graded))
    return results


def run_supervisor_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or DATASETS / "supervisor_v1.jsonl")
    results: list[TaskEvalResult] = []
    for case in cases:
        brief = compile_structured_brief(str(case["query"]))
        raw_coverage = case.get("coverage")
        judgement = CoverageJudgement.from_dict(raw_coverage) if isinstance(raw_coverage, dict) else None
        action = SupervisorAgent(agent=None).fallback_action(brief, judgement, dict(case.get("budget") or {}))
        action = SupervisorAgent(agent=None).resolve_action(action, judgement, brief)
        expect = dict(case.get("expected") or {})
        issues: list[str] = []
        if action.action != expect.get("action"):
            issues.append(f"action:{action.action}!={expect.get('action')}")
        if "task_count" in expect and len(action.research_tasks) != int(expect["task_count"]):
            issues.append("task_count_mismatch")
        if "min_task_count" in expect and len(action.research_tasks) < int(expect["min_task_count"]):
            issues.append("too_few_tasks")
        if expect.get("bounded") and len(action.research_tasks) > 4:
            issues.append("task_burst")
        objectives = [item.objective for item in action.research_tasks]
        if expect.get("no_duplicates") and len(objectives) != len(set(objectives)):
            issues.append("duplicate_objective")
        graded = {
            "ok": not issues,
            "issues": issues,
            "action": action.action,
            "task_count": len(action.research_tasks),
            "objectives": objectives,
        }
        results.append(_result(case, "supervisor", graded))
    return results


def run_evidence_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or DATASETS / "evidence_v1.jsonl")
    return [_result(case, "evidence", grade_evidence_case(case)) for case in cases]


def run_component_eval() -> list[TaskEvalResult]:
    return [
        *run_brief_eval(),
        *run_coverage_eval(),
        *run_supervisor_eval(),
        *run_evidence_eval(),
    ]
