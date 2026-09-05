# Research Runtime 故障链修复（2026-09）

## 结论

本次故障不是单点异常，而是四个问题叠加形成的故障链：

```text
Planner 生成超大 Research Task
        ↓
Worker 全部打满 step timeout
        ↓
已落盘 Artifact 没有转成 partial evidence
        ↓
Progress 只看 Worker 成败，误判 empty / semantic gap
        ↓
Replan 后某个 Worker 触发 SensitiveContentDetected
        ↓
Provider 异常穿透 Graph，整个 Run failed
```

修复目标是把系统边界改成 Best-effort Research：

- 单个 Worker 任务必须有粒度上限。
- Provider 拒绝是可恢复失败，不是 Runtime crash。
- Worker 超时后回收已获取 Artifact。
- Progress 基于证据覆盖，而不是只看 Worker 成败。
- 只要存在可信证据，Synthesis 输出 partial result。
- Trace Integrity 与 Run Success 解耦，失败 Run 的事件闭合即可通过。

## P0 修复

### 1. Planner 任务粒度守卫

新增 `app/research/planning/granularity.py`：

- 单个 Research Task 最多 2 个独立实体。
- 最多 4 个核心维度。
- 最多 8 个信息单元（entity × dimension）。
- 超限时自动拆分为多个 Worker-sized task。
- 拆分后自动改写下游依赖，保持 DAG 语义。

Lead Planner Prompt 也加入同样的硬约束，并要求开放式市场/赛道问题先做 Landscape Discovery，后续再由 Progress / Replan 决定深挖对象。

#### 前后对比

| 项 | 修复前 | 修复后 |
| --- | --- | --- |
| 5 实体 × 6 维度 | 1 个巨型 Worker | 拆成 6 个小 Worker |
| 依赖关系 | 拆分后可能断链 | 自动改写为全部 split task |
| Validator | 不检查任务粒度 | 输出 `task_too_large:<task_id>` |
| Planner Prompt | 仅鼓励拆分 | 明确 2 实体 / 4 维度 / 8 cell 硬上限 |

### 2. LLM Provider 错误统一分类

新增 `app/agent/llm_errors.py`：

- `content_filter`
- `rate_limit`
- `usage_limit`
- `timeout`
- `context_length`
- `auth`
- `bad_request`
- `unknown`

`SensitiveContentDetected` 会归类为 `content_filter`。Worker 对已知 Provider 错误返回 `WorkerResult(status="failed")`，不再把异常穿透 StateGraph。未知异常仍保持 fail-fast，避免掩盖代码 invariant 错误。

Tavily 的套餐额度错误：

```text
This request exceeds your plan's set usage limit.
Please upgrade your plan or contact support@tavily.com
```

会归类为 `usage_limit`。这属于外部检索服务配额耗尽，不能通过重试解决；系统应停止继续搜索、保留已获取 Artifact，并进入 partial synthesis。用户需要升级 Tavily 套餐、更换 API Key / 计划，或等待额度重置后再运行完整研究。

### 3. Timeout Partial Evidence Salvage

`LangChainWorkerRuntime` 新增 `salvage_worker_evidence()`：

- 从 Artifact Store 按当前 `step_index` / `task_id` 回收 Artifact。
- 生成 partial findings、sources 和 evidence refs。
- timeout 和 Provider 失败均执行回收。
- 普通失败 Worker 已注册的 evidence 不再被丢弃。

#### 前后对比

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| Worker timeout | `result=None`，evidence 为空 | 返回 Artifact-backed partial evidence |
| Content filter | 异常穿透 Graph | Worker failed + partial evidence |
| 普通 failed Worker | 已注册 evidence 被过滤 | 保留 evidence refs |
| 事件 | 只有 fail reason | 记录 `partial_evidence_count` |

### 4. Progress 变为证据驱动

`assess_progress()` 现在检查失败 Worker row 中的：

- `payload.findings`
- `payload.evidence_ids`
- `payload.sources`

如果失败 Worker 已有部分证据，不再自动追加 `empty:<task>` / `failed:<task>` 覆盖缺口。真正缺口仍由维度覆盖、brief contract、gaps、conflicts 评估决定。

### 5. Synthesis Best-effort Delivery

