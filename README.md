<div align='center'>
  <h1 style="margin-top: 15px;">「深度研搜」对话式多智能体研究系统</h1>
  <h4><b>deepsearch-agents</b></h4>
  <p><em>Deep Research 领域 Harness：StateGraph 调度 + 稳定 Worker Profile + 上下文虚拟化 + Memory 生产门禁 + MCP Capability Plane — 可见、可测、可控</em></p>
</div>

<div align='center'>

![AI](https://img.shields.io/badge/AI-Agent-00c853?style=flat)
![Harness](https://img.shields.io/badge/Agent-Harness-7C3AED?style=flat)
![DeepAgents](https://img.shields.io/badge/DeepAgents-0.5.7-1C3C3C.svg)
![MCP](https://img.shields.io/badge/MCP-Capability_Plane-2563EB?style=flat)
![Eval](https://img.shields.io/badge/Eval-8_metrics-F59E0B?style=flat)
![FastAPI](https://img.shields.io/badge/FastAPI-WebSocket-009688.svg?logo=fastapi&logoColor=white)
![Stars](https://img.shields.io/github/stars/didilili/deepsearch-agents?logo=github&style=flat)
[![Read Online](https://img.shields.io/badge/在线教程-点击访问-blue?logo=bookstack)](https://didilili.github.io/ai-agents-from-zero/#/%E5%AE%9E%E6%88%98%E9%A1%B9%E7%9B%AE-%E6%B7%B1%E5%BA%A6%E7%A0%94%E6%90%9C/0-%E5%89%8D%E8%A8%80)
[![CI Eval](https://img.shields.io/badge/CI-eval_regression-2EA44F?style=flat)](.github/workflows/eval-regression.yml)

</div>

**📢 说明**：本仓库在教学版多智能体研搜之上，已完成 **Agent Harness 企业级升级**。现行权威不是「一次 `create_deep_agent` 黑盒跑完全程」，而是：

- **Domain Harness** 管计划 / 校验 / 护栏 / 评测 / 记忆 / 上下文
- **Research StateGraph** 是生产调度权威（`graph_runtime_enabled: true`）
- **Leaf Worker** 按稳定 Profile 直调，主图不再二次路由
- **MCP** 是可插拔 capability 边界，默认仍可 LangChain 直连

配套文档：

| 文档 | 内容 |
|------|------|
| [**Personal Search 产品架构**](docs/PERSONAL_SEARCH.md) | **Mode Router、Quick/Deep 分流、Conversation、模块拆分、业务规则** |
| [Harness 运行时架构](docs/HARNESS_ARCHITECTURE.md) | StateGraph + Worker Profile + 与教学版对照 |
| [领域 Harness 决策边界](docs/RESEARCH_HARNESS.md) | 谁是 Agent、谁是控制面 |
| [Research Intelligence 闭环](docs/RESEARCH_INTELLIGENCE.md) | Progress Evaluator / Replan / 真并行（Deep 路径） |
| [上下文工程](docs/CONTEXT_SYSTEM.md) | Artifact / Evidence / JIT / glm-5.2 预算 |
| [Memory 系统](docs/MEMORY_SYSTEM.md) | 身份四元组、信任分级、来源台账 |
| [MCP Capability Plane](docs/MCP_SYSTEM.md) | 真身份、并发池、durable Tasks、DB 护栏 |
| [升级设计草案](docs/AGENT_HARNESS_DESIGN.md) | 2026-07 动机与路线（部分章节已被上表取代） |
| [BrowseComp-Plus 评测](docs/BROWSECOMP_PLUS_EVAL.md) | 固定语料评测 |

如果你正在找一个适合学习 `DeepAgents`、`WebSocket`、`Tavily`、`RAGFlow` 和 AI Agent 工程开发的实战项目，「深度研搜」很可能是最适合你的项目。

它不是只调用一次大模型接口，也不是套一个搜索 API 做问答演示。这个项目围绕深度研究场景，用 DeepAgents 组织主智能体和专家子智能体，让系统可以根据任务需要查公开网络、查结构化数据库、查 RAGFlow 私有知识库、读取用户上传附件，并把最终结果整理成回答、Markdown 或 PDF。换句话说，你学到的不是某一个框架 API，而是一条 AI 应用从多智能体规划、工具接入、上下文隔离、接口交付到前端联调的完整项目主线。

> 本套仓库是 [ai-agents-from-zero](https://github.com/didilili/ai-agents-from-zero) 教程体系中的 [实战项目-深度研搜](https://github.com/didilili/ai-agents-from-zero/tree/main/%E5%AE%9E%E6%88%98%E9%A1%B9%E7%9B%AE-%E6%B7%B1%E5%BA%A6%E7%A0%94%E6%90%9C) 配套源码仓库，除了可直接运行和二次开发的项目代码之外，也提供了与教程章节对应的 Git 分支演进过程，以及完整的在线图文讲义入口。
> 如果你想系统学习「AI 智能体 大模型应用开发」，也可直接从系统教程 [AI 智能体实战速成指南-大模型入门](https://didilili.github.io/ai-agents-from-zero/#/) 开始。

![深度研搜前端首页：任务示例、助手状态和对话式多智能体研究台](docs/images/deepsearch-agent-home.jpg)

## 🎯 Agent Harness 企业级升级（面试版）

> **定位**：在 DeepAgents/LangGraph 之上自研 Harness 运行时层，把隐式 Agent Loop **显式化**为可见、可测、可控的工程系统。

### 面试一句话

> 在深度研搜场景下，我自研了领域 Harness：显式 Loop 管计划/校验/护栏/评测；生产调度权威是 Research StateGraph；检索步按稳定 Worker Profile 直调。原文进 Artifact/Evidence，模型只看短卡和 ref。MCP 是可插拔 capability 边界，Gateway 做身份、策略、熔断和审计。任务进度只认落库的 LoopState。

相关说明：[Harness 架构](docs/HARNESS_ARCHITECTURE.md) · [MCP](docs/MCP_SYSTEM.md) · [上下文](docs/CONTEXT_SYSTEM.md) · [Memory](docs/MEMORY_SYSTEM.md)

### Harness 五层架构

```text
Layer 5  体验层     React — Phase 时间线 / HITL 审批 / Eval / Trace
Layer 4  服务层     FastAPI — 任务调度 / WebSocket / GET /health
Layer 3  Harness层  Domain 控制面：plan / policy / memory / citation / eval / context store
Layer 2  Runtime层  Research StateGraph（生产权威）+ Leaf Worker（create_agent）
Layer 1  工具层     MCP Capability Plane ↔ LangChain tools（Tavily / MySQL / RAGFlow / Files）
```

### 核心能力矩阵

| 能力 | 实现 | 关键路径 |
|------|------|----------|
| 显式 Loop | per-step execute/compress/validate/recover | `app/agent/harness/loop.py` |
| 生产调度 | Research StateGraph；legacy while 仅回退 | `app/research/runtime/graph.py` [架构](docs/HARNESS_ARCHITECTURE.md) |
| 按步工人 | 稳定 Profile 直调，主图不二次路由 | `worker_runtime.py` / `worker_profiles.py` |
| LoopState 落库 | checkpoint.json 为任务进度权威 | `app/agent/harness/loop_state_store.py` |
| 结果校验 | step + finalize 双层校验 | `app/agent/harness/validator.py` |
| 失败恢复 | 结构化 hint + 重试上限 + Kill Switch | `app/agent/harness/recovery.py` |
| 上下文虚拟化 | Artifact/Evidence + JIT + glm-5.2 预算 + 短卡合同 | [CONTEXT_SYSTEM.md](docs/CONTEXT_SYSTEM.md) |
| 跨会话记忆 | 身份四元组 + 信任分级 + 来源台账 + SUPERSEDE | [MEMORY_SYSTEM.md](docs/MEMORY_SYSTEM.md) |
| MCP Capability Plane | Registry / 真 token / 并发池 / durable Tasks / DB 护栏 | [MCP_SYSTEM.md](docs/MCP_SYSTEM.md) |
| 可观测性 | WebSocket + Langfuse + JSONL 日志 | `app/api/monitor.py` `tracing.py` `trace_logger.py` |
| 评测回归 | 10 条 golden task + 8 项指标 + baseline | `tests/eval/` + CI |
| 配置化 | `harness.yml` 统一开关 | `app/config/harness.yml` |
| 健康检查 | `GET /health` 依赖探针 | `app/api/health.py` |

### 执行数据流（升级后）

```text
用户任务 → FastAPI(thread_id, user_id, tenant_id, project_id)
  → AgentHarness.run()
      → 绑定 MemoryIdentity + MCP ToolCallContext（签发 access token）
      → understand → plan → Research Brief
      → Research StateGraph：
            dispatch / Send fan-out
            按 Worker Profile 直调 web/db/kb/file 工人
            工具短卡进窗口，原文进 Artifact Store
            join → synthesis（JIT read_evidence）
      → LoopState 写入 checkpoint.json
      → finalize(memory remember + consolidation job) → JSONL trace
  → WebSocket 推送 phase 事件 → 前端时间线
```

架构图与教学版对照见 [docs/HARNESS_ARCHITECTURE.md](docs/HARNESS_ARCHITECTURE.md)。

### 快速验证（面试 Demo）

```bash
# 1. 单元测试（无需 API Key）
uv run python tests/test_harness_phase1.py
uv run python tests/test_harness_phase10_tools.py
uv run python tests/test_harness_phase16_mcp_production.py
uv run python tests/test_harness_phase20_runtime.py
uv run python tests/test_harness_phase23_context.py
uv run python tests/test_harness_phase24_memory.py
uv run python tests/test_harness_phase25_mcp.py

# 2. Eval 评测 + 基线对比
uv run python tests/eval/run_eval.py --dry-run --baseline tests/eval/results/baseline.json --report-md

# 3. Live 评测（需 .env 配置 LLM/Tavily）
uv run python tests/eval/run_eval.py --live --limit 3 --report-md

# 4. 健康检查
uv run uvicorn app.api.server:app --reload
curl http://localhost:8000/health

# 5. MCP（默认关闭；打开后 Tavily/MySQL/RAGFlow/Files 走 Gateway）
# harness.yml: mcp.enabled: true  或  HARNESS_MCP_ENABLED=true
# 生产再开 mcp.require_auth: true
uv run python -m app.mcp.servers.tavily_server
```

### Eval 指标（8 项）

| 指标 | 含义 | 目标 |
|------|------|------|
| TSR | 任务成功率 | ≥ 70% |
| TSA | 工具/子 Agent 选择准确率 | ≥ 80% |
| SSR | 各 step 校验通过率 | ≥ 85% |
| RR | 失败后恢复成功率 | ≥ 60% |
| ATC | 平均工具调用次数 | ≤ 8 |
| AL | 端到端延迟 | ≤ 120s |
| CR | 压缩率 | ≤ 30% |
| MRH | 跨会话记忆召回命中 | ≥ 80% |

CI 在每次 push/PR 时自动跑 dry-run eval，**TSR 下降 > 5% 则阻断合并**（见 [`.github/workflows/eval-regression.yml`](.github/workflows/eval-regression.yml)）。

### 面试必须能演示的 5 件事

1. 完整链路：搜索 + DB + 生成 PDF
2. 前端 Phase 时间线（确定性进度；HITL 审批时冻结，不闪烁）
3. **HITL 审批**：数据库步骤 gate 或 `generate_markdown` interrupt_on；任务进入「等待审批」暂停态
4. Eval 跑分报告（侧边栏 **Eval 面板**）
5. Trace 查看器（JSONL + Langfuse 外链）

### 生产特性（Phase 5 + Phase 6）

| 特性 | 入口 | 说明 |
|------|------|------|
| HITL `interrupt_on` | 对话页审批卡片 | `generate_markdown` / `convert_md_to_pdf` 执行前暂停 |
| Step Gate | 同上 | `database_query` 步骤执行前人工确认 |
| **Plan Review + Edit** | 同上 | 多意图任务计划审批，支持 JSON 编辑步骤（Phase 6） |
| **Dynamic Re-plan** | Harness Loop | 失败/用户编辑后动态插入步骤（Phase 6） |
| **Citation-First** | finalize + Trace | 证据链 `evidence.json`、参考文献块、CCR/HR 指标 |
| **Trajectory Diff Eval** | Eval 面板 | TDS 轨迹相似度 + golden `expected_trajectory` |
| Resume API | `POST /api/task/{thread_id}/resume` | `{decisions:[{type:"approve"\|"edit"\|"reject"}]}` |
| Eval 面板 | 侧边栏 → Eval 面板 | `GET /api/eval/latest` + 一键 dry-run |
| Trace 查看器 | 侧边栏 → Trace 查看器 | JSONL / **证据链** / Langfuse |

```bash
# HITL 演示任务
从数据库查询心血管药品库存，生成 Markdown 报告   # 先弹 step gate
搜索 AI 趋势并生成 Markdown 报告                 # 命中 generate_markdown interrupt_on
```

---

## 📖 项目介绍

在真实研究场景里，用户的问题经常不是一句普通问答可以解决的。

比如：

```text
结合公开资料、数据库信息和我上传的文档，整理一份机器人行业研究报告，并生成 PDF。
```

这个任务背后可能包含多类动作：

- 判断需要公开资料、内部数据、私有知识库还是本次上传文件；
- 去互联网搜索最新新闻、政策、产品或行业资料；
- 到 MySQL 查询企业结构化业务数据；
- 到 RAGFlow 查询内部非结构化文档；
- 读取用户上传的 PDF、Word、Excel、Markdown 或文本文件；
- 汇总多来源信息，判断资料是否足够；
- 生成 Markdown 报告，并在需要时转换成 PDF；
- 把执行过程、最终结果和生成文件实时展示给前端。

所以「深度研搜」更像一个会分工、会查资料、会生成交付物的研究助手。用户只需要提出任务，系统会在后端组织一条可观察的多智能体执行链路。

```text
用户任务
  -> FastAPI 接口接收请求（含 user/tenant/project）
  -> AgentHarness.run() 领域控制面
  -> Research StateGraph：intent / plan / Send 并行工人 / synthesis
  -> Worker Profile 注入最小工具集；MCP 或 LangChain 执行
  -> Artifact / Evidence 外置原文；短卡回窗口
  -> monitor + JSONL + Langfuse 记录全链路
  -> 前端展示 Phase 时间线、HITL 审批、答案和文件列表
```

## ✨ 项目亮点

- **自研领域 Harness（面试核心）**
  - 显式 per-step Loop：execute → compress → validate → recover。
  - 生产调度权威是 **Research StateGraph**；DeepAgents 不再当第二导演。
  - `harness.yml` 配置化 + budget 守卫 + JSONL + `GET /health`。
- **稳定 Worker Profile，而不是把全部工具塞给模型**
  - `web_researcher` / `db_researcher` / `kb_researcher` / `file_researcher` / `mixed_researcher`。
  - 公开研究看不到 SQL / RAG / 写文件；schema 稳定也利于 KV cache。
- **上下文虚拟化**
  - 工具先把原文写入 Artifact Store，模型只看到 snippet + `artifact_id`。
  - 写报告 JIT `read_evidence`，压缩摘要可回读。详见 [CONTEXT_SYSTEM.md](docs/CONTEXT_SYSTEM.md)。
- **Memory 生产门禁**
  - 请求级身份四元组、信任分级、来源台账、SUPERSEDE、durable consolidation。
  - 默认不把网页原文写入长期记忆。详见 [MEMORY_SYSTEM.md](docs/MEMORY_SYSTEM.md)。
- **MCP Capability Plane，而不是「挂几个 MCP 函数」**
  - Tavily / MySQL / RAGFlow / Files 均可在 LangChain 直连 ↔ MCP 之间切换。
  - 真 caller token、task-scoped policy、server env 隔离、并发 pool、durable Tasks、DB 行/字节/超时护栏。
  - 详见 [MCP_SYSTEM.md](docs/MCP_SYSTEM.md)。
- **一主多专家的信息源分工**
  - 网络搜索、MySQL、RAGFlow、会话文件仍是不同 capability；由 Harness 按计划调度，而不是主 Agent 自己路由。
- **从检索到交付的完整可运行链路**
  - 真实调用工具、读取数据、生成 Markdown / PDF，HITL 可打断写文件和查库。
- **长任务执行过程可观察**
  - 工具调用、工人调用、Phase 事件、Eval 面板、Trace 查看器。
- **Golden Task 评测 + CI 回归门禁**
  - 10 条评测任务、8 项指标、baseline 对比；GitHub Actions 自动跑分。
- **配套教程文档**
  - 教学版演进仍见下方章节；`main` 是当前完整 Harness 实现。

这套课程十分适合这些场景：

- 想系统学习 `DeepAgents`，但不想只停留在几个玩具示例。
- 想把 `Tavily`、`MySQL`、`RAGFlow` 和大模型放到同一个研究助手场景里理解。
- 想做一个比简单模型调用更接近真实开发的 AI Agent 项目。
- 想把项目写进简历，并且能说清楚智能体层、工具层、服务层、文件层和前端层分别做了什么。

## 🏗️ 系统架构

![深度研搜系统架构图：体验层、FastAPI、Domain Harness、Research StateGraph、Leaf Worker 与 MCP Capability Plane](docs/images/deepsearch-system-architecture.svg)

项目采用 **领域 Harness + Research StateGraph + Leaf Workers**。教学版里的「主智能体调度三个专家」仍然是能力分工的来源，但生产路径里主图不再二次路由：计划由 Harness 生成，StateGraph `Send` fan-out，工人按 Profile 直调。

项目围绕两条主线展开：

| 主线             | 做什么                                                       | 涉及模块                                                                  |
| ---------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------- |
| 多智能体深度研搜 | 规划、分派、多源检索、生成交付物 | `LangGraph StateGraph` / Leaf Workers / `Tavily` / `MySQL` / `RAGFlow` / MCP Gateway |
| 前后端实时闭环   | 启动后台任务、上传文件、推送执行过程、展示结果和下载生成文件 | `FastAPI` / `WebSocket` / `React` / `Vite`                                |

### 智能体与工具

| 归属           | 能力                                     | 工具                                                          |
| -------------- | ---------------------------------------- | ------------------------------------------------------------- |
| `web_researcher` | 互联网公开资料                             | `internet_search` + `read_artifact` / `read_evidence`         |
| `db_researcher` | 发现表、预览、只读 SQL                     | `list_sql_tables`、`get_table_data`、`execute_sql_query`      |
| `kb_researcher` | RAGFlow 知识库                             | `get_assistant_list`、`create_ask_delete`                     |
| `file_researcher` / 合成工人 | 读附件、写 Markdown/PDF            | `read_file_content`、`generate_markdown`、`convert_md_to_pdf` |
| Harness 控制面 | 计划、策略、记忆、引用、HITL               | 不是 Agent；不进模型 tool surface                              |

![深度研搜网络搜索任务执行页：WebSocket 事件流、工具调用和最终回答](docs/images/deepsearch-network-search-result.jpg)

## 🛠️ 项目技术栈

| 模块           | 技术                                             | 作用                                                                          |
| -------------- | ------------------------------------------------ | ----------------------------------------------------------------------------- |
| 智能体框架     | `LangGraph` + `langchain.agents.create_agent` | 生产调度权威是 Research StateGraph；Leaf Worker 直调 |
| 图与检查点     | `LangGraph` checkpointer + LoopState JSON | 图内 HITL interrupt；任务进度认 checkpoint.json |
| 模型与工具抽象 | `LangChain` / `langchain-core`                   | OpenAI 兼容模型、StructuredTool、MCP schema adapter |
| 大模型接入     | OpenAI 兼容接口                                  | `OPENAI_BASE_URL`、`OPENAI_API_KEY`；token 预算默认 glm-5.2 |
| 网络搜索       | `Tavily`（LangChain 或 tavily-mcp）              | 公开资料检索                                                |
| 结构化数据     | `MySQL` + `db_core` 护栏                         | SELECT-only、LIMIT/bytes/timeout、可选读副本与表白名单 |
| 私有知识库     | `RAGFlow` / `ragflow-sdk`                        | 内部文档问答                                              |
| 文件处理       | `pypdf` / `python-docx` / `pandas` / `ReportLab` | 读取上传附件，生成 Markdown / PDF                         |
| 后端接口       | `FastAPI` / `Uvicorn`                            | 任务、取消、上传、HITL resume、Eval、Trace、WebSocket |
| 实时通信       | `WebSocket`                                      | 工具调用、Harness Phase、HITL、最终结果 |
| 可观测性       | `Langfuse` + JSONL                               | 可选 Langfuse + `logs/traces/*.jsonl` |
| MCP            | `mcp` SDK + Gateway / Pool / Tasks               | stdio（local）或 streamable-http；四 Server 可切换 |
| Harness 配置   | `harness.yml`                                    | 编排 / 上下文 / 记忆 / MCP / SQL / HITL / budget |
| 评测           | `tests/eval/`                                    | golden task + 8 指标 + baseline 回归 |
| 前端           | `React` / `Vite` / `Ant Design` / `Tailwind CSS` | 对话式研搜、确定性 Phase 进度、HITL 暂停态、Eval、Trace |
| 依赖管理       | `uv` / `pnpm`                                    | Python 后端和前端依赖 |

## 📁 项目结构

```text
deepsearch-agents/
├── app/
│   ├── agent/
│   │   ├── harness/                # Domain Harness：loop / context / artifacts / profiles
│   │   ├── memory/                 # 跨会话记忆：identity / trust / ledger / consolidation
│   │   ├── subagents/              # 教学期专家定义（生产由 Worker Registry 直调）
│   │   ├── llm.py
│   │   ├── main_agent.py           # 委托 AgentHarness.run()
│   │   └── prompts.py
│   ├── research/
│   │   ├── runtime/                # Research StateGraph（生产调度权威）
│   │   ├── planning/               # Hybrid planner / policy / PlanPatch
│   │   └── workers/                # Leaf Worker factory + registry
│   ├── api/
│   │   ├── monitor.py              # WebSocket + report_phase()
│   │   ├── tracing.py / trace_logger.py
│   │   ├── health.py
│   │   └── server.py
│   ├── config/harness.yml
│   ├── mcp/                        # Capability Plane
│   │   ├── registry.py / server_registry.py / client.py
│   │   ├── mcp_gateway.py / tool_gateway.py / policy_context.py
│   │   ├── auth.py / session_pool.py / http_transport.py
│   │   ├── task_store.py / server_env.py / sql_guard.py
│   │   └── servers/                # tavily / mysql / ragflow / files
│   ├── tools/                      # LangChain 直连实现；与 MCP 共用 db_core
│   └── logs/traces/
├── docs/
│   ├── HARNESS_ARCHITECTURE.md
│   ├── RESEARCH_HARNESS.md
│   ├── CONTEXT_SYSTEM.md
│   ├── MEMORY_SYSTEM.md
│   ├── MCP_SYSTEM.md
│   └── AGENT_HARNESS_DESIGN.md     # 升级草案（部分已过时，以现行文档为准）
├── tests/
│   ├── eval/
│   ├── test_harness_phase10_tools.py
│   ├── test_harness_phase16_mcp_production.py
│   ├── test_harness_phase23_context.py
│   ├── test_harness_phase24_memory.py
│   └── test_harness_phase25_mcp.py
├── frontend/
├── .env.example
└── requirements.txt
```

## 🚀 快速开始

### 1. 准备环境

- Python `3.12`
- `uv`
- Docker 与 Docker Compose
- Node.js 与 `pnpm`
- 可用的大模型 API Key
- Tavily API Key
- RAGFlow 服务与 API Key

### 2. 克隆项目

```bash
git clone https://github.com/skyer2/deepsearch-agents.git
cd deepsearch-agents
```

> 教学版源码与章节分支仍见 [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents)。本仓库 `main` 是 Harness / MCP Capability Plane 现行实现。

### 3. 安装后端依赖

```bash
uv sync
```

### 4. 配置环境变量

```bash
cp .env.example .env
```

按本机实际服务和密钥修改 `.env`：

```bash
# LLM 配置
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=你的大模型_API_KEY
LLM_QWEN_MAX=qwen-max

# Tavily 配置
TAVILY_API_KEY=你的_TAVILY_API_KEY

# RAGFlow 配置
RAGFLOW_API_URL=http://your-ragflow-host
RAGFLOW_API_KEY=ragflow-your-api-key

# MySQL 配置
MYSQL_USER=root
MYSQL_PASSWORD=root
MYSQL_DATABASE=deepsearch_db
MYSQL_HOST=localhost
MYSQL_PORT=3307
# 只读查询可指向副本：MYSQL_READ_HOST=localhost
MYSQL_CHARSET=utf8mb4
MYSQL_COLLATION=utf8mb4_unicode_ci
MYSQL_SQL_MODE=TRADITIONAL
```

### 5. 启动 MySQL 教学库

本仓库的 `docker/mysql/mysql.sql` 会在 MySQL 容器首次创建数据目录时自动导入药品、库存和销售记录模拟数据。

```bash
docker compose -f docker/docker-compose.yaml up -d
```

### 6. 准备 RAGFlow 知识库

RAGFlow 不在本仓库的 Docker Compose 中启动，需要接入你已有的 RAGFlow 服务，或按配套教程部署。仓库内的 `docs/knowledge_base/` 提供了电商、金融等示例 PDF，可用于创建 RAGFlow 知识库和聊天助手。

如果暂时不使用私有知识库能力，也可以先跑网络搜索、数据库查询和上传文件读取链路；只有任务触发 RAGFlow 助手时才会依赖 `RAGFLOW_API_URL` 和 `RAGFLOW_API_KEY`。

### 7. 启动后端

```bash
uv run uvicorn app.api.server:app --host 0.0.0.0 --port 8000 --reload
```

后端默认接口：

| 接口                                | 说明                                   |
| ----------------------------------- | -------------------------------------- |
| `POST /api/task`                    | 启动一次 Harness / StateGraph 后台任务 |
| `POST /api/task/{thread_id}/cancel` | 取消指定会话任务                       |
| `POST /api/upload`                  | 上传一个或多个文件到当前会话           |
| `GET /api/files`                    | 列出当前会话输出目录中的生成文件       |
| `GET /api/download`                 | 下载输出目录中的文件                   |
| `POST /api/task/{thread_id}/resume` | HITL 人工审批恢复（approve/reject/edit） |
| `GET /api/task/{thread_id}/hitl/pending` | 查询待审批动作 |
| `GET /api/eval/latest` | Eval 面板：最新评测报告 |
| `POST /api/eval/run` | 触发 dry-run / live eval |
| `GET /api/traces/jsonl/{session_id}` | Trace 查看器：本地 JSONL |
| `GET /api/traces/langfuse/{session_id}` | Trace 查看器：Langfuse 代理 |
| `GET /api/harness/capabilities`     | 运行时能力面（StateGraph / 护栏 / HITL） |
| `GET /api/metrics/summary`          | 滚动窗口在线指标 |
| `GET /api/metrics/prometheus`       | Prometheus exposition |
| `GET /api/memory/recall`            | 按身份召回长期记忆 |
| `GET /api/tools/mcp`                | MCP Gateway / Server 状态 |
| `GET /api/tools/mcp/gateway/audit`  | MCP 耐久审计 |
| `WebSocket /ws/{thread_id}`         | 推送工具调用、助手调用、Phase 事件和结果 |

### 8. 启动前端

```bash
cd frontend
pnpm install
pnpm dev
```

前端默认连接：

```text
API: http://localhost:8000
WS:  ws://localhost:8000
```

如需修改，可以在 `frontend/.env.local` 中配置：

```bash
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_BASE_URL=ws://localhost:8000
```

### 9. 试几个任务

```text
从数据库中查询心血管药品的库存情况，并生成 Markdown 报告。
```

```text
搜索 2026 年 AI 在电商行业的应用趋势，并结合知识库资料生成一份 PDF。
```

```text
请先读取我上传的行业报告，再结合公开资料整理一份研究摘要。
```

## 📚 配套教程目录

教程总入口：[深度研搜完整教程](https://didilili.github.io/ai-agents-from-zero/#/%E5%AE%9E%E6%88%98%E9%A1%B9%E7%9B%AE-%E6%B7%B1%E5%BA%A6%E7%A0%94%E6%90%9C/0-%E5%89%8D%E8%A8%80)

| 章节 | 标题                                                                                                                                   | 学习重点                                                      | 对应分支                              |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------- |
| 0    | [前言](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/0-前言)                                                      | 项目定位、学习价值、技术栈和能力边界                          | `-`                                   |
| 1    | [DeepAgents 基础与核心概念](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/1-DeepAgents基础与核心概念)             | 智能体演进、框架定位、核心能力和多智能体设计边界              | `-`                                   |
| 2    | [DeepAgents 快速入门与流式解析](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/2-DeepAgents快速入门与流式解析)     | `create_deep_agent()`、`invoke`、`stream`、`chunk`            | `02-quickstart-streaming`             |
| 3    | [子智能体进阶与异步执行](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/3-子智能体进阶与异步执行)                  | 字典式子智能体、助手调度、`astream` 和嵌套边界                | `03-deepagents-subagents-async`       |
| 4    | [接入 LangGraph 与 LangChain](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/4-接入LangGraph与LangChain)           | `CompiledSubAgent`、LangGraph 子图、LangChain Agent 包装      | `04-deepagents-langgraph-langchain`   |
| 5    | [人机协作与中断恢复](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/5-人机协作与中断恢复)                          | 人工审批、编辑工具参数、中断和恢复执行                        | `05-deepagents-hitl-interrupt`        |
| 6    | [长期记忆与 Backend 存储](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/6-长期记忆与Backend存储)                  | `FilesystemBackend`、`StoreBackend`、`CompositeBackend`       | `06-deepagents-backends-memory`       |
| 7    | [中间件机制与 Skills 配置](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/7-中间件机制与Skills配置)                | 上下文摘要、模型调用限制、工具调用限制、自定义中间件和 Skills | `07-deepagents-middleware-governance` |
| 8    | [项目总览与工程初始化](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/8-项目总览与工程初始化)                      | 一主三从架构、9 个工具、前后端交互、工程目录                  | `09-deepsearch-core-config`           |
| 9    | [基础模块与模型配置](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/9-基础模块与模型配置)                          | `.env`、`ContextVar`、`monitor`、路径工具、模型和提示词配置   | `09-deepsearch-core-config`           |
| 10   | [网络搜索子智能体与 Tavily 工具](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/10-网络搜索子智能体与Tavily工具)   | `internet_search`、Tavily 配置、网络搜索助手组装和进度上报    | `10-deepsearch-network-subagent`      |
| 11   | [数据库查询子智能体与 MySQL 工具](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/11-数据库查询子智能体与MySQL工具) | 本地 MySQL、查表、预览数据、执行 SQL、数据库助手组装          | `11-deepsearch-database-subagent`     |
| 12   | [RAGFlow 子智能体与知识库准备](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/12-RAGFlow子智能体与知识库准备)      | RAGFlow 部署、助手列表查询、临时会话问答、知识库助手组装      | `12-deepsearch-ragflow-subagent`      |
| 13   | [主智能体搭建与异步执行](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/13-主智能体搭建与异步执行)                 | 主智能体组装、上传文件读取、Markdown/PDF 工具、会话目录隔离   | `13-deepsearch-main-agent`            |
| 14   | [FastAPI 接口与项目闭环](https://didilili.github.io/ai-agents-from-zero/#/实战项目-深度研搜/14-FastAPI接口与项目闭环)                  | 任务启动/取消、上传、文件列表、下载、WebSocket 和前端联调     | `14-deepsearch-api-websocket`         |

可以用分支切换对照每一阶段的代码演进：

```bash
git checkout 10-deepsearch-network-subagent
git checkout main
```

`main` 分支保留当前完整闭环版本。

## 🚧 能力边界

现行实现已经超过「能跑通多智能体 Demo」，也超过「能 list_tools 的 MCP 适配层」。它适合把 **领域 Harness / 上下文虚拟化 / Memory 门禁 / MCP 治理** 作为面试主线。它仍然 **不是** 完整多租户 SaaS。

### 已覆盖

- Research StateGraph 生产调度 + 稳定 Worker Profile 直调
- Progress Evaluator 进主图：语义缺口 / 冲突 → constrained PlanPatch；工人隔离并行 + SQLite ResearchState
- 上下文虚拟化：Artifact/Evidence + Tool Output Contract + JIT + glm-5.2 预算
- Memory：请求级身份、信任分级、来源台账、SUPERSEDE、durable consolidation
- MCP Capability Plane：四 Server 可切换、真 token、并发 pool、durable Tasks、DB 护栏、env 隔离
- golden eval（8 指标）+ baseline + GitHub Actions
- HITL：interrupt_on、step gate、计划审批、Edit-in-the-Loop；前端按 `idle / running / awaiting_approval` 冻结进度
- 前端确定性 Phase 进度条、Eval 面板、Trace 查看器（JSONL / Langfuse）
- `harness.yml` + budget / timeout / replan 上限 + `GET /health`

### 未覆盖（面试里主动说清）

- 完整企业 OIDC/IdP（现行是 HMAC access token scaffold，已校验 audience/scope/tenant，但不是完整 IdP）
- 多实例分布式限流与审计汇聚（现行限流是进程内存；审计已落 SQLite）
- Redis/Postgres 多实例 LangGraph Checkpointer（现行默认是单实例 SQLite 文件；LoopState JSON 仍作副作用热恢复）
- 远程 MCP replica 的运维部署（客户端已支持 stateless HTTP）
- 用户登录与产品级 RBAC UI
- 任务队列 / Celery 级分布式执行

这些适合作为「我知道还缺什么、下一层怎么演进」，而不是把已经落地的 Gateway、Tasks、DB 护栏说成空白。
