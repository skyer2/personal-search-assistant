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
| `run_id` | 本次 harness run（16 位独立 ID，同一 thread 多轮互不覆盖） |
| `trace_id` | 因果树根 ID（与 `run_id` 一起写入 LoopState / eval metadata） |
| `span_id` / `parent_span_id` | 并行 Worker 用 `bind_worker()` 复制 context，span key = `phase + task_id + attempt` |

## 事件词表

`run.started/completed/failed` · `plan.created/validated` · `worker.*` · `tool.*` · `gen_ai.chat` · `evidence.registered` · `progress.evaluated` · `replan.proposed/applied/rejected` · `quality.evaluated` · `eval.scored`

## 看哪里

| 目的 | 位置 |
|------|------|
| 实时 UI | 提问后的过程框 / 执行过程（WebSocket `monitor_event`） |
| 因果树 | 阶段与 Worker 的父子 span。默认不展示 `llm_usage` / `gen_ai.chat`（仍在 JSONL）。`GET /api/traces/tree/{session_id}` |
| Worker / 进度 / Replan / Eval | Trace 查看器对应页签；JSONL/tree 响应里的 `summary`（identity、workers 按 `task_id+attempt` 合并 started/completed、progress、replans、evals、usage） |
| JSONL | 该 session 全部事件时间线，含每次 LLM 调用。用来对耗时、重试、abort |
| 证据链 | `evidence.json` 引用源，不是 JSONL |
| 落盘 journal | `app/logs/traces/{session_id}.jsonl`（运行时根是 `app/`，`schema=agent_event.v1`） |
| 窗口聚合 | `GET /api/metrics/summary`（同时读 nested `extra` 和顶层 `event=run_summary`） |
| 进程内 Counter/Histogram | `GET /api/metrics/prometheus` 中 `harness_live_*` |
| Langfuse | OTLP：`{LANGFUSE_HOST}/api/public/otel`；**不再**调用已弃用的 `GET /api/public/traces` |

### 排查 PDF 没出现

1. **因果树 / JSONL** 搜 `abort`：`budget_tool_calls` 会跳过 `generate_markdown` / `convert_pdf`。finalize 仍会尽量用已有工人材料写部分 PDF，并在聊天结果里写明原因。
2. **对话区** 看是否出现「任务因 … 提前结束」；以前 `final_content` 为空时连 `task_result` 都不会推，界面像没结果。
3. **Worker** 看研究任务是否 `ok`；全失败则没有可转 PDF 的正文。
4. 左侧文件架刷新依赖 `session_created` 的工作目录；有 PDF 文件但对话空白时点刷新。

## 隐私

默认 `OBS_CONTENT_MODE=redacted`：只保留 tool name、token、id、verdict。完整 prompt / 网页 / 工具输出需显式 `OBS_CONTENT_MODE=full`。

## Replan 指标

`replan.proposed` → `replan.applied` / `replan.rejected` 记录 `from_plan_version` / `to_plan_version` / `reason` / `gaps` / `added_tasks` / `remaining_budget`。窗口聚合给出 `replan_trigger_rate`、`replan_recovery_rate`（触发 replan 后仍 success 的比例）、`avg_replan_count`。进程内 Counter：`harness_live_replan_applied_total`、`harness_live_replan_recovered_total`（replan 后 run 仍成功）、`harness_live_replan_waste_total`（replan 后仍失败）。

Trace 查看器「进度 / Replan」页签同时列出 `progress.evaluated`。`replan_count=0` 并不等于没做进度评估：`max_parallel_workers` 会把原计划 READY 任务分批发完，第二波 Worker 经常不是 PlanPatch。

## Eval 关联

live eval 把 `trace_id` / `run_id` / `variant` / `case_id` 写入 `TaskEvalResult`，并 emit `eval.scored`。用 `HARNESS_EVAL_VARIANT=full_harness|no_replan|vanilla` 做 ablation。交互提问只会产生 Finalize 时的 `quality.evaluated`，不会有 `eval.scored`。

## 依赖

OTel SDK 为可选 extra：`pip install -e ".[otel]"`。未安装时本地 journal + WebSocket 仍工作。
