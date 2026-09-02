"""ReportStructureGrader：只评报告结构，不评答案对错。"""

from __future__ import annotations

from tests.eval.judge import (
    ReportJudgeResult,
    heuristic_report_judge,
    judge_report,
)

ReportStructureGrader = heuristic_report_judge

__all__ = [
    "ReportJudgeResult",
    "ReportStructureGrader",
    "heuristic_report_judge",
    "judge_report",
]
