from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.agent.harness.artifacts import ArtifactStore, reset_artifact_store, set_artifact_store
from app.agent.harness.citations import CitationManager
from app.agent.harness.tool_contract import apply_tool_output_contract
from app.research.delivery.synthesis_context import EvidenceDigest
from app.research.delivery.evidence_pack import (
    COMPACT_SYNTHESIS_INPUT_TOKENS,
    NORMAL_SYNTHESIS_INPUT_TOKENS,
    build_evidence_pack,
)
from app.research.execution.synthesis_executor import RETRYABLE_SYNTHESIS_FAILURES, SynthesisExecutor, SynthesisRequest
from app.research.runtime.worker import ResearchContext
from app.research.runtime.ingestion import _prepare_evidence
from app.research.workers.registry import worker_tools_for_step
from app.tools.fetch_url import fetch_url_content


def test_research_tool_authority_comes_from_one_registry() -> None:
    research_tools = worker_tools_for_step("research")
    assert set(research_tools) == {
        "internet_search",
        "fetch_url",
        "batch_search",
        "batch_fetch",
        "read_file_content",
        "read_artifact",
        "read_evidence",
    }
    web_only = worker_tools_for_step("research", allowed_sources=["web"])
    assert "read_file_content" not in web_only
    assert {"internet_search", "batch_search", "read_evidence"} <= set(web_only)


def test_synthesis_prompt_exposes_stable_binding_numbers_without_raw_digest() -> None:
    request = SynthesisRequest(
        mode="normal",
        evidence_refs=["E1"],
        evidence_digests=[
            EvidenceDigest(
                "E1",
                "Company filing",
                "https://example.com/filing",
                "Company raised 500 million USD.",
                citation_number=7,
            )
        ],
        insight_cards=[{
            "insight_id": "I1", "title": "企业结果导向", "core_claim": "企业正在要求可验证交付。",
            "mechanism": "ROI 与治理要求共同推动。", "why_it_matters": "影响生产采用。", "confidence": 0.8,
        }],
        claim_evidence_bindings=[{
            "claim_id": "C1", "display_evidence": [{"citation_number": 7, "source": "example.com", "date": "2026-01-01"}],
        }],
    )
    executor = SynthesisExecutor(SimpleNamespace(), SimpleNamespace())
    prompt = executor._prompt(request, ResearchContext(run_id="r", query="q", session_id="s"))

    assert "[7] example.com 2026-01-01" in prompt
    assert "禁止使用 E 编号" in prompt
    assert "Company raised 500 million USD." not in prompt


def test_selected_findings_project_numeric_claims_to_citations() -> None:
    manager = CitationManager()
    manager.bind_worker_facts(
        0,
        "research",
        ["公司完成 5 亿美元融资，估值 20 亿美元。"],
        ["https://example.com/funding"],
    )
    findings = [
        {
            "claim": "公司完成 5 亿美元融资，估值 20 亿美元。",
            "evidence_ids": ["E1"],
        }
    ]
    manager.bind_evidence_records(
        [
            {
                "evidence_id": "E1",
                "locator": "https://example.com/funding",
                "excerpt_ref": "Company raised 500 million USD at a 2 billion valuation.",
                "artifact_ref": "art-1",
                "source_kind": "url",
                "step_index": 0,
                "step_type": "research",
            }
        ],
        findings,
    )
    number_map = manager.source_number_map()
    evidence_numbers = {
        source.evidence_id: number_map[source.source_id]
        for source in manager.sources
        if source.evidence_id
    }
    report = manager.build_cited_report(
        manager.inject_finding_citations(
            "公司完成 5 亿美元融资，估值 20 亿美元。[99]另一句没有证据。",
            findings,
            evidence_numbers,
        )
    )

    assert "[1]" in report
    assert "[99]" not in report
    assert "## 参考文献" in report
    assert manager.validate_citations(report)[0] is True
    assert manager.validate_citations(report.replace("[1]", "[99]")) == (
        False,
        "citation_invalid_number",
    )
    # The research runner can assign a ledger-owned citation number to a
    # recovered EvidenceRecord that the legacy citation collector did not see.
    # It must validate that binding while continuing to reject arbitrary IDs.
    ledger_numbered_report = report.replace("[1]", "[2]")
    assert manager.validate_citations(
        ledger_numbered_report, additional_valid_numbers=[2]
    )[0] is True
    assert manager.validate_citations(
        ledger_numbered_report, additional_valid_numbers=[99]
    ) == (False, "citation_invalid_number")


def test_synthesis_output_removes_runtime_json_and_pdf_disclaimer() -> None:
    content = (
        "说明：当前对话环境无法直接生成或附带PDF文件，可复制后打印。\n\n"
        "## 报告\n\n公司完成 5 亿美元融资。\n\n"
        '{"ok": true, "findings": [{"claim": "bare"}]}\n\n'
        '```json\n{"ok": true, "findings": [{"claim": "nested"}]}\n```\n'
        '{\n  "ok": true,\n  "summary": "pretty worker payload",\n'
        '  "findings": []\n}\n'
        '{"ok": false, "summary": "budget denied worker payload", "findings": []}\n'
    )
    cleaned = SynthesisExecutor._clean_response_content(content)

    assert cleaned.startswith("## 报告")
    assert "无法直接生成" not in cleaned
    assert "```json" not in cleaned
    assert "bare" not in cleaned
    assert "nested" not in cleaned
    assert "pretty worker payload" not in cleaned
    assert "budget denied worker payload" not in cleaned


