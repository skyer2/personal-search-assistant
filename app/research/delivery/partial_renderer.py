"""Deterministic partial delivery renderer for synthesis-provider failures."""

from __future__ import annotations

import re
from typing import Any

from app.research.delivery.synthesis_context import EvidenceDigest


_INTERNAL_ID = re.compile(
    r"\b(?:coverage|gap|claim|finding|evidence|task|worker_result)_[A-Za-z0-9_-]{6,}\b",
    re.IGNORECASE,
)

_RUNTIME_CODE = re.compile(
    r"(?:budget_blocked:.+|"
    r"(?:worker|research_phase|run)_(?:token|llm_call)_cap|"
    r"search_query_cap|fetch_source_cap|tool_call_cap|"
    r"synthesis_timeout|worker_timeout|step_timeout)",
    re.IGNORECASE,
)


def scrub_internal_ids(content: str) -> str:
    """Remove machine-only semantic IDs from user-facing delivery."""
    return _INTERNAL_ID.sub("内部记录", str(content or ""))


def _user_visible_limitation(value: str) -> bool:
    return bool(value.strip() and not _RUNTIME_CODE.search(value))


def render_partial_delivery(
    *,
    objective: str,
    findings: list[dict[str, Any]],
    evidence_digests: list[EvidenceDigest],
    worker_summaries: list[dict[str, Any]],
    semantic_gaps: list[str],
    limitations: list[str],
    unresolved_conflicts: list[str],
    worker_failure_reasons: list[str],
    synthesis_failure_reason: str,
) -> str:
    """Render recovered material only; never invent missing facts."""
    _ = (worker_summaries, worker_failure_reasons, synthesis_failure_reason)
    publishable_findings = [
        item for item in findings
        if isinstance(item, dict) and bool(item.get("validated", False))
    ]
    if not publishable_findings and not semantic_gaps and not limitations:
        return ""

    lines = [
        "# 部分研究结果",
        "",
        "以下内容只包含本次已验证、可发布的研究结论。",
        "",
        "## 研究目标",
        "",
        objective.strip() or "（未记录）",
        "",
        "## 已确认的信息",
        "",
    ]
    if publishable_findings:
        for finding in publishable_findings[:24]:
            claim = str(finding.get("claim") or finding.get("text") or finding.get("summary") or "").strip()
            if _user_visible_limitation(claim):
                lines.append(f"- {claim}")
    else:
        lines.append("- 本轮仅恢复了可追溯证据，尚未形成结构化结论。")

    lines.extend(["", "## 尚未完成", ""])
    unfinished = [
        str(item)
        for item in semantic_gaps or limitations
        if _user_visible_limitation(str(item))
    ]
    if unfinished:
        lines.extend(f"- {item}" for item in unfinished[:12])
    else:
        lines.append("- 部分研究维度尚未完成交叉验证。")
    lines.append("- 这些缺口尚未获得足以直接回答的可发布证据。")
    if unresolved_conflicts:
        lines.extend(["", "## 未解决冲突", ""])
        lines.extend(f"- {item}" for item in unresolved_conflicts[:8])
    lines.extend(["", "本结果为部分交付；未列出的内容不应视为已得到确认。", ""])
    return scrub_internal_ids("\n".join(lines).strip() + "\n")


__all__ = ["render_partial_delivery", "scrub_internal_ids"]
