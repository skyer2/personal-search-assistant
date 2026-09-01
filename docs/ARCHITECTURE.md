# Personal Search Assistant — 架构方案

> **一句话：** Personal Search Assistant 是一个 adaptive research agent：简单问题直接回答，需要最新信息时搜索，复杂问题进入可恢复的 Research StateGraph；Research Domain 负责分解、证据充分性和 Replan，Worker Runtime 负责局部自主执行，所有事实通过 Evidence/Artifact 可追溯。
>
> 本文是本仓库的**权威架构**。旧文档（Harness / Memory / Intent）只描述子域细节；与本文冲突时以本文为准。
>
> 本轮做减法，不再从「还缺什么 Agent 能力」加功能。

---

## 0. 从哪来，到哪去

上一阶段仓库仍是 `deepsearch-agents` 的演进：StateGraph 方向正确，但作为 **Personal Search Assistant** 不合格。

| 问题 | 表现 | 本轮收敛 |
|------|------|----------|
| 双 Source of Truth | `ResearchState`（LangGraph SQLite）+ `LoopState`（`checkpoint.json` + 进程内 `_SESSIONS`）都描述 workflow | **只有 `ResearchState` 负责 resume / interrupt / progression** |
| StateGraph 是壳 | 节点直接调 `AgentHarness._phase_*` / `_run_single_step`，另有 100KB+ legacy while-loop | 图拥有 workflow；领域服务只提供 Intent/Plan/Progress；工人走 `WorkerRuntime` |
| 功能面过宽 | OAuth / SQL replica / MCP durable / RAGFlow / DB gate 混进核心 | 核心 = Web + Files + Evidence + Memory；DB/RAG/MCP/PDF 退出默认故事 |
| Worker 按工具种类分 | `network_search` / `file_read` 变成 Tool Planning | Research 路径按 **objective / dimension** 拆任务；工人自己选 Web / File |

不继续加 Feature。P0 先把边界改对。

---

## 1. 四层（加一层产品路由）

```text
┌──────────────────────────────────────────┐
│ Personal Research API / UI               │
│ 自动 / 直答 / 搜索 / 研搜                 │
└───────────────────┬──────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────┐
│ Task Router                              │
│                                          │
│   ANSWER     SEARCH      RESEARCH        │
└──────┬──────────┬────────────┬───────────┘
       │          │            │
       ▼          ▼            ▼
     LLM     Search+Fetch   Research Domain
                               │
┌──────────────────────────────────────────┐
│ Research Domain                          │
│                                          │
│ ResearchBrief  Planner  ProgressEvaluator│
│ EvidencePolicy  QualityGate              │
└───────────────────┬──────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────┐
│ Research Runtime                         │
│                                          │
│ StateGraph  Checkpoint  Budget           │
│ Parallelism  Replan                      │
│ 唯一 workflow truth = ResearchState      │
└───────────────────┬──────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────┐
│ Worker Runtime                           │
│                                          │
│ WorkerRuntime Protocol                   │
│  ├─ LangChainWorkerRuntime   (now)       │
│  └─ DeepSeekHarnessRuntime   (future)    │
└───────────────────┬──────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────┐
│ Capabilities                             │
│                                          │
│ Web Search · Web Fetch · Local Files     │
│ optional: GitHub / MCP                   │
│                                          │
│ Artifact Store  ·  Evidence Store        │
└──────────────────────────────────────────┘
```

| 层 | 负责 | 明确不负责 |
|----|------|------------|
| API / UI | 对话、模式、来源展示 | 不编排研究步骤 |
| Task Router | 决定 ANSWER / SEARCH / RESEARCH | 不规划 DAG |
| Research Domain | Brief、按维度拆任务、证据是否够、Replan | 不调工具、不 while-loop |
| Research Runtime | 图、checkpoint、并行、预算 | 不把原文塞进 Graph State |
| Worker Runtime | 局部自主执行一个 ResearchTask | 不改全局 workflow |
| Capabilities | 搜索、抓取、读文件 | 不决定「要不要再搜一轮」 |

---

## 2. 三档路由（Fast Path 必须有）

不是每句都走 `intent → plan → dispatch → progress → quality gate`。

```text
                     User Query
                         │
                         ▼
                   Task Router
                /        |        \
               /         |         \
           ANSWER      SEARCH    RESEARCH
             │           │          │
             ▼           ▼          ▼
           LLM       Search+LLM   ResearchGraph
```

| 档 | 何时 | 图路径 | 不做 |
|----|------|--------|------|
| **ANSWER**（直答） | 概念题、不需要最新网页。例：「std::apply 是什么」 | `conversation → router → direct_answer → finalize` | 搜索、Plan、Progress |
| **SEARCH**（搜索） | 要最新事实，但仍是单目标。例：「glibc 2.42 release notes 改了什么」 | `conversation → router → search → fetch → synthesize → finalize` | Intent DAG、Replan、并行工人 |
| **RESEARCH**（研搜） | 多实体/多维度/要对证。例：「对比 LangGraph、Temporal 与 DeepSeek Harness 的 durable workflow」 | 完整 StateGraph | 不把简单定义题拉进研搜 |

用户显式选择优先于 Auto。Auto 用信号：时效词 → SEARCH；比较/多维度/报告 → RESEARCH；定义/短概念 → ANSWER。

API 兼容旧值：`quick` → SEARCH，`deep` → RESEARCH，`direct` → ANSWER。

PlanningMode（`DIRECT` / `TEMPLATE` / `DYNAMIC`）只存在于 **RESEARCH 内部**，与产品三档同名但不同层，不要混用。

---

## 3. ResearchState 是唯一 Workflow Truth

