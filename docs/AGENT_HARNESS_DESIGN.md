# DeepSearch Agents — Agent Harness 企业级升级设计文档

> **已过时。** 本文描述的是 `deepsearch-agents` 企业 Harness 阶段。  
> Personal Search Assistant 的权威架构是 [ARCHITECTURE.md](ARCHITECTURE.md)。下文仅作历史对照。

> **版本**: v1.0（设计草案 2026-07-15）  
> **现行实现**：以代码和下列文档为准，本文保留升级动机与早期路线，**不要把第 8 节旧 MCP 方案、Mem0 默认、HITL「暂不实现」当成当前状态**。
>
> **产品演进（2026-08）**：产品目标从企业多数据源 Demo 转向个人 Search Assistant + 按需 Deep Research。Harness 层保留；Mode Router / Quick 路径 / Conversation / UI 见 [PERSONAL_SEARCH.md](./PERSONAL_SEARCH.md)。
>
> | 主题 | 现行文档 |
> |------|----------|
> | **产品架构 / Mode Router** | [PERSONAL_SEARCH.md](./PERSONAL_SEARCH.md) |
> | 运行时 / StateGraph | [HARNESS_ARCHITECTURE.md](./HARNESS_ARCHITECTURE.md) · [RESEARCH_HARNESS.md](./RESEARCH_HARNESS.md) |
> | Deep 闭环 / Progress | [RESEARCH_INTELLIGENCE.md](./RESEARCH_INTELLIGENCE.md) |
> | 上下文虚拟化 | [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md) |
> | Memory 生产门禁 | [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md) |
> | MCP Capability Plane | [MCP_SYSTEM.md](./MCP_SYSTEM.md) |
>
> **日期**: 2026-07-15（草案）；实现已演进到 2026-08 Phase 25  
> **目标**: 将 `deepsearch-agents` 从教学级多智能体 Demo 升级为适合 Agent / Harness 工程师面试的企业级参考实现  
> **核心原则**: Harness 过程 **可见、可测、可控**

---

## 1. 背景与目标

### 1.1 升级前评估（2026-07）

下表是草案撰写时的缺口清单，**不是 2026-08 现行状态**。Loop / StateGraph / MCP / Memory / Eval 已落地，见文首文档地图。

| 维度 | 升级前 | 2026 面试要求 |
|------|----------|---------------|
| 场景 | 多源深度研搜 ✅ | 合理场景 ✅ |
| 多智能体 | 一主三从 ✅ | 需能论证设计取舍 |
| Agent Loop | 隐式（`create_deep_agent` 黑盒） | 显式 Harness Loop |
| MCP | 无 | 2026 企业默认标准；现行：Registry + Gateway + 四 Server 可切换，见 [MCP_SYSTEM.md](./MCP_SYSTEM.md) |
| 上下文压缩 | 框架内置，未显式化 | 需可控 compression 阶段 |
| 记忆 | `InMemorySaver` 会话级 | 跨会话 long-term memory |
| 结果校验 | 无 | validate 阶段 |
| 失败恢复 | 任务取消 only | 结构化 recover |
| 可观测性 | WebSocket monitor | Langfuse trace + 结构化日志 |
| 评测体系 | 无 | golden task + 指标 |
| 权限/安全 | 无 | 工具级权限 + SQL 白名单 |
| 持久化 | 内存 checkpointer | 可切换 Redis/Postgres |

### 1.2 升级目标

**定位转变**:

```
Before: DeepAgents 教学项目 — 能跑通多智能体研搜
After:  Deep Research 场景下的 Agent Harness 工程实践 — 可见、可测、可控
```

**面试叙事**:

> 在深度研搜场景下，自研 Agent Harness 运行时层，在 DeepAgents/LangGraph 之上实现显式 Loop、MCP 工具注册、上下文压缩、跨会话记忆、结果校验、失败恢复、Langfuse 全链路 trace 和 golden task 评测体系。

### 1.3 非目标（控制范围）

- 不做完整多租户 SaaS 平台
- 不替换 DeepAgents/LangGraph **作为单步发动机**
- 不把 DeepAgents 主图当作第二套编排器（Phase 20：检索步直调工人）
- 不自研 LLM 或搜索引擎
- 不做 Coding Agent 赛道

运行时收口说明见 [HARNESS_ARCHITECTURE.md](./HARNESS_ARCHITECTURE.md)。

---

## 2. 总体架构

