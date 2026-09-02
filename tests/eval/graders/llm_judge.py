"""Answer / Evidence / Completeness quality judge.

Deterministic checks stay in graders/outcome.py and graders/evidence.py.
This module only scores claims that need a rubric: correctness, completeness, grounding.

Judge sources:
  disabled          eval_llm_judge_enabled is off
  llm               live model JSON rubric
  reference_grader  lexical overlap vs reference / must_include (surrogate, not official Accuracy)
  unavailable       enabled but no model and no reference
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

JUDGE_SCHEMA = {
    "correctness": 0.0,
    "completeness": 0.0,
    "grounding": 0.0,
    "unsupported_claims": [],
    "critical_error": False,
}

PASS_THRESHOLD = 0.6
_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

JUDGE_PROMPT = """You grade a research-agent answer. Return JSON only, no markdown:
{{
  "correctness": 0.0,
  "completeness": 0.0,
  "grounding": 0.0,
  "unsupported_claims": ["..."],
  "critical_error": false,
  "rationale": "one sentence"
}}

Scoring rules:
- correctness: 1 if the answer matches the reference / known facts; 0 if the main conclusion is wrong.
- completeness: 1 if required dimensions of the question are covered.
- grounding: 1 if numeric or causal claims are supported by Evidence; list unsupported claims.
- critical_error: true when the main conclusion is factually wrong given the reference.
- Do not reward headings, citation style, or length as correctness.

Question:
{question}

Reference (may be empty):
{reference}

Evidence:
{evidence}

Brief:
{brief}

