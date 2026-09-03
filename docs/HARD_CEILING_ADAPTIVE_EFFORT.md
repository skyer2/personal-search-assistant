# Hard Ceiling + Adaptive Effort Allocation

> **权威范围仍是 [ARCHITECTURE.md](./ARCHITECTURE.md)。**  
> 本文是预算与规划边界的设计说明：把「写死 retry/budget」从误区讲清楚，并落到  
> **Static Safety Ceiling + Adaptive Effort Allocation + Evidence-driven Stop/Replan。**

一句话主线：

> **Global deterministic control, local agentic autonomy.**  
> （全局确定性控制，局部 Agent 自治。）

---

## 0. 问题陈述

容易把三类责任混在一起：

| 责任 | 该谁管 | 不该谁管 |
|------|--------|----------|
| **语义规划 WHAT** | Lead Planner / Brief | 不该管超时、无限重试 |
| **执行 HOW locally** | Worker（独立 context 内搜索迭代） | 不该改全局计划 / 总预算 |
| **限制 WHETHER / HOW MUCH** | Harness Guardrail + Progress | 不该交给模型「我觉得还要搜」 |

`max_retries=2`、`max_tool_calls=40`、`max_run_sec=600` **不是 Planner 弱的补丁**，而是 OS 式配额：

```text
memory limit / CPU quota / timeout / max retry
```

模型不能说：「这个问题很复杂，给我无限 Token。」

真正该改的是：在 **硬顶之下**做 **自适应软资源分配**，而不是删掉硬顶。

---

## 1. 目标架构

```text
                    Research Brief
                          │
                          ▼
                  Complexity Estimator
                          │
                          ▼
                     Effort Plan ──────► soft budgets
                          │                  │
                          ▼                  │
                     Lead Planner            │
                     (full Brief)            │
                          │                  │
                      Objective DAG          │
                          │                  │
                          ▼                  ▼
                   Deterministic Guard ◄── Hard Ceiling
                          │
                          ▼
                       Scheduler
                    /      |      \
               Worker   Worker   Worker
             (local autonomy + step retrieval budget)
                          │
                  ProgressEvaluator
                    /            \
               ENOUGH             GAP
                  │            Budget remains?
                  │              /        \
                  │            YES        NO
                  │             │          │
                  │         PlanPatch   stop / synthesize
                  │         (+ grant)
                  └─────────────┴──────────┘
                                │
                            Synthesis
```

三层：

```text
Planner  → proposes work (+ effort hints)
Worker   → explores locally
Harness  → controls resources / stop / replan ceiling
```

---

## 2. Hard Ceiling（确定性安全顶）

来源：`app/config/harness.yml` 的 `budget` / `orchestration` / `planner`，以及 `.env` 覆盖（如 `HARNESS_MAX_RUN_SEC`）。

| 字段 | 含义 |
|------|------|
| `max_total_tokens` | 整次 run token 顶 |
| `max_tool_calls` | 整次 run 工具次数顶 |
| `max_step_tool_calls` | 单步联网检索顶（search/fetch） |
| `max_run_sec` | 墙钟顶 → `deadline_exceeded` |
| `max_replan_count` | PlanPatch 次数顶 |
| `max_plan_steps` | 计划步数顶 |
| `max_parallel_workers` | 并发 Worker 顶 |
| `planner.max_research_tasks` | 初始研究任务数顶 |
| `planner.max_plan_patch_tasks` | 单次 patch 新增任务顶 |

**规则：Adaptive Effort 只能 `min(申请值, Hard Ceiling)`，永远不能抬高 Hard Ceiling。**

---

## 3. Soft Budget：EffortPlan

`app/research/planning/effort.py`：

```text
ComplexityEstimator.estimate(brief, intent) → EffortPlan
apply_effort_to_hard_ceiling(effort, hard) → EffectiveBudget
```

### 3.1 Complexity（比单纯 compare-entity 更宽）

信号（确定性，不依赖 LLM）：

| 信号 | 推高复杂度 |
|------|------------|
| 多实体 / 比较 / 格局类 | breadth |
| `depth=thorough`、多 dimension | depth |
| 长 query、多 success_criteria | open-ended |
| `prefer_primary` / 新鲜度 recent | 证据门槛更高 → 略增检索 |
| PDF/MD 交付 | 略增（综合成本） |

分级：`narrow` | `compound` | `breadth_heavy` | `depth_heavy` | `open_ended`  
软档：`shallow` | `standard` | `thorough`（与 Brief.depth 对齐，可上调）

### 3.2 EffortPlan 字段

| 字段 | 含义 |
|------|------|
| `complexity` / `tier` | 复杂度与软档 |
| `suggested_research_tasks` | 建议初始研究任务数 |
| `suggested_workers` | 建议并行度 |
| `initial_session_tool_budget` | 建议会话工具预算（≤ hard） |
| `per_worker_tool_budget` | 建议每 Worker 检索预算（≤ hard step） |
| `replan_reserve_tasks` | 留给 GAP patch 的任务额度 |
| `reserve_step_tool_calls` | 留给 GAP 后新任务的检索额度 |
| `stop_criteria` | 给人读的停止提示（Progress 仍是裁决者） |

Lead Planner **可以**在 task metadata 里带 `effort: low|medium|high` 提示；**不可以**输出「必须 search 13 次」这种假精确。

### 3.3 Incremental Grant（GAP 时）

Progress → GAP 且 `can_replan`：