### 2.1 五层架构

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 5: 体验层                                                  │
│   React 前端 — Phase 时间线 / Trace 查看 / Eval 面板 / 文件下载   │
├─────────────────────────────────────────────────────────────────┤
│ Layer 4: 服务层                                                  │
│   FastAPI — 任务调度 / 取消 / 上传 / WebSocket / 健康检查         │
├─────────────────────────────────────────────────────────────────┤
│ Layer 3: Harness 层（核心自研）                                    │
│   Loop 状态机 / Validator / Recovery / ContextBuilder / Compressor│
├─────────────────────────────────────────────────────────────────┤
│ Layer 2: Runtime 层                                                │
│   Research StateGraph（生产调度权威）+ Leaf Worker（create_agent） │
│   DeepAgents 不再当第二导演；legacy while 仅回退                     │
├─────────────────────────────────────────────────────────────────┤
│ Layer 1: 工具层                                                  │
│   MCP Capability Plane ↔ LangChain tools                         │
│   Memory Store（默认 SQLite；可选 postgres / mem0）               │
│   Observability（Langfuse + JSONL）                              │
└─────────────────────────────────────────────────────────────────┘
```

现行对照图见 [HARNESS_ARCHITECTURE.md](./HARNESS_ARCHITECTURE.md) 与 README 架构 SVG。

### 2.2 核心数据流

```
用户任务
  → FastAPI 接收（thread_id, user_id, tenant_id, project_id）
  → AgentHarness.run()
      → 绑定 MemoryIdentity + 签发 MCP access token
      → [understand] / [plan] → Research Brief
      → Research StateGraph.ainvoke
          → dispatch / Send fan-out
          → 按 Worker Profile 直调 web/db/kb/file
          → 工具短卡进窗口，原文进 Artifact Store
          → join → synthesis（JIT read_evidence）
      → LoopState 落盘 checkpoint.json
      → [validate] / [recover]；失败可 replan
      → [finalize] memory remember + consolidation job
  → Langfuse + JSONL
  → WebSocket 推送 phase 事件
```

### 2.3 目录结构（2026-07 目标树）

现行目录以 README「项目结构」为准（已增加 `app/research/`、`app/mcp/` Capability Plane、Artifact/Evidence、phase 测试）。下面保留草案目标树，便于对照演进。

```text
deepsearch-agents/
├── app/
│   ├── agent/
│   │   ├── harness/                    # 【新增】Harness 核心
│   │   │   ├── __init__.py
│   │   │   ├── loop.py                 # AgentHarness 主入口
│   │   │   ├── state.py                # LoopState / Phase 枚举
│   │   │   ├── planner.py              # 计划生成
│   │   │   ├── validator.py              # 结果校验
│   │   │   ├── recovery.py             # 失败恢复策略
│   │   │   ├── compressor.py           # 显式上下文压缩
│   │   │   └── context_builder.py      # 4 层上下文工程
│   │   ├── memory/                     # 【新增】记忆系统
│   │   │   ├── __init__.py
│   │   │   ├── store.py                # 长期记忆读写
│   │   │   └── extractor.py            # 任务结束后提取关键事实
│   │   ├── main_agent.py               # 【改造】委托给 Harness
│   │   ├── llm.py
│   │   ├── prompts.py
│   │   └── subagents/
│   ├── mcp/                            # 【新增】MCP 层
│   │   ├── __init__.py
│   │   ├── registry.py                 # 工具注册表
│   │   ├── client.py                   # MCP Client 桥接 LangChain tool
│   │   └── servers/
│   │       ├── tavily_server.py
│   │       ├── file_server.py
│   │       └── db_server.py            # 可选，渐进迁移
│   ├── api/
│   │   ├── server.py                   # 【改造】接入 Harness
│   │   ├── monitor.py                  # 【改造】新增 report_phase
│   │   ├── tracing.py                # 【新增】Langfuse 集成
│   │   └── context.py
│   ├── tools/                          # 【保留】逐步迁移到 MCP
│   ├── prompt/
│   └── utils/
├── tests/
│   └── eval/                           # 【新增】评测体系
│       ├── tasks.jsonl                 # golden tasks
│       ├── run_eval.py
│       ├── metrics.py
│       └── results/                    # eval 跑分结果
├── docker/
│   ├── docker-compose.yaml             # 【改造】加 Langfuse + Redis
│   └── langfuse/
├── docs/
│   └── AGENT_HARNESS_DESIGN.md         # 本文档
└── frontend/                           # 【改造】Phase 时间线 UI
```

---

## 3. Harness 运行时设计（核心）

### 3.1 显式 Agent Loop 状态机

```python
class Phase(str, Enum):
    UNDERSTAND = "understand"
    PLAN = "plan"
    BUILD_CONTEXT = "build_context"
    EXECUTE = "execute"
    COMPRESS = "compress"
    VALIDATE = "validate"
    RECOVER = "recover"
    FINALIZE = "finalize"

@dataclass
class LoopState:
    session_id: str
    phase: Phase
    intent: Optional[TaskIntent] = None
    plan: Optional[ExecutionPlan] = None
    step_index: int = 0
    step_results: list[StepResult] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 2
    trace: list[PhaseEvent] = field(default_factory=list)  # 供 eval 使用
    started_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)
