"""L3 BrowseComp-Plus：薄封装，正式 runner 仍是 run_browsecomp_plus.py。"""

from __future__ import annotations

from pathlib import Path

from tests.eval.runners.experiment import VARIANT_PRESETS

BROWSECOMP_VARIANTS = ("retrieval", "vanilla", "no_replan", "full")


def recommended_ablation() -> list[str]:
    return [
        "retrieval-only",
        "vanilla",
        "no_replan",
        "full",
    ]


def variant_help() -> str:
    _ = Path
    lines = ["BrowseComp-Plus 对照应固定 manifest / 模型 / 语料，只改 Harness："]
    for name in recommended_ablation():
        preset = VARIANT_PRESETS.get(name, {})
        lines.append(f"- {name}: {preset or 'retriever only, no agent'}")
    return "\n".join(lines)
