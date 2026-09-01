# Agent-native Observability（Flight Recorder）

> 业务代码只产生一次语义事件。JSONL、WebSocket、OpenTelemetry/Langfuse、Metrics 都是 exporter。

本文是可观测性实现说明。[ARCHITECTURE.md](./ARCHITECTURE.md) 仍是仓库范围权威。

## 统一真源

```text
Agent Code
    emit / span
        │
 AgentTelemetry (app/observability/)
        │
 ┌──────┼──────────┐
 ▼      ▼          ▼
OTel  JSONL     WebSocket
 │      │
Langfuse  TraceViewer / Metrics
```

一次 Research Run 的 identity：

| 字段 | 含义 |
|------|------|
| `session_id` | 前端 thread / 工作目录 `session_*` |
| `run_id` | 本次 harness run（当前等于 session_id） |
| `trace_id` | 因果树根 ID |
| `span_id` / `parent_span_id` | 并行 Worker 用 `phase + task_id + attempt`，不再只用 phase 当 key |

## 事件词表

`run.started/completed/failed` · `plan.created` · `worker.*` · `tool.*` · `gen_ai.chat` · `evidence.registered` · `progress.evaluated` · `replan.applied/rejected` · `quality.evaluated` · `eval.scored`

## 看哪里

| 目的 | 位置 |
|------|------|
| 实时 UI | 提问后的过程框 / 执行过程（WebSocket `monitor_event`） |
| 因果树 | Trace 查看器「因果树」页签；`GET /api/traces/tree/{session_id}` |
| 落盘 journal | `app/logs/traces/{session_id}.jsonl`（运行时根是 `app/`，`schema=agent_event.v1`） |
| 窗口聚合 | `GET /api/metrics/summary`（同时读 nested `extra` 和顶层 `event=run_summary`） |
| 进程内 Counter/Histogram | `GET /api/metrics/prometheus` 中 `harness_live_*` |
| Langfuse | OTLP：`{LANGFUSE_HOST}/api/public/otel`；**不再**调用已弃用的 `GET /api/public/traces` |

## 隐私

默认 `OBS_CONTENT_MODE=redacted`：只保留 tool name、token、id、verdict。完整 prompt / 网页 / 工具输出需显式 `OBS_CONTENT_MODE=full`。

## Replan 指标

`replan.applied` 记录 `from_plan_version` / `to_plan_version` / `reason` / `added_tasks` / `remaining_budget`。窗口聚合给出 `replan_trigger_rate`、`avg_replan_count`。进程内 Counter：`harness_live_replan_applied_total`、`harness_live_replan_recovered_total`（replan 后 run 仍成功）、`harness_live_replan_waste_total`（replan 后仍失败）。

## Eval 关联

live eval 把 `trace_id` / `run_id` / `variant` / `case_id` 写入 `TaskEvalResult`，并 emit `eval.scored`。用 `HARNESS_EVAL_VARIANT=full_harness|no_replan|vanilla` 做 ablation。

## 依赖

OTel SDK 为可选 extra：`pip install -e ".[otel]"`。未安装时本地 journal + WebSocket 仍工作。