```

### 3.2 各阶段职责

| 阶段 | 输入 | 输出 | 耗时预期 | 失败处理 |
|------|------|------|----------|----------|
| **understand** | 用户原始 query | `TaskIntent`（意图、信息源、交付物类型） | 1-3s | 回退为通用意图 |
| **plan** | TaskIntent | `ExecutionPlan`（有序 step 列表） | 2-5s | 使用默认 plan 模板 |
| **build_context** | plan + memory recall | `messages[]` 4 层上下文 | <1s | 跳过 memory 层 |
| **execute** | 当前 step | 子 Agent / 工具执行结果 | 10-60s | 进入 recover |
| **compress** | 冗长 tool 结果 | 结构化摘要（≤500 tokens） | 2-5s | 截断至前 N 字符 |
| **validate** | step + result | `(ok, reason)` | <1s | 进入 recover |
| **recover** | failed step + reason | 重试策略 + 新 hint | 1-3s | 超过 max_retries 则 abort |
| **finalize** | 所有 results | 最终交付 + 写 memory | 2-5s | 返回部分结果 + 警告 |

### 3.3 Loop 主流程伪代码

```python
class AgentHarness:
    async def run(self, task_query: str, session_id: str) -> HarnessResult:
        state = LoopState(session_id=session_id, phase=Phase.UNDERSTAND)
        session_dir = self._prepare_session(session_id)

        try:
            # Phase 1: Understand
            state = await self._phase_understand(state, task_query)

            # Phase 2: Plan
            state = await self._phase_plan(state, task_query)

            # Phase 3: Build Context（含 memory recall）
            context = await self._phase_build_context(state, task_query)

            # Phase 4-7: Execute → Compress → Validate → Recover 循环
            for i, step in enumerate(state.plan.steps):
                state.step_index = i
                result = await self._phase_execute(state, step, context, session_dir)
                result = await self._phase_compress(state, step, result)

                ok, reason = self._phase_validate(state, step, result, session_dir)
                if not ok:
                    if state.retry_count < state.max_retries:
                        state.retry_count += 1
                        result = await self._phase_recover(state, step, reason)
                        ok, _ = self._phase_validate(state, step, result, session_dir)
                    if not ok:
                        state.trace.append(PhaseEvent(phase="abort", reason=reason))
                        break
                state.step_results.append(result)

            # Phase 8: Finalize
            return await self._phase_finalize(state, session_dir)

        except asyncio.CancelledError:
            monitor.report_phase("abort", "cancelled")
            raise
        except Exception as e:
            monitor.report_phase("error", str(e))
            raise
        finally:
            reset_session_context(...)
```

### 3.4 与现有 `main_agent.py` 的集成

```python
# app/agent/main_agent.py（改造后）

from app.agent.harness.loop import AgentHarness

harness = AgentHarness(
    agent=main_agent,          # 现有 create_deep_agent 实例
    validator=ResultValidator(),
    recovery=RecoveryManager(),
    compressor=ContextCompressor(),
    memory=MemoryStore(),
    context_builder=ContextBuilder(),
)

async def run_deep_agent(task_query, session_id):
    """统一入口：委托给 Harness"""
    return await harness.run(task_query, session_id)
```

---

## 4. 结果校验（Validator）

### 4.1 校验规则

```python
class ValidationRule(Enum):
    FILE_EXISTS = "file_exists"           # 交付物文件存在
    FILE_MIN_SIZE = "file_min_size"       # 文件非空（>1KB）
    HAS_SOURCE_CITATION = "has_citation"  # 回答包含来源引用
    SQL_NOT_EMPTY = "sql_not_empty"       # SQL 结果非空
    SEARCH_MIN_LENGTH = "search_min_len"  # 搜索结果足够长
    PLAN_COVERAGE = "plan_coverage"       # 计划步骤都被执行
    NO_ERROR_KEYWORD = "no_error"         # 结果不含异常关键词

@dataclass
class ValidationResult:
    passed: bool
    rule: ValidationRule
    reason: str
    severity: Literal["error", "warning"]
```

### 4.2 按 Step 类型的校验矩阵

| Step 类型 | 必过规则 | 警告规则 |
|-----------|----------|----------|
| `network_search` | `SEARCH_MIN_LENGTH`, `NO_ERROR_KEYWORD` | `HAS_SOURCE_CITATION` |
| `database_query` | `SQL_NOT_EMPTY`, `NO_ERROR_KEYWORD` | — |
| `knowledge_base` | `SEARCH_MIN_LENGTH`, `NO_ERROR_KEYWORD` | — |
| `generate_markdown` | `FILE_EXISTS`, `FILE_MIN_SIZE` | — |
| `convert_pdf` | `FILE_EXISTS`, `FILE_MIN_SIZE` | — |
| `finalize` | `PLAN_COVERAGE` | `HAS_SOURCE_CITATION` |

### 4.3 Self-Check（可选增强）

校验失败时，用小模型做二次检查：

```python
SELF_CHECK_PROMPT = """
以下 Agent 回答是否基于工具返回的真实数据？
列出：1) 已覆盖的原始问题  2) 未覆盖的部分  3) 可能的幻觉内容
"""
```

---

## 5. 失败恢复（Recovery）

### 5.1 恢复策略表

| 失败原因 | 恢复策略 | 最大重试 |
|----------|----------|----------|
| `sql_empty` | 追加 hint: "先 list_tables 确认表名，再重新查询" | 2 |
| `no_file_generated` | 重新执行 generate_markdown step | 2 |
| `search_too_short` | 换关键词重搜，或换 topic 参数 | 2 |
| `wrong_subagent` | 重新 plan，追加缺失 step | 1 |
| `tool_timeout` | 降低 max_results 重试 | 1 |
| `context_overflow` | 触发 compress 后重试 | 1 |
| `max_retries_exceeded` | abort，返回部分结果 + 错误报告 | — |

### 5.2 Recovery 实现

```python
class RecoveryManager:
    STRATEGIES: dict[str, Callable] = {
        "sql_empty": self._retry_with_list_tables,
        "no_file_generated": self._retry_generate_file,
        "search_too_short": self._retry_with_broader_query,
        "wrong_subagent": self._replan_and_retry,
    }

    async def recover(self, state: LoopState, step: PlanStep, reason: str) -> StepResult:
        monitor.report_phase("recover", "start", step=state.step_index, reason=reason)
        strategy = self.STRATEGIES.get(reason, self._generic_retry)
        result = await strategy(state, step, reason)
        monitor.report_phase("recover", "done", step=state.step_index)
        return result
