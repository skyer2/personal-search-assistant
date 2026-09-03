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

`run.*` · `brief.compiled` · `plan.created/validated` · `worker.*` · `tool.*` · `retrieval.search` · `gen_ai.chat` · `evidence.registered` · `progress.evaluated` · `replan.*` · `synthesis.*` · `recovery.*` · `context.*` · `checkpoint.*` · `budget.*` · `quality.evaluated` · `eval.scored`

每个重要事件可带：

- `input_refs` / `output_refs`：语义产物血缘（Brief → Plan → Finding → Evidence → Answer）
- `*_ref` / `*_hash`：完整 payload 落在 `logs/traces/payloads/{run_id}/`，事件本身只保留引用

## 看哪里

| 目的 | 位置 |
|------|------|
| 实时 UI | 提问后的过程框 / 执行过程（WebSocket `monitor_event`）。刷新后走 `GET /api/sessions/{id}/bootstrap`，WS `subscribe.after_seq` replay，按 `(run_id, seq)` 去重 |
| Run 投影 | `RunStore` SQLite：query / status / result / HITL / timestamps / 文件 metadata。不要从 Trace 重建业务状态 |
| 因果树 | 阶段与 Worker 的父子 span。默认不展示 `llm_usage` / `gen_ai.chat`（仍在 JSONL）。`GET /api/traces/tree/{session_id}` |
| Understanding / Plan / Synthesis | Trace 查看器页签；`summary.brief` / `summary.plans` / `summary.synthesis` / `summary.lineage` |
| Worker / 进度 / Replan / Eval | Trace 查看器对应页签；JSONL/tree 响应里的 `summary` |
| Semantic payload | `GET /api/traces/payloads/{run_id}/{name}` |
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

默认 `OBS_CONTENT_MODE=reference`：事件只保留 metadata + `*_ref` / `*_hash` / ids；完整结构化 payload 在本地 payload store。`redacted` 更激进地去掉正文；`full` 才把截断后的原文放进事件（opt-in）。

LLM generation 额外记录：`prompt_template_id/version`、`prompt_ref`/`output_ref`、`input_hash`/`output_hash`（不默认上报全文）。

## JSONL 布局（run-centric）

```text
logs/traces/
  {session_id}/
    {run_id}.jsonl
    index.jsonl
  {session_id}.jsonl          # legacy 仍可读
  payloads/{run_id}/*.json
```

采样：`OBS_TRACE_SAMPLE_RATE`（语义事件始终保留）。保留：`OBS_TRACE_RETENTION_DAYS`（默认 14）。

## 实时 fanout

默认单进程 WebSocket。多 API worker 时设：

```text
OBS_EVENT_BUS=redis
REDIS_URL=redis://...
```

JSONL/OTel 仍是 durable；EventBus 只负责跨进程 live delivery。

## Replan 指标

`replan.proposed` → `replan.applied` / `replan.rejected` 记录 `target_gap_ids` / `triggered_by` / `from_plan_version` / `to_plan_version` / `reason` / `added_tasks`。

**Gap closure（语义口径）**：`target_gap_ids` 在后续 `progress.evaluated.resolved_gap_ids` 中出现才算 recovered。`harness_live_replan_recovered_total` 不再等于「有 replan + run success」。

Trace summary 提供 `gap_closure_rate` / `replan_useful` / `failure_origin`（earliest evaluated failing stage）。

## Eval 关联

live eval 把 `trace_id` / `run_id` / `variant` / `case_id` 写入 `TaskEvalResult`，并 emit `eval.scored`（含 `target_span_id` / `target_artifact_id` / `grader`）。用 `HARNESS_EVAL_VARIANT=full_harness|no_replan|vanilla` 做 ablation。交互提问只会产生 Finalize 时的 `quality.evaluated`，不会有 `eval.scored`。

## 依赖

OTel SDK 为可选 extra：`pip install -e ".[otel]"`。未安装时本地 journal + WebSocket 仍工作。
`gen_ai.agent.name=research-agent-harness`；`agent.run_id` 单独承载 invocation id。Search 映射为 `gen_ai.operation.name=retrieval`。
