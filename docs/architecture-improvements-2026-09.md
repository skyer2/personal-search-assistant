# 架构改进落地文档（2026-09）

## 背景与结论

依据架构评审（对照 Anthropic / OpenAI / Google 公开的 Deep Research 实践），
当前系统在 Semantic Planning、StateGraph 编排、WorkerRuntime 解耦等方向已接近业界水平。
本次改造聚焦评审指出的三个最高优先级差距，全部已实现并通过测试：

1. **三态资源决策**（评审第八节）：资源耗尽 ≠ 系统失败；
2. **Evidence-driven 边际收益停止**（评审第十节）：预算不再是"还剩几个 tool call"；
3. **TaskShape Router**（评审第十一节）：Multi-Agent 不是什么题都更好。

## 一、三态资源决策（CONTINUE / DEGRADE / ABORT）

### 问题

原 `evaluate_run_guardrails()` 对所有预算触顶（工具次数 / token / LLM 调用 /
检索单元 / 墙钟时限 / replan 次数 / 计划步数）一律返回 `abort=True`，
导致 `[abort] budget_tool_calls` —— 用户拿不到任何合成交付。

### 语义表（新）

| 条件 | 决策 | 行为 |
|------|------|------|
| 预算充足 | CONTINUE | 继续研究 |
| 工具 / token / LLM / 检索单元 / 时限 / replan 触顶 | DEGRADE | 跳过剩余检索步，强制进入 synthesis，基于已有证据交付 |
| 用户取消 / 状态损坏 | ABORT | 终止运行（当前仅保留语义位） |

### 实现要点

- `app/agent/harness/guardrails.py`：新增 `GuardrailAction` 枚举与三态
  `GuardrailDecision`；保留 `.abort` 兼容属性（仅 ABORT 为 True）。
- `app/agent/harness/loop.py`：`_apply_run_guardrails()` 在 DEGRADE 时标记
  `force_synthesis` + 剩余检索步 `skipped`，主循环推进到合成步；仅 ABORT 中断。
- `app/agent/harness/run_budget.py`：`RunBudgetManager` 把工具上限纳入
  `force_synthesis` 判定；`research_allowed()` 先报具体资源原因再回落通用原因。

## 二、Evidence-driven 边际收益停止

### 问题

原 Adaptive Effort 本质是规则型启发式（query 特征 → 工具额度），只有"预算耗尽"
一种停止路径。即使最近几波搜索零新增证据，也会烧完剩余额度。

### 实现（`app/research/planning/marginal_gain.py`）

每波 Worker 结果按指纹去重后计算增量：

- `new_evidence` / `new_sources` / `new_facts`（按 task_id 跨波去重）；
- 最近 `window=2` 波全部零增益且存在成功 worker → 停止研究
  （`marginal_gain_low`），即使预算未耗尽；
- 失败波不触发误停（零证据场景交给 Progress 的 coverage-gap 语义处理）。

状态通过 `ResearchState.marginal_gain` 序列化进 graph checkpoint，
`node_progress` 在评估后将 `gap → enough` 覆盖并写入观测事件。

## 三、TaskShape Router

### 问题

原 `mode_router` 只区分实验对照（direct/agent），agent 内部对所有任务使用同一拓扑。

### 实现（`app/research/routing/task_shape.py`）

| 形态 | 信号 | 执行拓扑 |
|------|------|----------|
| simple_fact | 事实型短问句、单实体 | 1 worker，无 replan（direct 候选） |
| single_topic_deep_dive | 深挖/原理/机制 | 1 worker 迭代 + 1 replan |
| breadth_heavy | 对比/多实体/多维度 | 3 workers 并行 + 1 replan |
| hybrid_conflict | 冲突/争议/矛盾 | 3 workers + 2 replan + progress 全链路 |

Router 在 intent 节点执行：分类结果写入 `route_signals`（可观测），
并行度 / replan 预算合并进 `ResearchState.budget`，并经
`apply_graph_to_loop()` 与 Adaptive Effort 取 min 后投影到运行时 —— 
**Router 只收敛拓扑，永远不抬高 hard ceiling**。

## 四、附带修复

- `tests/test_wave_progress_dispatch.py`：`/workspace/...` 硬编码路径改为仓库相对路径
  （跨 Windows/Linux 可运行），并显式 UTF-8 读取。

## 修改文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `app/agent/harness/guardrails.py` | 修改 | 三态决策核心 |
| `app/agent/harness/loop.py` | 修改 | DEGRADE 降级路径 + 跳过逻辑 |
| `app/agent/harness/run_budget.py` | 修改 | 工具上限纳入 force_synthesis；原因归因 |
| `app/research/planning/marginal_gain.py` | 新增 | 边际增益追踪与停止策略 |
| `app/research/routing/task_shape.py` | 新增 | 任务形态路由器 |
| `app/research/runtime/graph.py` | 修改 | intent 节点接入 TaskShape |
| `app/research/runtime/runner.py` | 修改 | 生产 intent/progress 节点接入 |
| `app/research/runtime/state.py` | 修改 | marginal_gain 状态字段 |
| `app/research/runtime/project.py` | 修改 | graph budget → LoopState 投影 |
| `tests/test_guardrail_three_state.py` | 新增 | 三态决策测试 |
| `tests/test_marginal_gain.py` | 新增 | 边际收益测试 |
| `tests/test_task_shape_router.py` | 新增 | 路由器测试 |
| `tests/test_harness_phase13_guardrails.py` | 修改 | 语义更新 |
| `tests/test_adaptive_effort.py` | 修改 | 语义更新 |
| `tests/test_wave_progress_dispatch.py` | 修改 | 跨平台路径修复 |

## 验证结果

```
pytest (18 个直接相关测试文件) → 126 passed
```

全量套件中另有 13 个失败均为预存环境问题，与本次改动无关：
缺依赖（uvicorn / langchain / opentelemetry）、预存配置漂移
（phase6/8/14 配置断言）、Windows SQLite 临时文件锁
（test_research_checkpoint，代码未改动）。

## 后续建议（按评审优先级）

1. Durable distributed execution（Redis/Postgres + 任务队列，移除单进程约束）；
2. Transactional Outbox：State 变更与 AgentEvent 同事务，消除多真相源；
3. 用 Eval 校准 TaskShape / marginal gain 阈值（当前为可解释规则型）；
4. Claim-level truth model 深化（subject/metric/period/scope 结构化冲突判定）。
