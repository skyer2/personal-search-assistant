"""ANSWER 档：不检索，基于已有知识直答。"""

from __future__ import annotations


def compose_direct_answer(query: str) -> str:
    q = (query or "").strip() or "（空问题）"
    return (
        f"{q}\n\n"
        "这是直答路径：不检索网页，只用模型已有知识回答。"
        "若需要最新资料或对照多份来源，请改用「搜索」或「研搜」。"
    )


async def try_direct_llm(query: str, *, conversation_summary: str = "") -> str | None:
    """有模型时走一次短对话；失败则让调用方用 compose_direct_answer。"""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.agent.harness.usage_tracker import tracked_ainvoke
        from app.agent.llm import model
    except Exception:
        return None
    system = (
        "你是个人搜索助手的直答模式。只根据已有知识简明回答。"
        "不要假装检索过网页。不确定就明确说不确定。"
        "需要时效信息时，建议用户改用搜索模式。"
    )
    if conversation_summary:
        system += f"\n近期对话摘要：{conversation_summary[:800]}"
    try:
        response = await tracked_ainvoke(
            model,
            [SystemMessage(content=system), HumanMessage(content=query)],
            session_id="",
            phase="direct_answer",
        )
        text = getattr(response, "content", None) or str(response or "")
        text = str(text).strip()
        return text or None
    except Exception:
        return None
