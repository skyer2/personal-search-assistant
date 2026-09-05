"""
窗口卫生：每步独立图线程 + 可再取的 tool_result 占位清除。

跨步不复用 LangGraph thread_id，避免「精选 user message」和旧 tool 原文叠在同一窗口。
同一步内的 HITL resume 仍使用该步 thread，以便 interrupt 可恢复。
"""

from __future__ import annotations

from typing import Any

TOOL_RESULT_PLACEHOLDER = "[tool_result cleared: re-fetchable payload omitted]"
BULKY_TOOL_RESULT_CHARS = 500


def step_graph_thread_id(session_id: str, step_index: int, run_id: str = "") -> str:
    """主 Agent 单步执行使用的 LangGraph thread_id（Run 隔离）。

    thread_id 必须包含 run_id：否则同一 Session 的 Run2 Step0 会命中
    Run1 Step0 的全局 InMemorySaver checkpoint，直接污染 LLM 上下文。
    """
    if run_id:
        return f"{session_id}:{run_id}:step:{step_index}"
    return f"{session_id}:step:{step_index}"


def parallel_graph_thread_id(session_id: str, step_index: int, run_id: str = "") -> str:
    if run_id:
        return f"{session_id}:{run_id}:parallel:{step_index}"
    return f"{session_id}:parallel:{step_index}"


def _message_type_name(message: Any) -> str:
    name = getattr(message, "type", None) or getattr(message, "role", None)
    if name:
        return str(name).lower()
    cls = type(message).__name__.lower()
    return cls


def _get_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content or "")


def _set_content(message: Any, text: str) -> Any:
    if hasattr(message, "content"):
        try:
            message.content = text
            return message
        except Exception:
            pass
    if isinstance(message, dict):
        message["content"] = text
    return message


def is_tool_result_message(message: Any) -> bool:
    kind = _message_type_name(message)
    if kind in {"tool", "toolresult", "tool_result"}:
        return True
    if isinstance(message, dict) and str(message.get("role", "")).lower() == "tool":
        return True
    return "toolmessage" in kind


def clear_bulky_tool_results(
    messages: list[Any],
    *,
    keep_last: int = 1,
    max_chars: int = BULKY_TOOL_RESULT_CHARS,
) -> tuple[list[Any], int]:
    """把过旧且过长的 tool_result 换成占位符，保留 tool_use 记录。

    返回 (messages, 清除条数)。keep_last 条最近的 tool 结果原样保留。
    """
    if not messages:
        return messages, 0
    tool_indices = [i for i, msg in enumerate(messages) if is_tool_result_message(msg)]
    if not tool_indices:
        return messages, 0
    keep = set(tool_indices[-max(0, keep_last) :]) if keep_last > 0 else set()
    cleared = 0
    for idx in tool_indices:
        if idx in keep:
            continue
        content = _get_content(messages[idx])
        if len(content) <= max_chars:
            continue
        messages[idx] = _set_content(messages[idx], TOOL_RESULT_PLACEHOLDER)
        cleared += 1
    return messages, cleared


def messages_to_upsert_after_clear(messages: list[Any]) -> list[Any]:
    """供 LangGraph add_messages 按 id 覆盖：只回写已换成占位符的 tool 消息。"""
    return [
        msg
        for msg in messages
        if is_tool_result_message(msg) and _get_content(msg) == TOOL_RESULT_PLACEHOLDER
    ]


async def apply_checkpoint_tool_hygiene(
    agent: Any,
    config: dict[str, Any],
    *,
    snapshot: Any = None,
    keep_last: int = 1,
    max_chars: int = BULKY_TOOL_RESULT_CHARS,
) -> int:
    """读取图状态、清除过长 tool_result、按 id 写回 checkpoint。失败返回 0。"""
    try:
        snap = snapshot if snapshot is not None else await agent.aget_state(config)
    except Exception as exc:
        print(f"[Context] tool_result hygiene skipped (aget_state): {exc}")
        return 0
    values = getattr(snap, "values", None) or {}
    messages = list(values.get("messages") or [])
    if not messages:
        return 0
    updated, cleared = clear_bulky_tool_results(
        messages, keep_last=keep_last, max_chars=max_chars
    )
    if not cleared:
        return 0
    changed = messages_to_upsert_after_clear(updated)
    if not changed:
        return 0
    try:
        await agent.aupdate_state(config, {"messages": changed})
    except Exception as exc:
        print(f"[Context] tool_result hygiene skipped (aupdate_state): {exc}")
        return 0
    return cleared
