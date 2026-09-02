"""Judge calibration: human gold labels vs automatic grader.

LLM judges are an approximation of expert labels, not a replacement.
This module always compares against `human.pass_label` in the calibration set.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tests.eval.graders.llm_judge import (
    QualityJudgeResult,
    grade_against_reference,
    judge_answer_quality,
    meta_eval_agreement,
    pass_label_from_scores,
)
from tests.eval.runners.component import DATASETS, load_jsonl

CALIBRATION_PATH = DATASETS / "judge_calibration_v1.jsonl"


def _human_label(case: dict[str, Any]) -> str:
    human = dict(case.get("human") or {})
    label = str(human.get("pass_label") or "").strip().lower()
    if label in {"pass", "fail"}:
        return label
    return pass_label_from_scores(
        correctness=human.get("correctness"),
        critical_error=bool(human.get("critical_error")),
    ) or "fail"


def _judge_label(result: QualityJudgeResult) -> str:
    return result.pass_label or pass_label_from_scores(
        correctness=result.correctness,
        critical_error=result.critical_error,
    ) or "fail"


def _mae(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    return sum(abs(left - right) for left, right in pairs) / len(pairs)


async def calibrate_judge(
    cases: list[dict[str, Any]] | None = None,
    *,
    use_llm: bool = False,
) -> dict[str, Any]:
    rows = list(cases or load_jsonl(CALIBRATION_PATH))
    human_labels: list[str] = []
    judge_labels: list[str] = []
    correctness_pairs: list[tuple[float, float]] = []
    details: list[dict[str, Any]] = []

    for case in rows:
        human = dict(case.get("human") or {})
        if use_llm:
            judged = await judge_answer_quality(
                question=str(case.get("question") or ""),
                answer=str(case.get("answer") or ""),
                evidence=str(case.get("evidence") or ""),
                reference=str(case.get("reference") or ""),
                must_include=list(case.get("must_include") or []),
                enabled=True,
            )
        else:
            judged = grade_against_reference(
                question=str(case.get("question") or ""),
                answer=str(case.get("answer") or ""),
                evidence=str(case.get("evidence") or ""),
                reference=str(case.get("reference") or ""),
                must_include=list(case.get("must_include") or []),
            )
        h_label = _human_label(case)
        j_label = _judge_label(judged)
        human_labels.append(h_label)
        judge_labels.append(j_label)
        if isinstance(human.get("correctness"), (int, float)) and judged.correctness is not None:
            correctness_pairs.append((float(human["correctness"]), float(judged.correctness)))
        details.append(
            {
                "case_id": case.get("case_id"),
                "human": h_label,
                "judge": j_label,
                "agree": h_label == j_label,
                "judge_source": judged.judge_source,
                "correctness": judged.correctness,
            }
        )

    agreement = meta_eval_agreement(human_labels, judge_labels)
    return {
        "n": len(rows),
        "mode": "llm" if use_llm else "reference_grader",
        "agreement": agreement["agreement"],
        "precision": agreement["precision"],
        "recall": agreement["recall"],
        "kappa": agreement["kappa"],
        "correctness_mae": round(_mae(correctness_pairs), 3),
        "note": "Human labels are the gold standard. Automatic scores approximate them.",
        "details": details,
    }


def calibrate_judge_sync(
    cases: list[dict[str, Any]] | None = None,
    *,
    use_llm: bool = False,
) -> dict[str, Any]:
    return asyncio.run(calibrate_judge(cases, use_llm=use_llm))