```text
ResearchState
├─ brief
├─ plan
├─ task_status
├─ findings          # 结构化摘要，不是原文
├─ progress_assessment
├─ replan_count
├─ budget
├─ final_answer / final_content
└─ status
      ↓
LangGraph Checkpointer（SQLite）
      = 唯一 resume / interrupt / progression
```

**砍掉的是「LoopState 作为第二套 workflow state」**，不是立刻删光 `LoopState` 这个 dataclass。

`LoopState` 降级为 **进程内 Runtime Handles**：

```text
LoopState（不再 checkpoint 为 workflow）
├─ artifact / evidence 句柄
├─ tracer / lock / citation_manager
├─ step_results 等工人副作用缓存
└─ 由 ResearchState 单向投影而来的 plan/intent 副本
```

投影方向固定：

```text
ResearchState  ──apply──▶  LoopState（给仍吃 LoopState 的领域函数）
领域函数返回值 ──写入──▶  ResearchState
```

禁止再维护：

```text
Graph SQLite  +  LoopState checkpoint.json
```

两套恢复系统。`persist_loop_state` **默认关闭**。进程崩溃后只从 LangGraph checkpoint 恢复控制流；原文仍在 Artifact / Evidence Store。

`RunSession` / `_SESSIONS` 只活在一次 `ainvoke` 期间，给工人适配器拿 handles，不是第二套 durable state。

### 3.1 为什么原文不进 Graph

```text
Claim  →  Evidence  →  Artifact  →  Original Source
```

Graph State 只留 `Finding` / `EvidenceRef` / `ArtifactRef`。这是现有设计里最值得留下的部分。

---

## 4. WorkerRuntime：图不再直调 Harness 工人循环

```python
class WorkerRuntime(Protocol):
    async def execute(
        self,
        task: ResearchTask,
        context: ResearchContext,
    ) -> WorkerResult: ...
```

```text
StateGraph.research_worker
        │
        ▼
  WorkerRuntime.execute(ResearchTask)
        │
        ├─ LangChainWorkerRuntime     # 现在：create_agent / DeepAgents
        └─ DeepSeekHarnessRuntime     # 将来：换 runtime 不改 Domain
```

`ResearchTask` 带的是 **objective + source_policy**，不是「请调用 network_search 工具」。工人自己决定 Web / Fetch / File。

本轮适配器仍会复用 `_run_single_step`（避免一次拆光 100KB loop 导致全红），但 **图节点只依赖 Protocol**。后续把 `_phase_*` 收成 Domain 包时，图不用再改。

---

## 5. RESEARCH 内：Brief 驱动，而不是 Tool Planning

Planner 先决定研究深度与维度，再拆并行 `ResearchTask`：

```json
{
  "mode": "research",
  "objective": "比较三个 Agent Runtime",
  "dimensions": ["durability", "context", "scheduling", "tool isolation"],
  "source_policy": { "prefer_primary": true }
}
```

Progress Evaluator 对照 Brief 的维度与证据要求，而不是对照「还剩几个 network_search 步」。细节见 [INTENT_AND_PLAN.md](INTENT_AND_PLAN.md)。

---

## 6. 能力面（Core vs Optional）

| Core（默认故事） | Optional / 非核心 | 已移出产品 |
|------------------|-------------------|------------|
| Web Search / Fetch | GitHub | DB / SQL gate |
| Local Files | MCP | RAGFlow |
| Evidence / Artifact | PDF 导出 | OAuth audience |
| Personal Research Memory | Prometheus / Langfuse | MCP durable task |

Memory **留下**，但主故事是：

- 用户上次研究过什么？
- 当时结论是什么？
- 哪些证据过期了？
- 这次是不是上次研究的 continuation？

而不是 TTL / half-life / SUPERSEDE 展览。实现可以保留，文档与产品叙事不再以它们为中心。见 [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md)。

---

## 7. 本轮落地范围（P0）与刻意不做的

**P0（本 PR）**

1. 项目身份：`personal-search-assistant`（pyproject / API / 前端包名 / README）
2. `ResearchState` 增加 `brief` / `findings`；workflow 字段以 Graph 为准
3. 默认关闭 `persist_loop_state`；生产路径不走 `_run_legacy_loop`
4. `WorkerRuntime` + `LangChainWorkerRuntime`；工人节点走接口
5. Task Router：ANSWER / SEARCH / RESEARCH（含 auto 与旧别名）
6. 图增加 `direct_answer`；SEARCH 沿用 search→fetch→synthesize；RESEARCH 沿用 StateGraph
7. 前端四档：自动 / 直答 / 搜索 / 研搜

**刻意留给后续 PR**

- 物理删除 `LoopState` 与拆光 `loop.py` 的 `_phase_*`（本轮已切断「它是 workflow SoT」）
- GitHub 能力接入
- Personal Research Memory 存储模型重做（本轮只改叙事与入口）
- 把 Planner step_type 从 `network_search` 完全改成 `research_task`

**P2 不做**

- 把 DB / RAG / PDF 加回核心路径
- 继续堆 MCP / HITL / 企业护栏当产品卖点

---

## 8. 与代码的对应

| 概念 | 代码 |
|------|------|
| Task Router | `app/research/routing/mode_router.py` |
| ResearchState | `app/research/runtime/state.py` |
| StateGraph | `app/research/runtime/graph.py` |
| Runner / 投影 | `app/research/runtime/runner.py`、`project.py` |
| WorkerRuntime | `app/research/runtime/worker.py` |
| Brief / Plan / Progress | `app/agent/harness/research_brief.py`、`planner.py`、`app/research/planning/` |
| Artifact / Evidence | `app/agent/harness/artifacts.py`、`evidence_store.py` |
| Conversation | `app/conversation/store.py` |
