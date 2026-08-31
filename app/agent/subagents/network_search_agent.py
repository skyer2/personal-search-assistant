"""网络搜索子智能体 — 直连 Tavily LangChain 工具。"""

from app.agent.prompts import sub_agents_content
from app.tools.tavily_tool import internet_search


def build_network_search_agent() -> dict:
    return {
        "name": sub_agents_content["tavily"]["name"],
        "description": sub_agents_content["tavily"]["description"],
        "system_prompt": sub_agents_content["tavily"]["system_prompt"],
        "tools": [internet_search],
    }
