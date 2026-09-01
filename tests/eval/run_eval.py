"""
Harness 评测运行器

用法：
  # 仅测 planner（无需 API）
  python tests/eval/run_eval.py --dry-run

  # 完整 Harness 评测（需 .env 配置 LLM/Tavily 等）
  python tests/eval/run_eval.py --live --limit 3

  # 与基线对比
  python tests/eval/run_eval.py --dry-run --baseline tests/eval/results/baseline.json

  # 保存当前结果为基线
  python tests/eval/run_eval.py --dry-run --save-baseline
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

from app.agent.harness.planner import build_plan, understand_task
from tests.eval.intent_metrics import evaluate_intent_and_plan
from tests.eval.metrics import (
    TaskEvalResult,
    build_report,
    compare_with_baseline,
    evaluate_artifacts,
    evaluate_memory_recall,
    evaluate_tool_selection,
)
from tests.eval.trajectory import (
    compare_trajectories,
    extract_trajectory_dry,
    extract_trajectory_live,
    trajectory_passes,
)

BASELINE_PATH = ROOT / "tests" / "eval" / "results" / "baseline.json"
MEMORY_EVAL_USER = "eval_user_robot_research"
MEMORY_SEED_FACTS = [
    "用户此前研究过机器人行业，关注市场规模、主要厂商与产业链格局。",
    "2025年机器人行业全球增速约15%，服务机器人与协作机器人占比持续提升。",
    "工业机器人主要厂商包括发那科、ABB、库卡与中国本土品牌。",
]


def load_tasks(path: Path) -> list[dict]:
    tasks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            tasks.append(json.loads(line))
    return tasks


def _evaluate_trajectory(task: dict, actual: list[str], min_similarity: float = 0.6) -> tuple[float | None, dict]:
    expected = task.get("expected_trajectory")
    if not expected:
        return None, {}
    diff = compare_trajectories(actual, expected)
    return diff["similarity"], diff


def run_dry_eval(tasks: list[dict], min_trajectory_similarity: float = 0.6) -> list[TaskEvalResult]:
    results: list[TaskEvalResult] = []
    for task in tasks:
        eval_bundle = evaluate_intent_and_plan(task)
        intent = eval_bundle["intent"]
        plan = eval_bundle["plan"]
        expected_agents = task.get("expected_agents", [])
        planned_agents = [step.subagent for step in plan.steps if step.subagent]
        planned_steps = eval_bundle["planned_steps"]
        tool_ok = all(agent in planned_agents for agent in expected_agents) if expected_agents else True
        memory_expected = task.get("expected_memory_recall", False)

        actual_traj = extract_trajectory_dry(planned_steps, planned_agents)
        traj_sim, traj_diff = _evaluate_trajectory(task, actual_traj, min_trajectory_similarity)
        traj_ok = True
        if traj_sim is not None:
            traj_ok = trajectory_passes(traj_diff, min_similarity=min_trajectory_similarity)

        intent_ok = (
            eval_bundle["deliverable_ok"]
            and eval_bundle["slots_ok"]
            and eval_bundle["plan_validation_ok"]
        )

        results.append(
            TaskEvalResult(
                task_id=task["id"],
                query=task["query"],
                mode="dry-run",
                success=tool_ok and len(plan.steps) > 0 and traj_ok and intent_ok,
                tool_selection_ok=tool_ok,
                step_success_rate=1.0 if tool_ok and plan.steps else 0.0,
                memory_recall_hit=True if not memory_expected else None,
                trajectory_similarity=traj_sim,
                trajectory_diff=traj_diff,
                intent_deliverable_ok=eval_bundle["deliverable_ok"],
                intent_slots_ok=eval_bundle["slots_ok"],
                plan_validation_ok=eval_bundle["plan_validation_ok"],
                intent_confidence=eval_bundle["intent_confidence"],
                metadata={
                    "planned_steps": planned_steps,
                    "planned_agents": planned_agents,
                    "deliverable": intent.deliverable,
                    "expected_deliverable": task.get("expected_deliverable"),
                    "actual_trajectory": actual_traj,
                    "plan_issues": eval_bundle["plan_issues"],
                    "ambiguity_flags": intent.ambiguity_flags,
                    "slots": intent.slots.to_dict(),
                },
            )
        )
    return results


async def _seed_memory_for_task(harness, task: dict, session_id: str) -> str:
    if not task.get("expected_memory_recall"):
        return session_id
    await harness.memory.remember(
        MEMORY_SEED_FACTS,
        user_id=MEMORY_EVAL_USER,
        metadata={"seed": True, "topic": "robotics"},
    )
    return MEMORY_EVAL_USER


async def run_live_eval(tasks: list[dict], min_trajectory_similarity: float = 0.6) -> list[TaskEvalResult]:
    from app.agent.main_agent import harness
    from app.config.loader import get_harness_config
    from tests.eval.judge import judge_report

    eval_cfg = get_harness_config()
    results: list[TaskEvalResult] = []
    for task in tasks:
        session_id = f"eval_{task['id']}_{uuid.uuid4().hex[:8]}"
        session_id = await _seed_memory_for_task(harness, task, session_id)
        try:
            harness_result = await harness.run(task["query"], session_id)
            expected_agents = task.get("expected_agents", [])
            assistants = harness_result.metadata.get("assistants_called", [])
            tool_ok = evaluate_tool_selection(expected_agents, assistants)
            expected_artifacts = task.get("expected_artifacts", [])
            artifact_ok = evaluate_artifacts(
                expected_artifacts,
                harness_result.artifacts,
            )
            memory_expected = task.get("expected_memory_recall", False)
            memory_recalled = bool(harness_result.metadata.get("memory_recalled"))
            recall_at_k = harness_result.metadata.get("memory_recall_at_k")
            memory_ok = evaluate_memory_recall(
                memory_expected,
                memory_recalled,
                recall_at_k if isinstance(recall_at_k, (int, float)) else None,
            )
            recovery_used = harness_result.retry_count > 0
            actual_traj = extract_trajectory_live(
                harness_result.trace,
                assistants_called=assistants,
                replan_count=int(harness_result.metadata.get("replan_count", 0)),
            )
            traj_sim, traj_diff = _evaluate_trajectory(task, actual_traj, min_trajectory_similarity)
            traj_ok = True
            if traj_sim is not None:
                traj_ok = trajectory_passes(traj_diff, min_similarity=min_trajectory_similarity)

            ccr = harness_result.metadata.get("citation_coverage_rate")
            hr = harness_result.metadata.get("hallucination_rate")
            obs = harness_result.metadata.get("observability") or {}
            jcr = obs.get("structured_output_compliance_rate")
            ovr_count = int(obs.get("orchestration_violation_count") or 0)
            tokens_saved = int(obs.get("estimated_tokens_saved") or 0)

            report_judge_score = None
            report_judge_passed = None
            if harness_result.content and eval_cfg.eval_heuristic_judge_enabled:
                judge = await judge_report(
                    harness_result.content,
                    llm_judge_enabled=eval_cfg.eval_llm_judge_enabled,
                    min_score=eval_cfg.eval_heuristic_judge_min_score,
                    expect_citations=bool(expected_artifacts),
                )
                report_judge_score = judge.score
                report_judge_passed = judge.passed

            success = (
                harness_result.status == "success"
                and tool_ok
                and artifact_ok
                and memory_ok
                and traj_ok
            )
            if report_judge_passed is False and eval_cfg.eval_heuristic_judge_enabled:
                success = False
            variant = str(task.get("variant") or os.getenv("HARNESS_EVAL_VARIANT") or "full_harness")
            results.append(
                TaskEvalResult(
                    task_id=task["id"],
                    query=task["query"],
                    mode="live",
                    success=success,
                    tool_selection_ok=tool_ok,
                    recovery_used=recovery_used,
                    recovery_success=recovery_used and success,
                    artifacts=harness_result.artifacts,
                    assistants_called=assistants,
                    retry_count=harness_result.retry_count,
                    status=harness_result.status,
                    step_success_rate=float(
                        harness_result.metadata.get("step_success_rate", 0.0)
                    ),
                    tool_calls_count=int(
                        harness_result.metadata.get("tool_calls_count", 0)
                    ),
                    latency_ms=int(harness_result.metadata.get("latency_ms", 0)),
                    avg_compression_ratio=float(
                        harness_result.metadata.get("avg_compression_ratio", 1.0)
                    ),
                    memory_recall_hit=memory_recalled if memory_expected else None,
                    citation_coverage_rate=float(ccr) if ccr is not None else None,
                    hallucination_rate=float(hr) if hr is not None else None,
                    trajectory_similarity=traj_sim,
                    trajectory_diff=traj_diff,
                    structured_output_compliance=float(jcr) if jcr is not None else None,
                    orchestration_violation_count=ovr_count,
                    estimated_tokens_saved=tokens_saved,
                    report_judge_score=report_judge_score,
                    report_judge_passed=report_judge_passed,
                    session_id=session_id,
                    run_id=str(harness_result.metadata.get("run_id") or session_id),
                    trace_id=str(harness_result.metadata.get("trace_id") or ""),
                    variant=variant,
                    metadata={
                        **harness_result.metadata,
                        "actual_trajectory": actual_traj,
                        "case_id": task["id"],
                        "variant": variant,
                    },
                )
            )
            try:
                from app.observability import EventType, get_recorder

                recorder = get_recorder()
                recorder.emit(
                    EventType.EVAL_SCORED,
                    phase="eval",
                    status="pass" if success else "fail",
                    session_id=session_id,
                    run_id=str(harness_result.metadata.get("run_id") or session_id),
                    trace_id=str(harness_result.metadata.get("trace_id") or ""),
                    attributes={
                        "case_id": task["id"],
                        "benchmark": str(task.get("benchmark") or "harness_eval"),
                        "variant": variant,
                        "accuracy": 1.0 if success else 0.0,
                        "citation_score": float(ccr) if ccr is not None else None,
                        "tool_calls": int(harness_result.metadata.get("tool_calls_count", 0)),
                        "tokens": (harness_result.metadata.get("usage") or {}).get("total"),
                        "replan_count": int(harness_result.metadata.get("replan_count") or 0),
                        "latency_ms": int(harness_result.metadata.get("latency_ms", 0)),
                        "trace_id": harness_result.metadata.get("trace_id"),
                    },
                )
            except Exception:
                pass
        except Exception as exc:
            results.append(
                TaskEvalResult(
                    task_id=task["id"],
                    query=task["query"],
                    mode="live",
                    success=False,
                    memory_recall_hit=False if task.get("expected_memory_recall") else None,
                    error=str(exc),
                )
            )
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
    baseline = {
        "generated_at": report_dict.get("generated_at"),
        "mode": report_dict.get("mode"),
        "task_success_rate": report_dict.get("task_success_rate"),
        "tool_selection_accuracy": report_dict.get("tool_selection_accuracy"),
        "step_success_rate": report_dict.get("step_success_rate"),
        "recovery_rate": report_dict.get("recovery_rate"),
        "avg_tool_calls": report_dict.get("avg_tool_calls"),
        "avg_latency_ms": report_dict.get("avg_latency_ms"),
        "avg_compression_ratio": report_dict.get("avg_compression_ratio"),
        "memory_recall_hit_rate": report_dict.get("memory_recall_hit_rate"),
        "citation_coverage_rate": report_dict.get("citation_coverage_rate"),
        "hallucination_rate": report_dict.get("hallucination_rate"),
        "trajectory_similarity": report_dict.get("trajectory_similarity"),
        "structured_output_compliance_rate": report_dict.get("structured_output_compliance_rate"),
        "report_judge_pass_rate": report_dict.get("report_judge_pass_rate"),
        "avg_tokens_saved": report_dict.get("avg_tokens_saved"),
        "total": report_dict.get("total"),
        "passed": report_dict.get("passed"),
    }
    path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")


def write_comparison_markdown(
    report_dict: dict,
    comparison: dict | None,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = output_dir / f"eval_comparison_{ts}.md"
    lines = [
        f"# Harness Eval 对比报告",
        "",
        f"- 生成时间: {report_dict.get('generated_at')}",
        f"- 模式: {report_dict.get('mode')}",
        f"- 任务: {report_dict.get('passed')}/{report_dict.get('total')} 通过",
        "",
        "## 指标总览",
        "",
        "| 指标 | 当前 | 基线 | Delta |",
        "|------|------|------|-------|",
    ]
    metric_labels = {
        "task_success_rate": "TSR",
        "tool_selection_accuracy": "TSA",
        "step_success_rate": "SSR",
        "recovery_rate": "RR",
        "avg_tool_calls": "ATC",
        "avg_latency_ms": "AL(ms)",
        "avg_compression_ratio": "CR",
        "memory_recall_hit_rate": "MRH",
        "citation_coverage_rate": "CCR",
        "hallucination_rate": "HR",
        "trajectory_similarity": "TDS",
        "structured_output_compliance_rate": "JCR",
        "report_judge_pass_rate": "RJP",
        "avg_tokens_saved": "TS(saved)",
    }
    baseline = comparison or {}
    baseline_at = baseline.get("baseline_generated_at", "N/A")
    deltas = baseline.get("deltas", {})
    lines.append(f"> 基线时间: {baseline_at} ({baseline.get('baseline_mode', 'N/A')})")
    lines.append("")

    for key, label in metric_labels.items():
        cur = report_dict.get(key)
        base_val = None
        if comparison and "deltas" in comparison:
            delta = deltas.get(key)
            if delta is not None and cur is not None:
                base_val = round(float(cur) - float(delta), 3)
        cur_text = f"{cur:.1%}" if key.endswith("_rate") and isinstance(cur, (int, float)) else str(cur)
        base_text = (
            f"{base_val:.1%}"
            if base_val is not None and key.endswith("_rate")
            else (str(base_val) if base_val is not None else "-")
        )
        delta = deltas.get(key) if comparison else None
        delta_text = (
            f"{delta:+.1%}" if delta is not None and key.endswith("_rate") else (
                f"{delta:+.3f}" if delta is not None else "-"
            )
        )
        lines.append(f"| {label} | {cur_text} | {base_text} | {delta_text} |")

    if comparison and comparison.get("regressions"):
        lines.extend(["", "## 回归告警", ""])
        for item in comparison["regressions"]:
            lines.append(f"- {item}")

    failed = [item for item in report_dict.get("results", []) if not item.get("success")]
    lines.extend(["", "## 失败任务", ""])
    if failed:
        for item in failed:
            lines.append(f"- **{item['task_id']}**: status={item.get('status')} retry={item.get('retry_count')}")
    else:
        lines.append("- 无")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def print_summary(report_dict: dict, comparison: dict | None = None) -> None:
    print("=== Harness Eval Report ===")
    print(f"Tasks: {report_dict['total']} | Passed: {report_dict['passed']}")
    print(
        f"TSR: {report_dict['task_success_rate']:.1%} | "
        f"TSA: {report_dict['tool_selection_accuracy']:.1%} | "
        f"SSR: {report_dict['step_success_rate']:.1%} | "
        f"RR: {report_dict['recovery_rate']:.1%}"
    )
    print(
        f"ATC: {report_dict['avg_tool_calls']:.1f} | "
        f"AL: {report_dict['avg_latency_ms'] / 1000:.1f}s | "
        f"CR: {report_dict['avg_compression_ratio']:.1%}"
    )
    if report_dict.get("memory_recall_hit_rate") is not None:
        print(f"MRH: {report_dict['memory_recall_hit_rate']:.1%}")
    if report_dict.get("citation_coverage_rate") is not None:
        print(f"CCR: {report_dict['citation_coverage_rate']:.1%}")
    if report_dict.get("hallucination_rate") is not None:
        print(f"HR: {report_dict['hallucination_rate']:.1%}")
    if report_dict.get("trajectory_similarity") is not None:
        print(f"TDS: {report_dict['trajectory_similarity']:.1%}")
    if report_dict.get("structured_output_compliance_rate") is not None:
        print(f"JCR: {report_dict['structured_output_compliance_rate']:.1%}")
    if report_dict.get("report_judge_pass_rate") is not None:
        print(f"RJP: {report_dict['report_judge_pass_rate']:.1%}")
    if report_dict.get("intent_deliverable_accuracy") is not None:
        print(f"IDA: {report_dict['intent_deliverable_accuracy']:.1%}")
    if report_dict.get("plan_validation_pass_rate") is not None:
        print(f"PVR: {report_dict['plan_validation_pass_rate']:.1%}")
    if report_dict.get("avg_intent_confidence") is not None:
        print(f"AIC: {report_dict['avg_intent_confidence']:.2f}")

    if comparison:
        print("\n--- Baseline Comparison ---")
        print(f"Baseline: {comparison.get('baseline_generated_at')} ({comparison.get('baseline_mode')})")
        for key, delta in comparison.get("deltas", {}).items():
            if delta is None:
                continue
            sign = "+" if delta >= 0 else ""
            print(f"  {key}: {sign}{delta}")
        if comparison.get("regressions"):
            print(f"  REGRESSION: {', '.join(comparison['regressions'])}")
        if comparison.get("blocked_merge"):
            print("  >>> Merge blocked: TSR regression > 5%")

    failed = [item for item in report_dict["results"] if not item["success"]]
    if failed:
        print(f"\nFailed: {', '.join(item['task_id'] for item in failed)}")

    for item in report_dict["results"]:
        mark = "PASS" if item["success"] else "FAIL"
        extra = ""
        if item.get("memory_recall_hit") is not None:
            extra = f" memory_hit={item['memory_recall_hit']}"
        print(
            f"  [{mark}] {item['task_id']} retry={item['retry_count']} "
            f"status={item['status']}{extra}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run harness evaluation")
    parser.add_argument(
        "--tasks",
        default=str(ROOT / "tests" / "eval" / "tasks.jsonl"),
        help="Path to tasks.jsonl",
    )
    parser.add_argument("--dry-run", action="store_true", help="Planner-only eval")
    parser.add_argument("--live", action="store_true", help="Full harness eval")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of tasks")
    parser.add_argument(
        "--output",
        default=str(ROOT / "tests" / "eval" / "results"),
        help="Output directory for report json",
    )
    parser.add_argument(
        "--baseline",
        default=str(BASELINE_PATH),
        help="Baseline json for regression comparison",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Save current report metrics as baseline",
    )
    parser.add_argument(
        "--report-md",
        action="store_true",
        help="Write markdown comparison report",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit with code 1 when TSR regression exceeds 5%%",
    )
    args = parser.parse_args()

    tasks = load_tasks(Path(args.tasks))
    if args.limit > 0:
        tasks = tasks[: args.limit]

    if args.live:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        results = asyncio.run(
            run_live_eval(tasks, min_trajectory_similarity=cfg.eval_trajectory_min_similarity)
        )
    else:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        results = run_dry_eval(tasks, min_trajectory_similarity=cfg.eval_trajectory_min_similarity)

    report = build_report(results)
    report_dict = report.to_dict()
    report_dict["mode"] = "live" if args.live else "dry-run"
    report_dict["generated_at"] = datetime.now().isoformat()

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
