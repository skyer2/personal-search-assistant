"""
大模型初始化模块

负责从 .env 中读取模型配置，并创建项目统一复用的模型对象。
【Phase 2 新增】compression_model 供 Harness 上下文压缩使用（默认 qwen-turbo）。
"""

import os

from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv(find_dotenv())

_llm_timeout = float(os.getenv("LLM_TIMEOUT_SEC", "120"))
_supervisor_temperature = float(os.getenv("HARNESS_SUPERVISOR_TEMPERATURE", "0.1"))
_openai_base_url = (os.getenv("OPENAI_BASE_URL") or "").strip() or None
_openai_api_key = (os.getenv("OPENAI_API_KEY") or "").strip() or None

model = init_chat_model(
    model=os.getenv("LLM_QWEN_MAX"),
    model_provider="openai",
    timeout=_llm_timeout,
    temperature=_supervisor_temperature,
    base_url=_openai_base_url,
    api_key=_openai_api_key,
)

_compression_model_name = os.getenv("LLM_COMPRESSION_MODEL", "qwen-turbo")
_compression_enabled = os.getenv("HARNESS_LLM_COMPRESSION", "true").lower() != "false"

compression_model = None
if _compression_enabled:
    try:
        compression_model = init_chat_model(
            model=_compression_model_name,
            model_provider="openai",
            timeout=_llm_timeout,
            base_url=_openai_base_url,
            api_key=_openai_api_key,
        )
    except Exception as exc:
        print(f"[LLM] compression_model init failed, will use truncate: {exc}")

