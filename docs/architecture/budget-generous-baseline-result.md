# Budget Generous Baseline Result

日期：2026-09-11  
状态：实现完成；全量回归、评测门禁、前端投影、发布冒烟和 Golden Query 生产拓扑 E2E 均通过。

## 目标

本轮只调整预算基线，不改变八节点 Research StateGraph，不新增 Agent，不引入动态预算策略。当前阶段优先级是 **完整性 > 成本 > 延迟**：正常复杂 Worker 不应因为过紧的 per-worker token 或 LLM call 上限提前死亡。

## 前后对比

| 领域 | 修改前 | 修改后 |
|---|---|---|
| Run 硬顶 | 300K token / 80 LLM calls / 240 tool calls / Worker idle 75s | 500K token / 120 LLM calls / 300 tool calls / Worker idle 120s |
| per-worker Profile | small 12K/3/3/6/4；medium 24K/4/4/8/6；large 40K/6/6/12/8 | small 40K/10/6/10/10；medium 80K/16/10/16/16；large 120K/24/16/24/24 |
| 预算权威 | `ResearchTaskRequest`、`ResearchState`、Run config 都携带 per-worker 值，并在 PlanStep 处二次 `min` | `TaskBudgetProfile` 是 per-worker 唯一事实来源；Run config 只保留硬安全顶 |
| Supervisor 请求 | JSON contract 携带 `max_search_queries` / `max_llm_calls`，默认 4/4 | 删除这两个重复字段；Supervisor 只选择 `estimated_effort`，预算由 runtime 解析 |
| 旧钳制 | medium 10/16/16 可能被 state 默认 4/6/6 压回 | PlanStep 与 Worker lease 直接使用同一 Profile，不再被旧配置压小 |
| 资源独立性 | 已拆分 search / fetch / tool | 保持不变，并新增契约测试验证 batch_search 后仍可 batch_fetch 与补搜索 |

## 预算语义

Run 级 500K token、120 LLM calls、300 tool calls、30 分钟是 runaway 保护，不是正常停止条件。正常收敛仍应来自：

- `coverage_sufficient`
- `supervisor_complete`
- `marginal_gain_low`

Per-worker 三类检索资源继续独立：

- `batch_search(N)` 消耗 `N` 个 search query 和 1 次逻辑 tool invocation；
- `batch_fetch(N)` 消耗 `N` 个 fetch source 和 1 次逻辑 tool invocation；
- 搜索耗尽不影响抓取，抓取耗尽不影响补搜索。

## 实现位置

- `app/config/harness.yml`：Run hard ceiling。
- `app/config/loader.py`：无 YAML 时的默认硬顶。
- `app/research/runtime/task_budget.py`：三档 generous Profile 与统一 metadata。
- `app/research/runtime/graph.py`、`app/research/runtime/runner.py`：PlanStep 只从 Profile 注入预算。
- `app/research/runtime/worker.py`：Worker lease 使用 PlanStep 预算。
- `app/research/supervisor/models.py`、`app/research/supervisor/agent.py`、`app/research/supervisor/prompt.py`：删除 Supervisor 重复预算字段。
- `app/observability/privacy.py`：默认 reference 模式保留纯数值 `budget` 快照。
- `tests/test_budget_generous_baseline.py`：预算基线、lease 传递和检索资源独立性的契约测试。
- `tests/e2e/test_budget_generous_baseline_full_stack.py`：SDD 两个 Golden Query 的生产拓扑 E2E。

## 验证

| 验证 | 结果 |
|---|---|
| 预算契约测试 | 6 passed |
| Golden Query 生产拓扑 E2E | 1 passed，覆盖 SDD 两个查询；Worker 全部 completed，无 budget cap |
| 全量 pytest | 428 passed |
| `mypy` | 通过 |
| Eval dry-run gate | 60 / 60 passed |
| Frontend projection self-check | 通过 |
| Release smoke（L3 + Q1 × 3 + 开放研究 + 原子事实） | PASS，Trace Integrity 全部 PASS |

Golden Query E2E 使用生产八节点拓扑和真实 `batch_search` / `batch_fetch` 适配器，只替换外部 Provider 为确定性实现。两个查询的 Worker 终态预算快照均为：

```json
{
  "llm_calls_limit": 16,
  "token_limit": 80000,
  "search_queries_used": 6,
  "search_queries_limit": 10,
  "fetch_sources_used": 3,
  "fetch_sources_limit": 16,
  "tool_invocations_used": 3,
  "tool_invocations_limit": 16
}
```

## 边界

- 本轮不提交 Git commit。
- Budget cap 仍是 runaway 保护，不作为正常停止策略。
- 本工作区 `.env` 只有搜索凭据，没有 `OPENAI_API_KEY` / `OPENAI_BASE_URL`，因此未执行外部 Live Provider E2E。该前置条件补齐后可直接重跑生产入口；本轮验收使用同拓扑的确定性 Provider E2E，避免把凭据缺失误判为预算失败。
