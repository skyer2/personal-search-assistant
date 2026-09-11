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
    if not findings and not evidence_digests:
        return ""

    lines = [
        "# 部分研究结果",
        "",
        "本次研究未完成全部计划，但已经获得以下可确认信息。",
        "",
        "## 研究目标",
        "",
        objective.strip() or "（未记录）",
        "",
        "## 已确认的信息",
        "",
    ]
    if findings:
        for finding in findings[:24]:
            claim = str(finding.get("claim") or finding.get("text") or finding.get("summary") or "").strip()
            if _user_visible_limitation(claim):
                lines.append(f"- {claim}")
    else:
        lines.append("- 本轮仅恢复了可追溯证据，尚未形成结构化结论。")

    lines.extend(["", "## 证据", ""])
    if evidence_digests:
        for digest in evidence_digests[:24]:
            lines.append(f"- **{digest.title or '来源'}**：{digest.locator or '来源链接不可用'}")
            if digest.excerpt:
                lines.append(f"  - 摘录：{digest.excerpt}")
            if digest.supported_claims:
                lines.append(f"  - 支持信息：{'；'.join(digest.supported_claims[:3])}")
    else:
        lines.append("- 证据摘要不可用；请查看本次运行的证据文件。")

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
    lines.append("- 完整模型综合未完成，因此本结果不构成完整终稿。")

    lines.extend(["", "## 执行限制", ""])
    lines.append("- 部分研究执行因资源限制提前停止。")
    if unresolved_conflicts:
        lines.extend(["", "## 未解决冲突", ""])
        lines.extend(f"- {item}" for item in unresolved_conflicts[:8])
    lines.extend(["", "因此本结果属于降级部分交付，不能视为完整成功。", ""])
    return scrub_internal_ids("\n".join(lines).strip() + "\n")


__all__ = ["render_partial_delivery", "scrub_internal_ids"]
