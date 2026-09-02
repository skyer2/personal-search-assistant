"""ReportStructureGrader：只评报告结构（标题/引用标记/参考文献），不评答案对错。

真正的 Answer/Evidence Judge 见 tests/eval/graders/llm_judge.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CITATION_PATTERN = re.compile(r"\[\d+\]")
HEADING_PATTERN = re.compile(r"^#{1,3}\s+\S+", re.MULTILINE)


@dataclass
class ReportJudgeResult:
    score: float
    passed: bool
    reasons: list[str]
    judge_source: str = "heuristic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "passed": self.passed,
            "reasons": self.reasons,
            "judge_source": self.judge_source,
        }


def heuristic_report_judge(
    content: str,
    *,
    min_score: float = 0.6,
    expect_citations: bool = True,
    min_length: int = 200,
) -> ReportJudgeResult:
    """
    启发式报告评分（0~1）：
    - 长度、标题结构、引用标记、参考文献块
    """
    reasons: list[str] = []
    score = 0.0
    text = (content or "").strip()

    if len(text) < min_length:
        reasons.append("content_too_short")
    else:
        score += 0.25
        reasons.append("length_ok")

    if HEADING_PATTERN.search(text):
        score += 0.2
        reasons.append("has_headings")
    else:
        reasons.append("missing_headings")

    if CITATION_PATTERN.search(text):
        score += 0.25
        reasons.append("has_inline_citations")
    elif expect_citations:
        reasons.append("missing_citations")
    else:
        score += 0.15

    if "参考文献" in text or "## References" in text:
        score += 0.15
        reasons.append("has_references_section")
    else:
        reasons.append("missing_references_section")

    if "http" in text or "source:" in text.lower():
        score += 0.15
        reasons.append("has_source_urls")
    else:
        reasons.append("missing_source_urls")

    score = min(1.0, score)
    return ReportJudgeResult(
        score=score,
        passed=score >= min_score,
        reasons=reasons,
        judge_source="heuristic",
    )


async def judge_report(
    content: str,
    *,
    llm_judge_enabled: bool = False,
    min_score: float = 0.6,
    expect_citations: bool = True,
) -> ReportJudgeResult:
    """统一 Judge 入口：默认 heuristic；LLM 占位待接 compression_model。"""
    if not llm_judge_enabled:
        return heuristic_report_judge(
            content,
            min_score=min_score,
            expect_citations=expect_citations,
        )
    # LLM judge 占位：生产可接独立 judge 模型 + rubric prompt
    base = heuristic_report_judge(content, min_score=min_score, expect_citations=expect_citations)
    base.judge_source = "heuristic+llm_stub"
    return base
