"""
Tavily 网络搜索工具模块

封装 internet_search 工具，供网络搜索子智能体检索互联网公开信息。
底层搜索逻辑与 MCP Server 共用 app.tools.tavily_core。
"""

from typing import Literal

from langchain_core.tools import tool

from app.agent.harness.step_budget import consume_retrieval_or_block
from app.tools.tavily_core import search_internet


@tool
def internet_search(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
):
    """
    根据用户问题检索互联网公开信息

    注意：默认只返回标题、URL、摘要卡片。需要网页正文时再调用 fetch_url。
    :param query: 搜索关键词或自然语言问题
    :param topic: 搜索主题，可选 news、finance、general
    :param max_results: 返回的最大结果数
    :param include_raw_content: 是否返回网页原文内容；False 返回摘要，True 尝试返回更完整正文
    :return: Tavily 返回的结构化搜索结果
    """
    blocked = consume_retrieval_or_block("internet_search")
    if blocked:
        return blocked
    return search_internet(
        query=query,
        topic=topic,
        max_results=max_results,
        include_raw_content=include_raw_content,
    )


if __name__ == "__main__":
    from pprint import pprint

    pprint(
        internet_search.invoke(
            {"query": "2026中国法定节假日放假安排表，我天天都想要放假"}
        )
    )
