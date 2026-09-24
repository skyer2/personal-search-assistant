"""Timeout contracts shared by LLM stages."""

from __future__ import annotations

import pytest

from app.config.loader import reload_harness_config
from app.config.timeouts import llm_timeout_sec, model_timeout_sec


def test_model_timeout_defaults_to_global_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SEC", "180")
    monkeypatch.delenv("LLM_WORKER_TIMEOUT_SEC", raising=False)

    assert llm_timeout_sec() == 180.0
    assert model_timeout_sec("LLM_WORKER_TIMEOUT_SEC") == 180.0


def test_stage_wall_timeouts_inherit_global_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SEC", "180")
    monkeypatch.delenv("LLM_WORKER_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("HARNESS_STEP_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("HARNESS_SYNTHESIS_RETRY_TIMEOUT_SEC", raising=False)

    config = reload_harness_config()

    assert config.step_timeout_sec == 190
    assert config.synthesis_step_timeout_sec == 180
    assert config.synthesis_retry_timeout_sec == 180
    monkeypatch.undo()
    reload_harness_config()


def test_worker_wall_timeout_uses_worker_model_timeout_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SEC", "180")
    monkeypatch.setenv("LLM_WORKER_TIMEOUT_SEC", "60")
    monkeypatch.delenv("HARNESS_STEP_TIMEOUT_SEC", raising=False)

    config = reload_harness_config()

    assert model_timeout_sec("LLM_WORKER_TIMEOUT_SEC") == 60.0
    assert config.step_timeout_sec == 120
    monkeypatch.undo()
    reload_harness_config()


def test_explicit_stage_timeout_overrides_global_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SEC", "180")
    monkeypatch.setenv("LLM_WORKER_TIMEOUT_SEC", "60")
    monkeypatch.setenv("HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC", "45")
    monkeypatch.setenv("HARNESS_SYNTHESIS_RETRY_TIMEOUT_SEC", "35")

    config = reload_harness_config()

    assert model_timeout_sec("LLM_WORKER_TIMEOUT_SEC") == 60.0
    assert config.synthesis_step_timeout_sec == 45
    assert config.synthesis_retry_timeout_sec == 35
    monkeypatch.undo()
    reload_harness_config()
