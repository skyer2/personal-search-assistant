"""Evidence-driven marginal gain stop policy.

预算不再只看"还剩几个 tool call"，而是看最近几波研究的边际收益：
新增证据 / 新增来源 / 新增事实 / 重复率。连续零增益时即使预算未耗尽
也应停止研究并进入合成（quality-cost trade-off，而非无限多越好）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class WaveGain:
    """单波研究相对已见集合的增量。"""

    wave: int
    new_evidence: int = 0
    new_sources: int = 0
    new_facts: int = 0
    total_rows: int = 0
    ok_rows: int = 0

    @property
    def total_gain(self) -> int:
        return self.new_evidence + self.new_sources + self.new_facts

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave": self.wave,
            "new_evidence": self.new_evidence,
            "new_sources": self.new_sources,
            "new_facts": self.new_facts,
            "total_gain": self.total_gain,
            "total_rows": self.total_rows,
            "ok_rows": self.ok_rows,
        }


@dataclass
class MarginalGainState:
    """跨波累积的已见指纹 + 波次增益历史（可序列化进 graph state）。"""

    seen_task_ids: set[str] = field(default_factory=set)
    seen_evidence: set[str] = field(default_factory=set)
    seen_sources: set[str] = field(default_factory=set)
    seen_facts: set[str] = field(default_factory=set)
    wave_gains: list[WaveGain] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seen_task_ids": sorted(self.seen_task_ids),
            "seen_evidence": sorted(self.seen_evidence),
            "seen_sources": sorted(self.seen_sources),
            "seen_facts": sorted(self.seen_facts),
            "wave_gains": [g.to_dict() for g in self.wave_gains],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "MarginalGainState":
        raw = raw or {}
        gains_raw = raw.get("wave_gains") or []
        gains = [
            WaveGain(
                wave=int(g.get("wave") or 0),
                new_evidence=int(g.get("new_evidence") or 0),
                new_sources=int(g.get("new_sources") or 0),
                new_facts=int(g.get("new_facts") or 0),
                total_rows=int(g.get("total_rows") or 0),
                ok_rows=int(g.get("ok_rows") or 0),
            )
            for g in gains_raw
            if isinstance(g, dict)
        ]
        return cls(
            seen_task_ids={str(x) for x in raw.get("seen_task_ids") or [] if x},
            seen_evidence={str(x) for x in raw.get("seen_evidence") or [] if x},
            seen_sources={str(x) for x in raw.get("seen_sources") or [] if x},
            seen_facts={str(x) for x in raw.get("seen_facts") or [] if x},
            wave_gains=gains,
        )


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def record_wave_gain(
    state: MarginalGainState,
    worker_rows: Iterable[dict[str, Any]],
) -> WaveGain:
    """把一波 worker 结果并入已见集合，返回该波的增量。"""
    new_evidence = 0
    new_sources = 0
    new_facts = 0
    total_rows = 0
    ok_rows = 0
    for row in worker_rows or []:
        if not isinstance(row, dict):
            continue
        task_id = _norm(row.get("task_id"))
        if task_id and task_id in state.seen_task_ids:
            continue
        if task_id:
            state.seen_task_ids.add(task_id)
        total_rows += 1
        if not row.get("ok"):
            continue
        ok_rows += 1
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        for evidence_id in payload.get("evidence_ids") or []:
            key = _norm(evidence_id)
            if key and key not in state.seen_evidence:
                state.seen_evidence.add(key)
                new_evidence += 1
        for source in payload.get("sources") or []:
            key = _norm(source)
            if key and key not in state.seen_sources:
                state.seen_sources.add(key)
                new_sources += 1
        for fact in payload.get("facts") or []:
            key = _norm(fact)
            if key and key not in state.seen_facts:
                state.seen_facts.add(key)
                new_facts += 1
    wave = len(state.wave_gains) + 1
    gain = WaveGain(
        wave=wave,
        new_evidence=new_evidence,
        new_sources=new_sources,
        new_facts=new_facts,
        total_rows=total_rows,
        ok_rows=ok_rows,
    )
    state.wave_gains.append(gain)
    return gain


@dataclass(frozen=True)
class MarginalGainDecision:
    stop: bool
    reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"stop": self.stop, "reason": self.reason, **self.metrics}


def evaluate_marginal_gain(
    state: MarginalGainState,
    *,
    window: int = 2,
    min_waves: int = 2,
) -> MarginalGainDecision:
    """最近 window 波增益全为零 → 停止研究（marginal_gain_low）。

    判据（文档第十节）：
    - new evidence / new sources / new facts 全部为 0；
    - 且已经至少跑了 min_waves 波（避免首波空结果误杀，首波空由
      progress evaluator 的 coverage gap 语义处理）。
    """
    gains = state.wave_gains
    if len(gains) < max(2, min_waves):
        return MarginalGainDecision(stop=False, metrics={"waves": len(gains)})
    recent = gains[-window:]
    total_gain = sum(g.total_gain for g in recent)
    ok_rows = sum(g.ok_rows for g in recent)
    metrics = {
        "waves": len(gains),
        "window": window,
        "window_gain": total_gain,
        "window_ok_rows": ok_rows,
        "recent_gains": [g.to_dict() for g in recent],
    }
    if ok_rows > 0 and total_gain == 0:
        return MarginalGainDecision(
            stop=True,
            reason="marginal_gain_low",
            metrics=metrics,
        )
    return MarginalGainDecision(stop=False, metrics=metrics)


__all__ = [
    "MarginalGainDecision",
    "MarginalGainState",
    "WaveGain",
    "evaluate_marginal_gain",
    "record_wave_gain",
]
