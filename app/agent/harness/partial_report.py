"""Deterministic partial report when synthesis fails or run aborts mid-research.

Never dump raw Worker logs as the user-facing answer.
"""

from __future__ import annotations

from typing import Any


def render_partial_report(
    *,
    state: Any,
    abort_reason: str = "",
    synthesis_failed: bool = False,
    assessment: dict[str, Any] | None = None,
) -> str:
    plan = getattr(state, "plan", None)
    intent = getattr(state, "intent", None)
    brief = getattr(intent, "brief", None) if intent is not None else None
    objective = ""
    dimensions: list[str] = []
    if brief is not None:
        objective = str(getattr(brief, "objective", "") or "")
        dimensions = [str(x) for x in (getattr(brief, "dimensions", None) or []) if x]
    if not objective:
        objective = str(getattr(intent, "raw_query", "") or getattr(state, "task_query", "") or "")

    findings = _collect_findings(state)
    expected = []
    unresolved = []
    if isinstance(assessment, dict):
        expected = [str(x) for x in (assessment.get("expected_disagreements") or []) if x][:8]
        unresolved = [str(x) for x in (assessment.get("unresolved_conflicts") or []) if x][:8]
    # 补全 Claim Reconciliation 结果（跨 Worker 全局对账）
    meta = getattr(state, "metadata", None) or {}
    if isinstance(meta, dict):
        recon = meta.get("claim_reconciliation") if isinstance(meta.get("claim_reconciliation"), dict) else {}
        for item in recon.get("disclosed_labels") or []:
            text = str(item)
            if text and text not in expected:
                expected.append(text)
        for item in recon.get("unresolved_labels") or []:
            text = str(item)
            if text and text not in unresolved:
                unresolved.append(text)
        expected = expected[:8]
        unresolved = unresolved[:8]

    reason = abort_reason or ("synthesis_failed" if synthesis_failed else "incomplete")
    lines = [
        "# 本次研究未完整完成",
        "",
        f"> 运行状态：`{reason}`。下文是基于已收集材料的**可读摘要**，不是完整终稿。",
        "",
        "## 研究目标",
        "",
        objective or "（未记录）",
        "",
    ]
    if dimensions:
        lines.append("## 计划维度")
        lines.append("")
        for dim in dimensions:
            lines.append(f"- {dim}")
        lines.append("")

    lines.append("## 已收集要点")
    lines.append("")
    if findings:
        for item in findings[:12]:
            tid = item.get("task_id") or ""
            summary = item.get("summary") or ""
            lines.append(f"### {tid or 'finding'}")
            lines.append("")
            lines.append(summary)
            lines.append("")
            facts = item.get("facts") or []
            if facts:
                lines.append("关键事实：")
                for fact in facts[:6]:
                    lines.append(f"- {fact}")
                lines.append("")
    else:
        lines.append("（尚无结构化 Worker 摘要）")
        lines.append("")

    if expected:
        lines.append("## 预期口径差异（无需再搜，终稿应解释）")
        lines.append("")
        for item in expected:
            lines.append(f"- {item}")
        lines.append("")
    if unresolved:
        lines.append("## 仍待核实的冲突")
        lines.append("")
        for item in unresolved:
            lines.append(f"- {item}")
        lines.append("")

    lines.extend(
        [
            "## 尚未完成",
            "",
            "- 完整交叉综合与立场写作",
            "- 交付物终稿渲染（如 PDF）",
            "",
            "## 说明",
            "",
            "本页由 Harness `PartialReportRenderer` 确定性生成，避免把 Worker 原始中间产物直接当作最终答案。",
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _collect_findings(state: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(getattr(state, "step_results", None) or []):
        step_type = str(getattr(item, "step_type", "") or "")
        if step_type not in {"research", "network_search", "file_read"}:
            continue
        meta = getattr(item, "metadata", None) or {}
        payload = meta.get("worker_payload") if isinstance(meta, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        tid = str(meta.get("task_id") or "")
        summary = str(payload.get("summary") or getattr(item, "content", "") or "").strip()
        if not summary:
            continue
        # 截断，避免 partial 再变成 dump
        summary = summary[:800]
        key = tid or summary[:40]
        if key in seen:
            continue
        seen.add(key)
        facts = [str(x)[:200] for x in (payload.get("facts") or []) if str(x).strip()][:8]
        out.append({"task_id": tid, "summary": summary, "facts": facts})
    return out
