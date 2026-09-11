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
from app.agent.harness.usage_tracker import (
    get_current_worker_run_id,
    get_current_worker_session_id,
    get_current_worker_step_index,
    get_current_worker_task_id,
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
    worker_task_id: str = "",
    step_index: int = -1,
    run_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {"results": [{"content": _as_text(raw)}]}
    results = data.get("results") if isinstance(data.get("results"), list) else []
    cards: list[dict[str, Any]] = []
    artifact_ids: list[str] = []
    limit = max(1, contract.max_rows)
    structured_candidates = [
        dict(item)
        for item in data.get("candidates") or []
        if isinstance(item, dict)
    ]
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
            metadata={
                "tool_name": tool_name,
                "score": item.get("score"),
                **({"candidates": structured_candidates} if structured_candidates else {}),
                **({"task_id": worker_task_id} if worker_task_id else {}),
                **({"step_index": step_index} if step_index >= 0 else {}),
                **({"run_id": run_id} if run_id else {}),
                **({"session_id": session_id} if session_id else {}),
            },
            step_index=step_index,
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
            raw,
            tool_name=tool_name,
            step_type=step_type,
            worker_task_id=worker_task_id,
            step_index=step_index,
            run_id=run_id,
            session_id=session_id,
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
    worker_task_id: str = "",
    step_index: int = -1,
    run_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    artifact = store.put_from_tool_result(
        raw,
        tool_name=tool_name,
        step_type=step_type,
        worker_task_id=worker_task_id,
        step_index=step_index,
        run_id=run_id,
        session_id=session_id,
    )
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
    worker_task_id: str = "",
    step_index: int = -1,
    run_id: str = "",
    session_id: str = "",
) -> str:
    store = store or get_artifact_store()
    contract = contract or contract_for(tool_name)
    worker_task_id = worker_task_id or get_current_worker_task_id()
    if step_index < 0:
        step_index = get_current_worker_step_index()
    run_id = run_id or get_current_worker_run_id()
    session_id = session_id or get_current_worker_session_id()
    if not contract.artifact_ref:
        return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if tool_name == "internet_search":
        payload = compact_search_payload(
            raw,
            store=store,
            contract=contract,
            tool_name=tool_name,
            step_type=step_type or "network_search",
            worker_task_id=worker_task_id,
            step_index=step_index,
            run_id=run_id,
            session_id=session_id,
        )
    else:
        payload = compact_generic_payload(
            raw,
            store=store,
            contract=contract,
            tool_name=tool_name,
            step_type=step_type,
            worker_task_id=worker_task_id,
            step_index=step_index,
            run_id=run_id,
            session_id=session_id,
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


def _tool_item_count(raw: Any) -> int:
    if not isinstance(raw, dict):
        return 1
    for key in ("query_count", "url_count", "item_count"):
        try:
            value = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    results = raw.get("results")
    return len(results) if isinstance(results, list) else 1


def _tool_success_count(raw: Any) -> int:
    if not isinstance(raw, dict):
        return 1
    for key in ("ok_count", "success_count"):
        try:
            value = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    results = raw.get("results")
    if isinstance(results, list):
        return sum(1 for item in results if isinstance(item, dict) and item.get("ok"))
    return 1 if raw.get("ok") else 0


def _raw_artifact_ids(raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return []
    ids: list[str] = []
    direct = raw.get("artifact_id")
    if direct:
        ids.append(str(direct))
    ids.extend(str(item) for item in raw.get("artifact_ids") or [] if str(item).strip())
    for item in raw.get("results") or []:
        if isinstance(item, dict) and item.get("artifact_id"):
            ids.append(str(item["artifact_id"]))
    return list(dict.fromkeys(ids))


def wrap_tool_with_contract(
    tool: Any,
    *,
    tool_name: str = "",
    step_type: str = "",
    apply_output_contract: bool = True,
) -> Any:
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
        import json
        import time

        from app.research.runtime.activity import (
            get_current_worker_activity,
            tracked_worker_operation,
        )

        from app.observability import get_recorder

        recorder = get_recorder()
        call_id = ""
        started = time.perf_counter()
        if recorder.is_active:
            call_id = recorder.begin_tool(name, args=kwargs)
        try:
            with tracked_worker_operation(f"tool.{name}"):
                if hasattr(tool, "invoke"):
                    raw = tool.invoke(kwargs)
                else:
                    func = getattr(tool, "func", None)
                    raw = func(**kwargs) if callable(func) else tool(**kwargs)
            if (
                isinstance(raw, dict)
                and raw.get("error") == "budget_denied"
            ):
                if recorder.is_active:
                    recorder.finish_tool(
                        name,
                        tool_call_id=call_id,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        status="denied",
                        error=str(raw.get("reason") or "budget_denied"),
                        extra={
                            "error_type": "BudgetDenied",
                            "reason": str(raw.get("reason") or ""),
                            "resource": str(raw.get("resource") or ""),
                            "item_count": _tool_item_count(raw),
                            "success_count": 0,
                            "failure_count": _tool_item_count(raw),
                        },
                    )
                return raw
            output = (
                raw
                if not apply_output_contract
                else apply_tool_output_contract(raw, tool_name=name, step_type=step_type)
            )
            tracker = get_current_worker_activity()
            if tracker is not None:
                tracker.artifact_written()
            if recorder.is_active:
                payload = json.loads(output) if isinstance(output, str) else None
                artifact_ids: list[str] = []
                artifact_ids.extend(_raw_artifact_ids(raw))
                if isinstance(payload, dict):
                    artifact_id = str(payload.get("artifact_id") or "")
                    if artifact_id:
                        artifact_ids.append(artifact_id)
                artifact_ids = list(dict.fromkeys(artifact_ids))
                recorder.finish_tool(
                    name,
                    tool_call_id=call_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    status="ok",
                    result_ref=artifact_ids[0] if artifact_ids else "",
                    result_count=_tool_success_count(raw) if output else 0,
                    extra={
                        "item_count": _tool_item_count(raw),
                        "success_count": _tool_success_count(raw),
                        "failure_count": max(
                            0, _tool_item_count(raw) - _tool_success_count(raw)
                        ),
                    },
                    result_bytes=len(output.encode("utf-8")) if isinstance(output, str) else 0,
                    artifact_ids=artifact_ids,
                )
            return output
        except Exception as exc:
            if recorder.is_active:
                recorder.finish_tool(
                    name,
                    tool_call_id=call_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                    extra={"error_class": type(exc).__name__, "retryable": False},
                )
            raise

    wrapped = StructuredTool.from_function(
        func=_run,
        name=name,
        description=description + "。完整原文已外置为 artifact_id，需要时 read_artifact。",
        args_schema=args_schema,
    )
    wrapped._harness_contract_wrapped = True  # type: ignore[attr-defined]
    return wrapped
