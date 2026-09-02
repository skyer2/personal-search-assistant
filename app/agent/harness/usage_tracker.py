"""
【Phase 17】LLM 真实 token / 成本追踪

通过 LangChain Callback 捕获每次 LLM 调用的 prompt/completion token，
按 Harness phase 聚合，并写入 JSONL trace，供离线成本分析。
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler

from app.config.loader import get_harness_config

_current_phase: ContextVar[str] = ContextVar("harness_llm_phase", default="unknown")
_current_session: ContextVar[str] = ContextVar("harness_llm_session", default="unknown")


def set_llm_phase(phase: str) -> None:
    _current_phase.set(phase)


def get_llm_phase() -> str:
    return _current_phase.get()


def set_llm_session(session_id: str) -> None:
    _current_session.set(session_id)


def get_llm_session() -> str:
    return _current_session.get()


# 默认定价（USD / 1M tokens），可被 env 覆盖
DEFAULT_PRICING = {
    "qwen-max": {"input": 1.6, "output": 6.4},
    "qwen-turbo": {"input": 0.3, "output": 0.6},
    "glm-5.2": {"input": 0.8, "output": 2.0},
    "glm-4": {"input": 0.5, "output": 1.5},
    "default": {"input": 1.0, "output": 2.0},
}


def _load_pricing() -> dict[str, dict[str, float]]:
    raw = os.getenv("HARNESS_LLM_PRICING_JSON", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {**DEFAULT_PRICING, **data}
        except json.JSONDecodeError:
            pass
    return DEFAULT_PRICING


def get_pricing(model_name: str) -> dict[str, float]:
    pricing = _load_pricing()
    normalized = (model_name or "").lower()
    exact = pricing.get(model_name) or pricing.get(normalized)
    if exact:
        return exact
    # 网关经常返回带日期/供应商前缀的模型名，例如 qwen-max-2025-01-25。
    candidates = [
        (key, value)
        for key, value in pricing.items()
        if key != "default" and key.lower() in normalized
    ]
    if candidates:
        return max(candidates, key=lambda item: len(item[0]))[1]
    return pricing.get("default") or {"input": 1.0, "output": 2.0}


def _extract_cached_tokens(usage: dict[str, Any]) -> int:
    """OpenAI prompt_tokens_details.cached_tokens / 智谱 cached_tokens。"""
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        try:
            return int(details.get("cached_tokens") or 0)
        except (TypeError, ValueError):
            return 0
    for key in ("cached_tokens", "cache_read_tokens", "prompt_cache_hit_tokens"):
        if usage.get(key) is not None:
            try:
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def estimate_cost_usd(
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    price = get_pricing(model_name)
    return round(
        prompt_tokens / 1_000_000 * price["input"]
        + completion_tokens / 1_000_000 * price["output"],
        6,
    )


@dataclass
class LLMCallRecord:
    session_id: str
    phase: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    run_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UsageTracker:
    """进程内聚合 + JSONL 落盘。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, list[LLMCallRecord]] = {}
        self._log_dir: Optional[Path] = None

    def _ensure_log_dir(self) -> Optional[Path]:
        if self._log_dir is not None:
            return self._log_dir
        cfg = get_harness_config()
        if not cfg.jsonl_log_enabled:
            return None
        root = Path(__file__).resolve().parents[2]
        self._log_dir = root / cfg.jsonl_log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        return self._log_dir

    def record(self, rec: LLMCallRecord) -> None:
        with self._lock:
            self._by_session.setdefault(rec.session_id, []).append(rec)
        try:
            from app.observability import get_recorder

            recorder = get_recorder()
            if recorder.is_active:
                extra = dict(rec.extra or {})
                recorder.record_generation(
                    model=rec.model,
                    phase=rec.phase,
                    prompt_tokens=rec.prompt_tokens,
                    completion_tokens=rec.completion_tokens,
                    total_tokens=rec.total_tokens,
                    cache_read_tokens=rec.cache_read_tokens,
                    cost_usd=rec.cost_usd,
                    duration_ms=int(extra.pop("duration_ms", 0) or 0) or None,
                    finish_reason=str(extra.pop("finish_reason", "") or ""),
                    usage_missing=bool(extra.get("usage_missing")),
                    extra=extra,
                )
                return
        except Exception:
            pass
        log_dir = self._ensure_log_dir()
        if log_dir is None:
            return
        path = log_dir / f"{rec.session_id}.jsonl"
        line = json.dumps(
            {"event": "llm_usage", **rec.to_dict()},
            ensure_ascii=False,
        )
        with self._lock:
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")

    def session_summary(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            records = list(self._by_session.get(session_id, []))
        by_phase: dict[str, dict[str, Any]] = {}
        total = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.0,
            "calls": 0,
            "missing_usage_calls": 0,
            "cache_hit_ratio": 0.0,
        }
        for rec in records:
            bucket = by_phase.setdefault(
                rec.phase,
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cache_read_tokens": 0,
                    "cost_usd": 0.0,
                    "calls": 0,
                    "missing_usage_calls": 0,
                },
            )
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cost_usd",
            ):
                bucket[key] += getattr(rec, key)
                total[key] += getattr(rec, key)
            bucket["calls"] += 1
            total["calls"] += 1
            if rec.extra.get("usage_missing"):
                bucket["missing_usage_calls"] += 1
                total["missing_usage_calls"] += 1
        prompt = int(total["prompt_tokens"] or 0)
        cached = int(total["cache_read_tokens"] or 0)
        total["cache_hit_ratio"] = round(cached / prompt, 4) if prompt else 0.0
        return {
            "session_id": session_id,
            "by_phase": by_phase,
            "total": total,
            "records": [r.to_dict() for r in records],
        }

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._by_session.pop(session_id, None)


