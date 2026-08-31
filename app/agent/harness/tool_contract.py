"""
Tool Output Contract — 工具先外置原文，再返回短卡 + artifact_ref。

防止「50KB 网页进窗口后再压缩」：源头就限制 LLM 可见体积。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.agent.harness.artifacts import (
    ArtifactStore,
    get_artifact_store,
    infer_kind,
)
from app.agent.harness.token_counter import estimate_tokens

DEFAULT_MAX_RESULT_TOKENS = 700
DEFAULT_SNIPPET_CHARS = 280
DEFAULT_MAX_ROWS = 12


@dataclass
class ToolOutputContract:
    max_result_tokens: int = DEFAULT_MAX_RESULT_TOKENS
    pagination: bool = True
    artifact_ref: bool = True
    supports_query_within_result: bool = True
    snippet_chars: int = DEFAULT_SNIPPET_CHARS
    max_rows: int = DEFAULT_MAX_ROWS

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_result_tokens": self.max_result_tokens,
            "pagination": self.pagination,
            "artifact_ref": self.artifact_ref,
            "supports_query_within_result": self.supports_query_within_result,
            "snippet_chars": self.snippet_chars,
            "max_rows": self.max_rows,
        }


CONTRACT_BY_TOOL: dict[str, ToolOutputContract] = {
    "internet_search": ToolOutputContract(max_result_tokens=900, snippet_chars=220, max_rows=5),
    "fetch_url": ToolOutputContract(max_result_tokens=700, snippet_chars=280, max_rows=1),
    "execute_sql_query": ToolOutputContract(max_result_tokens=600, max_rows=15),
    "get_table_data": ToolOutputContract(max_result_tokens=500, max_rows=10),
    "create_ask_delete": ToolOutputContract(max_result_tokens=700, snippet_chars=320),
    "read_file_content": ToolOutputContract(max_result_tokens=800, snippet_chars=400),
}


def contract_for(tool_name: str) -> ToolOutputContract:
    return CONTRACT_BY_TOOL.get(tool_name, ToolOutputContract())


def _as_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except TypeError:
        return str(raw)


def compact_search_payload(
    raw: Any,
    *,
    store: ArtifactStore,
    contract: ToolOutputContract,
    tool_name: str = "internet_search",
    step_type: str = "network_search",
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {"results": [{"content": _as_text(raw)}]}
    results = data.get("results") if isinstance(data.get("results"), list) else []
    cards: list[dict[str, Any]] = []
    artifact_ids: list[str] = []
    limit = max(1, contract.max_rows)
    for item in results[:limit]:
        if not isinstance(item, dict):
            item = {"content": str(item)}
        title = str(item.get("title") or "")
        url = str(item.get("url") or item.get("locator") or "")
        raw_body = str(
            item.get("raw_content") or item.get("content") or item.get("snippet") or ""
        )
        snippet = str(item.get("content") or item.get("snippet") or raw_body)[: contract.snippet_chars]
        artifact = store.put(
            raw_body or json.dumps(item, ensure_ascii=False),
            kind=infer_kind(step_type, url),
            locator=url or title or f"tool:{tool_name}",
            title=title or url,
            summary=snippet,
            metadata={"tool_name": tool_name, "score": item.get("score")},
            step_type=step_type,
        )
        artifact_ids.append(artifact.artifact_id)
        card = {
            "title": title or artifact.title,
            "url": url,
            "snippet": snippet,
            "artifact_id": artifact.artifact_id,
            "ref": artifact.ref(),
        }
        if item.get("doc_id") is not None:
            card["doc_id"] = item.get("doc_id")
        cards.append(card)
    if not cards:
        artifact = store.put_from_tool_result(
            raw, tool_name=tool_name, step_type=step_type
        )
        artifact_ids.append(artifact.artifact_id)
        cards.append(artifact.compact_card(contract.snippet_chars))
    payload = {
        "query": data.get("query"),
        "results": cards,
        "artifact_ids": artifact_ids,
        "truncated": len(results) > limit,
        "hint": "需要原文时调用 read_artifact(artifact_id) 或带 query 检索片段。",
    }
    if data.get("provider"):
        payload["provider"] = data.get("provider")
    return payload


def compact_generic_payload(
    raw: Any,
    *,
    store: ArtifactStore,
    contract: ToolOutputContract,
    tool_name: str,
    step_type: str = "",
) -> dict[str, Any]:
    artifact = store.put_from_tool_result(raw, tool_name=tool_name, step_type=step_type)
    text = artifact.content or ""
    snippet = (artifact.summary or text)[: contract.snippet_chars]
    tokens = estimate_tokens(text)
    payload = {
        "ok": True,
        "tool": tool_name,
        "artifact_id": artifact.artifact_id,
        "ref": artifact.ref(),
        "locator": artifact.locator,
        "title": artifact.title,
        "snippet": snippet,
        "char_count": artifact.char_count,
        "estimated_tokens": tokens,
        "truncated": tokens > contract.max_result_tokens,
        "hint": "完整结果已外置。按需 read_artifact(artifact_id, start, end) 或 query=关键词。",
    }
    if isinstance(raw, dict):
        for key in ("row_count", "table", "sql", "filename", "assistant_id"):
            if raw.get(key) is not None:
                payload[key] = raw.get(key)
        rows = raw.get("rows") or raw.get("data")
        if isinstance(rows, list) and contract.pagination:
            payload["preview_rows"] = rows[: contract.max_rows]
            payload["row_count"] = raw.get("row_count", len(rows))
            payload["truncated"] = payload["truncated"] or len(rows) > contract.max_rows
    return payload


def apply_tool_output_contract(
    raw: Any,
    *,
    tool_name: str,
    step_type: str = "",
    store: ArtifactStore | None = None,
    contract: ToolOutputContract | None = None,
) -> str:
    store = store or get_artifact_store()
    contract = contract or contract_for(tool_name)
    if not contract.artifact_ref:
        return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if tool_name == "internet_search":
        payload = compact_search_payload(
            raw, store=store, contract=contract, tool_name=tool_name, step_type=step_type or "network_search"
        )
    else:
        payload = compact_generic_payload(
            raw, store=store, contract=contract, tool_name=tool_name, step_type=step_type
        )
    text = json.dumps(payload, ensure_ascii=False)
    # 硬合同：即使 JSON 仍偏长，也截到 token 上限，并保留 artifact_id。
    from app.agent.harness.token_counter import get_token_counter

    counter = get_token_counter()
    if counter.count(text) > contract.max_result_tokens:
        payload["snippet"] = str(payload.get("snippet") or "")[: max(80, contract.snippet_chars // 2)]
        payload.pop("preview_rows", None)
        payload["truncated"] = True
        text = json.dumps(payload, ensure_ascii=False)
    return text


def wrap_tool_with_contract(tool: Any, *, tool_name: str = "", step_type: str = "") -> Any:
    """包装 LangChain tool：执行后按合同外置原文。"""
    if tool is None:
        return tool
    name = tool_name or getattr(tool, "name", "") or "tool"
    if name in {"read_artifact", "read_evidence"}:
        return tool
    if getattr(tool, "_harness_contract_wrapped", False):
        return tool
    from langchain_core.tools import StructuredTool

    description = getattr(tool, "description", "") or name
    args_schema = getattr(tool, "args_schema", None)

    def _run(**kwargs: Any) -> Any:
        if hasattr(tool, "invoke"):
            raw = tool.invoke(kwargs)
        else:
            func = getattr(tool, "func", None)
            raw = func(**kwargs) if callable(func) else tool(**kwargs)
        return apply_tool_output_contract(raw, tool_name=name, step_type=step_type)

    wrapped = StructuredTool.from_function(
        func=_run,
        name=name,
        description=description + "。完整原文已外置为 artifact_id，需要时 read_artifact。",
        args_schema=args_schema,
    )
    wrapped._harness_contract_wrapped = True  # type: ignore[attr-defined]
    return wrapped
