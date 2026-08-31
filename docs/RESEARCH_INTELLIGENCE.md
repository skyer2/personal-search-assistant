# Research Intelligence Loop（生产控制面）

> **定位**：商业 Deep Research 产品更多靠强模型和 Agentic Supervisor 获得研究智能；本项目探索的是另一端——把开放式 Research Agent 放进可控、可审计、可恢复、可治理的生产 Harness。
>
> **一句话**：LLM 负责研究语义，Harness 负责运行时权力。
>
> 日期：2026-08-28。本文是 Progress Evaluator / 真并行 / Durable ResearchState / 语义再规划 的权威设计；实现以本文为准。  
> **适用范围**：本文描述的 Progress / Replan 闭环仅适用于 **Deep 路径**；Quick 路径不进 Progress Evaluator。产品入口分流见 [PERSONAL_SEARCH.md](./PERSONAL_SEARCH.md)。

---

## 1. 问题

业界 Deep Research 的智能核心是：

```text
Evidence → 发现缺口 / 冲突 / 低置信 → 改搜索策略 → 再检索 → 综合
```

本仓库已经有较强的确定性控制面（Hybrid Planning、Constrained Lead Planner、Objective DAG、Plan Validator、StateGraph、governed MCP、Evidence Store、Quality Gate），但主图在四个地方仍弱于目标设计，也弱于 Anthropic / Open Deep Research 那种高度 agentic 的研究循环：

| # | 缺口 | 表现 |
|---|------|------|
| 1 | Progress Evaluator 未进主图 | 检索步 `status=done` 就合成；不看语义缺口、冲突、过时证据 |
| 2 | `Send` 不是真并行 | `node_research_worker` 把整步包在共享 `session.lock` 里，fan-out 被串行化 |
| 3 | 双状态、图内不耐久 | `LoopState` JSON + 进程内 `_SESSIONS` + `InMemorySaver`；pod 重启后图控制流不是唯一权威 |
| 4 | 再规划偏 execution recovery | 主要是 failed step → PlanPatch，不是根据研究结果判断「还不知道什么」 |

本文只打这四个闭环。**不再横向加 Skills / Knowledge Graph / 又一个 Agent。**

非目标：

- 不宣称结果质量超过 Anthropic / OpenAI / Gemini
- 不上完整 Postgres 多实例 checkpointer 集群（本轮 SQLite 文件即可）
- 不训练 RL research 模型，不把 Lead Planner 改回拥有 runtime 权力的 Supervisor Agent

---

## 2. 不变的架构边界（Split Supervisor）

```text
              LLM Brain                         Harness / Runtime
         semantic decisions                 validate / authorize /
                  │                         schedule / bound / persist
                  ▼                                    │
          Structured Proposal                          ▼
          (Brief / DAG / Patch)                     Tools / MCP
```

Lead Planner **仍然不是运行时**：

- 不能调工具、不能 spawn worker、不能决定何时停止
- 只输出 objective-level DAG / PlanPatch proposal
- 代码校验来源策略、依赖、预算、任务上限；StateGraph 才调度

Progress Evaluator 也遵守同一边界：

- **语义**：缺口、冲突、低置信、过时、缺维度（启发式，可后续换 LLM）
- **权力**：是否允许补任务、补几条、用哪些来源、是否强制进入合成 —— 由 Harness + `max_replan_count` / `max_plan_patch_tasks` 决定

面试表达：

> Agent decides what to research; Harness decides whether and how that research is allowed to execute.

---

## 3. 目标主图

```text
                    User
                     │
                     ▼
             Research Brief
                     │
                     ▼
             Planning Policy
            /       |       \
        Direct   Template   Dynamic
                            │
                            ▼
                     Lead Planner
                            │
                            ▼
                    Objective DAG
                            │
                            ▼
                    Plan Validator
                            │
                            ▼
                 Durable StateGraph
                            │
                         dispatch
                 ┌──────────┼──────────┐
                 ▼          ▼          ▼
              Worker     Worker     Worker     ← 隔离 WorkerState + Semaphore
                 │          │          │
                 ▼          ▼          ▼
            Evidence    Evidence    Evidence
                 └──────────┼──────────┘
                            ▼
                    Progress Evaluator          ← 本轮进入主图
                            │
                 ┌──────────┴──────────┐
                 ▼                     ▼
             GAP/CONFLICT            ENOUGH
                 │                     │
                 ▼                     ▼
           Replanner LLM           Synthesis
           (constrained)               │
                 │                     ▼
             PlanPatch            Quality Gate
                 │                     │
             Validator                 ▼
                 │                    END
                 └──── dispatch
```

