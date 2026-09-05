"""TaskShape Router：按任务形态选择执行拓扑。

Multi-Agent 不是什么题都更好（Anthropic 实践）：
- 简单事实 → Direct / 单 Worker，30s 级；
- 单主题深挖 → 单 Worker 迭代检索；
- 广度型（多独立方向）→ Lead + 并行 Worker；
- 混合/冲突型 → Lead + Worker + Progress + Replan 全链路。

Router 只决定执行拓扑（并行度 / replan 预算），不改变 hard ceiling。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskShape(str, Enum):
    SIMPLE_FACT = "simple_fact"
    SINGLE_TOPIC_DEEP_DIVE = "single_topic_deep_dive"
    BREADTH_HEAVY = "breadth_heavy"
    HYBRID_CONFLICT = "hybrid_conflict"


_FACT_PATTERN = ("谁是", "是谁", "什么是", "什么时候", "哪一年", "多少", "是什么", "who is", "what is", "when did")
_COMPARE = ("比较", "对比", " vs ", " VS ", "versus", "横向对比", "各自")
_LANDSCAPE = ("竞争格局", "多维度", "综合对比", "全面", "全景", "有哪些", "分别")
_CONFLICT = ("冲突", "矛盾", "争议", "分歧", "不一致", "说法不一", "contradict", "dispute")
_DEEP = ("深入", "深挖", "原理", "机制", "端到端", "源码", "为什么", "why", "how does")


@dataclass(frozen=True)
class TaskShapeDecision:
    shape: TaskShape
    confidence: float
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape.value,
            "confidence": self.confidence,
            "signals": list(self.signals),
        }


def classify_task_shape(
    query: str,
    brief: dict[str, Any] | None = None,
) -> TaskShapeDecision:
    """规则型 TaskShape 分类（可后续被 eval 校准的 telemetry 替换权重）。"""
    q = str(query or "").strip()
    ql = q.lower()
    brief = brief or {}
    entities = [str(x) for x in brief.get("entities") or [] if str(x).strip()]
    dimensions = [str(x) for x in brief.get("dimensions") or [] if str(x).strip()]
    depth = str(brief.get("depth") or "").lower()
    signals: list[str] = []

    if any(token in q or token in ql for token in _CONFLICT):
        signals.append("conflict_marker")
        return TaskShapeDecision(TaskShape.HYBRID_CONFLICT, 0.9, signals)

    breadth = bool(
        any(token in q or token in ql for token in _COMPARE + _LANDSCAPE)
        or len(entities) >= 3
        or len(dimensions) >= 3
    )
    if breadth:
        signals.append("breadth_marker" if any(t in q or t in ql for t in _COMPARE + _LANDSCAPE) else "multi_entity_or_dimension")
        return TaskShapeDecision(TaskShape.BREADTH_HEAVY, 0.85, signals)

    deep = any(token in q or token in ql for token in _DEEP) or depth in {"deep", "thorough"}
    if deep:
        signals.append("depth_marker")
        return TaskShapeDecision(TaskShape.SINGLE_TOPIC_DEEP_DIVE, 0.8, signals)

    fact = any(token in q or token in ql for token in _FACT_PATTERN)
    if fact and len(q) <= 40 and len(entities) <= 1:
        signals.append("fact_pattern")
        return TaskShapeDecision(TaskShape.SIMPLE_FACT, 0.9, signals)

    return TaskShapeDecision(TaskShape.SINGLE_TOPIC_DEEP_DIVE, 0.6, ["default_deep_dive"])


def execution_profile_for_shape(shape: TaskShape) -> dict[str, int | bool]:
    """形态 → 执行拓扑（并行度 / replan 预算；仍受 hard ceiling clamp）。"""
    if shape == TaskShape.SIMPLE_FACT:
        return {
            "parallel_workers": 1,
            "max_replan_count": 0,
            "direct_candidate": True,
        }
    if shape == TaskShape.SINGLE_TOPIC_DEEP_DIVE:
        return {
            "parallel_workers": 1,
            "max_replan_count": 1,
            "direct_candidate": False,
        }
    if shape == TaskShape.BREADTH_HEAVY:
        return {
            "parallel_workers": 3,
            "max_replan_count": 1,
            "direct_candidate": False,
        }
    return {
        "parallel_workers": 3,
        "max_replan_count": 2,
        "direct_candidate": False,
    }


__all__ = [
    "TaskShape",
    "TaskShapeDecision",
    "classify_task_shape",
    "execution_profile_for_shape",
]
