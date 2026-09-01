# Harness 运行时架构（Phase 20–25）

> **权威方案**：[ARCHITECTURE.md](./ARCHITECTURE.md)。本文只补充 StateGraph Runtime 细节。  
> 生产配置 `orchestration.graph_runtime_enabled: true`；调度权威是 **Research StateGraph**。  
> `persist_loop_state` 默认 false。没有 ANSWER/SEARCH 产品路径。

对照：[教学版 deepsearch-agents](https://github.com/didilili/deepsearch-agents) 是一次 `create_deep_agent` 黑盒跑完全程。本仓库的调度权威是 Research StateGraph，`AgentHarness._run_legacy_loop()` 仅作显式回退。

---

## 整体架构

```text
API / UI（实验台，不是搜索产品）
   → Research Domain (Brief / Planner / Progress / QualityGate)
   → Agent Runtime (StateGraph + SQLite checkpointer)
   → WorkerRuntime (LangChain now; DeepSeek later)
   → Environment: search / fetch / file  +  Artifact / Evidence
```

`AgentHarness._run_legacy_loop()` 已弃用，仅在显式关闭 graph 时回退。

---

## 状态：Graph 是唯一 workflow truth

```text
ResearchState  →  LangGraph SQLite checkpointer
LoopState      →  进程内 handles（不 checkpoint 为 workflow）
Artifact/Evidence Store → 原文
```

不要再画「Graph SQLite + LoopState checkpoint.json」双恢复。

Harness 路径：

```text
intent → clarify → plan → validate → dispatch
Send(isolated workers via WorkerRuntime) → Progress Evaluator
GAP → replan；ENOUGH → synthesis → Quality Gate → finalize
```

`direct` 仅对照实验：single worker + search tool，不进上述节点。

---

## 和教学版的差别

| | [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents) | 本仓库 |
|--|--------------------------------------------------------------------------|--------|
| 入口 | 每问都进主 Agent | 默认全部进 Harness；direct 只做 ablation |
| 编排 | 主 Agent 自己决定调哪个子 Agent | Domain 出 Brief/Plan；StateGraph 调度；WorkerRuntime 执行 |
| 上下文 | 历史全塞 | Brief + JIT；原文在 Artifact Store |
| 进度 | 无任务级 checkpoint | **只有**图内 SQLite checkpointer |
| Search | 产品能力 | **Environment tool only** |

相关代码：`app/research/runtime/graph.py`、`app/research/runtime/worker.py`、`app/research/planning/progress.py`、`app/agent/harness/artifacts.py`、`evidence_store.py`。

权威设计：[ARCHITECTURE.md](./ARCHITECTURE.md)。

面试运维面：`GET /api/harness/capabilities` 返回当前 `graph_runtime_enabled`、实验档 `agent|direct`、environment tools。

---

## 工人契约与重试（避免研究空转）

研究步 `allowed_tools` = 来源工具（`internet_search` / `fetch_url` 或 `read_file_content`）**加上** JIT 回读 `read_artifact` / `read_evidence`。越权校验对这两个上下文工具始终放行。

`ResultValidator.no_error` 只认明确执行失败模板（如「步骤执行超时」），不扫摘要里的「失败 / 错误 / 异常」。

缺 JSON 时的 `structured_retry` / 外层 recover：

- **禁止**整步再搜再抓；检索额度置 0，指令要求只输出 JSON
- 产出抽取走最后一条助手正文，跳过 ToolMessage（避免对着 Tavily 文本找 `facts`）
- 仍没有 JSON 时，用本步已存 Artifact 卡片 salvage
- 工人已回 `summary`+`facts` 时压缩不再打 LLM

步内 `budget.max_step_tool_calls`（默认 8）硬限制 `internet_search` / `fetch_url`。会话 `max_tool_calls` 默认 40，给并行研究 + 写报告留余量。Monitor 只在 astream 报工具，避免中英双计。