`node_synthesize()` 现在只要存在：

- Graph `evidence_refs`
- Graph `findings`
- force synthesis 标记

就允许失败依赖进入合成。Synthesis 自身遇到已知 Provider 错误时，不再 abort 整个 Run，而是渲染 partial report。

Graph findings 也会投影到 LoopState `metadata.partial_findings`，`PartialReportRenderer` 会把这些内容写入“已收集要点”，避免部分报告为空。

## P1 修复

### 1. Failure Origin / Detected 分离

新增 `record_failure()`：

```text
failure.origin_stage   只写一次，不覆盖
failure.detected_stage 随终止阶段更新
failure.type           记录 provider / runtime 类型
```

Harness 顶层异常处理不再用当前 `state.phase` 覆盖最早失败来源。

### 2. Trace Integrity Stage Canonicalization

新增 stage alias：

- `brief -> understand`
- `plan -> planning`
- `execute / compress -> worker`
- `replan -> progress`
- `finalize -> synthesis`
- `validate -> quality`

Integrity 判断先 canonicalize 再比较，因此 `compress` 不再被当作未知 stage。

### 3. Terminal-aware Quality

失败 / 中断 Run 不再强制要求 quality event；quality 只在成功或 partial 交付，或 origin 就是 quality 时必需。

Trace Integrity 与 Run Status 解耦：

```text
Run Status = failed
Trace Integrity = PASS
```

只要事件闭合，这是合法结果。

### 4. 前端 `[object Object]`

`ConversationThread.tsx` 不再把 React Element 拼进字符串。现在标签结构为：

```tsx
<time>
  {statusPrefix}
  <ElapsedTimer clock={elapsedClock} />
</time>
```

完成、暂停、运行中状态都会渲染真实耗时文本。

## 修改文件

| 文件 | 修改 |
| --- | --- |
| `app/research/planning/granularity.py` | 新增任务复杂度分析、超限拆分、依赖改写 |
| `app/research/planning/lead_planner.py` | Prompt 粒度硬约束；LLM plan 自动 normalize |
| `app/research/planning/validator.py` | 新增 `task_too_large` 校验 |
| `app/research/planning/progress.py` | 失败 Worker 的 partial evidence 不再判 empty |
| `app/agent/llm_errors.py` | 新增 Provider 错误统一分类 |
| `app/research/runtime/worker.py` | Provider 降级、Artifact salvage、失败保留 evidence |
| `app/research/runtime/runner.py` | evidence refs 投影、失败依赖合成、Synthesis Provider partial |
| `app/research/runtime/project.py` | Graph findings 投影到 LoopState |
| `app/agent/harness/partial_report.py` | 渲染 Graph partial findings |
| `app/agent/harness/loop.py` | failure origin 不覆盖 |
| `app/observability/failure.py` | 新增 provider failure 类型和 `record_failure()` |
| `app/observability/integrity.py` | stage alias + terminal-aware quality |
| `frontend/src/components/ConversationThread.tsx` | 修复耗时渲染 |
| `tests/test_research_resilience.py` | 新增 7 个端到端契约回归 |

## 回归测试

新增测试覆盖：

1. 5 实体 × 6 维度 oversized task 自动拆分。
2. 拆分后下游依赖自动改写。
3. Worker timeout 回收 Artifact partial evidence。
4. `SensitiveContentDetected` 返回 `WorkerResult(content_filter)`，Graph 不 crash。
4. Tavily `usage limit` 返回 `WorkerResult(usage_limit)`，Graph 不 crash。
5. 失败 Worker 有 partial evidence 时不产生 empty gap。
6. 全部 Worker 失败但有 evidence 时输出 partial report。
7. worker / compress 失败 Run 不要求 quality event。

## 验证结果

```text
python -m mypy
Success: no issues found in 11 source files

python -m pytest tests/test_research_resilience.py tests/test_worker_runtime_contract.py tests/test_obs_correctness.py tests/test_hybrid_planning.py tests/test_wave_progress_dispatch.py tests/test_architecture_p0.py
48 passed

frontend: tsc -b --noEmit
PASS

frontend: npm run build
PASS
```

相关 Worker 强类型契约见 `docs/worker-runtime-contract-2026-09.md`。
