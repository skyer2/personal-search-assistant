# Research Agent Harness — 架构范围（框死）

> **A controllable and evaluable harness for long-running research agents.**
>
> This project is not a search engine.
>
> Search is only a tool environment used to study:
> - planning
> - multi-agent orchestration
> - progress evaluation
> - replanning
> - context management
> - durability
> - evidence grounding
> - evaluation
>
> **Deep Research 只是 Agent Harness 的 workload。Search 只是 Agent 可调用的一种环境能力。**
>
> 本文是本仓库的**唯一权威范围**。与本文冲突的旧文档（Personal Search、三档路由、Memory 主故事）一律视为历史。

---

## 0. 研究问题（只这十个）

1. Agent 如何把复杂任务拆成稳定 Research Plan？
2. 多 Worker 怎么并行且避免状态污染？
3. Worker 都完成后，怎么判断任务真的完成？
4. 什么情况下需要 Replan？
5. Replan 怎么限制，防止无限自治？
6. 长任务 Context 怎么控制？
7. 原始 Evidence 怎么在不塞爆窗口的情况下保留？
8. Agent 崩溃后如何恢复？
9. 如何确定失败发生在 Planning / Retrieval / Worker / Synthesis 哪一层？
10. Harness 的这些机制到底有没有实际增益？

**Search 不是第 11 个研究问题。** 不研究排序、query rewrite、召回质量、搜索引擎对比、freshness 产品化。

---

## 1. 四层：Search 不是一层

```text
┌─────────────────────────────┐
│ Research Domain             │
│ Brief / Plan / Progress     │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Agent Runtime               │
│ StateGraph / Budget /       │
│ Checkpoint / Parallelism    │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Worker Runtime              │
│ LangChain / DeepSeekHarness │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Environment                 │
│ Search / Fetch / File       │
│ Artifact / Evidence         │
└─────────────────────────────┘
```

Search 相当于强化学习里的 environment：Agent 与外界交互的接口，不是研究主体。

环境工具固定、尽量简单：

```python
search(query) -> SearchResult[]   # 标题 / URL / snippet
fetch(url)    -> Artifact         # 正文外置，不进 Graph State
file_read     -> Artifact         # 本地附件
```

实验时要：**same model + same search + same corpus**，只改变 Harness 机制。

---

## 2. 主路径只有 AGENT，Simple Fact 走受控旁路

产品路径 **ANSWER / SEARCH 删除**。`agent` 是唯一产品模式；TaskShape 只决定执行拓扑。单一稳定事实走 `SIMPLE_FACT` fast path，其余任务进入完整 Research Graph。

```text
                    User Task
                       │
                       ▼
                 Research Brief
                       │
                       ▼
                    Planner
                       │
                 Objective DAG
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       Worker 1     Worker 2     Worker N
          │            │            │
          └────── WorkerRuntime ─────┘
                       │
              dispatch barrier
                       │
              Minimal Capabilities
               ├── web_search
               ├── fetch_url
               └── file_read
                       │
                       ▼
              Artifact / Evidence
                      │
                      ▼
              Pure Assessments
               ├── Progress
               ├── Evidence
               ├── Execution Health
               └── Delivery Readiness
                       │
                       ▼
                  ControlPolicy
          dispatch / retry / replan / synthesize
          deliver partial / finalize
                      │
                   Synthesis
                      │
                Quality Gate
                      │
                 TerminalPolicy
                       │
                    Answer
```

最多两档，且第二档**不是产品能力**：

| 档 | 用途 | 路径 |
|----|------|------|
| **agent + simple_fact** | 单一稳定事实 | 一个 Worker → 搜索 → 来源分级 → deterministic answer |
| **agent + full graph** | 研究对象 | Brief → Plan → Workers → Progress / Replan → Answer |
| **direct** | 对照实验 baseline | Query → single agent + search tool → Answer |

Direct 用来回答：

> 同一个模型、同一个搜索工具，为什么增加 Agent Harness？增加以后得到了什么，又付出了什么？

---

## 3. 保留 / 砍掉

| 保留 | 为什么 |
|------|--------|
| Research Brief | 任务目标的稳定表示 |
| Planner | semantic decomposition |
| Objective DAG | multi-agent orchestration |
| Parallel Worker | 并行执行 + 隔离 |
| WorkerRuntime | Agent framework 解耦 |
| Pure Assessments | 语义进展、证据、执行健康、交付就绪 |
| ControlPolicy | 唯一 workflow routing authority |
| TerminalPolicy | 唯一 final outcome authority |
| Replan / PlanPatch | 动态规划 + 有界自治 |
| Context Engineering | 长任务核心问题 |
| Artifact / Evidence | 上下文外置和可追溯 |
| Checkpoint | durability |
| Budget / Guardrail | bounded autonomy（Hard Ceiling；其下 Adaptive Effort） |
| Trace | failure attribution |
| Eval / Ablation | 证明机制有效 |

补充设计：[HARD_CEILING_ADAPTIVE_EFFORT.md](./HARD_CEILING_ADAPTIVE_EFFORT.md) —  
**Global deterministic control, local agentic autonomy**；Retry ≠ Replan；Lead Planner 消费完整 Brief。

| 砍掉或降到最低 | |
|----------------|--|
| ANSWER 产品路径 | × |
| SEARCH 产品路径 | × |
| Search ranking / query expansion / 召回优化 | × |
| RAGFlow / DB / MCP plane / PDF | × |
| Personal Search UX / 复杂前端产品 | × |
| Cross-session Memory（Phase 2 课题） | 默认关闭 |

**可以砍掉 SEARCH 产品路径，不要砍掉 search tool。**

