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

## 2. 主路径只有 AGENT

产品路径 **ANSWER / SEARCH 删除**。不问「这是事实题还是概念题」，每个任务都是研究 workload。

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
              Minimal Capabilities
               ├── web_search
               ├── fetch_url
               └── file_read
                       │
                       ▼
              Artifact / Evidence
                       │
                       ▼
              ProgressEvaluator
                /             \
            ENOUGH             GAP
              │                │
              │            PlanPatch
              │                │
              └───────◄────────┘
                       │
                   Synthesis
                       │
                 Quality Gate
                       │
                    Answer
```

最多两档，且第二档**不是产品能力**：

| 档 | 用途 | 路径 |
|----|------|------|
| **agent**（默认） | 研究对象 | Brief → Plan → Workers → Progress / Replan → Answer |
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
| ProgressEvaluator | semantic stopping |
| Replan / PlanPatch | 动态规划 + 有界自治 |
| Context Engineering | 长任务核心问题 |
| Artifact / Evidence | 上下文外置和可追溯 |
| Checkpoint | durability |
| Budget / Guardrail | bounded autonomy |
| Trace | failure attribution |
| Eval / Ablation | 证明机制有效 |

| 砍掉或降到最低 | |
|----------------|--|
| ANSWER 产品路径 | × |
| SEARCH 产品路径 | × |
| Search ranking / query expansion / 召回优化 | × |
| RAGFlow / DB / MCP plane / PDF | × |
| Personal Search UX / 复杂前端产品 | × |
| Cross-session Memory（Phase 2 课题） | 默认关闭 |

**可以砍掉 SEARCH 产品路径，不要砍掉 search tool。**

Memory：Phase 1 只研究单次 long-running Agent。`Context ≠ Memory`，`Checkpoint ≠ Memory`，`Evidence ≠ Memory`。跨任务经验积累放到 Phase 2。

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
- **原文外置**：Artifact / Evidence（Claim → Evidence → Artifact → Source）
- `LoopState` 只是进程内 handles
- `WorkerRuntime` 是图与 Agent 框架的边界
- 研究步允许 JIT 回读（`read_artifact` / `read_evidence`）；缺 JSON 时补 JSON，不整步重搜
- 步内限制联网工具次数（`max_step_tool_calls`），会话预算拦下一步而不是步内连打

---

## 6. 代码对应

| 概念 | 代码 |
|------|------|
| Experiment mode `agent` / `direct` | `app/research/routing/mode_router.py` |
| StateGraph | `app/research/runtime/graph.py` |
| WorkerRuntime | `app/research/runtime/worker.py` |
| Brief / Plan / Progress | `research_brief.py` / `planner.py` / `app/research/planning/` |
| Environment search/fetch | `app/tools/`（`internet_search`、`fetch_url`） |
| Artifact / Evidence | `artifacts.py`、`evidence_store.py` |

补充文档（非范围权威）：

- [OBSERVABILITY.md](./OBSERVABILITY.md) — Agent Flight Recorder（统一 Trace / Replan / Eval）
- [HARNESS_ARCHITECTURE.md](./HARNESS_ARCHITECTURE.md) — StateGraph 运行时细节
- [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md) — Context / Artifact / Evidence
- [BROWSECOMP_PLUS_EVAL.md](./BROWSECOMP_PLUS_EVAL.md) — 固定语料评测
- [OPENEULER_BARE_METAL.md](./OPENEULER_BARE_METAL.md) — openEuler 裸机部署（非架构范围）
