"""
上下文压缩

Phase 2：优先使用 LLM 摘要压缩，失败或未配置模型时降级为规则截断。
Phase 19：按 step_type 分模板 + 压缩后 URL/数字保留检查，失败则打补丁。
Phase 23：可恢复压缩 — 先把全文写入 Artifact Store，摘要只带 ref；
长文按窗口压缩而不是只看前 12,000 字符；截断保头+尾+定位符。
"""

from __future__ import annotations

from typing import Any, Optional

from app.agent.harness.artifacts import ArtifactStore, get_artifact_store, infer_kind
from app.agent.harness.retention import apply_retention_patch, extract_numbers, extract_urls
from app.agent.harness.token_counter import estimate_tokens

COMPRESS_PROMPT = """请将以下 Agent 执行结果压缩为结构化摘要。

要求：
1. 保留关键事实、数据、来源信息（URL、表名、文件名必须保留）
2. 删除重复和无关内容
3. 使用中文，不超过 {max_chars} 字
4. 步骤类型：{step_type}
5. 【重要】保留原始 source 标记，如 [source:src-N] 或 URL 链接
6. 不要丢弃 artifact_id / evidence_id

原始内容：
{content}
"""

COMPRESS_PROMPT_BY_STEP = {
    "network_search": """请将以下网络检索结果压缩为结构化摘要。

要求：
1. 所有 URL 必须原样保留
2. 所有百分比、金额、年份等数字必须原样保留
3. 删除广告、导航、重复段落
4. 使用中文，不超过 {max_chars} 字
5. 步骤类型：{step_type}
6. 保留 artifact_id

原始内容：
{content}
""",
    "file_read": """请将以下文件读取结果压缩为结构化摘要。

要求：
1. 文件名必须保留
2. 关键数字、条款标题必须保留
3. 使用中文，不超过 {max_chars} 字
4. 步骤类型：{step_type}

原始内容：
{content}
""",
}

COMPRESS_THRESHOLD_CHARS = 2000
MAX_OUTPUT_CHARS = 2000
LLM_WINDOW_CHARS = 8000
LLM_WINDOW_OVERLAP = 400