```

### 5.3 与框架级恢复的关系

| 层级 | 机制 | 职责 |
|------|------|------|
| 工具层 | try/except 返回中文错误 | 不中断 Loop |
| 框架层 | LangGraph checkpointer | 会话内状态恢复 |
| Harness 层 | RecoveryManager | 业务级精准重试 |
| 服务层 | asyncio.CancelledError | 用户主动取消 |
| 持久化层 | Redis checkpointer（可选） | 服务重启后续跑 |

---

## 6. 上下文工程与压缩（Context Engineering）

> **现行实现全貌（先框架后细节）：** [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md)  
> **面试讲法 + 问题清单：** [CONTEXT_INTERVIEW.md](./CONTEXT_INTERVIEW.md)
>
> 下面 6.1–6.3 是升级设计时的草案。其中「返回 `list[Message]`」「DeepAgents SummarizationMiddleware 85% 自动摘要」**不是**当前 Harness 主路径。以 CONTEXT_SYSTEM.md 和 `context_builder.py` / `compressor.py` 为准。

### 6.1 四层 Context Builder

```python
class ContextBuilder:
    def build(self, state: LoopState, task_query: str) -> list[Message]:
        return [
            *self.system_context(),        # 角色 + 规则（来自 prompts.yml）
            *self.memory_context(state),     # 长期记忆 recall
            *self.session_context(state),   # 工作目录 + 上传文件
            *self.task_context(task_query), # 用户任务 + 计划
            *self.tool_context(state),      # 动态裁剪的工具描述
        ]
```

| 层 | 来源 | 何时更新 | Token 预算 |
|----|------|----------|------------|
| System | `prompts.yml` | 静态 | ~2000 |
| Memory | Mem0 / Store search | 每次任务开始 | ~1000 |
| Session | ContextVar + session_dir | 每次任务 | ~500 |
| Task | 用户 query + plan | 每次任务 | ~2000 |
| Tool | MCP registry 动态裁剪 | 按 step 类型 | ~1500 |

### 6.2 三层压缩策略

```
Layer 1: 子 Agent 隔离（已有）
  → 子 Agent 在独立 context 执行，只回传结果

Layer 2: 框架自动压缩（DeepAgents 内置）
  → SummarizationMiddleware: 历史 messages 超 85% 窗口时自动摘要
  → Filesystem offloading: 大 tool 结果写文件，context 留指针

Layer 3: Harness 显式压缩（新增）
  → 子 Agent 结果回传后，compressor 压缩再进主 Agent context
```

### 6.3 Compressor 设计

```python
class ContextCompressor:
    MAX_COMPRESSED_TOKENS = 500

    async def compress(self, raw_result: str, step_type: str) -> str:
        if self._estimate_tokens(raw_result) <= self.MAX_COMPRESSED_TOKENS:
            return raw_result

        summary = await self.compression_model.ainvoke(
            COMPRESS_PROMPT.format(
                step_type=step_type,
                content=raw_result,
                max_tokens=self.MAX_COMPRESSED_TOKENS,
            )
        )
        monitor.report_phase("compress", "done", {
            "original_tokens": self._estimate_tokens(raw_result),
            "compressed_tokens": self._estimate_tokens(summary),
            "ratio": f"{len(summary)/len(raw_result):.0%}",
        })
        return summary
```

压缩模型选型：使用便宜的小模型（如 `qwen-turbo` / `gpt-4.1-mini`），与主模型分离。

---

## 7. 记忆系统（Memory）

### 7.1 三层记忆架构

```
┌─────────────────────────────────────────────┐
│  Long-term Memory（跨会话）                   │
│  Mem0 或 LangGraph Store                     │
│  存储：关键事实、用户偏好、历史研究结论         │
│  生命周期：持久化，任务结束时 extract 写入      │
├─────────────────────────────────────────────┤
│  Session Memory（单任务）                     │
│  session_dir + output/ 文件                   │
│  存储：中间产物、最终报告、上传文件             │
│  生命周期：任务期间                            │
├─────────────────────────────────────────────┤
│  Working Memory（当前 Loop）                  │
│  InMemorySaver checkpointer + messages        │
│  存储：当前会话 messages、tool results          │
│  生命周期：thread 期间，服务重启丢失             │
└─────────────────────────────────────────────┘
```

### 7.2 推荐方案：Mem0（面试友好）

```python
# app/agent/memory/store.py

