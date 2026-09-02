"""
Harness Eval 入口。

  # PR CI：L1 component + L2 scenario dry-run（无 LLM）
  python tests/eval/run_eval.py --dry-run --fail-on-regression

  # Live scenario（需 LLM；--fixture 走固定语料）
  python tests/eval/run_eval.py --live --variant full --limit 5 --fixture

  # Repeat reliability
  python tests/eval/run_eval.py --live --variant full --repeat 3 --fixture --limit 5

  # Judge calibration（human gold vs automatic grader）
  python tests/eval/run_eval.py --calibrate-judge

  # 对照实验
  python tests/eval/run_eval.py --live --variant vanilla --fixture
  python tests/eval/run_eval.py --live --variant no_replan --fixture
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.eval.graders.llm_judge import judge_answer_quality
from tests.eval.graders.outcome import grade_gates
from tests.eval.graders.structure import heuristic_report_judge
from tests.eval.graders.trajectory import grade_constraints
from tests.eval.metrics import (
    TaskEvalResult,
    build_report,
    compare_with_baseline,
)
from tests.eval.reliability import reliability_report
from tests.eval.runners.component import load_jsonl, run_component_eval
from tests.eval.runners.experiment import (
    apply_variant,
    experiment_manifest,
    load_variant,
    restore_variant,
)
from tests.eval.runners.scenario import run_scenario_dry_eval

BASELINE_PATH = ROOT / "tests" / "eval" / "results" / "baseline.json"
SCENARIO_PATH = ROOT / "tests" / "eval" / "datasets" / "harness_scenarios_v1.jsonl"
LEGACY_PATH = ROOT / "tests" / "eval" / "datasets" / "legacy" / "tasks_legacy.jsonl"


def load_tasks(path: Path) -> list[dict]:
    return load_jsonl(path)


def run_dry_eval(
    tasks: list[dict] | None = None,
    min_trajectory_similarity: float = 0.6,
) -> list[TaskEvalResult]:
    """默认跑 L1 component + L2 scenario dry-run。传入 tasks 时只评这些 scenario。"""
    _ = min_trajectory_similarity
    if not tasks:
        return run_component_eval() + run_scenario_dry_eval()
    if tasks and "case_id" in tasks[0]:
        return run_scenario_dry_eval(cases=tasks)
    # 兼容旧调用：只做 planner invariants，不再用 expected_agents / SequenceMatcher 判成败
    from app.agent.harness.planner import understand_task
    from app.research.planning.compose import compose_execution_plan_sync
    from tests.eval.graders.deterministic import grade_plan_invariants

    results: list[TaskEvalResult] = []
    for task in tasks:
        intent = understand_task(task["query"], bool(task.get("requires_upload")))
        plan, issues = compose_execution_plan_sync(intent)
        expect = {"acyclic": True, "has_synthesis": True}
        if task.get("expected_deliverable"):
            expect["deliverable"] = task["expected_deliverable"]
        graded = grade_plan_invariants(plan, expect)
        deliverable_ok = not task.get("expected_deliverable") or intent.deliverable == task.get(
            "expected_deliverable"
        )
        ok = bool(graded["ok"] and deliverable_ok and not issues)
        results.append(
            TaskEvalResult(
                task_id=str(task.get("id") or task.get("case_id")),
                query=task["query"],
                mode="dry-run",
                success=ok,
                gate_ok=ok,
                plan_validation_ok=not issues,
                intent_deliverable_ok=deliverable_ok,
                intent_confidence=intent.intent_confidence,
                metadata={"plan_grade": graded, "plan_issues": issues},
            )
        )
    return results


def _events_from_harness(metadata: dict, trace: list) -> list[str]:
    events = ["run.started"]
    if metadata.get("replan_count"):
        events.append("replan.applied")
    if metadata.get("progress_assessment") or metadata.get("observability"):
        events.append("progress.evaluated")
    for event in trace or []:
        phase = getattr(event, "phase", None) or (event.get("phase") if isinstance(event, dict) else None)
        if phase:
            events.append(str(phase))
    return events


async def run_live_eval(
    tasks: list[dict],
    min_trajectory_similarity: float = 0.6,
    *,
    variant_name: str = "full",
    repeat: int = 1,
    judge_enabled: bool | None = None,
    fixture: bool = False,
) -> list[TaskEvalResult]:
    from app.agent.main_agent import harness
    from app.config.loader import get_harness_config

    _ = min_trajectory_similarity
    if fixture:
        os.environ["HARNESS_EVAL_FIXTURE"] = "1"
    eval_cfg = get_harness_config()
    variant = load_variant(variant_name)
    previous = apply_variant(eval_cfg, variant)
    quality_enabled = eval_cfg.eval_llm_judge_enabled if judge_enabled is None else bool(judge_enabled)
    repeats = max(1, int(repeat or 1))
    results: list[TaskEvalResult] = []
    try:
        for task in tasks:
            for repeat_index in range(repeats):
                session_id = (
                    f"eval_{task.get('case_id') or task.get('id')}_{repeat_index}_{uuid.uuid4().hex[:8]}"
                )
                try:
                    harness_result = await harness.run(
                        task["query"],
                        session_id,
                        mode=str(variant.get("mode") or "agent"),
                    )
                    meta = dict(harness_result.metadata or {})
                    events = _events_from_harness(meta, harness_result.trace)
                    constraint = grade_constraints(
                        events,
                        task.get("constraints"),
                        counts={
                            "replan_count": int(meta.get("replan_count") or 0),
                            "tool_calls": int(meta.get("tool_calls_count") or 0),
                        },
                        attributes={
                            "progress.verdict": str(
                                (meta.get("progress_assessment") or {}).get("verdict") or ""
                            )
                        },
                    )
                    structure = None
                    if harness_result.content and eval_cfg.eval_heuristic_judge_enabled:
                        structure = heuristic_report_judge(
                            harness_result.content,
                            min_score=eval_cfg.eval_heuristic_judge_min_score,
                            expect_citations=True,
                        )
                    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
                    quality = await judge_answer_quality(
                        question=task["query"],
                        answer=harness_result.content or "",
                        evidence=str(meta.get("evidence_digest") or ""),
                        brief=str(meta.get("brief") or ""),
                        reference=str(task.get("reference") or task.get("expected_answer") or ""),
                        must_include=list(task.get("must_include") or []),
                        enabled=quality_enabled,
                    )
                    gates = grade_gates(
                        harness_status=harness_result.status,
                        abort_reason=str(meta.get("abort_reason") or ""),
                        constraint_ok=True,
                        plan_ok=True,
                        requested=list(task.get("gates") or []),
                        outcome_wrong=bool(quality.critical_error),
                        unsupported=(
                            bool(quality.unsupported_claims)
                            if quality.judge_source not in {"disabled", "unavailable"}
                            else False
                        ),
                    )
                    taxonomy = dict(task.get("taxonomy") or {})
                    success = gates["ok"] and harness_result.status in {"success", "partial", ""}
                    ccr = meta.get("citation_coverage_rate")
                    results.append(
                        TaskEvalResult(
                            task_id=str(task.get("case_id") or task.get("id")),
                            query=task["query"],
                            mode="live",
                            success=success,
                            gate_ok=gates["ok"],
                            outcome_score=quality.correctness,
                            grounding_score=quality.grounding
                            if quality.grounding is not None
                            else (float(ccr) if ccr is not None else None),
                            trajectory_score=constraint["score"],
                            status=harness_result.status,
                            retry_count=harness_result.retry_count,
                            artifacts=harness_result.artifacts,
                            tool_calls_count=int(meta.get("tool_calls_count") or 0),
                            latency_ms=int(meta.get("latency_ms") or 0),
                            tokens=int(meta.get("total_tokens") or usage.get("total_tokens") or 0),
                            citation_coverage_rate=float(ccr) if ccr is not None else None,
                            hallucination_rate=meta.get("hallucination_rate"),
                            report_judge_score=structure.score if structure else None,
                            report_judge_passed=structure.passed if structure else None,
                            session_id=session_id,
                            run_id=str(meta.get("run_id") or session_id),
                            trace_id=str(meta.get("trace_id") or ""),
                            variant=str(variant.get("name") or variant_name),
                            replan_count=int(meta.get("replan_count") or 0),
                            failure_stage="" if success else str(taxonomy.get("stage") or "runtime"),
                            failure_type="" if success else ",".join(gates["failures"] or [taxonomy.get("type") or ""]),
                            metadata={
                                **meta,
                                "constraints": constraint,
                                "gates": gates,
                                "structure_judge": structure.to_dict() if structure else None,
                                "quality_judge": quality.to_dict(),
                                "case_id": task.get("case_id") or task.get("id"),
                                "variant": variant.get("name"),
                                "repeat_index": repeat_index,
                            },
                        )
                    )
                    try:
                        from app.observability import EventType, get_recorder

                        get_recorder().emit(
                            EventType.EVAL_SCORED,
                            phase="eval",
                            status="pass" if success else "fail",
                            session_id=session_id,
                            run_id=str(meta.get("run_id") or session_id),
                            trace_id=str(meta.get("trace_id") or ""),
                            attributes={
                                "case_id": task.get("case_id") or task.get("id"),
                                "benchmark": "harness_scenarios_v1",
                                "variant": variant.get("name"),
                                "accuracy": quality.correctness,
                                "citation_score": ccr,
                                "replan_count": int(meta.get("replan_count") or 0),
                                "latency_ms": int(meta.get("latency_ms") or 0),
                                "repeat_index": repeat_index,
                                "judge_source": quality.judge_source,
                            },
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    results.append(
                        TaskEvalResult(
                            task_id=str(task.get("case_id") or task.get("id")),
                            query=task["query"],
                            mode="live",
                            success=False,
                            gate_ok=False,
                            error=str(exc),
                            variant=str(variant.get("name") or variant_name),
                            metadata={"repeat_index": repeat_index},
                        )
                    )
    finally:
        restore_variant(eval_cfg, previous)
    return results


def save_report(report_dict: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = output_dir / f"eval_report_{ts}.json"
    out.write_text(json.dumps(report_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_baseline(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_baseline(report_dict: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {
        "generated_at": report_dict.get("generated_at"),
        "mode": report_dict.get("mode"),
        "dataset": report_dict.get("dataset"),
        "total": report_dict.get("total"),
        "passed": report_dict.get("passed"),
        "task_success_rate": report_dict.get("task_success_rate"),
        "gate_pass_rate": report_dict.get("gate_pass_rate"),
        "outcome_score": report_dict.get("outcome_score"),
        "grounding_score": report_dict.get("grounding_score"),
        "trajectory_score": report_dict.get("trajectory_score"),
        "plan_validation_pass_rate": report_dict.get("plan_validation_pass_rate"),
        "intent_deliverable_accuracy": report_dict.get("intent_deliverable_accuracy"),
        "note": report_dict.get("note"),
    }
    path.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")


def write_comparison_markdown(
    report_dict: dict,
    comparison: dict | None,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = output_dir / f"eval_comparison_{ts}.md"
    lines = [
        "# Harness Eval 对比报告",
        "",
        f"- 生成时间: {report_dict.get('generated_at')}",
        f"- 模式: {report_dict.get('mode')}",
        f"- 数据集: {report_dict.get('dataset')}",
        f"- 任务: {report_dict.get('passed')}/{report_dict.get('total')} 通过",
        "",
        "## 指标总览",
        "",
        "| 指标 | 当前 | 基线 | Delta |",
        "|------|------|------|-------|",
    ]
    metric_labels = {
        "task_success_rate": "Gate",
        "gate_pass_rate": "Gate",
        "outcome_score": "Outcome",
        "grounding_score": "Grounding",
        "trajectory_score": "Trajectory",
        "plan_validation_pass_rate": "Invariants",
        "replan_recovery_rate": "Replan Recovery",
        "avg_tool_calls": "Tool Calls",
        "latency_p95_ms": "P95(ms)",
    }
    baseline = comparison or {}
    deltas = baseline.get("deltas", {})
    lines.append(f"> 基线时间: {baseline.get('baseline_generated_at', 'N/A')} ({baseline.get('baseline_mode', 'N/A')})")
    lines.append("")
    for key, label in metric_labels.items():
        cur = report_dict.get(key)
        if cur is None:
            continue
        delta = deltas.get(key) if comparison else None
        lines.append(f"| {label} | {cur} | {('-' if delta is None else round(float(cur) - float(delta), 3))} | {delta if delta is not None else '-'} |")
    if comparison and comparison.get("regressions"):
        lines.extend(["", "## 回归告警", ""])
        for item in comparison["regressions"]:
            lines.append(f"- {item}")
    failed = [item for item in report_dict.get("results", []) if not item.get("success")]
    lines.extend(["", "## 失败任务", ""])
    if failed:
        for item in failed:
            lines.append(
                f"- **{item['task_id']}**: stage={item.get('failure_stage')} type={item.get('failure_type')}"
            )
    else:
        lines.append("- 无")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def print_summary(report_dict: dict, comparison: dict | None = None) -> None:
    print("=== Harness Eval Report ===")
    print(f"Layer: {report_dict.get('mode')} | Dataset: {report_dict.get('dataset')}")
    print(f"Tasks: {report_dict['total']} | Passed: {report_dict['passed']}")
    print(f"Gate: {report_dict.get('task_success_rate', 0):.1%}")
    if report_dict.get("outcome_score") is not None:
        print(f"Outcome: {report_dict['outcome_score']:.3f}")
    if report_dict.get("grounding_score") is not None:
        print(f"Grounding: {report_dict['grounding_score']:.3f}")
    reliability = report_dict.get("reliability") or {}
    if reliability.get("repeat", 0) > 1:
        print(
            "Reliability: "
            f"pass@1={reliability.get('pass_at_1')} "
            f"pass@{reliability.get('repeat')}={reliability.get('pass_at_k')} "
            f"pass^{reliability.get('repeat')}={reliability.get('pass_hat_k')}"
        )
    if report_dict.get("failure_distribution"):
        print(f"Failures: {report_dict['failure_distribution']}")
    if comparison and comparison.get("regressions"):
        print(f"REGRESSION: {', '.join(comparison['regressions'])}")
    for item in report_dict["results"]:
        mark = "PASS" if item["success"] else "FAIL"
        print(f"  [{mark}] {item['task_id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run harness evaluation")
    parser.add_argument("--tasks", default="", help="Optional jsonl override")
    parser.add_argument("--dry-run", action="store_true", help="L1+L2 deterministic eval")
    parser.add_argument("--live", action="store_true", help="L2 live harness eval")
    parser.add_argument("--component", action="store_true", help="Only L1 component datasets")
    parser.add_argument("--variant", default=os.getenv("HARNESS_EVAL_VARIANT") or "full")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1, help="Reliability repeats per case")
    parser.add_argument("--fixture", action="store_true", help="Use deterministic search/fetch corpus")
    parser.add_argument("--judge", action="store_true", help="Enable QualityJudge for this run")
    parser.add_argument("--calibrate-judge", action="store_true", help="Compare human gold vs automatic judge")
    parser.add_argument("--output", default=str(ROOT / "tests" / "eval" / "results"))
    parser.add_argument("--baseline", default=str(BASELINE_PATH))
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--report-md", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    args = parser.parse_args()

    if args.calibrate_judge:
        from tests.eval.graders.calibration import calibrate_judge_sync

        payload = calibrate_judge_sync(use_llm=bool(args.judge))
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = out_dir / f"judge_calibration_{ts}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("=== Judge Calibration ===")
        print(f"n={payload['n']} mode={payload['mode']}")
        print(
            f"agreement={payload['agreement']} kappa={payload['kappa']} "
            f"precision={payload['precision']} recall={payload['recall']} "
            f"correctness_mae={payload['correctness_mae']}"
        )
        print(payload["note"])
        print(f"Report saved: {out}")
        return

    if args.live:
        path = Path(args.tasks) if args.tasks else SCENARIO_PATH
        tasks = load_tasks(path)
        if args.limit > 0:
            tasks = tasks[: args.limit]
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        results = asyncio.run(
            run_live_eval(
                tasks,
                cfg.eval_trajectory_min_similarity,
                variant_name=args.variant,
                repeat=args.repeat,
                judge_enabled=True if args.judge else None,
                fixture=bool(args.fixture),
            )
        )
        mode = f"live:{args.variant}"
        dataset = path.name
    elif args.component:
        results = run_component_eval()
        mode = "component"
        dataset = "planner_v2+progress_v1+replan_v1+evidence_v1"
    else:
        if args.tasks:
            results = run_dry_eval(load_tasks(Path(args.tasks)))
            dataset = Path(args.tasks).name
        else:
            results = run_dry_eval()
            dataset = "component+harness_scenarios_v1"
        mode = "dry-run"

    report = build_report(results)
    report_dict = report.to_dict()
    report_dict["mode"] = mode
    report_dict["dataset"] = dataset
    report_dict["generated_at"] = datetime.now().isoformat()
    report_dict["note"] = (
        "Gate pass rate is hard-constraint only. "
        "ReportStructureGrader is not answer quality. "
        "QualityJudge is disabled unless --judge or eval.llm_judge_enabled. "
        "BrowseComp official Accuracy is separate from this report."
    )
    if args.live:
        report_dict["manifest"] = experiment_manifest(
            dataset=dataset,
            variant=load_variant(args.variant),
            repeat=args.repeat,
        )
        reliability = reliability_report(results, k=args.repeat)
        report_dict["reliability"] = reliability
        report_dict["pass_at_1"] = reliability["pass_at_1"]
        report_dict["pass_at_k"] = reliability["pass_at_k"]
        report_dict["pass_hat_k"] = reliability["pass_hat_k"]

    baseline = load_baseline(Path(args.baseline))
    comparison = compare_with_baseline(report_dict, baseline) if baseline else None
    if comparison:
        report_dict["baseline_comparison"] = comparison

    out = save_report(report_dict, Path(args.output))
    print_summary(report_dict, comparison)
    print(f"Report saved: {out}")

    if args.report_md:
        md_out = write_comparison_markdown(report_dict, comparison, Path(args.output))
        print(f"Comparison report: {md_out}")
    if args.save_baseline:
        save_baseline(report_dict, Path(args.baseline))
        print(f"Baseline saved: {args.baseline}")
    if args.fail_on_regression and comparison and comparison.get("blocked_merge"):
        print("ERROR: Eval regression detected, exiting with code 1")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
