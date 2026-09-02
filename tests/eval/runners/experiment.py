"""L4 Ablation：Vanilla / No-Replan / Full，固定模型与语料只改一个变量。"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
VARIANTS = Path(__file__).resolve().parents[1] / "variants"

VARIANT_PRESETS: dict[str, dict[str, Any]] = {
    "vanilla": {"mode": "direct", "max_replan": 0, "progress_eval": False, "parallel": False},
    "no_replan": {"mode": "agent", "max_replan": 0, "progress_eval": True, "parallel": True},
    "full": {"mode": "agent", "max_replan": 2, "progress_eval": True, "parallel": True},
}


def load_variant(name: str) -> dict[str, Any]:
    key = str(name or "full").strip().lower()
    aliases = {"v0": "vanilla", "v1": "no_replan", "v2": "full", "full_harness": "full"}
    key = aliases.get(key, key)
    path = VARIANTS / f"{key}.yml"
    preset = dict(VARIANT_PRESETS.get(key) or VARIANT_PRESETS["full"])
    preset["name"] = key
    if path.exists():
        try:
            import yaml

            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                preset.update({k: v for k, v in loaded.items() if v is not None})
        except Exception:
            pass
    return preset


def apply_variant(config: Any, variant: dict[str, Any]) -> dict[str, Any]:
    """临时改 HarnessConfig；返回旧值以便还原。"""
    previous = {
        "max_replan_count": getattr(config, "max_replan_count", 2),
        "progress_eval_enabled": getattr(config, "progress_eval_enabled", True),
        "parallel_retrieval_enabled": getattr(config, "parallel_retrieval_enabled", True),
    }
    if hasattr(config, "max_replan_count"):
        config.max_replan_count = int(variant.get("max_replan", previous["max_replan_count"]))
    if hasattr(config, "progress_eval_enabled"):
        config.progress_eval_enabled = bool(variant.get("progress_eval", previous["progress_eval_enabled"]))
    if hasattr(config, "parallel_retrieval_enabled"):
        config.parallel_retrieval_enabled = bool(variant.get("parallel", previous["parallel_retrieval_enabled"]))
    os.environ["HARNESS_EVAL_VARIANT"] = str(variant.get("name") or "full")
    return previous


def restore_variant(config: Any, previous: dict[str, Any]) -> None:
    for key, value in previous.items():
        if hasattr(config, key):
            setattr(config, key, value)


def git_sha() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                timeout=2,
                stderr=subprocess.DEVNULL,
            )
            .decode("utf-8")
            .strip()
        )
    except Exception:
        return ""


def config_hash(variant: dict[str, Any]) -> str:
    raw = f"{variant.get('name')}|{variant.get('mode')}|{variant.get('max_replan')}|{variant.get('progress_eval')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def experiment_manifest(
    *,
    dataset: str,
    variant: dict[str, Any],
    repeat: int = 1,
    model: str = "",
) -> dict[str, Any]:
    return {
        "git_sha": git_sha(),
        "dataset": dataset,
        "model": model or os.getenv("OPENAI_MODEL") or os.getenv("HARNESS_MODEL") or "",
        "search_backend": os.getenv("BROWSECOMP_PLUS_ENABLED", "live"),
        "temperature": 0,
        "config_hash": config_hash(variant),
        "variant": variant.get("name"),
        "repeat": int(repeat),
    }
