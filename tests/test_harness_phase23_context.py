"""Phase 23: Artifact/Evidence Store、可恢复压缩、glm-5.2 budget、JIT、Worker Profile。"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.artifacts import ArtifactStore, reset_artifact_store, set_artifact_store
from app.agent.harness.compressor import ContextCompressor
from app.agent.harness.context_builder import ContextBuilder
from app.agent.harness.context_budget import ContextBuildSettings, fit_layers_to_token_budget
from app.agent.harness.evidence_store import EvidenceStore, reset_evidence_store, set_evidence_store
from app.agent.harness.research_brief import compile_research_brief
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep, StepResult, TaskIntent
from app.agent.harness.token_counter import TokenCounter, stage_from_step_type
from app.agent.harness.tool_contract import apply_tool_output_contract
from app.agent.harness.worker_profiles import (
    PROFILE_FILE,
    PROFILE_MIXED,
    PROFILE_WEB,
    resolve_worker_profile,
)
from app.config.loader import reload_harness_config
from app.research.workers.registry import resolve_execute_target, worker_tools_for_step


def test_artifact_store_roundtrip(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    art = store.put(
        "前半介绍。" * 10 + "关键财务数字 42.8 亿美元 https://example.com/fin",
        kind="web",
        locator="https://example.com/fin",
        title="财报",
        step_type="network_search",
    )
    assert art.artifact_id.startswith("art-web-")
    store.persist(tmp_path)
    other = ArtifactStore(tmp_path)
    other.load(tmp_path)
    loaded = other.get(art.artifact_id)
    assert loaded is not None
    assert "42.8" in loaded.content
    sliced = other.read(art.artifact_id, query="42.8")
    assert sliced["ok"] is True
    assert sliced["hits"]
    print("[OK] artifact store roundtrip")


def test_reversible_compression_keeps_tail_and_ref():
    reset_artifact_store()
    store = ArtifactStore()
    set_artifact_store(store)
    head = "介绍段落。" * 400
    tail = "关键财务数字 99.1 亿元，来源 https://example.com/late-numbers"
    raw = head + tail
    compressor = ContextCompressor(model=None, enabled=False, threshold_chars=100, max_output_chars=600)
    compressed, meta = asyncio.run(compressor.compress(raw, step_type="network_search"))
    assert meta["reversible"] is True
    assert meta["artifact_id"]
    assert "99.1" in compressed
    assert "https://example.com/late-numbers" in compressed
    assert "read_artifact" in compressed
    restored = store.read(meta["artifact_id"])
    assert restored["ok"] is True
    assert "99.1" in restored["text"]
    reset_artifact_store()
    print("[OK] reversible compression")


def test_evidence_claim_binding():
    reset_evidence_store()
    store = EvidenceStore()
    set_evidence_store(store)
    artifacts = ArtifactStore()
    page = artifacts.put(
        "IDC 称 2026 年全球市场规模约 15.5 亿美元。后文还有别的。",
        kind="web",
        locator="https://example.com/idc",
        title="IDC",
    )
    findings = store.ingest_worker_payload(
        {
            "facts": ["2026 年全球市场规模约 15.5 亿美元"],
            "sources": ["https://example.com/idc"],
        },
        artifact_ids=[page.artifact_id],
        step_type="network_search",
        artifact_store=artifacts,
    )
    assert findings
    span = store.get(findings[0].evidence_ids[0])
    assert span is not None
    assert span.artifact_id == page.artifact_id
    block = store.lookup_block(query="市场规模")
    assert "15.5" in block
    assert findings[0].evidence_ids[0] in block
    reset_evidence_store()
    print("[OK] claim-evidence binding")


def test_research_brief_anchor():
    intent = TaskIntent(
        raw_query="分析人形机器人商业化，比较 Tesla 和 Figure，不要使用 2024 年以前资料，输出 PDF",
        summary="人形机器人商业化对比",
        needs_network=True,
        deliverable="pdf",
    )
    brief = compile_research_brief(task_query=intent.raw_query, intent=intent)
    prompt = brief.to_prompt()
    assert "Research Brief" in prompt
    assert brief.deliverable == "pdf"
    assert "横向比较" in brief.dimensions or "商业化" in brief.dimensions
    settings = ContextBuildSettings(wrap_untrusted_external=False, jit_retrieval_enabled=True)
    builder = ContextBuilder(settings)
    state = LoopState(
        session_id="s1",
        intent=intent,
        plan=ExecutionPlan(
            steps=[PlanStep(step_type="network_search", description="搜索 Tesla 商业化", objective="Tesla 商业化")],
            summary="plan",
            research_brief=brief.objective,
        ),
    )
    state.research_brief_obj = brief
    msg = builder.build_step_message(
        intent.raw_query,
        state,
        state.plan.steps[0],
        0,
        "output/session_s1",
    )
    assert "Research Brief" in msg
    print("[OK] research brief")


def test_tool_output_contract_search():
    store = ArtifactStore()
    set_artifact_store(store)
    raw = {
        "query": "tesla",
        "results": [
            {
                "title": "Tesla 2026",
                "url": "https://example.com/tesla",
                "content": "摘要片段",
                "raw_content": "全文" * 2000 + " 营收 97.7 亿美元",
            }
        ],
    }
    compact = apply_tool_output_contract(raw, tool_name="internet_search", step_type="network_search")
    payload = json.loads(compact)
    assert payload["results"][0]["artifact_id"]
    assert "97.7" not in compact
    art_id = payload["results"][0]["artifact_id"]
    body = store.read(art_id, query="97.7")
    assert body["ok"] is True
    assert any("97.7" in hit.get("text", "") for hit in body.get("hits") or [])
    reset_artifact_store()
    print("[OK] tool output contract")


def test_worker_profiles_minimize_surface():
    assert resolve_worker_profile("network_search") == PROFILE_WEB
    assert resolve_worker_profile("file_read") == PROFILE_FILE
    assert resolve_worker_profile("research", ["internet_search"]) == PROFILE_WEB
    assert resolve_worker_profile("research", ["internet_search", "execute_sql_query"]) == PROFILE_MIXED
    assert "internet_search" in worker_tools_for_step("network_search")
    assert "read_artifact" in worker_tools_for_step("network_search")
    assert "execute_sql_query" not in worker_tools_for_step("network_search")
    worker = object()
    agent, mode = resolve_execute_target(
        "research",
        workers={"research": object(), PROFILE_WEB: worker},
        profile=PROFILE_WEB,
    )
    assert agent is worker
    assert mode == "direct"
    print("[OK] worker profiles")


def test_stage_token_budget_differs():
    counter = TokenCounter("glm-5.2")
    plan = counter.plan()
    assert plan.model == "glm-5.2"
    assert plan.available_dynamic < plan.context_window
    assert counter.budget_for_step_type("network_search") == plan.stage_budgets["researcher"]
    assert counter.budget_for_step_type("generate_markdown") == plan.stage_budgets["synthesis"]
    assert counter.budget_for_step_type("generate_markdown") > counter.budget_for_step_type("network_search")
    assert stage_from_step_type("plan") == "planner"
    print("[OK] stage token budget")


def test_jit_synthesis_uses_relevant_evidence():
    reset_evidence_store()
    store = EvidenceStore()
    set_evidence_store(store)
    store.add_finding("Tesla 2026 产能目标 10 万台", evidence_ids=["E1"], claim_id="C1")
    store.add_span(
        "Tesla 2026 产能目标 10 万台",
        artifact_id="art-web-1",
        locator="https://example.com/tesla",
        source_kind="url",
    )
    store.add_span(
        "Unitree 出货以消费级为主，与本题无关的长文。" * 8,
        artifact_id="art-web-2",
        locator="https://example.com/unitree",
        source_kind="url",
    )
    settings = ContextBuildSettings(wrap_untrusted_external=False, jit_retrieval_enabled=True)
    builder = ContextBuilder(settings)
    state = LoopState(
        session_id="s1",
        intent=TaskIntent(raw_query="写 Tesla 商业化", summary="Tesla", needs_network=True, deliverable="md"),
        plan=ExecutionPlan(
            steps=[PlanStep(step_type="generate_markdown", description="写 Tesla 章节", objective="Tesla 商业化")],
            summary="plan",
        ),
    )
    msg = builder.build_step_message("写 Tesla 报告", state, state.plan.steps[0], 0, "output/s")
    assert "Tesla" in msg
    assert builder.last_step_metrics is not None
    assert builder.last_step_metrics.stage == "synthesis"
    reset_evidence_store()
    print("[OK] jit synthesis evidence")


def test_architecture_doc_mentions_stategraph():
    text = (ROOT / "docs" / "HARNESS_ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Research StateGraph" in text
    assert "graph_runtime_enabled" in text
    assert "while 外环（领域 Harness）是权威" not in text
    print("[OK] architecture doc")


def test_config_phase23():
    cfg = reload_harness_config()
    assert cfg.token_budget_model == "glm-5.2"
    assert cfg.context_jit_retrieval_enabled is True
    assert cfg.context_reversible_compression is True
    assert cfg.token_stage_budgets["synthesis"] == 40000
    assert cfg.graph_runtime_enabled is True
    assert cfg.progress_eval_enabled is True
    print("[OK] config phase23")


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        test_artifact_store_roundtrip(Path(tmp))
    test_reversible_compression_keeps_tail_and_ref()
    test_evidence_claim_binding()
    test_research_brief_anchor()
    test_tool_output_contract_search()
    test_worker_profiles_minimize_surface()
    test_stage_token_budget_differs()
    test_jit_synthesis_uses_relevant_evidence()
    test_architecture_doc_mentions_stategraph()
    test_config_phase23()
    print("\n=== Phase 23 context tests passed ===")
