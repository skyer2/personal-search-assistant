# Deadline-Aware Runtime 设计

## 目标

本轮修复不提高总运行时长，而是让固定 deadline 内的证据产出最大化：

1. 正在执行的 LLM / Tool 不再被状态轮询误判为 idle。
2. Discovery 失败不再让整个 Research DAG 永久阻塞。
3. Understand / Plan / Context / Replan / Synthesis 各自有墙钟预算。
4. Emergency Synthesis 使用 Minimal Evidence Pack，不再走重型上下文构建。
5. Execution Health 与 Evidence Coverage 分离，Replan 不再消费阻塞文本。
6. Partial 的 termination lifecycle 统一，Trace Integrity 按真实生命周期判定。

## Worker Activity Model

`WorkerActivityTracker` 是 idle watchdog 的唯一主信号：

- `LLM_REQUEST_STARTED / FIRST_TOKEN / STREAM_DELTA / COMPLETED`
- `TOOL_STARTED / COMPLETED`
- `ARTIFACT_WRITTEN / EVIDENCE_EXTRACTED / FINDING_EMITTED`

idle 判定改为：

```text
no in-flight operation
AND no heartbeat/state progress
for worker_idle_timeout_sec
```

原有 child state signature 只作为 fallback heartbeat。LLM request 在飞行中时由
operation timeout / worker wall timeout 管理，不触发 idle timeout。

## Degraded Discovery DAG

Discovery 不再是下游任务的硬依赖。计划标注会转换为：

```text
Landscape Discovery -> produces candidate_set
Deep Dive          -> requires artifact candidate_set
```

Discovery 完成或失败后，Progress 都会物化一个 `CandidateSet`：

- complete：来自 Discovery 的结构化 findings / facts
- partial：来自 salvage evidence
- fallback：来自 Brief / 查询约束，允许下游继续自主检索

下游 Worker 收到 `candidate_context`，但不依赖 `t_landscape == done`。

## Phase Budgets

新增配置：

| 配置 | 默认 | 语义 |
| --- | ---: | --- |
| `understand_wall_budget_sec` | 30 | LLM intent 超时后使用规则 intent |
| `planner_wall_budget_sec` | 45 | LLM planner 超时后使用 deterministic plan |
| `context_build_budget_sec` | 30 | Context 超时后使用轻量上下文继续 |
| `fast_synthesis_threshold_sec` | 45 | 剩余时间低于阈值时进入 fast path |
| `emergency_context_budget_sec` | 10 | Minimal Evidence Pack 构建上限 |
| `worker_idle_timeout_sec` | 75 | 无 in-flight 操作且无心跳时的 idle 阈值 |

## Granularity

复杂度模型从单一 `entity × dimension` 扩展为：

```text
TaskKind × target_items × dimensions × source_factor
```

Discovery 任务按 `target_items × coverage_keys` 估算，并按 coverage lane 拆分；
Deep Dive 继续沿用 entity/dimension guard。

## Emergency Synthesis

进入条件：

- `force_synthesis`
- run 已有 abort reason
- 剩余时间低于 `fast_synthesis_threshold_sec`

Fast path 直接构建：

```json
{
  "brief": "...",
  "findings": [],
  "evidence_refs": [],
  "artifact_summaries": [],
  "coverage_gaps": []
}
```

然后确定性渲染 partial report。目标 context build < 10s，不再召回 memory、
读取大量 artifact 或执行完整上下文压缩。

## Progress 与 Replan

`ProgressAssessment` 拆出：

- `execution_failed_tasks`
- `execution_blocked_tasks`
- `execution_failure_reasons`

语义 gap 只来自 coverage matrix / dimensions / conflicts / stale evidence。
Replan affordability 使用完整 wave cost：

```text
context + queue + worker execution + progress + checkpoint + safety margin
```

## Termination

所有 partial 最终写入统一对象：

```json
{
  "status": "partial",
  "reason": "deadline_exceeded",
  "stage": "synthesis",
  "research_completed": false,
  "synthesis_attempted": true,
  "synthesis_status": "partial_fast_path",
  "quality_attempted": false
}
```

Trace Integrity 只有在 quality 已尝试或 termination stage 达到 quality/finalize
时才要求 quality event；partial 且 quality not reached 是合法生命周期。

## Trace Integrity 与投影一致性

Trace Integrity 读取 terminal event 中的统一 `termination` 对象：

- partial 必须有 `termination.reason`，否则报 `partial_without_termination_reason`。
- `quality_attempted=false` 的 deadline partial 不要求 quality event。
- `quality_attempted=true` 或 completed run 必须有 quality event。
- Worker 从未启动的 pre-research partial 不要求 worker / progress event。

Tree / Summary / Lineage projection 均记录 `event_count`。读取缓存时先加载事件：

```text
cached.event_count == current.event_count
AND cached.span_count > 0 when events exist
```

任一条件不满足则重建，避免早期 `span_count=0` 的 projection 在事件落盘后继续污染
TraceViewer。

## 回归验证

新增 `tests/test_deadline_aware_runtime.py` 覆盖：

1. LLM in-flight 不触发 idle timeout。
2. LLM 异常关闭 in-flight operation。
3. 真实无操作 / 无心跳触发 idle timeout。
4. Discovery `target_items × dimensions` 成本与 coverage lane 拆分。
5. CandidateSet 替代 Discovery 硬依赖，失败后 fallback 可继续。
6. Planner 超时回退 heuristic plan。
7. Emergency Synthesis Minimal Evidence Pack 与 context 超时降级。
8. Quality gate 显式标记 `quality_attempted`。
9. Execution blocker 与 semantic coverage gap 分离。
10. 失败 Worker 的 partial evidence 不生成 empty gap。
11. Replan affordability 包含 context / queue / progress / checkpoint / safety。
12. deadline partial 的 termination 与 Trace Integrity 规则。
13. 陈旧 `span_count=0` projection 自动重建。