from mem0 import Memory

class MemoryStore:
    def __init__(self):
        self.memory = Memory()

    async def recall(self, query: str, user_id: str, top_k: int = 5) -> list[str]:
        """任务开始时：按语义检索历史记忆"""
        results = self.memory.search(query, user_id=user_id, limit=top_k)
        return [r["memory"] for r in results]

    async def remember(self, facts: list[str], user_id: str, metadata: dict):
        """任务结束时：提取关键事实写入"""
        for fact in facts:
            self.memory.add(fact, user_id=user_id, metadata=metadata)

    async def extract_facts(self, task_result: str) -> list[str]:
        """用 LLM 从任务结果中提取可持久化的事实"""
        ...
```

### 7.3 记忆写入时机

```
finalize 阶段：
  1. 从 step_results 中提取关键事实（LLM extract）
  2. 去重（semantic similarity > 0.9 则 merge）
  3. memory.remember(facts, user_id=session_id)
  4. monitor.report_phase("memory", "saved", count=len(facts))
```

### 7.4 记忆召回时机

```
build_context 阶段：
  1. memory.recall(task_query, user_id=session_id)
  2. 注入 ContextBuilder.memory_context()
  3. 提示词追加："以下是该用户的历史研究记忆，请参考但注意时效性"
```

### 7.5 Demo 场景（面试 live demo）

```
第一次：「研究机器人行业趋势，生成报告」 → 完成，记忆写入
第二次：「继续上次的研究，补充 2026 年最新数据」 → 自动 recall → 增量研究
```

---

## 8. MCP 工具层（现行实现，已取代本节原草案）

> 原草案写「先 MCP 化 Tavily，DB/RAGFlow 后期迁移」。代码已经支持四个 Server 按开关切换，并且把 MCP 升级成 **Capability Plane**。细节以 [MCP_SYSTEM.md](./MCP_SYSTEM.md) 为准。

### 8.1 设计原则（现行）

- **不是全部 MCP 化**：MCP 是 pluggable provider；默认 LangChain 直连，打开开关后按 Server 替换。
- **Registry 是 Harness 自己的**：Server `list_tools()` 只 describe；`step_types` / permissions / 所有权由 Host policy 决定。
- **同一 choke point**：LangChain 与 MCP 共用 `db_core` + `ToolGateway`，MCP 路径不能绕过 SQL / step 策略。
- **MCP 不管治理**：身份、task allowlist、budget、approval、audit、副作用分类重试属于 Harness。

### 8.2 现行链路

```text
Worker Profile（最小 tool surface）
        ▼
PolicyEngine（principal ∩ task ∩ scopes ∩ step ∩ resource ACL）
        ▼
ToolGateway fail-closed
        ▼
MCP Gateway（trusted registry / token / breaker / retry taxonomy / durable audit）
        ▼
stdio pool（local）或 stateless HTTP（production）
        ▼
tavily-mcp / mysql-mcp / ragflow-mcp / files-mcp
        ▼
Normalizer + Tool Output Contract → 短卡 + artifact_id
```

四个 Server 均已实现：`app/mcp/servers/{tavily,mysql,ragflow,files}_server.py`。

### 8.3 已落地、草案里还写成「P1 / 暂不」的部分

| 草案说法 | 现行 |
|----------|------|
| 仅 Tavily 真 MCP | Tavily / MySQL / RAGFlow / Files 均可 MCP |
| 进程 env 自校验 OAuth | caller access token（audience/issuer/exp/tenant）；禁止 passthrough |
| 内存 dict Tasks | SQLite durable store + `tasks_get` |
| `os.environ.copy()` | per-server env allowlist |
| Session Pool 单 worker 串行 | `pool_size=3` round-robin + crash 重建 |
| `execute_sql_query` fetchall | LIMIT / max bytes / timeout / 读副本 / 表白名单 |
| 未知 tool 手工签名 | `inputSchema` → StructuredTool；未入 policy 则 fail-closed |

### 8.4 面试话术

> 搜索、库、知识库、文件都可以在 LangChain 直连和 MCP 之间切换，上层 Worker 不受影响。MCP 标准化 discovery/invocation；谁可以调、能调到哪张表、失败能不能重试，仍然是 Harness 的事。

---

## 9. 可观测性（Observability）

### 9.1 双通道观测

```
通道 1: WebSocket 实时推送（已有 monitor.py）
  → 前端 EventStream 展示 phase 时间线
  → 用户面向

通道 2: Langfuse 结构化 trace（新增 tracing.py）
  → 开发/评测面向
  → 支持回放、对比、成本分析
```

### 9.2 Langfuse 集成

```python
# app/api/tracing.py

from langfuse.callback import CallbackHandler

def create_trace_handler(session_id: str, metadata: dict) -> CallbackHandler:
    return CallbackHandler(
        session_id=session_id,
        metadata={
            "project": "deepsearch-agents",
            "harness_version": "1.0",
            **metadata,
        },
    )

# 在 Harness 中使用
config = {
    "callbacks": [create_trace_handler(session_id, {"phase": state.phase})],
    "configurable": {"thread_id": session_id},
}
```

### 9.3 Monitor 升级

```python
# app/api/monitor.py 新增

