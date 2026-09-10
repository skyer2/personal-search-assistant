"""Deterministic partial delivery renderer for synthesis-provider failures."""

from __future__ import annotations

import re
from typing import Any

from app.research.delivery.synthesis_context import EvidenceDigest


_INTERNAL_ID = re.compile(
    r"\b(?:coverage|gap|claim|finding|evidence|task|worker_result)_[A-Za-z0-9_-]{6,}\b",
    re.IGNORECASE,
)


def scrub_internal_ids(content: str) -> str:
    """Remove machine-only semantic IDs from user-facing delivery."""
    return _INTERNAL_ID.sub("内部记录", str(content or ""))


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
    _ = worker_summaries
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
            if claim:
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
    unfinished = [str(item) for item in semantic_gaps or limitations if str(item).strip()]
    lines.extend(f"- {item}" for item in unfinished[:12] or ["- 部分研究维度尚未完成交叉验证。"])
    lines.append("- 完整模型综合未完成，因此本结果不构成完整终稿。")

    execution_limits = [str(item) for item in limitations if str(item).strip()]
    execution_limits.extend(
        f"Research worker: {item}" for item in worker_failure_reasons if str(item).strip()
    )
    if synthesis_failure_reason:
        execution_limits.append(f"Synthesis: {synthesis_failure_reason}")
    lines.extend(["", "## 执行限制", ""])
    lines.extend(f"- {item}" for item in list(dict.fromkeys(execution_limits))[:12])
    if unresolved_conflicts:
        lines.extend(["", "## 未解决冲突", ""])
        lines.extend(f"- {item}" for item in unresolved_conflicts[:8])
    lines.extend(["", "因此本结果属于部分交付，不能视为完整成功。", ""])
    return scrub_internal_ids("\n".join(lines).strip() + "\n")


__all__ = ["render_partial_delivery", "scrub_internal_ids"]
