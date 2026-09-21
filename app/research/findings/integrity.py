"""Reject snippets that are visibly incomplete before they become findings."""

from __future__ import annotations

import re


_DANGLING = re.compile(r"(?:超过|约|近|达|为|有|增长|降至)\s*\d+(?:\.\d+)?$", re.I)
_CONNECTIVE = ("但", "但是", "然而", "因为", "以及", "其中", "and", "but", "because", "which")


def complete_sentence(text: str) -> tuple[bool, str]:
    value = str(text or "").strip()
    if not value:
        return False, "empty_claim"
    # Search snippets often stop at a Chinese semicolon.  It may be a valid
    # clause in prose, but it is not a safely quotable standalone finding.
    if value.endswith(("…", "...", ",", "，", "、", ";", "；", ":", "：", "-", "—")):
        return False, "truncated_terminal"
    if _DANGLING.search(value):
        return False, "dangling_number"
    lower = value.casefold()
    if any(lower.endswith(token) for token in _CONNECTIVE):
        return False, "dangling_connective"
    pairs = (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】"), ('"', '"'), ("“", "”"))
    for left, right in pairs:
        if left == right:
            if value.count(left) % 2:
                return False, "unclosed_quote"
        elif value.count(left) != value.count(right):
            return False, "unbalanced_delimiter"
    return True, ""


__all__ = ["complete_sentence"]