def report_phase(self, phase: str, status: str, **data):
    """上报 Harness 阶段事件"""
    self._emit("phase", f"[{phase}] {status}", {
        "phase": phase,
        "status": status,       # start / done / failed
        "step_index": data.get("step_index"),
        "duration_ms": data.get("duration_ms"),
        **{k: v for k, v in data.items() if k not in ("step_index", "duration_ms")},
    })
```

### 9.4 结构化日志（JSONL）

```python
# 每次 Harness run 写入 logs/traces/{session_id}.jsonl
{
    "trace_id": "uuid",
    "session_id": "xxx",
    "phase": "execute",
    "step_index": 1,
    "step_type": "network_search",
    "duration_ms": 4200,
    "status": "ok",
    "tool_calls": 3,
    "tokens_used": 8500,
    "timestamp": "2026-07-15T10:30:00Z"
}
```

### 9.5 前端 Phase 时间线（现行 UI）

进度条跟任务状态机走，**HITL 期间冻结**，不再用无限扫光当「还在跑」。

```
idle → running（确定性 %）→ awaiting_approval（暂停、无动画）→ running → completed
```

时间线展示：

```
understand  完成
plan        完成
execute     已暂停  Step 2/3 · database_query
```

审批卡片吸顶；顶栏 / 侧栏显示「等待审批」。原始事件日志默认折叠。
实现：`frontend/src/lib/runStatus.ts`、`phaseProgress.ts`、`components/RunProgress.tsx`。

---

## 10. 评测体系（Evaluation）

### 10.1 Golden Task 设计

```jsonl
// tests/eval/tasks.jsonl（每行一条）
{"id": "t01", "query": "搜索2026年AI电商趋势，生成Markdown报告", "expected_agents": ["网络搜索助手"], "expected_artifacts": ["md"], "expected_sources": ["network"], "difficulty": "easy"}
{"id": "t02", "query": "从数据库查询心血管药品库存，生成报告", "expected_agents": ["数据库查询助手"], "expected_artifacts": ["md"], "expected_sources": ["database"], "difficulty": "easy"}
{"id": "t03", "query": "结合公开资料和数据库，整理机器人行业报告并生成PDF", "expected_agents": ["网络搜索助手", "数据库查询助手"], "expected_artifacts": ["md", "pdf"], "expected_sources": ["network", "database"], "difficulty": "medium"}
{"id": "t04", "query": "读取上传的行业报告，结合网络资料生成摘要", "expected_agents": ["网络搜索助手"], "expected_artifacts": ["md"], "requires_upload": true, "difficulty": "medium"}
{"id": "t05", "query": "查询知识库中的金融电商报告，结合网络信息生成PDF", "expected_agents": ["网络搜索助手", "RAGFlow助手"], "expected_artifacts": ["pdf"], "difficulty": "medium"}
{"id": "t06", "query": "全面研究心血管药品市场：网络+数据库+知识库", "expected_agents": ["网络搜索助手", "数据库查询助手", "RAGFlow助手"], "expected_artifacts": ["md"], "difficulty": "hard"}
{"id": "t07", "query": "只搜索AI新闻，不需要生成文件", "expected_agents": ["网络搜索助手"], "expected_artifacts": [], "difficulty": "easy"}
{"id": "t08", "query": "查询不存在的表 xyz_nonexist", "expected_agents": ["数据库查询助手"], "expected_recovery": true, "difficulty": "hard"}
{"id": "t09", "query": "继续上次机器人行业研究，补充最新数据", "expected_memory_recall": true, "difficulty": "medium"}
{"id": "t10", "query": "生成一份关于量子计算的深度研究报告PDF", "expected_agents": ["网络搜索助手"], "expected_artifacts": ["pdf"], "difficulty": "medium"}
```

### 10.2 评测指标

| 指标 | 定义 | 目标 |
|------|------|------|
| **Task Success Rate (TSR)** | 最终交付物合格 + validate 通过 | ≥ 70% |
| **Tool Selection Accuracy (TSA)** | 调用了期望的子 Agent | ≥ 80% |
| **Step Success Rate (SSR)** | 各 step validate 通过比例 | ≥ 85% |
| **Recovery Rate (RR)** | 校验失败后重试成功 | ≥ 60% |
| **Avg Tool Calls (ATC)** | 平均工具调用次数（成本 proxy） | ≤ 8 |
| **Avg Latency (AL)** | 端到端耗时 | ≤ 120s |
| **Compression Ratio (CR)** | 压缩后 token / 原始 token | ≤ 30% |
| **Memory Recall Hit (MRH)** | 跨会话任务正确 recall | ≥ 80% |

### 10.3 Eval 运行流程

```bash
# 批量跑评测
uv run python tests/eval/run_eval.py --tasks tests/eval/tasks.jsonl --output tests/eval/results/

