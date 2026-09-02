"""L2 Harness Scenario dry-run：planner invariants + constraint schema，不跑 LLM。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.harness.planner import understand_task
from app.research.planning.compose import compose_execution_plan_sync
from tests.eval.graders.deterministic import grade_plan_invariants
from tests.eval.graders.outcome import grade_gates
from tests.eval.graders.trajectory import events_from_plan, grade_constraints
from tests.eval.metrics import TaskEvalResult
from tests.eval.runners.component import DATASETS, load_jsonl


def run_scenario_dry_eval(
    path: Path | None = None,
    cases: list[dict[str, Any]] | None = None,
) -> list[TaskEvalResult]:
    rows = list(cases or load_jsonl(path or DATASETS / "harness_scenarios_v1.jsonl"))
    results: list[TaskEvalResult] = []
    for case in rows:
        intent = understand_task(str(case["query"]), bool(case.get("requires_upload")))
        plan, compose_issues = compose_execution_plan_sync(intent)
        expect = dict(case.get("planner_expect") or {})
        if expect.get("deliverable") and intent.deliverable != expect["deliverable"]:
            compose_issues = list(compose_issues) + [
                f"deliverable:{intent.deliverable}!={expect['deliverable']}"
            ]
        plan_grade = grade_plan_invariants(plan, expect)
        if compose_issues:
            plan_grade["ok"] = False
            plan_grade["issues"] = list(plan_grade.get("issues") or []) + compose_issues
        events = events_from_plan(plan)
        constraint = grade_constraints(
            events,
            case.get("constraints"),
            counts={"replan_count": 0, "tool_calls": 0, "workers": plan_grade["independent_research"]},
        )
        # dry-run 只把门禁放在 planner invariants；因果 if/then 留给 live
        gates = grade_gates(
            plan_ok=bool(plan_grade["ok"]),
            constraint_ok=True,
            requested=list(case.get("gates") or []),
        )
        taxonomy = dict(case.get("taxonomy") or {})
        ok = bool(plan_grade["ok"] and gates["ok"])
        results.append(
            TaskEvalResult(
                task_id=str(case["case_id"]),
                query=str(case["query"]),
                mode="scenario-dry",
                success=ok,
                gate_ok=ok,
                outcome_score=1.0 if ok else 0.0,
                trajectory_score=1.0 if plan_grade["ok"] else 0.0,
                plan_validation_ok=bool(plan_grade["ok"]),
                intent_deliverable_ok=not expect.get("deliverable")
                or intent.deliverable == expect.get("deliverable"),
                intent_confidence=intent.intent_confidence,
                failure_stage="" if ok else str(taxonomy.get("stage") or "planning"),
                failure_type="" if ok else str(taxonomy.get("type") or "planner"),
                variant="dry-run",
                metadata={
                    "category": case.get("category"),
                    "failure_mode": case.get("failure_mode"),
                    "plan_grade": plan_grade,
                    "constraints": constraint,
                    "gates": gates,
                },
            )
        )
    return results