Memory：`Conversation History ≠ Context ≠ Long-term Memory`。UI 显示全部 Run，不等于全部进入模型上下文；ContextSelector 只取当前 Run、相关 RunSummary Top-K 和 Memory Top-K。长期记忆带 provenance / trust tier，并提供查看、单条遗忘、按 Run/Session 遗忘和用户级遗忘。

---

## 4. 实验主线

```text
Vanilla Agent          vs          Full Harness          vs          Harness - Replan
Query → single agent               Brief → Plan →                   关掉 PlanPatch
     + search → answer             Workers → Progress               其余相同
```

控制：same model / same search tool / same corpus / same tasks / same prompt budget。

观测：Accuracy、Citation、Success Rate、Tool Calls、Tokens、Latency，以及 Failure Attribution、Replan Trigger Rate、Recovery Rate、Context Consumption。

---

## 5. 状态模型（不变）

- **唯一 workflow truth**：`ResearchState` → LangGraph SQLite
- **Business Gap truth**：Gap 使用稳定 `gap_id`；Replan 用 replacement task 继承 Gap，不把 Task ID 当 Gap，也不追加必需任务
- **Task truth**：`TaskExecutionStatus` 与 `ResultStatus` 分离；`FAILED + PARTIAL` 是合法降级交付输入
- **并行 barrier truth**：同一 `dispatch_wave_id` 的所有 Worker 先 join，再执行一次 Progress / ControlPolicy；Worker 只返回自己的 task delta
- **Readiness truth**：`TaskReadiness` 是由 Plan dependency 与 resource 推导的临时值，不落成 Task 状态
- **Run / UI projection truth**：`RunStore` SQLite（`app/run_store/`）。刷新、断线、HITL、计时、文件列表都从这里 hydrate，不从 Trace 反推业务状态
- **删除语义**：删除 Run 会级联 RunStore 行、run 目录、Trace/Projection/Payload、Graph checkpoint、RunSummary 和 `provenance.run_id` 派生 Memory；删除 Session 会级联全部 Run
- **Agent history / debug**：Flight Recorder `AgentEvent` journal + JSONL。WebSocket 是 tail：先 `after_seq` replay，再 live
- **原文外置**：Artifact / Evidence（Claim → Evidence → Artifact → Source）
- `LoopState` 只是进程内 handles
- `active_tasks` / HITL `Future` / `RunJournal` 只是 execution cache
- `WorkerRuntime` 是图与 Agent 框架的边界
- 研究步允许 JIT 回读（`read_artifact` / `read_evidence`）；缺 JSON 时补 JSON，不整步重搜
- 步内限制联网工具次数（`max_step_tool_calls`），会话预算拦下一步而不是步内连打
- **部署不变量**：single backend process。不要用 `uvicorn --workers > 1`

```text
session_id / thread_id  = 一段会话（localStorage 只存这个）
run_id                  = 一次 Agent 执行（POST /api/task 每次新建）
turn_id                 = 前端一条用户问题（= run_id）
```

原则：

> Frontend state is a projection, not a source of truth.
> Persist before publish.
> Checkpoint restores execution; RunStore restores UX; Event Journal restores history.

---

## 6. 代码对应

| 概念 | 代码 |
|------|------|
| Product route and experiment mode | `app/research/routing/mode_router.py` |
| Task shape and fast-path budget | `app/research/routing/task_shape.py` |
| Simple-fact deterministic renderer | `app/research/runtime/simple_fact.py` |
| Source tier classification | `app/agent/harness/citations.py` |
| StateGraph | `app/research/runtime/graph.py` |
| Pure assessments | `app/research/assessment/` |
| ControlPolicy | `app/research/control/policy.py` |
| TerminalPolicy | `app/research/control/terminal_policy.py` |
| Task state / readiness | `app/research/domain/task_state.py` |
| Failure model | `app/research/domain/failure.py` |
| WorkerRuntime | `app/research/runtime/worker.py` |
| Brief / Plan / Progress | `research_brief.py` / `planner.py` / `app/research/planning/` |
| Environment search/fetch | `app/tools/`（`internet_search`、`fetch_url`） |
| Artifact / Evidence | `artifacts.py`、`evidence_store.py` |
| Run / UI projection | `app/run_store/` + `GET /api/sessions/{id}/bootstrap` |
| Run / Session delete cascade | `app/run_store/deletion.py` + `DELETE /api/runs/{id}` / `DELETE /api/sessions/{id}` |
| Long-term memory management | `app/agent/memory/` + `app/api/memory_routes.py` + Memory 面板 |
| Event replay | `app/observability/replay.py` + WS `subscribe.after_seq` |

补充文档（非范围权威）：

- [HARD_CEILING_ADAPTIVE_EFFORT.md](./HARD_CEILING_ADAPTIVE_EFFORT.md) — Hard Ceiling + Adaptive Effort + 面试叙事
- [OBSERVABILITY.md](./OBSERVABILITY.md) — Agent Flight Recorder（统一 Trace / Replan / Eval）
- [EVALUATION.md](./EVALUATION.md) — 五层 Eval 与 Vanilla / No-Replan / Full ablation
- [HARNESS_ARCHITECTURE.md](./HARNESS_ARCHITECTURE.md) — StateGraph 运行时细节
- [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md) — Context / Artifact / Evidence
- [BROWSECOMP_PLUS_EVAL.md](./BROWSECOMP_PLUS_EVAL.md) — 固定语料评测
- [OPENEULER_BARE_METAL.md](./OPENEULER_BARE_METAL.md) — 裸机部署（非架构范围）
