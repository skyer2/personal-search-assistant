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
budget.denied · semantic.fallback
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
| `workers` / `worker_count` | `worker.started` / `worker.completed` / `worker.failed` |
| `evidence`, `synthesis`, `lineage`, `quality` | 对应执行与交付事件 |

Trace Viewer 的 `Understanding` 面板展示 Brief 与拓扑；`Supervisor / Coverage` 面板展示策略决策、压缩发现、Coverage 判断和行动缺口。

## UI Projection Contract

- Live 和 Replay 都从 canonical event 计算 Worker 成功 / partial / failed、工具调用、工具失败、运行异常与 Provider 异常；不能靠 legacy callback 双计数。
- 新 Run 提交时原子清空事件、文件、结果、状态与统计；旧 Run 的迟到事件按 `run_id` 丢弃。
- 进度条由 `phaseProjection` 从全量事件聚合，不再依赖单个 callback 或最后一条 phase 消息。
- 终态语义区分执行完成、部分可确认、执行失败、无法找到可靠来源和已取消；质量拒绝不得显示成“任务失败”。
- `SOURCES` 面板展示当前 Run 实际发生的 search query、已采纳 evidence、上传文件、数据库 / KB 来源和来源质量分层，不展示静态能力清单。
- Trace Viewer 中 `Evidence` 是证据源登记表，`Lineage` 是结论溯源，`Span Tree` 是执行因果与耗时；三者不能合并成一个“证据链”概念。
- 打开 `Span Tree` 时同时加载事件索引，选中 span 后可直接查看 `Related Events`，不需要先进入 JSONL 页签。

## Latency Breakdown

Run latency 汇总包含 brief、supervisor、worker、coverage、synthesis、quality 与 finalize 阶段。Worker 结果还包含 queue、execution、tool、LLM、token、cache、artifact 与失败原因指标。任何分钟级等待都应能定位到具体阶段或 Worker，而不是只看到全局 elapsed。

## Failure Observability

`budget.denied` 是所有预算拒绝的唯一事件口径，携带：

| 字段 | 含义 |
|---|---|
| `scope` | `worker` / `research` / `run` |
| `resource` / `reason` | 被拒绝资源与稳定原因码 |
| `used` / `limit` | 当前资源计数 |
| `worker_*` / `run_*` | Worker lease 与 Run 级 token / LLM / tool 快照 |

`semantic.fallback` 记录 Brief / Supervisor 结构化输出降级，不吞异常：`error_type`、`error_message`、`error_category`、`model`、`schema`、`fallback` 全部保留。Run metadata 同时聚合 `control_plane`，用于显示控制面是否 degraded。

Worker 终态事件携带完整预算快照：LLM calls、tokens、search queries、fetch sources、tool invocations 的 used/limit。`gen_ai.chat` 记录 phase、task、call index、token 估算、duration、TTFT 与 Worker 剩余额度。工具事件只记录 `args_meta`（参数名和列表条目数），不记录 query、URL、prompt 或网页正文。

## 看哪里

| 目的 | 位置 |
|---|---|
| 实时 UI | 提问后的过程框 / 执行过程（WebSocket `monitor_event`）。刷新后走 `GET /api/sessions/{id}/bootstrap`，WS `subscribe.after_seq` replay，按 `(run_id, seq)` 去重 |
| Run 投影 | `RunStore` SQLite：query / status / result / HITL / timestamps / 文件 metadata。不要从 Trace 重建业务状态 |
| Span Tree（执行因果与耗时） | `GET /api/traces/tree/{session_id}` |
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
| `action` | `CONDUCT_RESEARCH` / `COMPLETE` |
| `runtime_action` | `RuntimePolicy` 的 `dispatch` / `retry` / `synthesize` / `deliver_partial` / `wait` / `stop` |
| `reason` | Supervisor 的研究理由 |
| `runtime_reasons` | 预算、安全、终态等确定性理由 |
| `task_count` | 本次行动生成的任务数 |

`coverage.assessed` 携带 `sufficient`、`missing`、`conflicts`、`weak_claims`、`recommended_next_questions`、`source`、`reason`。`missing` 必须是可行动问题，而不是不可执行的泛化标签。

`progress.assessed` 是 Coverage 的兼容投影，供既有进度展示和 eval 使用；它不是第二个语义控制权。

## Trace Integrity

`trace_integrity` 是生产门禁，不只是 UI 展示：

- Worker run 必须同时有 `worker.started` 与 `worker.completed` / `worker.failed`，不允许只有完成态的“幽灵完成”。
- 每次执行必须有至少一个 root span；Worker 必须挂在 Research root / synthesis 相应 span 下。
- Root span 恢复上下文时不继承 Worker 的 `task_id`、`plan_version` 或 `attempt`。
- Research synthesis 阶段必须有 synthesis span / event；失败时必须给出 `fail_reason`。
- Evidence 引用必须形成 lineage；`synthesis.failed` 与 `evidence_ids` 也计入 lineage。显式 input/output 引用存在时，ID 匹配只补缺失侧，不允许重复边。
- Atomic fact fast path 必须保留 worker 生命周期、root span、coverage / synthesis 事件与证据血缘；它只豁免开放研究图的 Supervisor 循环，不豁免可观测性。
- 存在可用 evidence 且 final content 为空时，Trace Integrity 必须失败，即使外层 run 没有抛异常。

观测系统自身失败不能吞掉业务事件：Worker span 创建失败会 emit `observability.internal_error`，随后 `worker.started` / `worker.completed` 仍按业务事实记录。

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

`args_meta` 是白名单字段，只允许 `arg_names` 和 `item_count`。即使误传完整参数，`args` 仍会被 privacy 层降级为字节数，不允许进入事件正文。

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