_tracker: UsageTracker | None = None


def get_usage_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker


class UsageTrackingCallback(BaseCallbackHandler):
    """
    LangChain BaseCallbackHandler 兼容实现。
    在 on_llm_end 时从 response.llm_output / usage_metadata 提取 token。
    """

    def __init__(self, session_id: str = "", phase: str = "") -> None:
        # 空值表示在 callback 触发时读取 ContextVar，而不是构造时冻结。
        # 这对同一个模型实例被 Planner/Compressor/Memory 并发复用很重要。
        self.session_id = session_id
        self.phase = phase
        self._starts: dict[str, float] = {}

    def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id") or "default")
        self._starts[run_id] = time.perf_counter()

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            self._handle_llm_end(response, **kwargs)
        except Exception as exc:
            print(f"[UsageTracker] on_llm_end failed: {exc}")

    def _handle_llm_end(self, response: Any, **kwargs: Any) -> None:
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        cache_read_tokens = 0
        model_name = "unknown"

        llm_output = getattr(response, "llm_output", None) or {}
        if isinstance(llm_output, dict):
            model_name = llm_output.get("model_name") or llm_output.get("model") or model_name
            token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
            if isinstance(token_usage, dict):
                prompt_tokens = int(
                    token_usage.get("prompt_tokens")
                    or token_usage.get("input_tokens")
                    or 0
                )
                completion_tokens = int(
                    token_usage.get("completion_tokens")
                    or token_usage.get("output_tokens")
                    or 0
                )
                total_tokens = int(token_usage.get("total_tokens") or 0)
                cache_read_tokens = _extract_cached_tokens(token_usage)

        # OpenAI 兼容层有时把 usage 放在 generations[0].message.usage_metadata
        if total_tokens == 0:
            generations = getattr(response, "generations", None) or []
            for gen_list in generations:
                for gen in gen_list or []:
                    msg = getattr(gen, "message", None)
                    usage = getattr(msg, "usage_metadata", None) if msg else None
                    if isinstance(usage, dict):
                        prompt_tokens = int(usage.get("input_tokens") or 0)
                        completion_tokens = int(usage.get("output_tokens") or 0)
                        total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
                        model_name = getattr(msg, "response_metadata", {}).get("model_name") or model_name
                        break
                if total_tokens:
                    break

        if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
            total_tokens = prompt_tokens + completion_tokens

        if total_tokens <= 0 and prompt_tokens <= 0:
            # 某些 OpenAI 兼容网关不返回 usage。保留零 token 记录，
            # 让 Benchmark 报告能显式暴露“缺失计量”，避免低估成本。
            usage_missing = True
        else:
            usage_missing = False

        phase = self.phase or get_llm_phase()
        session_id = self.session_id or get_llm_session()
        cost = estimate_cost_usd(model_name, prompt_tokens, completion_tokens)
        started = self._starts.pop(str(kwargs.get("run_id") or "default"), None)
        duration_ms = int((time.perf_counter() - started) * 1000) if started is not None else None
        finish_reason = ""
        if isinstance(llm_output, dict):
            finish_reason = str(llm_output.get("finish_reason") or "")
        rec = LLMCallRecord(
            session_id=session_id,
            phase=phase,
            model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost,
            run_id=str(kwargs.get("run_id") or ""),
            extra={
                "usage_missing": usage_missing,
                "duration_ms": duration_ms,
                "finish_reason": finish_reason,
            },
        )
        get_usage_tracker().record(rec)


def build_usage_callback(session_id: str, phase: str = "") -> Optional[UsageTrackingCallback]:
    """供 build_run_config 追加到 callbacks。"""
    cfg = get_harness_config()
    if not cfg.usage_tracking_enabled:
        return None
    return UsageTrackingCallback(session_id=session_id, phase=phase)


async def tracked_ainvoke(
    model: Any,
    input_value: Any,
    *,
    session_id: str = "",
    phase: str = "",
    config: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> Any:
    """为不经过 Agent/LangGraph 的直接模型调用补齐 usage callback。

    Planner、Compressor 和 MemoryExtractor 都直接调用 ``model.ainvoke``；
    这些调用不会继承主 Agent 的 callbacks，因此必须在调用点显式注入。
    ContextVar token 在 finally 中恢复，保证 asyncio 并发任务互不污染。
    """
    resolved_session = session_id or get_llm_session()
    resolved_phase = phase or get_llm_phase()
    session_token = _current_session.set(resolved_session)
    phase_token = _current_phase.set(resolved_phase)
    try:
        run_config = dict(config or {})
        callbacks = list(run_config.get("callbacks") or [])
        callback = build_usage_callback(resolved_session, resolved_phase)
        if callback is not None:
            callbacks.append(callback)
            run_config["callbacks"] = callbacks
        return await model.ainvoke(input_value, config=run_config, **kwargs)
    finally:
        _current_phase.reset(phase_token)
        _current_session.reset(session_token)