`route_dispatch()` **不再**在「检索全 done」时直接 `synthesize`，也 **不再**在「有 failed」时直接 `replan`。无 READY 研究任务时一律进 `progress`。

```text
dispatch
  ├ ready research?  → Send(workers)
  ├ aborted?         → abort
  └ else             → progress

progress
  ├ abort            → abort
  ├ run（仍有 READY）→ dispatch
  ├ gap 且未耗尽     → replan → plan_validate → dispatch
  └ enough / 耗尽    → synthesize（允许部分失败）→ quality_gate
```

---

## 4. P0-1 Research Intelligence Loop

### 4.1 ProgressAssessment

`evaluate_progress()` 仍返回 `enough | gap | abort | run`，供旧测试与路由使用。结构化评估由 `assess_progress()` 产出：

```text
verdict: enough | gap | abort | run
coverage_gaps[]
conflicts[]
low_confidence_claims[]
stale_evidence[]
missing_dimensions[]
reason
```

写入 `ResearchState.progress_assessment`，并进入图 checkpoint。

### 4.2 判定规则（先启发式，可测，不依赖 LLM）

在 **无 READY 检索任务** 之后：

| 信号 | 来源 | 默认 verdict |
|------|------|----------------|
| 进程 aborted / 空计划 | LoopState / Graph | `abort` |
| 仍有 pending 且 deps 满足 | 调度器 | `run`（回到 dispatch） |
| 失败任务 / 空摘要 / payload.gaps | WorkerResult | `gap`（coverage） |
| 同指标显著不同数字 | facts / findings | `gap`（conflicts） |
| 低置信且无来源 | confidence / 措辞 | 计入 low_confidence；dynamic 时倾向 `gap` |
| 查询要 2026、证据停在更早年份 | 年份启发式 | stale；dynamic 时倾向 `gap` |
| 比较/商业化等维度缺失 | query + objective vs facts | missing_dimensions；dynamic 时 `gap` |
| 以上皆无 | — | `enough` → 合成 |

DIRECT / TEMPLATE 保持克制：没有显式 gaps/conflicts/失败时不要为了「再搜一次」而烧 token。DYNAMIC 才启用较完整的语义缺口。

Worker 回图时必须带上真实 `facts` / `sources` / `gaps` / `conflicts` / `confidence`（来自 `worker_payload`），禁止只填 400 字 summary、facts 恒为空——否则 Evaluator 看不见证据。

### 4.3 Constrained Replanner

`build_progress_patch()` 根据 assessment 生成最多 `planner.max_plan_patch_tasks` 条 objective-level 任务：

- coverage → `补充证据：{原 objective}`
- conflict → `交叉验证冲突：{conflict}`
- stale → `补充最新年份证据：{query 年份}`
- missing dimension → `补充维度：{dimension}`

仍然走 `apply_plan_patch()`：来源策略、环、步数上限、禁止「只搜网页」式 task。空补丁或校验失败 → `replan_exhausted=true`，**强制 enough**，避免 gap→replan→gap 死循环。

这是 **semantic research adaptation**，不是只对 failed step 做 execution recovery。失败步仍会进入 coverage_gaps，因此旧路径被新评估覆盖。

---

## 5. P0-2 True Parallelism

### 5.1 禁止

不要直接删掉 `session.lock`。三个 Worker 同时 mutate 同一份 `LoopState` 会产生竞态。

### 5.2 目标

```text
                Global ResearchState / LoopState
                       │
                     Send
             ┌─────────┼─────────┐
             ▼         ▼         ▼
       WorkerState WorkerState WorkerState   ← deepcopy + 本地增量
             │         │         │
          result     result     result
             │         │         │
             └─────────┼─────────┘
                       ▼
                    Reducer / merge lock
                 WorkerResult · EvidenceRef · TaskStatus · Usage
```

LangGraph 图状态已用 reducer（`task_status` merge_dicts、`worker_results` / `evidence_refs` append）。缺的是 **执行期隔离**。

### 5.3 实现约定

1. `asyncio.Semaphore(max_parallel_workers)` 限制并发。
2. HITL `interrupt()` 仍在隔离执行之前（不能在持锁等待审批）。
3. 执行期：`deepcopy(LoopState)`，清空子状态增量字段，调用现有 `_execute_and_validate_step`；**citation_manager 传 None**，避免共享引用登记竞态。
4. 执行期 **不持** `session.lock`，**不写** 共享 `checkpoint.json`。
5. Join：短 merge lock 合并 trace / tool_calls / 本步 `StepResult` / 计划状态；再登记 citation 与 idempotency；必要时写一步 checkpoint。
6. 合成节点保持串行（合成本应在 join 之后）。

