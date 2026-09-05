"""Untrusted external content boundary for research artifacts."""

from __future__ import annotations

from html import unescape
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
    summary = sanitize_untrusted_content(
        str(getattr(artifact, "summary", "") or getattr(artifact, "content", "") or "")
    )
    return {
        "evidence_id": f"candidate_{artifact_id}",
        "artifact_id": artifact_id,
        "locator": locator,
        "title": title[:200],
        "excerpt": summary,
        "trust": "external_extracted",
        "instruction_free": True,
    }