class ContextCompressor:
    def __init__(
        self,
        model: Any = None,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        enabled: bool = True,
        threshold_chars: int = COMPRESS_THRESHOLD_CHARS,
        retention_check: bool = True,
        min_url_retention: float = 0.8,
        min_number_retention: float = 0.5,
        reversible: bool = True,
        window_chars: int = LLM_WINDOW_CHARS,
    ):
        self.model = model if enabled else None
        self.max_output_chars = max_output_chars
        self.threshold_chars = max(200, threshold_chars)
        self.retention_check = retention_check
        self.min_url_retention = min_url_retention
        self.min_number_retention = min_number_retention
        self.reversible = reversible
        self.window_chars = max(2000, window_chars)

    def estimate_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    def _prompt_for(self, step_type: str) -> str:
        return COMPRESS_PROMPT_BY_STEP.get(step_type, COMPRESS_PROMPT)

    async def compress(
        self,
        raw_result: str,
        step_type: str = "generic",
        source_metadata: dict | None = None,
        *,
        artifact_store: ArtifactStore | None = None,
        locator: str = "",
        title: str = "",
    ) -> tuple[str, dict]:
        """返回 (压缩后文本, 元数据)。摘要始终带 artifact_id，原文可回读。"""
        original_len = len(raw_result or "")
        meta: dict[str, Any] = {
            "method": "none",
            "step_type": step_type,
            "original_chars": original_len,
            "compressed_chars": original_len,
            "compression_ratio": 1.0,
            "entity_retention": 1.0,
            "retention_patched": False,
            "reversible": False,
            "artifact_id": "",
            "windows": 1,
        }
        if source_metadata:
            meta["source_metadata"] = source_metadata

        artifact_id = ""
        if self.reversible and original_len:
            store = artifact_store or get_artifact_store()
            existing = (source_metadata or {}).get("artifact_id") if source_metadata else ""
            if existing and store.get(str(existing)):
                artifact_id = str(existing)
            else:
                artifact = store.put(
                    raw_result or "",
                    kind=infer_kind(step_type, locator),
                    locator=locator or f"step:{step_type}",
                    title=title or step_type,
                    step_type=step_type,
                    metadata={"phase": "compress"},
                )
                artifact_id = artifact.artifact_id
            meta["artifact_id"] = artifact_id
            meta["reversible"] = True

        if original_len <= self.threshold_chars:
            return self._with_restore_footer(raw_result, artifact_id, original_len), meta

        compressed = raw_result
        if self.model is not None:
            llm_result, windows = await self._compress_with_llm(raw_result, step_type)
            meta["windows"] = windows
            if llm_result:
                compressed = llm_result
                meta["method"] = "llm"
            else:
                compressed = self._truncate(raw_result, artifact_id)
                meta["method"] = "truncate"
        else:
            compressed = self._truncate(raw_result, artifact_id)
            meta["method"] = "truncate"

        if self.retention_check and meta["method"] != "none":
            compressed, retention_meta = apply_retention_patch(
                raw_result,
                compressed,
                min_url_retention=self.min_url_retention,
                min_number_retention=self.min_number_retention,
            )
            meta.update(retention_meta)
            if retention_meta.get("retention_patched"):
                meta["method"] = f"{meta['method']}+retention_patch"

        compressed = self._with_restore_footer(compressed, artifact_id, original_len)
        meta["compressed_chars"] = len(compressed)
        meta["compression_ratio"] = round(len(compressed) / original_len, 3) if original_len else 1.0
        return compressed, meta

    async def _compress_with_llm(self, raw_result: str, step_type: str) -> tuple[Optional[str], int]:
        windows = _split_windows(raw_result, self.window_chars, LLM_WINDOW_OVERLAP)
        parts: list[str] = []
        per_window = max(400, self.max_output_chars // max(1, min(len(windows), 4)))
        for idx, chunk in enumerate(windows):
            header = ""
            if len(windows) > 1:
                header = f"[片段 {idx + 1}/{len(windows)}]\n"
            text = await self._invoke_llm(step_type, header + chunk, per_window)
            if text:
                parts.append(text)
        if not parts:
            return None, len(windows)
        if len(parts) == 1:
            return parts[0], len(windows)
        merged = "\n".join(parts)
        if len(merged) <= self.max_output_chars * 2:
            return merged, len(windows)
        folded = await self._invoke_llm(step_type, merged[: self.window_chars], self.max_output_chars)
        return folded or merged[: self.max_output_chars * 2], len(windows)

    async def _invoke_llm(self, step_type: str, content: str, max_chars: int) -> Optional[str]:
        prompt = self._prompt_for(step_type).format(
            max_chars=max_chars,
            step_type=step_type,
            content=content,
        )
        try:
            from app.agent.harness.usage_tracker import tracked_ainvoke

            response = await tracked_ainvoke(
                self.model,
                prompt,
                phase="compress",
            )
            content_out = getattr(response, "content", response)
            if isinstance(content_out, list):
                content_out = "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content_out
                )
            text = str(content_out).strip()
            if text:
                return text
        except Exception as exc:
            print(f"[Compressor] LLM compress failed, fallback to truncate: {exc}")
        return None

    def _truncate(self, raw_result: str, artifact_id: str = "") -> str:
        """保头+尾+定位符，避免关键数字只出现在后半段时被不可逆丢掉。"""
        budget = max(240, self.max_output_chars)
        urls = extract_urls(raw_result, limit=20)
        numbers = extract_numbers(raw_result, limit=24)
        locators = []
        if urls:
            locators.append("URLs: " + " ".join(urls[:12]))
        if numbers:
            locators.append("NUMBERS: " + " ".join(numbers[:16]))
        locator_block = ("\n".join(locators) + "\n") if locators else ""
        remaining = max(160, budget - len(locator_block))
        if len(raw_result) <= remaining:
            body = raw_result
        else:
            head_size = remaining // 2
            tail_size = remaining - head_size
            body = (
                f"{raw_result[:head_size]}\n"
                f"...[middle omitted, artifact={artifact_id or 'pending'}]...\n"
                f"{raw_result[-tail_size:]}"
            )
        return (
            f"{locator_block}{body}\n\n"
            f"[已截断压缩: 原始 {len(raw_result)} 字符 → 头尾保留；"
            f"{'可 read_artifact(' + artifact_id + ')' if artifact_id else '无 artifact'}]"
        )

    def _with_restore_footer(self, text: str, artifact_id: str, original_len: int) -> str:
        if not artifact_id:
            return text
        if "read_artifact" in (text or "") and artifact_id in (text or ""):
            return text
        return (
            f"{text.rstrip()}\n"
            f"[artifact:{artifact_id}] 原文 {original_len} 字符已外置，"
            f"需要时 read_artifact(\"{artifact_id}\")。"
        )

    def compress_sync(self, raw_result: str, step_type: str = "generic") -> str:
        """同步兼容接口（可恢复截断），供单元测试使用。"""
        if len(raw_result) <= self.threshold_chars:
            return raw_result
        return self._truncate(raw_result, artifact_id="")


def _split_windows(text: str, window: int, overlap: int) -> list[str]:
    body = text or ""
    if len(body) <= window:
        return [body]
    step = max(1, window - overlap)
    chunks: list[str] = []
    start = 0
    while start < len(body):
        chunks.append(body[start : start + window])
        if start + window >= len(body):
            break
        start += step
    return chunks
