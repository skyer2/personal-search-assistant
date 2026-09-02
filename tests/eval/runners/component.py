"""L1 Agent Component Eval：Planner / Progress / Replan / Evidence。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent.harness.planner import understand_task
from app.research.planning.compose import compose_execution_plan_sync
from tests.eval.graders.deterministic import grade_plan_invariants
from tests.eval.graders.evidence import grade_evidence_case
from tests.eval.graders.progress import grade_progress_case
from tests.eval.graders.replan import grade_replan_case
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


def run_planner_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or DATASETS / "planner_v2.jsonl")
    results: list[TaskEvalResult] = []
    for case in cases:
        intent = understand_task(case["query"], bool(case.get("requires_upload")))
        plan, issues = compose_execution_plan_sync(intent)
        expect = dict(case.get("expected") or {})
        if expect.get("deliverable") and intent.deliverable != expect["deliverable"]:
            issues = list(issues) + [f"deliverable:{intent.deliverable}!={expect['deliverable']}"]
        graded = grade_plan_invariants(plan, expect)
        if issues:
            graded["ok"] = False
            graded["issues"] = list(graded.get("issues") or []) + issues
        result = _result(case, "planner", graded)
        result.intent_deliverable_ok = not expect.get("deliverable") or intent.deliverable == expect.get(
            "deliverable"
        )
        result.intent_confidence = intent.intent_confidence
        results.append(result)
    return results


def run_progress_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or DATASETS / "progress_v1.jsonl")
    return [_result(case, "progress", grade_progress_case(case)) for case in cases]


def run_replan_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or DATASETS / "replan_v1.jsonl")
    return [_result(case, "replan", grade_replan_case(case)) for case in cases]


def run_evidence_eval(path: Path | None = None) -> list[TaskEvalResult]:
    cases = load_jsonl(path or DATASETS / "evidence_v1.jsonl")
    return [_result(case, "evidence", grade_evidence_case(case)) for case in cases]


def run_component_eval() -> list[TaskEvalResult]:
    return [
        *run_planner_eval(),
        *run_progress_eval(),
        *run_replan_eval(),
        *run_evidence_eval(),
    ]
