"""Minimal evidence pack and deterministic emergency synthesis."""

from __future__ import annotations

from typing import Any


def build_minimal_evidence_pack(
    *,
    state: Any,
    graph_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    graph_state = graph_state or {}
    findings = [
        item
        for item in list(graph_state.get("findings") or [])
        if isinstance(item, dict)
    ]
    if not findings:
        metadata = getattr(state, "metadata", None)
        if isinstance(metadata, dict):
            findings = [
                item
                for item in list(metadata.get("partial_findings") or [])
                if isinstance(item, dict)
            ]
    if not findings:
        for result in list(getattr(state, "step_results", None) or []):
            if str(getattr(result, "step_type", "") or "") not in {
                "research",
                "network_search",
                "file_read",
            }:
                continue
            metadata = getattr(result, "metadata", None) or {}
            payload = metadata.get("worker_payload") if isinstance(metadata, dict) else {}
            if not isinstance(payload, dict):
                continue
            summary = str(payload.get("summary") or getattr(result, "content", "") or "")
            if summary:
                findings.append(
                    {
                        "task_id": str(metadata.get("task_id") or ""),
                        "summary": summary[:800],
                        "facts": [str(x)[:200] for x in payload.get("facts") or []][:6],
                    }
                )

    evidence_refs = [str(x) for x in list(graph_state.get("evidence_refs") or []) if x]
    assessment = graph_state.get("progress_assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    coverage_gaps = [
        str(x)
        for x in (
            assessment.get("missing_dimensions")
            or [
                item.get("description")
                for item in assessment.get("gaps") or []
                if isinstance(item, dict)
                and item.get("type") == "coverage_gap"
            ]
        )
        if x
    ][:12]
    intent = getattr(state, "intent", None)
    brief = getattr(intent, "brief", None) if intent is not None else None
    return {
        "brief": {
            "objective": str(getattr(brief, "objective", "") or ""),
            "dimensions": [str(x) for x in getattr(brief, "dimensions", None) or []],
        },
        "findings": findings[:16],
        "evidence_refs": evidence_refs[:32],
        "sources": _dedupe_sources(
            _sources_from_findings(findings)
            + _sources_from_worker_rows(
                [
                    item
                    for item in list(graph_state.get("worker_results") or [])
                    if isinstance(item, dict)
                ]
            )
        ),
        "artifact_summaries": [
            {
                "task_id": str(item.get("task_id") or ""),
                "summary": str(item.get("summary") or "")[:400],
            }
            for item in findings[:16]
        ],
        "coverage_gaps": coverage_gaps,
    }


def _sources_from_findings(findings: list[dict[str, Any]]) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for finding in findings:
        for source in list(finding.get("sources") or []):
            value = str(source).strip()
            if value and value.lower() not in seen:
                seen.add(value.lower())
                sources.append(value)
            if len(sources) >= 16:
                return sources
    return sources


def _sources_from_worker_rows(rows: list[dict[str, Any]]) -> list[str]:
    sources: list[str] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        sources.extend(str(item) for item in payload.get("sources") or [])
    return sources


def _dedupe_sources(sources: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for source in sources:
        value = str(source).strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            output.append(value)
        if len(output) >= 16:
            break
    return output


def render_fast_partial_report(
    pack: dict[str, Any],
    *,
    reason: str,
) -> str:
    brief_value = pack.get("brief")
    brief = brief_value if isinstance(brief_value, dict) else {}
    findings = [item for item in pack.get("findings") or [] if isinstance(item, dict)]
    gaps = [str(x) for x in pack.get("coverage_gaps") or [] if str(x).strip()]
    lines = [
        "# 基于当前证据的部分结论",
        "",
        f"> 运行状态：`partial`；原因：`{reason}`。本报告由 Emergency Synthesis Fast Path 生成。",
        "",
        "## 研究目标",
        "",
        str(brief.get("objective") or "（未记录）"),
        "",
        "## 当前可确认要点",
        "",
    ]
    if findings:
        for item in findings[:12]:
            lines.append(f"### {item.get('task_id') or 'finding'}")
            lines.append("")
            lines.append(str(item.get("summary") or "")[:800])
            lines.append("")
            facts = [str(x) for x in item.get("facts") or [] if str(x).strip()]
            if facts:
                lines.append("关键事实：")
                lines.extend(f"- {fact[:240]}" for fact in facts[:5])
                lines.append("")
        sources = [str(x) for x in pack.get("sources") or [] if str(x).strip()]
        if sources:
            lines.append("证据来源：")
            lines.extend(f"- {source[:400]}" for source in sources[:12])
            lines.append("")
    else:
        lines.append("当前没有可结构化确认的 Worker 摘要。")
        lines.append("")
    if gaps:
        lines.append("## 尚未核验的维度")
        lines.append("")
        lines.extend(f"- {gap[:240]}" for gap in gaps)
        lines.append("")
    lines.extend(
        [
            "## 说明",
            "",
            "- 本次运行时间不足，未完成完整交叉综合与质量评估。",
            "- 以上内容只基于已落盘的结构化证据，不虚构缺失结论。",
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"