Answer:
{answer}
"""


@dataclass
class QualityJudgeResult:
    correctness: float | None = None
    completeness: float | None = None
    grounding: float | None = None
    unsupported_claims: list[str] = field(default_factory=list)
    critical_error: bool = False
    judge_source: str = "disabled"
    pass_label: str | None = None
    rationale: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "correctness": self.correctness,
            "completeness": self.completeness,
            "grounding": self.grounding,
            "unsupported_claims": list(self.unsupported_claims),
            "critical_error": self.critical_error,
            "judge_source": self.judge_source,
            "pass_label": self.pass_label,
            "rationale": self.rationale,
        }


def pass_label_from_scores(
    *,
    correctness: float | None,
    critical_error: bool,
    threshold: float = PASS_THRESHOLD,
) -> str | None:
    if correctness is None:
        return None
    if critical_error or correctness < threshold:
        return "fail"
    return "pass"


def parse_judge_json(raw: str | dict[str, Any]) -> QualityJudgeResult:
    data = _coerce_json(raw)
    result = QualityJudgeResult(
        correctness=_clamp(data.get("correctness")),
        completeness=_clamp(data.get("completeness")),
        grounding=_clamp(data.get("grounding")),
        unsupported_claims=[str(item) for item in (data.get("unsupported_claims") or [])],
        critical_error=bool(data.get("critical_error")),
        judge_source="llm",
        rationale=str(data.get("rationale") or ""),
        raw=data,
    )
    result.pass_label = pass_label_from_scores(
        correctness=result.correctness,
        critical_error=result.critical_error,
    )
    return result


def _coerce_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    text = (raw or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {}
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _clamp(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


def _units(text: str) -> set[str]:
    tokens = {token.lower() for token in _TOKEN.findall(text or "") if len(token) > 1}
    compact = re.sub(r"\s+", "", text or "").lower()
    grams = {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}
    return tokens | grams


def grade_against_reference(
    *,
    question: str,
    answer: str,
    evidence: str = "",
    reference: str = "",
    must_include: list[str] | None = None,
) -> QualityJudgeResult:
    """Lexical surrogate. Never call this official Answer Accuracy."""
    required = [item for item in (must_include or []) if str(item).strip()]
    answer_l = (answer or "").lower()
    missing = [item for item in required if item.lower() not in answer_l]

    ref_units = _units(reference)
    ans_units = _units(answer)
    overlap = (len(ref_units & ans_units) / len(ref_units)) if ref_units else None

    if required:
        completeness = (len(required) - len(missing)) / len(required)
        correctness = completeness if overlap is None else min(1.0, 0.65 * completeness + 0.35 * overlap)
    elif overlap is not None:
        completeness = min(1.0, overlap)
        correctness = overlap
    else:
        completeness = 1.0 if len((answer or "").strip()) >= 80 else 0.3
        correctness = completeness

    source_text = f"{evidence}\n{reference}"
    answer_numbers = _NUMBER.findall(answer or "")
    source_numbers = set(_NUMBER.findall(source_text))
    unsupported = [num for num in answer_numbers if num not in source_numbers]
    if answer_numbers:
        grounding = 1.0 - min(1.0, len(unsupported) / len(answer_numbers))
    elif (evidence or reference).strip():
        grounding = 0.8
    else:
        grounding = 0.4

    if required:
        critical = len(missing) == len(required)
    elif ref_units:
        critical = (overlap or 0.0) < 0.12
    else:
        critical = False

    result = QualityJudgeResult(
        correctness=_clamp(correctness),
        completeness=_clamp(completeness),
        grounding=_clamp(grounding),
        unsupported_claims=[f"missing:{item}" for item in missing] + [f"unsupported_number:{num}" for num in unsupported[:8]],
        critical_error=critical,
        judge_source="reference_grader",
        rationale="lexical overlap against reference/must_include; not official accuracy",
        raw={"missing": missing, "overlap": overlap},
    )
    result.pass_label = pass_label_from_scores(
        correctness=result.correctness,
        critical_error=result.critical_error,
    )
    _ = question
    return result


async def _default_complete(prompt: str) -> str:
    from app.agent.llm import compression_model, model

    judge_model = compression_model or model
    if judge_model is None:
        raise RuntimeError("no judge model configured")
    from app.agent.harness.usage_tracker import tracked_ainvoke

    response = await tracked_ainvoke(
        judge_model,
        prompt,
        phase="eval_judge",
    )
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


async def judge_answer_quality(
    *,
    question: str,
    answer: str,
    evidence: str = "",
    brief: str = "",
    reference: str = "",
    must_include: list[str] | None = None,
    enabled: bool = False,
    complete: Callable[[str], Awaitable[str]] | None = None,
) -> QualityJudgeResult:
    """Score answer quality. Disabled by default so structure scores cannot masquerade as correctness."""
    if not enabled:
        return QualityJudgeResult(judge_source="disabled")

    env_force_reference = os.getenv("HARNESS_EVAL_JUDGE_MODE", "").strip().lower() == "reference"
    if env_force_reference:
        return grade_against_reference(
            question=question,
            answer=answer,
            evidence=evidence,
            reference=reference,
            must_include=must_include,
        )

    try:
        completer = complete or _default_complete
        prompt = JUDGE_PROMPT.format(
            question=question or "",
            reference=reference or "(none)",
            evidence=evidence or "(none)",
            brief=brief or "(none)",
            answer=answer or "",
        )
        raw = await completer(prompt)
        parsed = parse_judge_json(raw)
        if parsed.correctness is None and parsed.completeness is None and parsed.grounding is None:
            raise ValueError("empty judge json")
        parsed.judge_source = "llm"
        return parsed
    except Exception:
        if reference or must_include:
            return grade_against_reference(
                question=question,
                answer=answer,
                evidence=evidence,
                reference=reference,
                must_include=must_include,
            )
        return QualityJudgeResult(
            judge_source="unavailable",
            rationale="judge model failed and no reference provided",
        )


def meta_eval_agreement(human: list[str], judge: list[str]) -> dict[str, float]:
    """Human label vs Judge label 的粗校准。"""
    if not human:
        return {"agreement": 0.0, "precision": 0.0, "recall": 0.0, "kappa": 0.0}
    paired = list(zip(human, judge))
    agreed = sum(1 for h, j in paired if h == j)
    tp = sum(1 for h, j in paired if h == "pass" and j == "pass")
    fp = sum(1 for h, j in paired if h != "pass" and j == "pass")
    fn = sum(1 for h, j in paired if h == "pass" and j != "pass")
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    n = len(human)
    p_o = agreed / n
    labels = set(human) | set(judge)
    p_e = sum((human.count(label) / n) * (judge.count(label) / n) for label in labels)
    kappa = 1.0 if p_e == 1 else (p_o - p_e) / (1 - p_e)
    return {
        "agreement": round(p_o, 3),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "kappa": round(kappa, 3),
    }
