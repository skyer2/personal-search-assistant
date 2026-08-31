"""Tools API — 个人版本地工具清单。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.config.loader import get_harness_config

router = APIRouter(prefix="/api/tools", tags=["tools"])

LOCAL_TOOLS = [
    {"name": "internet_search", "source": "web", "description": "检索互联网公开信息（卡片）"},
    {"name": "fetch_url", "source": "web", "description": "按 URL 拉取网页正文进 Artifact"},
    {"name": "read_file_content", "source": "file", "description": "读取会话上传附件"},
    {"name": "read_artifact", "source": "context", "description": "读取外置原文 Artifact"},
    {"name": "read_evidence", "source": "context", "description": "读取证据摘录"},
    {"name": "generate_markdown", "source": "synthesis", "description": "生成 Markdown 文件（显式请求时）"},
    {"name": "convert_md_to_pdf", "source": "synthesis", "description": "Markdown 转 PDF（显式请求时）"},
]


@router.get("/registry")
def list_tool_registry() -> dict[str, Any]:
    config = get_harness_config()
    return {
        "total": len(LOCAL_TOOLS),
        "tools": LOCAL_TOOLS,
        "enabled_sources": {"web": True, "file": True},
        "personal_search": getattr(config, "personal_search", {}),
    }


@router.get("/policy")
def tool_policy() -> dict[str, Any]:
    config = get_harness_config()
    return {
        "fail_closed": config.tools_fail_closed,
        "enforce_step_policy": config.tools_enforce_step_policy,
        "enabled_sources": {"web": True, "file": True},
    }
