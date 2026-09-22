"""Canonical source formatting for the delivery model."""

from __future__ import annotations

from urllib.parse import urlparse
from typing import Any

from app.research.delivery.view_model import ReferenceView


def reference_views(records: list[dict[str, Any]]) -> tuple[ReferenceView, ...]:
    unique: dict[str, ReferenceView] = {}
    for row in records:
        evidence_id = str(row.get("evidence_id") or "").strip()
        source_id = str(row.get("source_id") or evidence_id).strip()
        locator = str(row.get("locator") or "").strip()
        if not evidence_id or not locator:
            continue
        parsed = urlparse(locator)
        title = str(row.get("title") or parsed.netloc or source_id).strip()
        if not title or title.casefold() in {"unknown", "来源"}:
            title = parsed.netloc or "可验证来源"
        # Delivery cites canonical sources, not internal evidence aliases.
        unique.setdefault(source_id, ReferenceView(source_id, title[:160], locator))
    return tuple(unique.values())


__all__ = ["reference_views"]
