"""Untrusted external content boundary for research artifacts."""

from __future__ import annotations

from html import unescape
import json
import re
from typing import Any

_BLOCK_TAGS = re.compile(
    r"<(script|style|noscript|iframe|object|embed)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_INSTRUCTION_LINES = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"
    r"|disregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"
    r"|system\s+prompt\s*:"
    r"|you\s+are\s+now"
    r"|assistant\s*:"
    r"|execute\s+the\s+following"
    r"|call\s+the\s+following\s+tool)",
    re.IGNORECASE,
)


def sanitize_untrusted_content(content: str, *, max_chars: int = 1200) -> str:
    text = _BLOCK_TAGS.sub(" ", content or "")
    text = unescape(_TAG.sub("\n", text))
    lines = []
    for line in text.splitlines():
        cleaned = _WHITESPACE.sub(" ", line).strip()
        if not cleaned or _INSTRUCTION_LINES.search(cleaned):
            continue
        lines.append(cleaned)
    return _WHITESPACE.sub(" ", " ".join(lines)).strip()[:max_chars]


def structured_evidence_from_artifact(artifact: Any) -> dict[str, Any]:
    artifact_id = str(getattr(artifact, "artifact_id", "") or "")
    locator = str(getattr(artifact, "locator", "") or "")
    title = str(getattr(artifact, "title", "") or locator[:100])
    full_content = str(getattr(artifact, "content", "") or "")
    summary_content = str(getattr(artifact, "summary", "") or "")
    raw_content = full_content or summary_content
    fragments: list[str] = []
    titles: list[str] = []

    def collect(value: Any) -> None:
        if len(fragments) >= 4:
            return
        if isinstance(value, list):
            for item in value:
                collect(item)
            return
        if not isinstance(value, dict):
            return
        title = str(value.get("title") or value.get("name") or "").strip()
        content = str(
            value.get("content")
            or value.get("snippet")
            or value.get("summary")
            or ""
        ).strip()
        if title or content:
            if title:
                titles.append(title[:200])
            fragments.append(" — ".join(part for part in (title, content) if part))
        for key in ("results", "items", "data", "documents"):
            collect(value.get(key))

    metadata = getattr(artifact, "metadata", {}) or {}
    structured_candidates = [
        dict(item)
        for item in metadata.get("candidates") or []
        if isinstance(item, dict)
    ] if isinstance(metadata, dict) else []
    parsed: Any = None
    try:
        parsed = json.loads(raw_content)
        collect(parsed)
        if isinstance(parsed, dict) and isinstance(parsed.get("candidates"), list):
            structured_candidates = [dict(item) for item in parsed["candidates"] if isinstance(item, dict)]
    except (TypeError, ValueError, json.JSONDecodeError):
        fragments = []
    if not fragments and summary_content and summary_content != raw_content:
        try:
            collect(json.loads(summary_content))
        except (TypeError, ValueError, json.JSONDecodeError):
            fragments = []

    summary = (
        "\n".join(
            sanitize_untrusted_content(fragment, max_chars=320)
            for fragment in fragments
            if sanitize_untrusted_content(fragment, max_chars=320)
        )[:1200]
        if fragments
        else sanitize_untrusted_content(raw_content)
    )
    evidence_facts = titles or ([title[:200]] if title else [])
    return {
        "evidence_id": f"candidate_{artifact_id}",
        "artifact_id": artifact_id,
        "locator": locator,
        "title": title[:200],
        "excerpt": summary,
        "facts": evidence_facts,
        "candidates": structured_candidates,
        "trust": "external_extracted",
        "instruction_free": True,
    }
