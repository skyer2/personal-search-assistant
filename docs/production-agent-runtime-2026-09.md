# Production Agent Runtime 设计（2026-09）

## 1. 目标

把现有 Research Agent 从“多 Agent Demo”升级为生产级 Harness：

```text
LLM 决策层：Planner / Worker / Evaluator
确定性控制层：Budget / Deadline / Granularity / Failure Policy / Security
持久数据层：Artifact / Evidence / Coverage / Trace
```

核心原则：

- LLM 负责不确定推理和探索策略。
- Runtime 负责资源、时间、安全、失败和状态边界。
- Evidence 负责判定进度，而不是 Worker 成败。
- 有可信证据时优先 partial delivery，而不是整体失败。

## 2. 目标架构

```text
User Query
    ↓
Research Brief
    ↓
Complexity / Effort Allocator
    ↓
Lead Planner
    ↓
Granularity Guard + Plan Budget Validator
    ↓
Dispatch Wave
    ↓
Worker Lease
 ├─ hard wall timeout
 ├─ idle timeout
 ├─ tool/token budget
 └─ heartbeat
    ↓
Artifact Store ── Write-ahead raw artifact
    ↓
Untrusted Content Boundary
    ↓
Evidence Candidate / Evidence Store
    ↓
Coverage Matrix
    ↓
Progress Evaluator
 ├─ enough → Synthesis
 ├─ gap → Gap-only Replan
 └─ deadline → Partial Synthesis
    ↓
Citation / Quality Gate
    ↓
SUCCESS / PARTIAL / FAILED
```

## 3. Planning 设计

### 3.1 Budget-aware Planning

Planning 顺序固定为：

1. Research Brief 提取实体、维度、深度、成功标准。
2. 确定性 Complexity Estimation。
3. Effort Allocation 得到任务数、Worker 数、单 Worker 工具预算。
4. Lead Planner 只生成研究目标 DAG。
5. Granularity Guard 强制拆分 oversized task。
6. Plan Budget Validator 校验 hard ceiling。

### 3.2 Worker Task Spec

每个 research step 的 metadata 必须包含：

```text
entities
coverage_keys
estimated_cells
max_retrieval_calls
effort
granularity_split_from
```

当前粒度策略：

- entity ≤ 2
- dimension ≤ 4
- estimated cells ≤ 8

这些阈值是本项目策略，不是行业固定标准；后续由 eval 数据和工具延迟回归调优。

### 3.3 先广后深

开放式市场调研第一波只做 candidate discovery：

- 不做单公司完整 due diligence。
- 每个 Landscape Worker 只收集候选、赛道、融资、产品、商业化信号。
- Coverage Matrix 生成后，第二波只补 missing cells。

## 4. Worker Lease 设计

Worker 不再只有 hard wall timeout，而是三层控制：

| 控制项 | 语义 | 当前默认 |
| --- | --- | --- |
| `max_wall_sec` | 最长执行时间 | `step_timeout_sec` |
| `idle_timeout_sec` | 无进展时间 | 30s |
| heartbeat | child state / trace / artifact 数变化 | 每秒采样 |

进展信号包括：

- child `tool_calls_count` 变化
- child `trace` 长度变化
- child `step_results` 长度变化
- Artifact Store 数量变化

超时分类：

- `step_timeout`：hard wall 到期。
- `idle_timeout`：持续无进展。

两者都返回 `WorkerResult(status="failed")`，并执行 partial evidence salvage。

## 5. Write-ahead Evidence 设计

工具结果落盘时即成为持久状态，不等待 Worker 成功：

```text
search/fetch
    ↓
Artifact Store: raw + trust metadata
    ↓
sanitize/extract
    ↓
Evidence Candidate
    ↓
Worker timeout/failure
    ↓
salvage partial evidence
```

因此 Worker timeout 后仍可获得：

- artifact refs
- sanitized excerpts
- evidence candidates
- source locators
- partial findings

## 6. Untrusted Content Boundary 设计

所有外部网页/文件/API 返回均视为 `untrusted_external`。

原始 Artifact 保留原文，但进入模型前必须经过：

1. 去除 script/style/noscript。
2. 去除 HTML 标签。
3. 折叠空白。
4. 移除常见 prompt-injection 指令行。
5. 限制摘要长度。

Extracted evidence 标记：

```text
trust=external_extracted
instruction_free=true
artifact_id
locator
```

 privileged Agent 不直接消费完整 raw external content；优先消费结构化 excerpt 和 evidence refs。

## 7. Provider Failure Taxonomy 设计

| 类型 | Retry | 处理 |
| --- | ---: | --- |
| `rate_limit` | 是 | 尊重 Retry-After，有限重试 |
| `usage_limit` | 否 | 停止搜索，partial synthesis |
| `server_error` | 是 | backoff + jitter |
| `connection_error` | 是 | 有限重试 |
| `timeout` | 有限 | 重试或降级 |
| `content_filter` | 否原样重试 | 缩上下文/结构化摘录 |
| `context_length` | 否原样重试 | compact/rebuild context |
| `invalid_request` | 否 | fail node |
| `auth` | 否 | fail dependency |
| `unknown` | 否 | fail fast |

Provider 错误是 Worker 级失败，不默认升级为 Run 级失败。

## 8. Coverage Matrix 设计

Progress 从“Worker 成败”改为 claim/evidence 覆盖：

```text
entity × dimension
    ↓
covered / partial / missing
    ↓
coverage_ratio
    ↓
gap-only replan
```

每个 cell 记录：

- entity
- dimension
- status
- evidence ids
- confidence
- task id

Stop condition 不看全部 Worker 是否完成，而看：

