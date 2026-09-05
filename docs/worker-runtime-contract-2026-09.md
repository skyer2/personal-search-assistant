# Worker Runtime 契约修复（2026-09）

## 结论

`LangChainWorkerRuntime.execute()` 的接口声明要求返回 `WorkerResult`，但 timeout、预算阻断和可选步骤跳过分支实际返回 Graph state `dict`。Graph worker 节点随后按契约读取 `result.raw`，导致 `AttributeError: 'dict' object has no attribute 'raw'`，真实异常又被失败恢复逻辑包装为 `missing_step` 类失败，掩盖了根因。

本次修复将 Worker Runtime 的返回值收敛为单一强类型契约，并在 Graph 边界增加运行时防御，防止同类契约漂移再次进入任务执行路径。

## 根因链路

```text
WorkerRuntime.execute() 声明返回 WorkerResult
        ↓
timeout / budget blocked / optional skip 分支返回 dict
        ↓
node_research_worker() 读取 result.raw
        ↓
AttributeError: 'dict' object has no attribute 'raw'
        ↓
异常被上层 worker fallback 捕获并转成 missing_step 失败
```

## 前后对比

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 正常完成 | 返回 `WorkerResult`，无状态字段 | `WorkerResult(status="done")` |
| 步骤超时 | 返回 Graph state `dict`，缺完整指标与事件 | `WorkerResult(status="failed", fail_reason="step_timeout")`，保留 `raw`、耗时、事件与 Span |
| 预算阻断 | 返回 `dict`，上层崩溃 | `WorkerResult(status="blocked")`，Graph 投影为强制综合 |
| 可选步骤跳过 | 返回 `dict`，上层崩溃 | `WorkerResult(status="skipped")`，Graph 保留 skipped 状态 |
| 缺 session / 缺 step | 无 `status`，语义弱 | `WorkerResult(status="failed")` |
| Graph 节点 | 直接信任 runtime 返回值 | 运行时断言必须为 `WorkerResult` |
| 类型检查 | 无 runtime 范围配置 | mypy 仅检查 runtime，11 个文件全部通过 |

## 契约语义

`WorkerResult.status` 使用受限字面量：

- `done`：worker 正常完成，`ok=True`。
- `failed`：worker 失败，包括超时、缺 session、缺 step 或 agent 执行失败。
- `skipped`：可选步骤被主动跳过，不消耗后续重试。
- `blocked`：全局预算要求停止继续研究，需要进入综合。

`ok` 保留为兼容字段，但不能替代 `status` 表达控制流语义。

## Graph 投影规则

`node_research_worker()` 将 runtime 状态映射为 Graph task 状态：

```text
done    -> done
failed  -> failed
skipped -> skipped
blocked -> failed
```

当 `status="blocked"` 时，Graph 额外写入：

- `replan_exhausted=True`
- `progress_assessment.verdict="enough"`
- `progress_assessment.reason="force_synthesis_budget"`

这样预算阻断不会再触发重计划风暴，而是直接进入强制综合路径。

## 可观测性补齐

timeout、budget blocked 和 optional skip 分支现在统一补齐：

- `WORKER_COMPLETED` / `WORKER_FAILED` 语义事件
- `queue_ms`
- `execution_ms`
- `duration_ms`
- Span 关闭
- `worker_status` 属性

因此后续 trace 可以直接区分“执行超时”、“预算阻断”和“主动跳过”，不会再只剩 `missing_step` 一类误导性摘要。

## 类型检查

`pyproject.toml` 新增 runtime 范围的 mypy 配置：

- 检查范围：`app/research/runtime`
- `follow_imports="skip"`，避免把未治理的外层历史类型债务纳入本次门禁
- `check_untyped_defs=true`
- `no_implicit_optional=true`
- unreachable、冗余 cast、未使用配置告警开启

同时修复 runtime 内部的可空收窄和 TypedDict 访问问题，使 `python -m mypy` 达到 0 错误。

## 回归测试

新增 `tests/test_worker_runtime_contract.py`，覆盖：

1. 正常执行返回 `status="done"`
2. 超时返回 `status="failed"` 且 `fail_reason="step_timeout"`
3. 预算阻断返回 `status="blocked"`
4. 可选步骤跳过返回 `status="skipped"`
5. 缺 session / 缺 step 返回 `status="failed"`
6. Graph worker 节点处理 timeout 结果不再崩溃
7. runtime 返回 `dict` 时被边界契约断言拒绝

## 验证结果

```text
python -m mypy
Success: no issues found in 11 source files

python -m pytest tests/test_worker_runtime_contract.py tests/test_architecture_p0.py -o asyncio_mode=auto -p no:cacheprovider -q
17 passed
```

相关失败观测设计见 `docs/failure-observability-lifecycle-2026-09.md`。
