"""Environment tools：fetch_url + worker tool 白名单（无需 LLM / Tavily）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.tools.fetch_url import fetch_url_content, strip_html


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


def test_fetch_url_in_web_tools():
    from app.research.workers.registry import worker_tools_for_step

    tools = worker_tools_for_step("network_search")
    assert "internet_search" in tools
    assert "fetch_url" in tools
    print("[OK] search+fetch remain environment tools")


if __name__ == "__main__":
    test_fetch_url_strips_html_and_stores_artifact()
    test_fetch_url_in_web_tools()
    print("\n=== environment tool tests passed ===")
