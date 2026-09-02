"""Deterministic search/fetch fixture: no live web."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.tools.eval_fixture import fetch_fixture, reset_fixture_cache, search_fixture
from app.tools.fetch_url import fetch_url_content
from app.tools.tavily_core import search_internet


def setup_function():
    os.environ["HARNESS_EVAL_FIXTURE"] = "1"
    os.environ.pop("BROWSECOMP_PLUS_ENABLED", None)
    reset_fixture_cache()


def teardown_function():
    os.environ.pop("HARNESS_EVAL_FIXTURE", None)
    reset_fixture_cache()


def test_search_fixture_ranks_tesla():
    setup_function()
    try:
        payload = search_fixture("Tesla 2026 收入 量产")
        assert payload["provider"] == "harness-eval-fixture"
        urls = [item["url"] for item in payload["results"]]
        assert "https://fixture.local/tesla-2026" in urls
        print("[OK] fixture search")
    finally:
        teardown_function()


def test_search_internet_uses_fixture_not_tavily():
    setup_function()
    try:
        payload = search_internet("Figure BMW 试点")
        assert payload["provider"] == "harness-eval-fixture"
        print("[OK] search_internet fixture")
    finally:
        teardown_function()


def test_fetch_known_and_unknown_urls():
    setup_function()
    try:
        known = fetch_fixture("https://fixture.local/unitree-revenue-a")
        assert known["ok"] is True
        assert "10 亿" in known["text"]
        missing = fetch_fixture("https://example.com/not-in-corpus")
        assert missing["ok"] is False
        assert missing["error"] == "not_in_fixture_corpus"
        live = fetch_url_content("https://example.com/not-in-corpus")
        assert live["ok"] is False
        print("[OK] fixture fetch isolation")
    finally:
        teardown_function()


if __name__ == "__main__":
    test_search_fixture_ranks_tesla()
    test_search_internet_uses_fixture_not_tavily()
    test_fetch_known_and_unknown_urls()
    print("=== eval fixture tests passed ===")