# 输出示例
# ═══ Eval Report 2026-07-15 ═══
# Tasks: 10 | Passed: 7 | Failed: 3
# TSR: 70% | TSA: 82% | SSR: 88% | RR: 67%
# ATC: 6.2 | AL: 95s | CR: 22%
# Failed: t06 (timeout), t08 (recovery failed), t09 (memory miss)
```

### 10.4 回归策略

```
每次改动 prompt / Harness / 工具 后：
  1. 跑 eval → 对比上次结果
  2. TSR 下降 > 5% → 阻断合并
  3. 结果写入 tests/eval/results/ 存档
  4. 面试时展示「优化前后对比」
```

---

## 11. 企业级补充能力

以下不在 MVP 范围，但需在设计和面试中体现「我知道还缺什么」。

### 11.1 安全与权限

| 能力 | MVP 方案 | 生产方案 |
|------|----------|----------|
| SQL 注入防护 | 禁止 DDL/DROP；只允许 SELECT | SELECT-only + 表白名单 + LIMIT/bytes/timeout + 读副本（已落地，见 sql_guard / db_core） |
| 文件越界读取 | `resolve_path` 约束 session_dir ✅ 已有 | chroot 沙箱；Resource ACL 已校验 session |
| 工具权限 | MCP Server 声明 permissions（不可信） | Host PolicyEngine：principal ∩ task ∩ step；trusted registry |
| HITL 审批 | 已实现 interrupt_on / step gate / 计划审批 | 高风险写操作审批流可再接到企业 IdP |
| 密钥管理 | `.env` + per-server env allowlist | Vault / K8s Secret |
| 输入消毒 | 基础 prompt 约束 + `<untrusted>` | 内容审核 API |

### 11.2 持久化与可靠性

| 能力 | MVP | 生产 |
|------|-----|------|
| Checkpointer | `InMemorySaver`（图内）+ LoopState JSON | `RedisSaver` / `PostgresSaver` |
| 任务队列 | `asyncio.create_task` | Redis Queue / Celery |
| 幂等性 | 同 thread_id 互斥 + `IdempotencyRegistry` | 请求去重 + 幂等 key |
| 超时控制 | per-step + 全局 `max_run_sec` | 更细的 deadline 传播 |
| 熔断 | per-MCP-server circuit breaker ✅ | 跨实例共享状态 |

### 11.3 成本治理

```python
@dataclass
class CostBudget:
    max_total_tokens: int = 100_000
    max_tool_calls: int = 20
    max_retries: int = 2
    compression_model: str = "qwen-turbo"  # 便宜模型做压缩

# Harness 在每个 phase 检查预算
if state.total_tokens > budget.max_total_tokens:
    monitor.report_phase("budget", "exceeded")
    return partial_result
```

### 11.4 审计与合规

```python
# 每次 Harness run 生成审计记录
@dataclass
class AuditRecord:
    session_id: str
    user_query: str
    plan: ExecutionPlan
    tools_called: list[str]
    agents_called: list[str]
    validation_results: list[ValidationResult]
    recovery_attempts: int
    final_status: str
    duration_ms: int
    tokens_used: int
    artifacts: list[str]
    timestamp: datetime
```

### 11.5 限流与并发

| 场景 | MVP | 生产 |
|------|-----|------|
| 同用户并发 | 同 thread_id 互斥 ✅ | 用户级 semaphore |
| 全局并发 | 无限制 | 全局 queue + worker pool |
| 模型 TPM | 无控制 | rate limiter + 排队 |

### 11.6 健康检查与部署

```python
# GET /health
{
    "status": "ok",
    "dependencies": {
        "llm": "ok",
        "mysql": "ok",
        "tavily": "ok",
        "ragflow": "degraded",
        "langfuse": "ok",
        "mem0": "ok"
    },
    "version": "1.0.0-harness"
}
```

### 11.7 版本化与配置管理

```yaml
# app/config/harness.yml
harness:
  max_retries: 2
  compression:
    enabled: true
    max_tokens: 500
    model: "qwen-turbo"
  memory:
    provider: "mem0"        # mem0 | store
    recall_top_k: 5
  validation:
    strict_mode: false      # true = warning 也算失败
  observability:
    langfuse_enabled: true
    jsonl_log_enabled: true
  budget:
    max_total_tokens: 100000
    max_tool_calls: 20
```

---

## 12. 依赖变更

```toml
# pyproject.toml 新增依赖
dependencies = [
    # ... 现有依赖 ...
    "langfuse>=2.0.0",           # 可观测性
    "mem0ai>=0.1.0",             # 长期记忆（可选）
    "mcp>=1.0.0",                # MCP SDK
    "tiktoken>=0.5.0",           # token 估算
]
```

```yaml
# docker/docker-compose.yaml 新增服务
services:
  langfuse:
    image: langfuse/langfuse:2
    ports: ["3000:3000"]
    depends_on: [langfuse-db]
  langfuse-db:
    image: postgres:15
  redis:
    image: redis:7
    ports: ["6379:6379"]
