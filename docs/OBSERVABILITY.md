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
|---|---|
| `session_id` | 前端 thread / 工作目录 `session_*` |
| `run_id` | 本次 harness run（16 位独立 ID，同一 thread 多轮互不覆盖） |
| `trace_id` | 因果树根 ID，与 `run_id` 一起写入 LoopState / eval metadata |
| `span_id` / `parent_span_id` | 并行 Worker 用 `bind_worker()` 复制 context，span key = `phase + task_id + plan_version + attempt` |

## 事件词表

Canonical semantic events:

```text
brief.compiled
topology.decided
supervisor.started
supervisor.decided
plan.created
finding.compressed
coverage.assessed
progress.assessed
```

Execution, evidence, and delivery events:

```text
run.* · worker.* · task.transitioned · tool.* · retrieval.search
gen_ai.chat · evidence.registered · evidence.assessed
synthesis.* · quality.assessed · delivery.assessed
checkpoint.* · budget.* · context.* · run.terminated
observability.internal_error · eval.scored
```

每个重要事件可带：

- `input_refs` / `output_refs`：语义产物血缘（Query → Brief → Supervisor Action → Plan → Task → Finding → Evidence → Coverage → Synthesis → Answer）
- `*_ref` / `*_hash`：完整 payload 落在 `logs/traces/payloads/{run_id}/`，事件本身只保留引用

## Trace Summary 投影

`summarize_trace()` 输出以下新架构面板：

| 字段 | 来源事件 |
|---|---|
| `brief` | `brief.compiled` |
| `topology` | `topology.decided` |
| `supervisor_decisions` / `supervisor_decision_count` | `supervisor.decided` |
| `plans` | `plan.created` |
| `findings` / `finding_count` | `finding.compressed` |
| `coverage_judgements` / `coverage_judgement_count` | `coverage.assessed` |
| `progress` / `progress_count` | `progress.assessed` |
| `workers` / `worker_count` | `worker.started` / `worker.done` |
| `evidence`, `synthesis`, `lineage`, `quality` | 对应执行与交付事件 |

Trace Viewer 的 `Understanding` 面板展示 Brief 与拓扑；`Supervisor / Coverage` 面板展示策略决策、压缩发现、Coverage 判断和行动缺口。

## 看哪里

| 目的 | 位置 |
|---|---|
| 实时 UI | 提问后的过程框 / 执行过程（WebSocket `monitor_event`）。刷新后走 `GET /api/sessions/{id}/bootstrap`，WS `subscribe.after_seq` replay，按 `(run_id, seq)` 去重 |
| Run 投影 | `RunStore` SQLite：query / status / result / HITL / timestamps / 文件 metadata。不要从 Trace 重建业务状态 |
| 因果树 | `GET /api/traces/tree/{session_id}` |
| Brief / Topology | Trace Viewer `Understanding` 页签 |
| Supervisor / Findings / Coverage | Trace Viewer `Supervisor / Coverage` 页签 |
| Worker / Evidence / Synthesis / Quality | Trace Viewer 对应页签 |
| Semantic payload | `GET /api/traces/payloads/{run_id}/{name}` |
| JSONL | `app/logs/traces/{session_id}/{run_id}.jsonl`，`schema=agent_event.v1` |
| 窗口聚合 | `GET /api/metrics/summary` |
| 进程内 Counter/Histogram | `GET /api/metrics/prometheus` |
| Langfuse | OTLP：`{LANGFUSE_HOST}/api/public/otel` |

## Control 语义

`supervisor.decided` 携带：

| 字段 | 含义 |
|---|---|
| `action` | `THINK` / `CONDUCT_RESEARCH` / `COMPLETE` |
| `runtime_action` | `RuntimePolicy` 的 `dispatch` / `retry` / `synthesize` / `deliver_partial` / `wait` / `stop` |
| `reason` | Supervisor 的研究理由 |
| `runtime_reasons` | 预算、安全、终态等确定性理由 |
| `task_count` | 本次行动生成的任务数 |

`coverage.assessed` 携带 `sufficient`、`missing`、`conflicts`、`weak_claims`、`recommended_next_questions`、`source`、`reason`。`missing` 必须是可行动问题，而不是不可执行的泛化标签。

`progress.assessed` 是 Coverage 的兼容投影，供既有进度展示和 eval 使用；它不是第二个语义控制权。

## Trace Integrity

`trace_integrity` 是生产门禁，不只是 UI 展示：

- Worker run 必须同时有 `worker.started` 与 `worker.done`，不允许只有 done 的“幽灵完成”。
- 每次执行必须有至少一个 root span；Worker 必须挂在 Research root / synthesis 相应 span 下。
- Research synthesis 阶段必须有 synthesis span / event；失败时必须给出 `fail_reason`。
- Evidence 引用必须形成 lineage；`synthesis.failed` 与 `evidence_ids` 也计入 lineage。
- Simple Fact fast path 只校验 worker 生命周期与 root span，不要求开放研究图的 coverage / synthesis 事件。
- 存在可用 evidence 且 final content 为空时，Trace Integrity 必须失败，即使外层 run 没有抛异常。

观测系统自身失败不能吞掉业务事件：Worker span 创建失败会 emit `observability.internal_error`，随后 `worker.started` / `worker.done` 仍按业务事实记录。

## Synthesis 观测

`synthesis.started` / `synthesis.completed` / `synthesis.failed` 携带：

| 字段 | 含义 |
|---|---|
| `mode` | synthesis 输入模式（例如 full / compact） |
| `attempt` / `attempts` | 当前尝试 / 总尝试次数 |
| `duration_ms` | 单次执行耗时 |
| `input_tokens_estimated` | 去重与裁剪后的估算输入 |
| `evidence_count` | 进入 synthesis 的 evidence 数 |
| `fail_reason` | provider / context / budget / empty 等失败分类 |
| `fallback_action` | `deterministic_partial` 或 `none` |
| `content_chars` | 最终 / 兜底内容长度 |

Run metadata 同步暴露 `synthesis_attempts`、`synthesis_failed`、`synthesis_fail_reason`、`fallback_used`。这用于区分“模型临时失败但已部分交付”和“没有可信证据导致失败”。

## 隐私

默认 `OBS_CONTENT_MODE=reference`：事件只保留 metadata + `*_ref` / `*_hash` / ids；完整结构化 payload 在本地 payload store。`redacted` 更激进地去掉正文；`full` 才把截断后的原文放进事件（opt-in）。

## JSONL 布局

```text
logs/traces/
  {session_id}/
    {run_id}.jsonl
    index.jsonl
  payloads/{run_id}/*.json
```

采样：`OBS_TRACE_SAMPLE_RATE`（语义事件始终保留）。保留：`OBS_TRACE_RETENTION_DAYS`（默认 14）。

## Eval 关联

live eval 把 `trace_id` / `run_id` / `variant` / `case_id` 写入 `TaskEvalResult`，并 emit `eval.scored`（含 `target_span_id` / `target_artifact_id` / `grader`）。交互提问只会产生 Finalize 时的 `quality.assessed`，不会有 `eval.scored`。

## 依赖

OTel SDK 为可选 extra：`pip install -e ".[otel]"`。未安装时本地 journal + WebSocket 仍工作。
