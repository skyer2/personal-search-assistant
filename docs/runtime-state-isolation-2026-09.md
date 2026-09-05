# Runtime State Isolation 修复记录（2026-09）

## 根因

线上错误 `cannot pickle '_thread.RLock' object` 的确定性链路是：

```text
_bootstrap_run()
  → get_or_create_run_budget(state)
  → RunBudgetManager._lock = threading.RLock()
  → state.metadata["_run_budget_manager"] = manager
  → snapshot_worker_loop_state(session.state)
  → copy.deepcopy(state)
  → deepcopy(RLock)
  → TypeError
```

`RunBudgetManager` 是进程内 runtime handle，包含 `RLock`、mutable counters、monotonic clock 与 deadline。它不应该进入 `LoopState.metadata`，因为 `LoopState` 会被 Worker isolation 深拷贝，也可能被 checkpoint / JSON 持久化。

该问题不是 `63fe203 feat: render problem` 直接引入。`63fe203` 新增的是模块级 `threading.Lock`，异常类型和对象位置都不匹配；本问题在更早的 Harness runtime 已经存在，只是 clean redeploy 后 Graph Runtime 真正执行到 Research Worker 才暴露。

## 前后对比

| 层级 | 修复前 | 修复后 |
| --- | --- | --- |
| 生命周期 | Manager 创建后塞进 `LoopState.metadata` | Manager 挂在 `HarnessRunContext` 与 `RunSession` |
| Workflow State | metadata 可携带 RLock / runtime handle | 只保存 `run_budget`、`budget_snapshot` 等 JSON-safe 数据 |
| Worker 快照 | 直接 `deepcopy(state)`，遇到 RLock 崩溃 | 先过滤 runtime metadata，再深拷贝 Workflow State |
| Legacy 并行 | 第二处独立 `copy.deepcopy(state)` | 统一复用 `snapshot_worker_loop_state` |
| 持久化 | metadata 原样进入 checkpoint | 过滤 `_` 前缀 runtime key，并校验 JSON-safe |
| 失败方式 | 线上运行中抛 `cannot pickle RLock` | 开发/测试阶段抛出带字段路径的明确错误 |

## 修复内容

### P0：切断当前故障链路

- `RunBudgetManager` 不再写入 `state.metadata["_run_budget_manager"]`。
- 新增 `create_run_budget_manager()` 工厂，明确 Manager 是进程内对象。
- Worker 快照复制前排除 `_` 前缀 runtime metadata，并校验剩余 metadata 可 JSON 序列化。
- Legacy parallel fan-out 删除独立 `copy.deepcopy(state)`，统一走 `snapshot_worker_loop_state()`。

### P1：防止运行态对象进入 checkpoint

- `serialize_loop_state()` 只序列化 Workflow-safe metadata。
- `_` 前缀 runtime key 被过滤。
- 非 JSON-safe 值直接抛出 `non-serializable state: metadata.{key} -> {type}`。

### P2：修正生命周期边界

- `HarnessRunContext` 新增 `budget_manager` 字段。
- `RunSession` 直接持有 `budget_manager`，Graph Runner、Worker、Guardrails 全部通过 Session/Context 获取。
- Graph 与 legacy 两条执行路径共用同一个 Manager，计数与 deadline 语义不变。
- State 中保留 `run_budget` 与 `budget_snapshot`，用于观测、恢复和调度，不携带锁或可变 runtime 对象。

## 修改文件

- `app/agent/harness/run_budget.py`
- `app/agent/harness/loop.py`
- `app/agent/harness/loop_state_store.py`
- `app/research/runtime/isolation.py`
- `app/research/runtime/runner.py`
- `app/research/runtime/worker.py`
- `tests/test_latency_engineering.py`
- `tests/test_research_parallelism.py`
- `tests/test_harness_phase20_runtime.py`

## 回归测试

- Worker snapshot 中放入含 `RLock` 的 `RunBudgetManager`，深拷贝不再失败，父 State runtime handle 保留、子 State 不携带。
- LoopState 序列化过滤 `_run_budget_manager`，并对普通 key 中的非序列化对象抛出明确字段路径。
- 预算时钟测试改为验证“创建即绑定 run_started，且不污染 State metadata”。
- Graph / legacy 并行语义测试继续通过。
