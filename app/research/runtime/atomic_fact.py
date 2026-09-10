"""Structured answer extraction for the atomic-fact fast path."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.research.evidence.policy import SIMPLE_FACT_EVIDENCE_POLICY
from app.research.execution.structured_llm_gateway import StructuredLLMGateway


AnswerType = Literal[
    "person",
    "date",
    "number",
    "organization",
    "location",
    "definition",
    "other",
]


@dataclass(frozen=True)
class AtomicFactAnswer:
    answer: str
    answer_type: AnswerType
    supporting_source_ids: tuple[str, ...]
    confidence: float
    sufficient: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "answer_type": self.answer_type,
            "supporting_source_ids": list(self.supporting_source_ids),
            "confidence": self.confidence,
            "sufficient": self.sufficient,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AtomicFactAnswer":
        row = data or {}
        answer_type = str(row.get("answer_type") or "other")
        if answer_type not in {
            "person",
            "date",
            "number",
            "organization",
            "location",
            "definition",
            "other",
        }:
            answer_type = "other"
        return cls(
            answer=str(row.get("answer") or ""),
            answer_type=answer_type,  # type: ignore[arg-type]
            supporting_source_ids=tuple(
                str(item) for item in row.get("supporting_source_ids") or [] if str(item).strip()
            ),
            confidence=max(0.0, min(1.0, float(row.get("confidence") or 0.0))),
            sufficient=bool(row.get("sufficient")),
            reason=str(row.get("reason") or ""),
        )


def _unconfirmed(reason: str) -> AtomicFactAnswer:
    return AtomicFactAnswer(
        answer="",
        answer_type="other",
        supporting_source_ids=(),
        confidence=0.0,
        sufficient=False,
        reason=reason,
    )


def _evidence_pack(sources: list[Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source in sources:
        source_id = str(getattr(source, "source_id", "") or "")
        if not source_id:
            continue
        rows.append(
            {
                "source_id": source_id,
                "source_tier": str(getattr(source, "source_tier", "") or ""),
                "locator": str(getattr(source, "locator", "") or ""),
                "evidence": str(
                    getattr(source, "bound_fact", "")
                    or getattr(source, "excerpt", "")
                    or ""
                )[:1200],
            }
        )
    return rows


def _prompt(query: str, evidence_pack: list[dict[str, str]]) -> str:
    return "\n".join(
        [
            "你是原子事实抽取器。只根据 Evidence Pack 回答，不使用外部知识，不调用工具。",
            "输出必须包含：answer、answer_type、supporting_source_ids、confidence、reason。",
            "answer 必须是直接答案（人名/日期/数字/组织/地点/定义），不是来源摘要或背景介绍。",
            "supporting_source_ids 只能引用 Evidence Pack 中存在的 source_id，且必须直接支撑 answer。",
            "如果证据不足或无法明确回答，answer 留空，并在 reason 说明。",
            "",
            f"Question: {query}",
            f"Evidence Pack: {json.dumps(evidence_pack, ensure_ascii=False)}",
        ]
    )


async def extract_atomic_fact_answer(
    *,
    query: str,
    sources: list[Any],
    model: Any,
    budget_manager: Any | None,
    timeout_sec: float = 10,
) -> AtomicFactAnswer:
    evidence_pack = _evidence_pack(sources)
    if model is None or not evidence_pack:
        return _unconfirmed("atomic_fact_extractor_unavailable")

    try:
        extracted = await asyncio.wait_for(
            StructuredLLMGateway(budget_manager).ainvoke(
                model=model,
                schema=AtomicFactAnswer,
                prompt=_prompt(query, evidence_pack),
                phase="atomic_fact",
                timeout_sec=min(10.0, max(0.1, timeout_sec)),
            ),
            timeout=min(10.0, max(0.1, timeout_sec)),
        )
    except Exception as exc:
        return _unconfirmed(f"atomic_fact_extractor_failed:{type(exc).__name__}")

    available = {
        str(getattr(source, "source_id", "") or ""): source
        for source in sources
        if str(getattr(source, "source_id", "") or "").strip()
    }
    supporting_ids = tuple(
        source_id
        for source_id in extracted.supporting_source_ids
        if source_id in available
    )
    if not extracted.answer.strip() or not supporting_ids:
        return _unconfirmed(extracted.reason or "atomic_fact_answer_not_bound")

    supporting_sources = [available[source_id] for source_id in supporting_ids]
    sufficient = SIMPLE_FACT_EVIDENCE_POLICY.is_sufficient(supporting_sources)
    return AtomicFactAnswer(
        answer=extracted.answer.strip(),
        answer_type=extracted.answer_type,
        supporting_source_ids=supporting_ids,
        confidence=extracted.confidence,
        sufficient=sufficient,
        reason=extracted.reason
        or ("simple fact evidence policy satisfied" if sufficient else "insufficient_trusted_evidence"),
    )


def render_atomic_fact_answer(
    answer: AtomicFactAnswer,
    citation_manager: Any,
) -> str:
    if not answer.sufficient or not answer.answer.strip():
        return (
            "## 当前无法可靠确认\n\n"
            "现有检索结果没有达到可信来源标准，因此暂不作为确认答案。"
        )

    source_map = citation_manager.source_number_map()
    numbers = [
        source_map[source_id]
        for source_id in answer.supporting_source_ids
        if source_id in source_map
    ]
    citation = "".join(f"[{number}]" for number in numbers)
    body = answer.answer.strip()
    if citation and not re.search(r"\[\d+\]", body):
        body = f"{body} {citation}"
    lines = ["## 答案", "", body]
    references = citation_manager.build_references_block(
        source_ids=answer.supporting_source_ids
    )
    if references:
        lines.extend(["", "### 依据", "", references.strip()])
    return "\n".join(lines)


__all__ = ["AtomicFactAnswer", "extract_atomic_fact_answer", "render_atomic_fact_answer"]