1. `PlanPatch` 新增任务数 ≤ `min(hard.max_plan_patch_tasks, remaining_plan_patch_tasks, severity)`  
2. 新任务的 `metadata.max_retrieval_calls` 从 `remaining_reserve_step_tool_calls` 发放  
3. 成功 patch 后 **扣减** `remaining_*`（`apply_grant_to_run_budget`）  
4. **不提高**会话 `max_tool_calls` / `max_run_sec` 硬顶  
5. reserve 耗尽 / 连续无边际收益 / replan 耗尽 → ENOUGH 或 abort（现有路径）

`run_budget.max_parallel_workers` 在 Plan 落盘后刷新 `RunSession.worker_sem` 与 legacy loop fan-out Semaphore。

Task 上的 `effort: low|medium|high` 只缩放该步 `max_retrieval_calls`（仍 ≤ hard step），禁止假精确 `exact_search_calls`。

---

## 4. Retry ≠ Replan

| Failure | 行为 |
|---------|------|
| JSON / schema 错 | **format-only retry**；`retrieval_remaining=0`；禁止重搜 |
| 空 Worker / unauthorized | JSON-only 或 fail-closed（现有 `JSON_ONLY_FAIL_REASONS`） |
| 网络瞬态 | 有界 retry（工具层） |
| Evidence 不足 | **Progress GAP → PlanPatch**，不是同 query 盲 retry |
| 任务过大 / 墙钟 | deadline / split via replan，不是无限 retry |
| 重复搜索无新证据 | Progress stop |

仓库已实现 JSON-only retry；本设计明确：**Retry 是 Recovery Policy，不是 Planner 权限。**

---

## 5. Lead Planner 必须吃完整 Brief

此前 LLM Lead Planner 只收到 `summary/deliverable/slots`，丢掉了 `entities/dimensions/depth/freshness/prefer_primary/success_criteria/...`。

现在 `{brief}` = `brief_payload_for_lead_planner(intent)`：

- 完整 `ResearchBrief.to_dict()`
- 外加 `summary` / `deliverable` / `slots` / `effort`（EffectiveBudget 摘要）

启发式 DAG / Worker prompt 本来就会用 Brief；LLM 路径与之对齐。

Lead Planner 仍只输出 **objective DAG**，不调工具、不调度、不定停止。

---

## 6. 何时 Multi-Agent，何时 Direct

| 场景 | 模式 |
|------|------|
| 简单事实 | `direct` / 单 Worker |
| 单主题深挖 | 少 Worker + 迭代检索 |
| 多实体 / breadth-heavy | Lead + 并行 Worker |
| 复杂 + 证据缺口 | + Progress + PlanPatch |

**不是「每个请求 3 个 Agent」。** 与 Anthropic Research / Gemini Deep Research / Perplexity 公开叙述一致：plan ↔ evidence 反馈，而不是一次 Plan 机械跑完。

---

## 7. 代码落点

| 模块 | 路径 |
|------|------|
| Effort / Ceiling | `app/research/planning/effort.py` |
| Compose 挂钩 | `app/research/planning/compose.py` |
| Full Brief → Lead | `app/research/planning/lead_planner.py` |
| 步检索预算 | `app/agent/harness/loop.py`（`step.metadata.max_retrieval_calls`） |
| GAP grant | `app/research/runtime/runner.py` `node_replan` |
| Hard 配置 | `app/config/harness.yml` `budget` + `effort` 注释段 |
| 能力清单 | `GET /api/harness/capabilities` → `control_model` / `effort` |

图边不变：仍在 domain compose / replan 挂钩，不新增 graph 节点。

---

## 8. 面试叙事（推荐）

> 我没有把整个 Research Loop 都交给一个 Supervisor Agent。系统拆成 **semantic control** 与 **operational control**：Lead Planner 只把 Research Brief 分解成 objective DAG；Worker 在独立 context 里自主搜索迭代；全局预算、超时、并发、checkpoint、权限由 **deterministic Harness** 控制。结果进 ProgressEvaluator；有 gap 时产生受约束的 PlanPatch。  
> 因此 `max_tool_calls` / `max_replan` / timeout 是 **hard safety ceiling**，不是 Planner 能力不足的 workaround。其下是 **adaptive effort allocation**：按任务复杂度提出 worker 数与研究深度，Runtime 按 evidence gain / remaining gaps 动态追加（不超过 ceiling）。

卖点一句：

> **Global deterministic control, local agentic autonomy.**

---

## 8.1 Research Brief = Task Understanding IR

Brief **不是** Intent，也 **不是** Plan：

| 层 | 回答 |
|----|------|
| Intent / Routing | 这是哪类任务？ |
| **Research Brief（IR）** | 用户究竟要什么？什么算完成？ |
| Planning | 为了完成它，要做哪些步骤？ |
| Harness Policy | 给多少自治权？ |

Progress 会对照 `success_criteria` / `constraints` 判 GAP（`unmet_*`），与 coverage / conflict / stale 并列。

`effort.adaptive_enabled`（`HarnessConfig.effort_adaptive_enabled`）关闭时，软配额贴齐硬顶，行为退化为静态 ceiling。

---

## 9. 非目标（本阶段不做）

- 删掉 Hard Ceiling 或交给模型自行抬顶  
- 自由 Agent Swarm / 无限 spawn  
- 跨会话 Self-evolving Skill 主路径（仍 Phase 2 Memory）  
- 把 Search 做成产品排序引擎  

---

## 10. 与路线图的关系

本设计对应「2027 Reliable Long-horizon Agent」中的：

```text
Planning × Verification × Environment × (bounded) Tools
```

Memory / Self-evolution / Agent Protocol 仍按 [ARCHITECTURE.md](./ARCHITECTURE.md) 分期，不在本次预算改造范围膨胀。
