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