```

---

## 13. 实施路线图

### Phase 1: Harness 核心（第 1 周）

| 天 | 任务 | 产出 |
|----|------|------|
| D1 | `harness/state.py` + `harness/loop.py` 骨架 | 显式 5 阶段 Loop 能跑 |
| D2 | `validator.py` + `recovery.py` | 校验 + 恢复能工作 |
| D3 | `monitor.report_phase()` + 前端展示 | Phase 时间线可见 |
| D4 | 改造 `main_agent.py` 委托 Harness | 原有功能不回归 |
| D5 | 手动测试 5 个场景 | 基线可演示 |

### Phase 2: 观测评测（第 2 周）

| 天 | 任务 | 产出 |
|----|------|------|
| D1 | Langfuse 接入 + `tracing.py` | trace 截图 |
| D2 | `tests/eval/tasks.jsonl` 10 条 | golden set |
| D3 | `run_eval.py` + `metrics.py` | 首次 eval 跑分 |
| D4 | `compressor.py` | 压缩率数据 |
| D5 | 优化 prompt → 重跑 eval | 前后对比数据 |

### Phase 3: 记忆 + MCP（第 3 周）

| 天 | 任务 | 产出 |
|----|------|------|
| D1-D2 | `memory/store.py` + Mem0 接入 | 跨会话 recall demo |
| D3-D4 | `mcp/registry.py` + tavily/file MCP | MCP 工具发现 |
| D5 | 整合测试 + 文档更新 | 完整可面试演示 |

---

## 14. 面试准备清单

### 14.1 必须能演示

- [ ] 本地跑通：搜索 + DB + 生成 PDF 完整链路
- [ ] 前端 Phase 时间线可见
- [ ] Langfuse trace 截图
- [ ] Eval 跑分报告（有前后对比更佳）
- [ ] 跨会话 memory recall demo
- [ ] 校验失败 → 自动恢复 demo

### 14.2 必须能讲清

- [ ] 为什么深度研搜需要多 Agent（场景论证）
- [ ] 隐式 vs 显式 Loop 对照图
- [ ] Harness 五层架构
- [ ] 三层压缩策略
- [ ] 三层记忆架构
- [ ] MCP Registry 设计
- [ ] 校验规则 + 恢复策略表
- [ ] Eval 指标定义
- [ ] 已知局限 + 生产演进路线

### 14.3 必须准备的「踩坑故事」

- [ ] 模型乱写绝对路径 → `resolve_path` 修复
- [ ] 子 Agent 漏调 → validate 检测 + recover
- [ ] 长任务 token 爆炸 → compression 解决
- [ ] eval 优化 prompt 后 TSR 提升 X%

---

## 15. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Harness 层增加延迟 | 每阶段多 1-3s | 压缩/校验用小模型；plan 可缓存 |
| Mem0 依赖外部服务 | demo 不稳定 | 降级为 LangGraph InMemoryStore |
| MCP 迁移破坏现有工具 | 功能回归 | 渐进迁移，保留 `@tool`  fallback |
| Langfuse 自托管复杂 | 搭建耗时 | Docker Compose 一键启动 |
| eval 场景依赖外部 API | 跑分不稳定 | mock 工具层 + 标记 `requires_live` |

---

## 16. 成功标准

| 标准 | 指标 |
|------|------|
| 功能不回归 | 原有 3 个示例任务全部通过 |
| Harness 可见 | 前端能展示完整 phase 时间线 |
| Harness 可测 | eval TSR ≥ 70%，有跑分报告 |
| Harness 可控 | 校验失败能自动恢复，有次数上限 |
| 面试辨识度 | 能讲清 5 层架构 + 与 open_deep_research 的差异 |
| 部署可行 | `docker compose up` 一键启动全部依赖 |

---

## 附录 A：与 open_deep_research 的差异

| 维度 | open_deep_research | 本项目（升级后） |
|------|-------------------|-----------------|
| 场景 | 纯网络研搜 | 多源（网络+DB+RAGFlow+文件） |
| 框架 | LangGraph StateGraph | DeepAgents + Harness 外包 |
| 压缩 | 图节点级 compress | Harness 层 compress + 框架自动 |
| 评测 | Deep Research Bench | 自定义 golden task |
| 前端 | LangGraph Studio | 自研 React + Phase 时间线 |
| 优势 | 官方标杆、bench 成绩 | 全栈 + 多源 + Harness 工程化 |

## 附录 B：面试 30 秒 Elevator Pitch

> 我做了一个 Deep Research 场景的领域 Harness。显式 Loop 管计划、校验、护栏和评测；生产调度权威是 Research StateGraph；工人按稳定 Profile 直调。上下文把原文外置到 Artifact/Evidence，模型只看短卡和 ref。MCP 是可插拔 capability 边界，不是唯一通道；Gateway 做身份、策略、熔断和审计。技术栈是 LangGraph + FastAPI + React，Harness 让过程可见、可测、可控。

## 附录 C：关键参考

- [open_deep_research](https://github.com/langchain-ai/open_deep_research) — 同赛道生产级参考
- [DeepAgents Context Engineering](https://docs.langchain.com/oss/python/deepagents/context-engineering) — 框架内置压缩
- [Langfuse Docs](https://langfuse.com/docs) — 可观测性
- [Mem0](https://github.com/mem0ai/mem0) — 记忆层
- [mcp-agent](https://github.com/lastmile-ai/mcp-agent) — MCP 编排参考
- [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) — 评测框架参考
