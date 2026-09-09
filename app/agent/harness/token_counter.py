"""
Model-aware token 预算。

默认按 GLM-5.2 计：中文接近 1 字 1 token，英文仍约 4 字符 1 token。
`len(text)//4` 会把中文预算低估 3~4 倍，不能再当生产口径。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping

# CJK 统一汉字 + 扩展 + 全角标点/假名/韩文
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002b73f"
    r"\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af"
    r"\uff01-\uff60\uffe0-\uffe6]"
)

DEFAULT_MODEL = "glm-5.2"

# GLM-5.2 / GLM-4 公开窗口：按 128k 计，可用配置覆盖。
MODEL_WINDOWS: dict[str, int] = {
    "glm-5.2": 128_000,
    "glm5.2": 128_000,
    "glm-5": 128_000,
    "glm-4": 128_000,
    "glm-4-plus": 128_000,
    "glm-4-long": 1_000_000,
    "qwen-max": 32_000,
    "qwen-turbo": 128_000,
    "qwen-plus": 128_000,
    "gpt-4o": 128_000,
    "gpt-4.1": 1_000_000,
    "claude-3-5": 200_000,
}

DEFAULT_STAGE_BUDGETS: dict[str, int] = {
    "planner": 8_000,
    "researcher": 16_000,
    "synthesis": 40_000,
    "verifier": 12_000,
    "compress": 8_000,
}

SYNTHESIS_STAGES = frozenset({"generate_markdown", "summarize", "convert_pdf"})
PLANNER_STAGES = frozenset({"understand", "plan", "planner", "supervisor"})
VERIFIER_STAGES = frozenset({"validate", "verify", "citation_verify"})
COMPRESS_STAGES = frozenset({"compress"})


def normalize_model_name(model: str | None) -> str:
    raw = (model or "").strip().lower()
    if not raw:
        return DEFAULT_MODEL
    raw = raw.replace("_", "-")
    aliases = {
        "glm5.2": "glm-5.2",
        "glm52": "glm-5.2",
        "glm-5.2-plus": "glm-5.2",
        "glm-4.5": "glm-4",
        "chatglm": "glm-4",
    }
    if raw in aliases:
        return aliases[raw]
    if raw.startswith("glm-5"):
        return "glm-5.2"
    if raw.startswith("glm"):
        return "glm-5.2" if "5" in raw else "glm-4"
    return raw


def is_glm_family(model: str | None) -> bool:
    name = normalize_model_name(model)
    return name.startswith("glm")


def _count_cjk(text: str) -> int:
    return len(_CJK_RE.findall(text or ""))


def estimate_glm_tokens(text: str) -> int:
    """GLM-4/5 家族启发式：汉字≈1 token，ASCII BPE≈4 字符 1 token。"""
    if not text:
        return 0
    cjk = _count_cjk(text)
    ascii_len = max(0, len(text) - cjk)
    # 智谱公开口径：中文约 1 字 1 token；英文/代码接近 GPT BPE。
    ascii_tokens = (ascii_len + 3) // 4
    # JSON 括号、URL、数字略密；给 4% 余量避免低估。
    total = cjk + ascii_tokens
    if "{" in text or "http" in text or "<" in text:
        total = int(total * 1.04)
    return max(1, total)


def estimate_qwen_tokens(text: str) -> int:
    """Qwen tokenizer 对中文略密于 GLM，约 1 字 1.3 token。"""
    if not text:
        return 0
    cjk = _count_cjk(text)
    ascii_len = max(0, len(text) - cjk)
    return max(1, int(cjk * 1.3 + (ascii_len + 3) // 4))


def estimate_cl100k_tokens(text: str) -> int:
    """无 tiktoken 时的 cl100k 近似：中文约 1.5 token/字。"""
    if not text:
        return 0
    cjk = _count_cjk(text)
    ascii_len = max(0, len(text) - cjk)
    return max(1, int(cjk * 1.5 + (ascii_len + 3) // 4))


def _try_tiktoken(model: str) -> Any | None:
    try:
        import tiktoken  # type: ignore
    except Exception:
        return None
    encoding_name = "cl100k_base"
    lowered = (model or "").lower()
    if "gpt-4o" in lowered or "gpt-4.1" in lowered or "o1" in lowered or "o3" in lowered:
        encoding_name = "o200k_base"
    try:
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return None


@dataclass
class TokenBudgetPlan:
    model: str
    context_window: int
    tool_schema_tokens: int
    reserved_output_tokens: int
    safety_margin: int
    available_dynamic: int
    stage_budgets: dict[str, int] = field(default_factory=dict)
    tokenizer: str = "glm-cjk"

    def budget_for(self, stage: str, fallback: int | None = None) -> int:
        stage_key = (stage or "").strip().lower()
        if stage_key in self.stage_budgets:
            return min(self.stage_budgets[stage_key], self.available_dynamic)
        if fallback is not None:
            return min(int(fallback), self.available_dynamic)
        return min(self.stage_budgets.get("researcher", 16_000), self.available_dynamic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "context_window": self.context_window,
            "tool_schema_tokens": self.tool_schema_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "safety_margin": self.safety_margin,
            "available_dynamic": self.available_dynamic,
            "stage_budgets": dict(self.stage_budgets),
            "tokenizer": self.tokenizer,
        }


class TokenCounter:
    """按模型族选择 tokenizer；生产默认 glm-5.2。"""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        context_window: int | None = None,
        tool_schema_tokens: int = 4_000,
        reserved_output_tokens: int = 8_000,
        safety_margin: int = 2_000,
        stage_budgets: Mapping[str, int] | None = None,
    ):
        self.model = normalize_model_name(model)
        self.context_window = int(
            context_window
            or MODEL_WINDOWS.get(self.model)
            or MODEL_WINDOWS.get(self.model.split("-")[0], 128_000)
        )
        self.tool_schema_tokens = max(0, int(tool_schema_tokens))
        self.reserved_output_tokens = max(0, int(reserved_output_tokens))
        self.safety_margin = max(0, int(safety_margin))
        budgets = dict(DEFAULT_STAGE_BUDGETS)
        if stage_budgets:
            budgets.update({str(k): int(v) for k, v in stage_budgets.items()})
        self.stage_budgets = budgets
        self._tiktoken = None
        self.tokenizer_name = self._select_backend()

    def _select_backend(self) -> str:
        if is_glm_family(self.model):
            return "glm-cjk"
        if self.model.startswith("qwen"):
            return "qwen-cjk"
        self._tiktoken = _try_tiktoken(self.model)
        if self._tiktoken is not None:
            return "tiktoken"
        return "cl100k-approx"

    @classmethod
    def from_config(cls, config: Any | None = None) -> "TokenCounter":
        if config is None:
            try:
                from app.config.loader import get_harness_config

                config = get_harness_config()
            except Exception:
                config = None
        model = (
            os.getenv("HARNESS_TOKEN_MODEL")
            or (getattr(config, "token_budget_model", None) if config else None)
            or os.getenv("LLM_QWEN_MAX")
            or DEFAULT_MODEL
        )
        stages = getattr(config, "token_stage_budgets", None) if config else None
        return cls(
            model=str(model or DEFAULT_MODEL),
            context_window=getattr(config, "token_context_window", None) if config else None,
            tool_schema_tokens=int(getattr(config, "token_tool_schema_tokens", 4_000) or 4_000)
            if config
            else 4_000,
            reserved_output_tokens=int(
                getattr(config, "token_reserved_output_tokens", 8_000) or 8_000
            )
            if config
            else 8_000,
            safety_margin=int(getattr(config, "token_safety_margin", 2_000) or 2_000)
            if config
            else 2_000,
            stage_budgets=stages,
        )

    def count(self, text: str | None) -> int:
        raw = text or ""
        if not raw:
            return 0
        if self.tokenizer_name == "glm-cjk":
            return estimate_glm_tokens(raw)
        if self.tokenizer_name == "qwen-cjk":
            return estimate_qwen_tokens(raw)
        if self.tokenizer_name == "tiktoken" and self._tiktoken is not None:
            try:
                return max(1, len(self._tiktoken.encode(raw)))
            except Exception:
                return estimate_cl100k_tokens(raw)
        return estimate_cl100k_tokens(raw)

    def plan(self) -> TokenBudgetPlan:
        used = (
            self.tool_schema_tokens
            + self.reserved_output_tokens
            + self.safety_margin
        )
        available = max(1_000, self.context_window - used)
        capped = {
            name: min(int(value), available) for name, value in self.stage_budgets.items()
        }
        return TokenBudgetPlan(
            model=self.model,
            context_window=self.context_window,
            tool_schema_tokens=self.tool_schema_tokens,
            reserved_output_tokens=self.reserved_output_tokens,
            safety_margin=self.safety_margin,
            available_dynamic=available,
            stage_budgets=capped,
            tokenizer=self.tokenizer_name,
        )

    def budget_for_stage(self, stage: str, fallback: int | None = None) -> int:
        return self.plan().budget_for(stage, fallback=fallback)

    def budget_for_step_type(self, step_type: str, fallback: int | None = None) -> int:
        return self.budget_for_stage(stage_from_step_type(step_type), fallback=fallback)


def stage_from_step_type(step_type: str) -> str:
    kind = (step_type or "").strip().lower()
    if kind in SYNTHESIS_STAGES:
        return "synthesis"
    if kind in PLANNER_STAGES:
        return "planner"
    if kind in VERIFIER_STAGES:
        return "verifier"
    if kind in COMPRESS_STAGES:
        return "compress"
    return "researcher"


_COUNTER: TokenCounter | None = None


def get_token_counter() -> TokenCounter:
    global _COUNTER
    if _COUNTER is None:
        _COUNTER = TokenCounter.from_config()
    return _COUNTER


def reset_token_counter(counter: TokenCounter | None = None) -> TokenCounter:
    global _COUNTER
    _COUNTER = counter if counter is not None else TokenCounter.from_config()
    return _COUNTER


def estimate_tokens(text: str | None, *, model: str | None = None) -> int:
    """对外统一入口。未指定 model 时用全局（默认 glm-5.2）。"""
    if model:
        return TokenCounter(model).count(text or "")
    return get_token_counter().count(text or "")


@lru_cache(maxsize=8)
def cached_counter(model: str) -> TokenCounter:
    return TokenCounter(model)
