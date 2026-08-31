"""P1: Mode Router / Conversation / fetch_url（无需 LLM / Tavily）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.conversation.store import ConversationStore, ConversationTurn, looks_like_follow_up, rewrite_query
from app.research.routing.mode_router import budget_for_mode, classify_auto, route
from app.research.runtime.quick import compose_quick_answer, pick_urls
from app.tools.fetch_url import fetch_url_content, strip_html


def test_user_override_mode():
    quick = route("比较 A 和 B", user_mode="quick")
    assert quick.mode == "quick" and quick.user_override
    deep = route("今天休市吗", user_mode="deep")
    assert deep.mode == "deep" and deep.user_override
    print("[OK] user override SearchMode")


def test_auto_quick_vs_deep():
    fact = classify_auto("纳斯达克今天休市了吗")
    assert fact.mode == "quick"
    compare = classify_auto("比较 Tesla / Figure / Unitree 的差异")
    assert compare.mode == "deep"
    report = classify_auto("搜索 AI 趋势并生成 Markdown 报告")
    assert report.mode == "deep"
    print("[OK] auto classify quick/deep")


def test_budget_for_mode():
    q = budget_for_mode("quick")
    d = budget_for_mode("deep")
    assert q["max_replan_count"] == 0
    assert q["max_tool_calls"] == 3
    assert d["max_replan_count"] == 2
    assert d["max_tool_calls"] == 15
    print("[OK] mode budgets")


def test_conversation_follow_up(tmp_path: Path | None = None):
    root = tmp_path or (ROOT / "output" / "test_conversations")
    store = ConversationStore(root, user_id="me", project_id="Inbox")
    tid = "thr-follow"
    store.append_turn(tid, ConversationTurn(role="user", content="创业板今天为什么跌？"))
    store.append_turn(tid, ConversationTurn(role="assistant", content="主要受大盘拖累。"))
    thread = store.get(tid)
    assert looks_like_follow_up("创业板呢？")
    rewritten = rewrite_query("创业板呢？", thread)
    assert "此前主题" in rewritten or "上一轮" in rewritten
    assert "创业板呢" in rewritten
    turns, _summary = store.get_context(tid)
    assert len(turns) == 2
    print("[OK] conversation follow-up rewrite")


def test_conversation_evicts_to_summary(tmp_path: Path | None = None):
    root = tmp_path or (ROOT / "output" / "test_conversations")
    store = ConversationStore(root / "evict", user_id="me", project_id="KRX")
    tid = "thr-long"
    for i in range(10):
        store.append_turn(tid, ConversationTurn(role="user", content=f"问题{i}"), max_turns=6)
        store.append_turn(tid, ConversationTurn(role="assistant", content=f"回答{i}"), max_turns=6)
    thread = store.get(tid)
    assert len(thread.turns) <= 6
    assert thread.rolling_summary
    print("[OK] conversation rolling summary")


def test_fetch_url_strips_html_and_stores_artifact():
    from app.agent.harness.artifacts import ArtifactStore, reset_artifact_store, set_artifact_store
    from app.agent.harness.evidence_store import EvidenceStore, reset_evidence_store, set_evidence_store

    html = "<html><title>Demo Page</title><body><p>正文数字 42</p><script>x=1</script></body></html>"
    assert "正文数字 42" in strip_html(html)
    assert "x=1" not in strip_html(html)

    store = ArtifactStore()
    set_artifact_store(store)
    set_evidence_store(EvidenceStore())
    try:
        def fake_fetch(url: str, timeout: float):
            return html, "text/html"

        payload = fetch_url_content("https://example.com/demo", fetcher=fake_fetch)
        assert payload["ok"] is True
        assert payload["title"] == "Demo Page"
        assert payload["artifact_id"]
        assert store.get(payload["artifact_id"]) is not None
        print("[OK] fetch_url artifact")
    finally:
        reset_artifact_store()
        reset_evidence_store()


def test_route_after_mode_and_fetch_url_in_web_tools():
    from app.research.runtime.graph import route_after_mode
    from app.research.workers.registry import worker_tools_for_step

    assert route_after_mode({"search_mode": "quick"}) == "quick_search"
    assert route_after_mode({"search_mode": "deep"}) == "intent"
    assert route_after_mode({"search_mode": ""}) == "intent"
    tools = worker_tools_for_step("network_search")
    assert "internet_search" in tools
    assert "fetch_url" in tools
    print("[OK] route_after_mode + fetch_url on web worker")


def test_quick_compose_and_pick_urls():
    cards = [
        {"title": "A", "url": "https://a.example", "snippet": "alpha"},
        {"title": "B", "url": "https://b.example", "snippet": "beta"},
        {"title": "A2", "url": "https://a.example", "snippet": "dup"},
    ]
    assert pick_urls(cards, limit=2) == ["https://a.example", "https://b.example"]
    answer = compose_quick_answer("休市吗", cards)
    assert "Sources" in answer
    assert "https://a.example" in answer
    print("[OK] quick compose")


if __name__ == "__main__":
    test_user_override_mode()
    test_auto_quick_vs_deep()
    test_budget_for_mode()
    test_conversation_follow_up()
    test_conversation_evicts_to_summary()
    test_fetch_url_strips_html_and_stores_artifact()
    test_quick_compose_and_pick_urls()
    test_route_after_mode_and_fetch_url_in_web_tools()
    print("\n=== P1 personal search tests passed ===")
