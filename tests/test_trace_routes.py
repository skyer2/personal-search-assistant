"""Trace API：citations / JSONL / 未配置 Langfuse 不得 500。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.health import _check_langfuse
from app.api.trace_routes import get_citations, get_jsonl_trace, get_langfuse_traces, langfuse_viewer_config
from app.observability.paths import APP_ROOT


def test_citations_missing_file_is_empty_not_500():
    payload = get_citations("no-such-session")
    assert payload["session_id"] == "no-such-session"
    assert payload["sources"] == []
    assert payload["total"] == 0


def test_citations_reads_app_output(tmp_path, monkeypatch):
    session_dir = APP_ROOT / "output" / "session_trace_cite_ok"
    session_dir.mkdir(parents=True, exist_ok=True)
    evidence = session_dir / "evidence.json"
    evidence.write_text(
        json.dumps({"sources": [{"source_id": "s1", "locator": "https://example.com"}], "generated_at": "now"}),
        encoding="utf-8",
    )
    try:
        payload = get_citations("trace_cite_ok")
        assert payload["total"] == 1
        assert payload["sources"][0]["source_id"] == "s1"
    finally:
        evidence.unlink(missing_ok=True)
        try:
            session_dir.rmdir()
        except OSError:
            pass


def test_jsonl_skips_bad_lines(tmp_path, monkeypatch):
    from app.api import trace_routes

    log_dir = tmp_path / "traces"
    log_dir.mkdir()
    (log_dir / "sess.jsonl").write_text(
        '{"type":"run.started","span_id":"a"}\nnot-json\n{"type":"run.completed","span_id":"a"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(trace_routes, "traces_log_dir", lambda: log_dir)
    payload = get_jsonl_trace("sess")
    assert payload["total"] == 2
    assert payload["tree"]["span_count"] == 1


def test_langfuse_unconfigured_returns_local_tree():
    config = langfuse_viewer_config()
    payload = get_langfuse_traces("no-such-session")
    assert payload["session_id"] == "no-such-session"
    assert payload["traces"] == []
    if not config["enabled"]:
        assert payload["enabled"] is False
        assert "未配置" in payload["message"]
        assert _check_langfuse() == "disabled"


if __name__ == "__main__":
    test_citations_missing_file_is_empty_not_500()
    test_langfuse_unconfigured_returns_local_tree()
    print("[OK] trace routes")
