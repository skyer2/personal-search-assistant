"""Canonical filesystem roots for observability exporters."""

from __future__ import annotations

from pathlib import Path

# app/observability/paths.py → app/
APP_ROOT = Path(__file__).resolve().parents[1]
# repository root (git sha, docs)
REPO_ROOT = Path(__file__).resolve().parents[2]


def traces_log_dir() -> Path:
    """JSONL journal lives under the runtime root (`app/logs/traces`), matching AgentHarness."""
    from app.config.loader import get_harness_config

    return APP_ROOT / get_harness_config().jsonl_log_dir
