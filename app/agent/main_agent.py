"""
主入口：Domain Harness + Leaf WorkerRegistry。

生产路径：run_deep_agent → AgentHarness.run → research_graph.ainvoke。
检索与合成按步直调 create_agent Leaf，不再创建 Main DeepAgent。
"""

import asyncio
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.harness.context_builder import ContextBuilder
from app.agent.harness.loop import AgentHarness
from app.agent.harness.compressor import ContextCompressor
from app.agent.llm import compression_model, model
from app.agent.memory.extractor import MemoryExtractor
from app.agent.memory.store import MemoryStore
from app.config.loader import get_harness_config
from app.research.workers.registry import build_worker_registry

harness_config = get_harness_config()

agent_checkpointer = InMemorySaver()

_interrupt_on = (
    harness_config.hitl_interrupt_on
    if harness_config.hitl_enabled
    else {k: False for k in harness_config.hitl_interrupt_on}
)

worker_registry = build_worker_registry(
    model=model,
    checkpointer=agent_checkpointer,
    interrupt_on=_interrupt_on,
)
worker_graphs = worker_registry.as_step_map()
_default_agent = worker_registry.get("generate_markdown") or next(
    iter(worker_graphs.values())
)

project_root_path = Path(__file__).parents[1].resolve()

memory_store = MemoryStore()
memory_extractor = MemoryExtractor(model=compression_model)

harness = AgentHarness(
    agent=_default_agent,
    project_root=project_root_path,
    compressor=ContextCompressor(
        model=compression_model,
        max_output_chars=harness_config.compression_max_chars,
        enabled=harness_config.compression_enabled,
        threshold_chars=harness_config.compression_threshold_chars,
        retention_check=harness_config.compression_retention_check,
        min_url_retention=harness_config.compression_retention_min_url,
        min_number_retention=harness_config.compression_retention_min_number,
        reversible=getattr(harness_config, "context_reversible_compression", True),
    ),
    memory=memory_store,
    memory_extractor=memory_extractor,
    harness_config=harness_config,
    context_builder=ContextBuilder.from_harness_config(),
    workers=worker_graphs,
)


async def run_deep_agent(
    task_query,
    session_id,
    *,
    user_id="me",
    tenant_id="local",
    project_id="Inbox",
    mode="auto",
):
    """异步执行入口 — StateGraph 为 workflow 权威，领域服务仍由 harness 提供。"""
    print(f"[MainAgent] Harness 开始执行，session_id={session_id}, mode={mode}")
    result = await harness.run(
        task_query,
        session_id,
        user_id=user_id or "me",
        tenant_id=tenant_id or "local",
        project_id=project_id or "Inbox",
        mode=mode,
    )
    print(
        f"[MainAgent] Harness 完成，status={result.status}, "
        f"retries={result.retry_count}, artifacts={result.artifacts}, "
        f"memory_recalled={result.metadata.get('memory_recalled')}"
    )
    return result


if __name__ == "__main__":
    asyncio.run(
        run_deep_agent("今天 A 股创业板为什么跌？", "test_session_001")
    )