- coverage ratio
- critical dimensions
- unresolved high-severity conflicts
- remaining budget
- marginal value

## 9. 终止状态设计

| 状态 | 条件 |
| --- | --- |
| `SUCCESS` | 核心证据和 quality gate 达标 |
| `PARTIAL` | 有可信证据但有明确 coverage gap / provider 限制 / deadline |
| `FAILED` | 无法生成基本交付，或 invariant / 依赖致命错误 |

Worker timeout、content filter、rate limit、source missing 通常只影响 coverage，不应自动导致 Run failed。

## 10. 前后对比

| 项 | 旧设计 | 新设计 |
| --- | --- | --- |
| Planner | 语义拆任务 | Brief → complexity → effort → granularity guard |
| Worker timeout | 仅 hard wall | hard wall + idle timeout + heartbeat |
| Tool 结果 | Worker 成功后才消费 | Write-ahead Artifact/Evidence |
| 外部内容 | 原文进入上下文风险高 | Untrusted boundary + structured excerpt |
| Provider 错误 | `except Exception` | taxonomy + policy + selective retry |
| Progress | Worker 成败优先 | Coverage Matrix 优先 |
| Replan | 容易重复大任务 | 只补 missing cells |
| 终止 | failed 或 success | success / partial / failed |

## 11. 验收标准

1. oversized task 自动拆分且依赖闭合。
2. Worker idle / wall timeout 均返回 `WorkerResult` 并回收证据。
3. retryable provider 错误有限重试；不可重试错误降级。
4. 外部内容进入模型前带 trust metadata 并完成 sanitization。
5. Progress 输出 coverage matrix 和 coverage ratio。
6. GAP 只生成 missing cell 补任务。
7. 有证据但研究不完整时输出 partial。
8. Runtime mypy、相关回归、前端类型检查和构建通过。

## 12. 本轮实现状态

### 12.1 Planning / Granularity

- 已实现 Budget-aware Planning 主链：Brief → complexity → effort → hard ceiling。
- 已实现 deterministic Granularity Guard，超限 task 自动拆分并改写依赖。
- Planner Prompt 已明确 Landscape Discovery 与单 Worker 粒度边界。

### 12.2 Worker Lease

- 新增 `_run_worker_step_with_lease()`。
- 同时执行 hard wall timeout 与 idle timeout。
- 每秒采样 child tool count、trace length、step result length 和 Artifact 数量作为 heartbeat。
- `idle_timeout` 与 `step_timeout` 分开落 trace 和 `WorkerResult.fail_reason`。

### 12.3 Provider Failure Policy

- Provider taxonomy 扩展为：
  - `rate_limit`
  - `usage_limit`
  - `server_error`
  - `connection_error`
  - `timeout`
  - `content_filter`
  - `context_length`
  - `invalid_request`
  - `auth`
  - `unknown`
- 新增 `failure_policy()` 与 `retry_delay_sec()`。
- retryable 错误有限重试；不可重试错误进入 Worker 失败或 partial synthesis。

### 12.4 Untrusted Content Boundary

- 外部 web/file/kb/pdf Artifact 写入时自动标记 `trust=untrusted_external`。
- 新增 `sanitize_untrusted_content()`：
  - 去除 script/style/noscript/iframe/object/embed。
  - 去除 HTML 标签。
  - 按 HTML 块分行。
  - 移除常见 prompt-injection 指令行。
  - 限制摘录长度。
- 新增 `structured_evidence_from_artifact()`，输出 `trust=external_extracted` 与 `instruction_free=true`。
- Timeout salvage 只把结构化摘录给后续 Progress/Synthesis，不直接消费 raw webpage。

### 12.5 Coverage Matrix

- 新增 `build_coverage_matrix()`，以 `entity × dimension × evidence` 构建覆盖矩阵。
- 新增 `coverage_gap_items()`，只输出 missing cell。
- `ProgressAssessment` 新增：
  - `coverage_matrix`
  - `coverage_ratio`
- GAP Replan 继续复用现有 `build_progress_patch()`，但输入来自 Coverage Matrix，因此只补缺失 cell，不重复原大任务。

### 12.6 前后对比

| 项 | 修改前 | 修改后 |
| --- | --- | --- |
| Worker 超时 | 只有 hard wall timeout | hard wall + idle timeout + heartbeat |
| Provider 429/5xx/连接错误 | 统一异常或简单失败 | 分类 + selective retry |
| Provider usage/content filter | 可能穿透 Run | Worker failed + partial evidence |
| 外部网页 | raw content 直接有进入模型风险 | untrusted artifact + sanitized excerpt |
| Timeout salvage | Artifact 摘要直接复用 | instruction-free structured evidence |
| Progress | Worker/文本启发式为主 | Coverage Matrix + coverage ratio |
| Replan | 可能重复原任务 | 只补 entity × dimension missing cell |

## 13. 验证记录

```text
python -m mypy
Success: no issues found in 12 source files

python -m pytest tests/test_research_resilience.py tests/test_worker_runtime_contract.py tests/test_obs_correctness.py tests/test_hybrid_planning.py tests/test_wave_progress_dispatch.py tests/test_architecture_p0.py tests/test_latency_engineering.py tests/test_adaptive_effort.py
78 passed

python -m pytest tests
321 passed, 12 failed

frontend: tsc -b --noEmit
PASS

frontend: npm run build
PASS
```

全量测试中的 12 个失败与既有基线一致，主要为：

- 本地未安装 `opentelemetry`
- 历史 phase 配置期望漂移
- Windows SQLite 文件锁
- 历史工具预算期望漂移

这些不是本轮修改引入的回归。
