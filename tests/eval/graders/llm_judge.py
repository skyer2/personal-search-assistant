"""Answer / Evidence / Completeness LLM Judge。

默认关闭。启发式结构分不得冒充本模块。Judge 本身需要 human meta-eval。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


JUDGE_SCHEMA = {
    "correctness": 0.0,
    "completeness": 0.0,
    "grounding": 0.0,
    "unsupported_claims": [],
    "critical_error": False,
}


@dataclass
class QualityJudgeResult:
    correctness: float | None = None
    completeness: float | None = None
    grounding: float | None = None
    unsupported_claims: list[str] = field(default_factory=list)
    critical_error: bool = False
    judge_source: str = "disabled"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "correctness": self.correctness,
            "completeness": self.completeness,
            "grounding": self.grounding,
            "unsupported_claims": list(self.unsupported_claims),
            "critical_error": self.critical_error,
            "judge_source": self.judge_source,
        }


def parse_judge_json(raw: str | dict[str, Any]) -> QualityJudgeResult:
    data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    return QualityJudgeResult(
        correctness=_clamp(data.get("correctness")),
        completeness=_clamp(data.get("completeness")),
        grounding=_clamp(data.get("grounding")),
        unsupported_claims=[str(x) for x in (data.get("unsupported_claims") or [])],
        critical_error=bool(data.get("critical_error")),
        judge_source="llm",
        raw=data,
    )


def _clamp(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


async def judge_answer_quality(
    *,
    question: str,
    answer: str,
    evidence: str = "",
    brief: str = "",
    reference: str = "",
    enabled: bool = False,
) -> QualityJudgeResult:
    """真正的质量 Judge。未启用时返回 disabled，避免用结构分冒充正确性。"""
    _ = (question, answer, evidence, brief, reference)
    if not enabled:
        return QualityJudgeResult(judge_source="disabled")
    return QualityJudgeResult(judge_source="llm_stub")


def meta_eval_agreement(human: list[str], judge: list[str]) -> dict[str, float]:
    """Human label vs Judge label 的粗校准。"""
    if not human:
        return {"agreement": 0.0, "precision": 0.0, "recall": 0.0}
    agreed = sum(1 for h, j in zip(human, judge) if h == j)
    tp = sum(1 for h, j in zip(human, judge) if h == "pass" and j == "pass")
    fp = sum(1 for h, j in zip(human, judge) if h != "pass" and j == "pass")
    fn = sum(1 for h, j in zip(human, judge) if h == "pass" and j != "pass")
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "agreement": round(agreed / len(human), 3),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
    }
