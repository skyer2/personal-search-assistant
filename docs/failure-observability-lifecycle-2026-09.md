# Plan 失败、可观测性与前端生命周期修复记录（2026-09）

## 问题拆分

本次线上症状由三个独立但相互放大的问题组成：

1. **Plan 阶段真实失败**：执行轨迹停在 `phase=plan start`，没有 `plan done`，说明 `compose_execution_plan()` 内部抛出异常。
2. **异常路径未关闭 Observability Run**：RunStore 与 WebSocket 已标记失败，但 Flight Recorder 没有 `run.failed`、failure origin 和 root span close，Trace Viewer 只能显示 `missing_plan_event`。
3. **前端主动丢弃失败上下文**：`taskFailure` 后删除 ChatTurn、重置 projection、创建新 thread，导致最有价值的失败证据从 UI 消失。

## 前后对比

| 问题 | 修复前 | 修复后 |
| --- | --- | --- |
| Planner 异常 | `compose_execution_plan()` 异常直接终止 Run | 严格 Planner 失败后降级为 deterministic template plan，并在 issues 中标记 `planning_fallback:{ExceptionType}` |
| 异常日志 | 只有错误字符串 | 记录完整 exception stack、异常类型、abort 信息 |
| Run 失败事件 | 不调用 `finish_run()`，无 `run.failed` | 调用 `finish_run(status="failed")`，写 `run.failed` 与 `run_summary`，关闭 root span |
| Failure 归因 | `failure.origin_stage` 缺失 | 根据 `state.phase` 写 `failure.origin_stage` / `failure.detected_stage`，Plan 阶段归因为 `planning` |
| Plan 语义事件 | 已有实现但缺回归保护 | 增加 `PLAN_CREATED` / `PLAN_VALIDATED` 回归测试，防止未来遗漏 |
| Trace Integrity | 失败 Run 仍要求 Worker / Progress / Quality 事件 | 根据 failure origin 判断阶段是否未到达，Planning 失败时后续缺失为合理 |
| 前端失败 Turn | 删除 Turn、清空 Session、创建新 thread | 保留 Turn、events、files、currentRunId 与 Trace，只把该 Turn 标记失败并回填提问 |
| Retry | 失败后上下文消失 | 原提问回到输入框，可直接重试；失败 Run 可继续查看 Trace |

## 实施细节

### P0：Plan 降级与真实异常保留

- `compose_execution_plan_sync()` 拆出严格路径 `_compose_execution_plan_strict()`。
- 严格路径异常时，使用 `build_plan()` + `finalize_plan()` 生成 fallback template。
- fallback issues 首项为 `planning_fallback:{ExceptionType}`，不会静默吞掉缺陷。
- `AgentHarness.run()` 异常路径调用 `logger.exception()`，确保完整 traceback 进入服务日志。

### P0：失败 Run 一等公民建模

- 异常路径计算 run duration 并调用 `recorder.finish_run(status="failed")`。
- metadata 写入：
  - `error`
  - `exception_type`
  - `abort_reason`
  - `abort_message`
  - `failure.origin_stage`
  - `failure.detected_stage`
- `finish_run()` 把 metadata 中的 failure 字段提升到 event attributes，`run.failed` 与 `run_summary` 保持一致。

### P1：Plan 语义事件回归

当前 `_report_phase(Phase.PLAN, "done")` 已发送：

- `EventType.PLAN_CREATED`
- `EventType.PLAN_VALIDATED`

本轮新增回归测试锁定该行为，避免 Planner 成功但 Trace Integrity 仍报 `missing_plan_event`。

### P1：前端保留失败上下文

- 删除 `discardFailedTask()`。
- 失败时只更新最后一个 Turn：
  - `isRunning=false`
  - `result="任务失败：..."`
- 原提问回填输入框，方便直接重试。
- 不清空 events / files / result / currentRunId，不创建新 thread。

### P2：阶段感知 Trace Integrity

Trace Integrity 现在读取 `run.failed` / `run_summary` 的 `failure.origin_stage`：

```text
failure.origin_stage = planning
```

时，以下缺失被视为“未到达”，不再报错：

- `plan.created`
- worker terminal
- progress
- synthesis
- quality

已到达阶段仍正常检查。例如 Planning 失败前必须有 `brief.compiled`。

## 修改文件

- `app/run_store/files.py`
- `app/research/planning/compose.py`
- `app/agent/harness/loop.py`
- `app/observability/recorder.py`
- `app/observability/integrity.py`
- `frontend/src/App.tsx`
- `frontend/src/hooks/useDeepAgentSession.ts`
- `tests/test_hybrid_planning.py`
- `tests/test_obs_correctness.py`

## 验证

- Planner fallback 测试：严格 Planner 抛 `RuntimeError` 后仍生成 template plan，并标记 fallback issue。
- Failure observability 测试：`run.failed` 携带 `failure.origin_stage=planning`、`failure.stage=planning`。
- Stage-aware integrity 测试：Planning 失败 Run 无 Plan / Worker / Progress / Quality 事件时，Trace Integrity 通过。
- Plan 语义事件测试：`PLAN_CREATED` 与 `PLAN_VALIDATED` 均存在。
- 前端 TypeScript / production build 通过。

### 附带修复

全量回归发现 NTFS 目录 mtime 不能稳定反映新增文件，Session artifact TTL 缓存可能短暂返回旧列表。缓存指纹已扩展为“根目录 stat + 一级子项 name/mtime/size”，新增或修改一级文件会立即失效缓存。
