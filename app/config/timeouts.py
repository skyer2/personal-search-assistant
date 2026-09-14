"""Shared LLM timeout resolution.

``LLM_TIMEOUT_SEC`` is the default model-call timeout. A stage may replace it
only through an explicit stage-specific environment variable.
"""

from __future__ import annotations

import os


DEFAULT_LLM_TIMEOUT_SEC = 120.0


def _read_seconds(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def llm_timeout_sec() -> float:
    return _read_seconds("LLM_TIMEOUT_SEC", DEFAULT_LLM_TIMEOUT_SEC)


def model_timeout_sec(override_env: str) -> float:
    return _read_seconds(override_env, llm_timeout_sec())


def wall_timeout_sec(
    override_env: str,
    configured_default_sec: float,
    *,
    model_margin_sec: float = 0.0,
) -> float:
    override = os.getenv(override_env)
    if override is not None and override.strip():
        return _read_seconds(override_env, configured_default_sec)
    return max(configured_default_sec, llm_timeout_sec() + model_margin_sec)