验收：两个带 sleep 的工人墙钟应明显小于串行之和。

---

## 6. P0-3 Durable ResearchState

### 6.1 权威划分

| 状态 | 权威用途 | 持久化 |
|------|----------|--------|
| **ResearchState** | 计划、task_status、budget、replan、progress_assessment、abort | LangGraph checkpointer（默认 SQLite 文件） |
| **Artifact / Evidence Store** | 原文、span、finding | `evidence_store.json` / artifact 文件 |
| **LoopState JSON** | 工人执行上下文、step_results 摘要、热恢复副作用 | `checkpoint.json`（逐步降为非 control-critical） |
| **`_SESSIONS`** | 进程内 harness 句柄（不能进 checkpoint 的大对象） | 进程内存；崩溃后靠上面三层重建 |

> StateGraph 是 workflow authority，也必须是 **durable control-state authority**。
> LoopState / `_SESSIONS` 不再承担「图挂了控制流还在」的职责。

### 6.2 Checkpointer

- 默认：`langgraph-checkpoint-sqlite` + `output/.harness/graph_checkpoints.sqlite`（可用 `HARNESS_GRAPH_CHECKPOINT` / `orchestration.graph_checkpoint_path` 覆盖）
- 失败或 `orchestration.graph_checkpoint_backend=memory`：回退 `InMemorySaver`
- **本轮不做** Redis/Postgres 多实例；面试里仍主动说「单实例 SQLite，不是多副本」

崩溃恢复：

1. LoopState JSON 若命中，重建 `_SESSIONS`（工人要 harness / citation / 路径）
2. 若同一 `thread_id` 的图 checkpoint 仍有 `next` 或 interrupt → `ainvoke(None)` 续跑
3. 否则从 ResearchState 初始 payload 再进图（兼容无 SQLite 的旧热恢复）

原始网页 / SQL / PDF **仍然不进** checkpoint。

---

## 7. P0-4 与 Intelligence Loop 的关系

第四个问题不是独立子系统，而是把 Evaluator 接进主图之后的能力：

```text
旧：initial Plan + failed step → PlanPatch     （execution recovery）
新：Evidence → Progress → GAP → constrained Replanner → PlanPatch
```

Harness Intelligence 已经较厚；本轮补的是 Research Intelligence 的最小可测闭环，而不是再加基础设施模块。

---

## 8. 配置

```yaml
orchestration:
  progress_eval_enabled: true
  graph_checkpoint_backend: sqlite   # sqlite | memory
  graph_checkpoint_path: output/.harness/graph_checkpoints.sqlite
```

环境变量：`HARNESS_PROGRESS_EVAL`、`HARNESS_GRAPH_CHECKPOINT`、`HARNESS_GRAPH_CHECKPOINT_BACKEND`。

---

## 9. 验收

无 LLM 即可：

1. 动态比较任务：工人全部 `done` 但 Tesla 无收入 / Figure 只有旧年 / Unitree 数字冲突 → `verdict=gap`，且三类列表非空。
2. 主图：无 READY 时路由到 `progress`，enough 才 `synthesize`。
3. 空补丁或超过 `max_replan_count` → 不再循环，进入合成或 quality_gate。
4. 两隔离工人 sleep 重叠，墙钟 < 串行。
5. SQLite checkpointer：新进程/新 saver 能读到同一 `thread_id` 的 plan / task_status / progress_assessment。

---

## 10. 做好以后仍然不是 Anthropic

| | Anthropic / ODR | 本项目 |
|--|-----------------|--------|
| Supervisor | 语义脑 + delegation 权力 | 语义脑 + 确定性控制面（Split Supervisor） |
| 并行 | Lead 动态 spawn + 真并行 | 计划内 READY + 隔离 Worker + Semaphore |
| 缺口 | Lead/Supervisor reflection | Progress Evaluator（先启发式） |
| 耐久 | 产品运行时 | ResearchState + SQLite checkpointer |
| 治理 | 产品策略 | Source policy / MCP Gateway / Quality Gate |

潜在优势在权限、计划可验证、预算、审计、企业 source policy；研究自主性、模型训练、内部 eval 仍明显弱于商业产品。没有 benchmark 前不比较报告质量。
