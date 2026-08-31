# Harness 运行时架构（Phase 20–25）

> **权威模型**：Domain Harness（领域控制面）+ **Research StateGraph Runtime**（生产调度权威）+ Leaf Workers（按步直调）+ **MCP Capability Plane**（工具治理）。  
> LangGraph 跑整个研究工作流（intent → plan → dispatch / Send → synthesis）；DeepAgents 只在需要 filesystem 时组装工人，不再当第二导演。  
> MCP 是 pluggable provider 边界，不污染 Research Domain Model。全貌见 [MCP_SYSTEM.md](./MCP_SYSTEM.md)。  
> **产品层**：Quick / Deep 分流、Conversation、个人默认能力面见 [PERSONAL_SEARCH.md](./PERSONAL_SEARCH.md)（Harness 层本文描述的部分 **不推翻**，在其前面加 Mode Router）。

对照：[教学版 deepsearch-agents](https://github.com/didilili/deepsearch-agents) 是一次 `create_deep_agent` 黑盒跑完全程。本仓库在其上加了显式 Loop 之后，Phase 20 把执行入口收成「计划指定谁就直调谁」；当前生产配置 `orchestration.graph_runtime_enabled: true`，调度权威是 Research StateGraph，`AgentHarness._run_legacy_loop()` 仅作回退。

---

## 整体架构

```text
┌─────────────────────────────────────────────────────────────────┐
│ 体验 / 服务                                                       │
│  React · HITL 暂停态 · FastAPI · WebSocket · /health · eval       │
└───────────────────────────────┬─────────────────────────────────┘
                                │ run(task, session_id)
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│ Domain Harness（领域控制面）                                      │
│  Intent / Plan / Policy / Budget / Memory / Citation / Eval      │
│  Context Selector · Artifact/Evidence Store · Tool Gateway       │
│  MCP PolicyContext（tenant/user/run/task/scopes/allowlist）       │
│  控制状态：ResearchState → SQLite checkpointer                     │
│  副作用状态：LoopState → output/session_*/.harness/checkpoint.json  │
└───────────────┬─────────────────────────────────────────────────┘
                │ research_graph.ainvoke
                ▼
┌─────────────────────────────────────────────────────────────────┐
│ Research StateGraph Runtime（生产调度权威）                        │
│  intent → clarify → plan → validate → dispatch                   │
│  Send(isolated workers) → join → Progress Evaluator              │
│  GAP → constrained replan；ENOUGH → synthesis → Quality Gate     │
│  interrupt() = HITL；SQLite checkpointer = 图内恢复                │
└───────────────┬───────────────────────────┬─────────────────────┘
                │ invoke(本步工人)           │ invoke(合成工人)
                ▼                           ▼
┌──────────────────────────┐   ┌──────────────────────────────────┐
│ Leaf Workers（create_agent）│   │ 合成工人                          │
│ web / db / kb / file /    │   │ generate_markdown / PDF / 读附件 │
│ mixed researcher          │   │ read_artifact / read_evidence    │
│ 稳定 Worker Profile        │   │ interrupt_on = 写文件 HITL       │
│ 最小 tool surface          │   └───────────────┬──────────────────┘
└────────────┬─────────────┘                   │
             │ tool call                         ▼
             ▼                     Artifact Store + Evidence Store
┌────────────────────────────────┐ （原文外置；窗口只留 ref）
│ MCP / Tool Control Plane       │
│  Registry · Gateway · Policy   │
│  stdio pool 或 stateless HTTP  │
│  Tavily / MySQL / RAGFlow /    │
│  Files（可切换 LangChain 直连） │
└────────────────────────────────┘
```

配置开关（默认开启）：

- `orchestration.direct_worker_invoke`：检索步直调工人
- `orchestration.persist_loop_state`：checkpoint 写入整份 LoopState
- `orchestration.graph_runtime_enabled`：**生产调度权威是 Research StateGraph**
- `context.jit_retrieval_enabled` / `token_budget.model: glm-5.2` / `reversible_compression`
- `mcp.enabled`：false 时 LangChain 直连；true 时按 Server 走 MCP Gateway
- `mcp.require_auth`：生产打开后校验 caller access token（不是进程自校验 env）
- `tools.sql_max_rows` / `sql_table_allowlist`：数据源层截断，不把百万行拉进进程

回退：`HARNESS_GRAPH_RUNTIME=false` 时走 `AgentHarness._run_legacy_loop()`（while 外环）。`HARNESS_DIRECT_WORKER_INVOKE=false` 仅用于对比评测，生产不要关。

---

## 一次任务怎么跑

以「查阿莫西林公开市场和库存，写 Markdown」为例：

1. **Intent → Research Brief**：多轮对话压成稳定说明书（目标 / 实体 / 时间 / 来源策略 / 交付物）。
2. **Plan**：`network_search` → `database_query` → `generate_markdown`（可并行前两步）。StateGraph `dispatch` 用 `Send` fan-out。
3. **步 1**：`WorkerProfileResolver` 选 `web_researcher`，只挂 `internet_search` + `read_artifact`。工具返回 title/url/snippet/`artifact_id`，原文进 Artifact Store。
4. **步 2**：直调 `db_researcher`。SQL 仍走 `ToolGateway`。结果同样外置。
5. **Join**：facts → Finding + EvidenceSpan；working notes 只留 claim + evidence_id。
6. **步 3**：合成工人 JIT 检索与本节相关的 evidence，不确定时 `read_evidence(E27)`，不把 100 条 digest 一次塞进窗口。
7. **落库**：LoopState + artifact index + evidence_store.json。图内 HITL 认 LangGraph interrupt。

---

## 和教学版的差别

| | [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents) | 本仓库 |
|--|--------------------------------------------------------------------------|--------|
| 编排 | 主 Agent 自己决定调哪个子 Agent | Domain Harness 出计划；StateGraph 调度；Leaf 直调 |
| 工具隔离 | 靠 prompt | Worker Profile 物理上没有越权工具；Gateway fail-closed；MCP 与 LangChain 共用 choke point |
| 工具接入 | 本地 `@tool` | Registry 隔离 domain；底层可切换 LangChain / MCP；stdio 或 stateless HTTP |
| 上下文 | 历史全塞 | Brief + JIT；原文在 Artifact Store |
| 失败 | 模型再试或任务失败 | validate 失败码 + recover / replan + Kill Switch |
| 引用 / 记忆 / 评测 | 无 | claim→span、分层 Memory、golden eval |
| 进度 | 无任务级 checkpoint | LoopState JSON（副作用热恢复）+ 图内 SQLite checkpointer |
| HITL | 无或仅工具中断 | 澄清 / 计划审批 / 查库 gate / 写文件 interrupt |

教学版解决「DeepAgents 怎么把三个专家跑起来」。本仓库解决「一次研搜如何按剧本交付，并且 **LLM context ≠ application state**」。

---

## 面试怎么说

> 研搜要的是领域 Harness，不是再造一个 LangGraph。Domain Harness 管计划、校验、护栏、评测和 Context Store；生产调度权威是 Research StateGraph；Leaf Worker 按稳定 Profile 直调。MCP 只标准化 capability 接入，权限和副作用治理仍在 Harness。窗口只保留当前决策需要的信息，可重新取得的大内容全部 `artifact://` / `evidence://` 外置。

相关代码：`app/research/runtime/graph.py`、`app/research/planning/progress.py`、`app/agent/harness/loop.py`、`context_builder.py`、`artifacts.py`、`evidence_store.py`、`token_counter.py`、`worker_profiles.py`、`app/mcp/`。

权威设计：[RESEARCH_INTELLIGENCE.md](./RESEARCH_INTELLIGENCE.md)。

---

## Phase 20–26 落地对照

| Phase | 做了什么 | 权威文档 |
|-------|----------|----------|
| 20 | 检索步直调 Leaf Worker；主图不再二次路由 | 本文 |
| 21 | Research StateGraph 成为生产调度权威 | [RESEARCH_HARNESS.md](./RESEARCH_HARNESS.md) |
| 22 | Hybrid planning：DIRECT / TEMPLATE / DYNAMIC | [RESEARCH_HARNESS.md](./RESEARCH_HARNESS.md) |
| 23 | 上下文虚拟化：Artifact/Evidence + glm-5.2 预算 + JIT | [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md) |
| 24 | Memory 生产门禁：身份四元组、信任分级、来源台账 | [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md) |
| 25 | MCP 从 stdio 适配层升级为 Capability Plane | [MCP_SYSTEM.md](./MCP_SYSTEM.md) |
| 26 | Research Intelligence Loop：Progress Evaluator + 隔离并行 + SQLite ResearchState | [RESEARCH_INTELLIGENCE.md](./RESEARCH_INTELLIGENCE.md) |

面试运维面：`GET /api/harness/capabilities` 返回当前 `graph_runtime_enabled`、`direct_worker_invoke`、fail-closed / SQL 护栏。

前端：`idle / running / awaiting_approval`。HITL 时进度条与计时冻结，审批卡片吸顶，不再把 interrupt 渲染成仍在跑的无限动画。详见 `frontend/README.md`。