def test_canonical_evidence_bypasses_legacy_six_source_step_cap() -> None:
    manager = CitationManager()
    findings = []
    records = []
    for index in range(1, 9):
        evidence_id = f"E{index}"
        findings.append(
            {
                "claim": f"Company fact {index} with 100 units.",
                "evidence_ids": [evidence_id],
            }
        )
        records.append(
            {
                "evidence_id": evidence_id,
                "locator": f"https://example.com/{evidence_id}",
                "excerpt_ref": f"Evidence {index}",
                "artifact_ref": f"art-{index}",
                "source_kind": "url",
                "step_index": 0,
                "step_type": "research",
            }
        )

    manager.bind_evidence_records(records, findings)

    assert len(manager.sources) == 8
    assert set(manager.evidence_number_map()) == {f"E{index}" for index in range(1, 9)}


def test_batch_search_contract_preserves_provider_published_at(tmp_path: Path) -> None:
    store = ArtifactStore(session_dir=tmp_path)
    raw = {
        "results": [
            {
                "query": "deepseek company",
                "results": [
                    {
                        "title": "DeepSeek official update",
                        "url": "https://example.com/deepseek",
                        "content": "DeepSeek published an update.",
                        "published_at": "2026-01-02",
                    }
                ],
            }
        ]
    }
    output = json.loads(
        apply_tool_output_contract(
            raw,
            tool_name="batch_search",
            step_type="research",
            store=store,
        )
    )
    card = output["results"][0]
    assert card["published_at"] == "2026-01-02"
    artifact = store.get(card["artifact_id"])
    assert artifact is not None
    assert artifact.metadata["published_at"] == "2026-01-02"


def test_fetch_contract_extracts_and_persists_published_at(tmp_path: Path) -> None:
    store = ArtifactStore(session_dir=tmp_path)
    set_artifact_store(store)
    try:
        html = """
        <html><head><title>Company update</title>
        <meta property="article:published_time" content="2026-03-04T08:00:00Z">
        </head><body>Company published an official update.</body></html>
        """

        def fetcher(_url: str, _timeout: float) -> tuple[str, str]:
            return html, "text/html"

        result = fetch_url_content(
            "https://example.com/update",
            fetcher=fetcher,
            use_cache=False,
        )
        assert result["published_at"] == "2026-03-04"
        artifact = store.get(str(result["artifact_id"]))
        assert artifact is not None
        assert artifact.metadata["published_at"] == "2026-03-04"
    finally:
        reset_artifact_store()


def test_ingestion_reads_published_at_from_runtime_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(session_dir=tmp_path)
    set_artifact_store(store)
    try:
        artifact = store.put(
            "Official company fact.",
            kind="web",
            locator="https://example.com/fact",
            metadata={"published_at": "2026-05-06"},
        )
        records, _ = _prepare_evidence(
            [
                {
                    "task_id": "task-runtime-metadata",
                    "run_id": "run-runtime-metadata",
                    "payload": {
                        "evidence_ids": ["E1"],
                        "artifact_ids": [artifact.artifact_id],
                    },
                }
            ]
        )
        assert records[0].published_at == "2026-05-06"
        assert records[0].artifact_ref == artifact.artifact_id
    finally:
        reset_artifact_store()


def test_evidence_pack_is_criterion_balanced_and_compact_retry_shrinks() -> None:
    criteria = ["candidate_pool", "company_evidence"]
    evidence = []
    findings = []
    for criterion_index, criterion in enumerate(criteria):
        for item_index in range(8):
            evidence_id = f"E{criterion_index * 8 + item_index + 1}"
            tier = "PRIMARY" if item_index == 0 else "SECONDARY"
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "artifact_ref": f"art-{evidence_id}",
                    "locator": f"https://example.com/{evidence_id}",
                    "published_at": "2026-01-01",
                    "source_tier": tier,
                    "authority_score": 0.9 if tier == "PRIMARY" else 0.5,
                    "excerpt_ref": f"Evidence {evidence_id}",
                }
            )
            findings.append(
                {
                    "finding_id": f"f-{evidence_id}",
                    "claim": f"{criterion} finding {item_index}",
                    "evidence_ids": [evidence_id],
                    "supported_criteria": [criterion],
                    "confidence": 0.8,
                }
            )
    conflicts = [
        {"edge_id": "resolved", "status": "resolved", "winner_id": "claim-1", "evidence_ids": ["E1"]},
        {"edge_id": "expected", "status": "disclosed", "kind": "expected_disagreement", "evidence_ids": ["E2"]},
        {"edge_id": "noise", "status": "unresolved", "blocking": False, "evidence_ids": ["E3"]},
    ]
    normal = build_evidence_pack(
        criteria,
        findings,
        [],
        evidence,
        conflicts,
        NORMAL_SYNTHESIS_INPUT_TOKENS,
    )
    compact = build_evidence_pack(
        criteria,
        findings,
        [],
        evidence,
        conflicts,
        COMPACT_SYNTHESIS_INPUT_TOKENS,
        compact=True,
    )
    assert len(normal.findings) == 6
    assert len(compact.findings) == 4
    assert NORMAL_SYNTHESIS_INPUT_TOKENS == 8_000
    assert COMPACT_SYNTHESIS_INPUT_TOKENS == 4_000
    assert compact.estimated_tokens < normal.estimated_tokens
    assert {row["edge_id"] for row in normal.conflict_resolutions} == {"resolved", "expected"}
    assert normal.findings[0]["claim"] == "candidate_pool finding 0"
    assert "synthesis_timeout" in RETRYABLE_SYNTHESIS_FAILURES
