"""Deterministic partial delivery renderer for synthesis-provider failures."""

from __future__ import annotations

from typing import Any

from app.research.delivery.synthesis_context import EvidenceDigest


def render_partial_delivery(
    *,
    objective: str,
    findings: list[dict[str, Any]],
    evidence_digests: list[EvidenceDigest],
    worker_summaries: list[dict[str, Any]],
    business_gaps: list[str],
    limitations: list[str],
    unresolved_conflicts: list[str],
    worker_failure_reasons: list[str],
    synthesis_failure_reason: str,
) -> str:
    """Render recovered material only; never invent missing facts."""
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
        "## 已获得的信息",
        "",
    ]
    if findings:
        for finding in findings[:24]:
            claim = str(finding.get("claim") or finding.get("summary") or "").strip()
            if not claim:
                continue
            evidence_ids = ", ".join(
                str(item) for item in finding.get("evidence_ids") or [] if str(item)
            )
            lines.append(f"- {claim}" + (f"（证据：{evidence_ids}）" if evidence_ids else ""))
    else:
        lines.append("- 本轮仅恢复了可追溯证据，尚未形成结构化结论。")

    if worker_summaries:
        lines.extend(["", "## Worker 摘要", ""])
        for row in worker_summaries[:12]:
            summary = str(row.get("summary") or "").strip()
            if summary:
                lines.append(f"- [{row.get('task_id') or 'worker'}] {summary}")

    lines.extend(["", "## 证据", ""])
    if evidence_digests:
        for digest in evidence_digests[:24]:
            lines.append(f"- **{digest.title or digest.evidence_id}**：{digest.locator or digest.evidence_id}")
            if digest.excerpt:
                lines.append(f"  - 摘录：{digest.excerpt}")
            if digest.supported_claims:
                lines.append(f"  - 支持信息：{'；'.join(digest.supported_claims[:3])}")
    else:
        lines.append("- 证据 ID 已恢复，但证据摘要不可用；请查看 evidence.json。")

    lines.extend(["", "## 尚未完成", ""])
    unfinished = [str(item) for item in business_gaps if str(item).strip()]
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
    return "\n".join(lines).strip() + "\n"


__all__ = ["render_partial_delivery"]
