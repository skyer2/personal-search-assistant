"""fetch_url：搜索卡片之后按需拉正文进 Artifact。"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from langchain_core.tools import tool

from app.agent.harness.step_budget import consume_retrieval_or_block

Fetcher = Callable[[str, float], tuple[str, str]]

_DEFAULT_HEADERS = {
    "User-Agent": "PersonalSearchAssistant/1.0 (+https://localhost)",
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
}
_SCRIPT_RE = re.compile(r"(?is)<script[^>]*>.*?</script>")
_STYLE_RE = re.compile(r"(?is)<style[^>]*>.*?</style>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


def strip_html(raw: str) -> str:
    text = _SCRIPT_RE.sub(" ", raw or "")
    text = _STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _default_fetch(url: str, timeout: float) -> tuple[str, str]:
    req = Request(url, headers=_DEFAULT_HEADERS, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        body = resp.read()
        content_type = str(resp.headers.get("Content-Type") or "text/html")
    try:
        decoded = body.decode(charset, errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")
    return decoded, content_type


def fetch_url_content(
    url: str,
    *,
    max_chars: int = 8000,
    timeout: float = 12.0,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return {"ok": False, "error": "invalid_url", "url": target}

    active_fetcher = fetcher
    if active_fetcher is None:
        from app.tools.eval_fixture import fetch_fixture, fixture_enabled

        if fixture_enabled():
            def _fixture_fetcher(request_url: str, _timeout: float) -> tuple[str, str]:
                payload = fetch_fixture(request_url, max_chars=max_chars)
                if not payload.get("ok"):
                    raise ValueError(str(payload.get("error") or "not_in_fixture_corpus"))
                return str(payload.get("text") or ""), "text/plain"

            active_fetcher = _fixture_fetcher

    try:
        raw, content_type = (active_fetcher or _default_fetch)(target, timeout)
    except HTTPError as exc:
        return {"ok": False, "error": f"http_{exc.code}", "url": target}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:200], "url": target}

    title_m = _TITLE_RE.search(raw or "")
    title = strip_html(title_m.group(1)) if title_m else ""
    text = strip_html(raw) if "html" in (content_type or "").lower() or "<html" in (raw[:200] or "").lower() else (raw or "")
    text = text[: max(200, int(max_chars or 8000))]
    if not text:
        return {"ok": False, "error": "empty_body", "url": target}

    artifact_id = ""
    try:
        from app.agent.harness.artifacts import get_artifact_store

        store = get_artifact_store()
        art = store.put(
            text,
            kind="web",
            locator=target,
            title=title or target,
            summary=text[:280],
            metadata={"tool_name": "fetch_url", "content_type": content_type},
            step_type="network_search",
        )
        artifact_id = art.artifact_id
        try:
            from app.agent.harness.evidence_store import get_evidence_store

            get_evidence_store().add_span(
                text[:1200],
                artifact_id=artifact_id,
                locator=target,
                source_kind="url",
                step_type="network_search",
            )
        except Exception:
            pass
    except Exception:
        artifact_id = ""

    return {
        "ok": True,
        "url": target,
        "title": title or target,
        "snippet": text[:280],
        "char_count": len(text),
        "artifact_id": artifact_id,
        "hint": "正文已进 Artifact；需要更多原文时 read_artifact(artifact_id)。",
    }


@tool
def fetch_url(url: str, max_chars: int = 8000) -> dict[str, Any]:
    """按 URL 拉取网页正文并外置为 Artifact。搜索卡片不够用时再调用。"""
    blocked = consume_retrieval_or_block("fetch_url")
    if blocked:
        return {"ok": False, "error": "step_retrieval_budget", "message": blocked}
    return fetch_url_content(url, max_chars=max_chars)
