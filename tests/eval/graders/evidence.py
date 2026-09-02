"""Evidence / grounding grader：claim → evidence → source，不用格式分冒充正确性。"""

from __future__ import annotations

from typing import Any

from app.agent.harness.citations import CitationManager


def grade_evidence_case(case: dict[str, Any]) -> dict[str, Any]:
    manager = CitationManager()
    for index, source in enumerate(case.get("sources") or []):
        if not isinstance(source, dict):
            continue
        locator = str(source.get("locator") or "")
        excerpt = str(source.get("excerpt") or "")
        manager.register_from_step(
            index,
            str(source.get("step_type") or "research"),
            f"{excerpt} {locator}".strip(),
            {"locator": locator, "source_kind": source.get("source_kind") or "url"},
        )
    answer = str(case.get("answer") or "")
    cited = manager.build_cited_report(answer) if manager.sources else answer
    metrics = manager.compute_metrics(cited if manager.sources else answer)
    expect = dict(case.get("expected") or {})
    issues: list[str] = []

    if "min_citation_coverage" in expect and metrics["citation_coverage_rate"] < float(
        expect["min_citation_coverage"]
    ):
        issues.append("citation_coverage_low")
    if "max_citation_coverage" in expect and metrics["citation_coverage_rate"] > float(
        expect["max_citation_coverage"]
    ):
        issues.append("citation_coverage_high")
    if "max_hallucination_rate" in expect and metrics["hallucination_rate"] > float(
        expect["max_hallucination_rate"]
    ):
        issues.append("hallucination_high")
    if "min_hallucination_rate" in expect and metrics["hallucination_rate"] < float(
        expect["min_hallucination_rate"]
    ):
        issues.append("hallucination_low")
    if expect.get("must_have_references") and "参考文献" not in cited and "References" not in cited:
        issues.append("missing_references")
    if "registered_sources" in expect and metrics["registered_sources"] != int(expect["registered_sources"]):
        issues.append("registered_sources_mismatch")
    if "citation_coverage_rate" in expect and metrics["citation_coverage_rate"] != float(
        expect["citation_coverage_rate"]
    ):
        issues.append("coverage_mismatch")
    if expect.get("unsupported_ok") is False and metrics["hallucination_rate"] < 0.5:
        issues.append("expected_unsupported_claims")
    if "conflict_sources" in expect and metrics["registered_sources"] < int(expect["conflict_sources"]):
        issues.append("missing_conflict_sources")

    return {
        "ok": not issues,
        "issues": issues,
        "metrics": metrics,
        "grounding_score": round(1.0 - float(metrics.get("hallucination_rate") or 0.0), 3),
    }
