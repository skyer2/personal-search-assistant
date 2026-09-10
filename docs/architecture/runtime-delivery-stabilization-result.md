# Runtime Delivery Stabilization Result

日期：2026-09-10  
基线：`77665aa677dbf18265a3cee8b619609d40d14d7d`  
状态：实现完成，全量回归与发布冒烟通过。

## 目标

这轮修复不改变八节点 Research StateGraph 主流程，而是收敛控制面契约、任务预算、原子事实交付、合成失败语义、UI 投影和 Trace 血缘。目标是让“执行失败”和“质量拒绝/部分可确认”分开，让简单事实不再进入开放研究循环，并让 FILES 只展示用户真正需要的交付物。

## 前后对比

| 领域 | 修改前 | 修改后 |
|---|---|---|
| 语义控制面 | Brief / Supervisor 的结构化输出逻辑分散在 prompt/worker 逻辑中 | 新增 `StructuredLLMGateway`，语义 LLM 统一 raw ChatModel + `with_structured_output` + `ainvoke` |
| Coverage | Coverage LLM 在生产关键路径可能引入额外失败 | Coverage 语义判断不依赖 LLM；生产路径使用 `CoverageJudge(None, ...)` |
| Worker 预算 | Admission 与 Execution 可能使用不同 lease 语义 | 新增 `TaskBudgetProfile`，两处共享 token/LLM/search/fetch/output-token 契约 |
| 原子事实 | 旧 `simple_fact.py` 首句答案路径 | 新 `atomic_fact.py`，结构化抽取 + `SIMPLE_FACT_EVIDENCE_POLICY` + 只引用 supporting sources |
| 原子事实终止 | 证据不足时可能进入开放研究或重复合成 | 证据不足立即终态，返回“当前无法可靠确认”；不再重试同证据合成 |
| 合成调用 | 依赖 worker graph `astream`，timeout 语义混杂 | 直接调用 `synthesis_model.ainvoke`；正常 60s，降级重试 30s |
| 合成重试 | 失败分类与重试策略不清晰 | 仅 rate limit / unavailable / context length 可重试一次；timeout、auth、bad request、content filter、budget 不重试 |
| FILES | 内部 `run_summary.json`、`evidence.json`、raw web artifact 可能进入用户文件列表 | 只列 `.md/.pdf/.xlsx/.docx` 用户交付物，并排除内部执行文件 |
| UI 进度 | 依赖局部 callback，终态文案容易把质量拒绝显示成任务失败 | `phaseProjection` 聚合全事件，partial 显示“流程已结束 · 结果：部分可确认” |
| Trace | Root span 可能继承 Worker 上下文，lineage 可能重复 | Root 强制清空 task/plan/attempt 继承；lineage 以显式引用为准并去重 |
| Release Smoke | 仍断言旧 240s timeout，假模型不支持结构化输出 | 断言 60s；假模型支持结构化 atomic fact 输出；Q4 必须命中快路径 |

## 修改文件

### 新增

- `app/research/execution/structured_llm_gateway.py`
- `app/research/runtime/atomic_fact.py`
- `app/research/runtime/task_budget.py`
- `frontend/src/lib/phaseProjection.ts`
- `docs/architecture/runtime-delivery-stabilization-result.md`

### 删除

- `app/research/runtime/simple_fact.py`

### 核心实现

- `app/research/brief/compiler.py`
- `app/research/supervisor/agent.py`
- `app/research/runtime/admission.py`
- `app/research/runtime/runner.py`
- `app/research/runtime/worker.py`
- `app/research/runtime/state.py`
- `app/research/execution/worker_executor.py`
- `app/research/execution/synthesis_executor.py`
- `app/agent/harness/run_budget.py`
- `app/agent/harness/usage_tracker.py`
- `app/agent/harness/citations.py`
- `app/agent/harness/loop.py`
- `app/agent/llm.py`
- `app/agent/main_agent.py`

### 配置与交付

- `app/config/harness.yml`
- `app/config/loader.py`
- `app/run_store/files.py`
- `frontend/src/components/DeliverableFiles.tsx`
- `frontend/src/components/EventStream.tsx`
- `frontend/src/components/RunProgress.tsx`
- `frontend/src/lib/phaseProgress.ts`
- `frontend/src/lib/runProgressSelfCheck.ts`
- `frontend/src/styles.css`

### 可观测性与发布验证

- `app/observability/recorder.py`
- `app/observability/semantic.py`
- `scripts/release_smoke.py`
- `tests/test_observability_integrity.py`
- `tests/test_simple_fact_fast_path.py`
- `tests/test_synthesis_resilience.py`
- `tests/e2e/test_production_fidelity_synthesis.py`

## 验证结果

| 验证 | 结果 |
|---|---|
| `mypy` | 通过，20 个源文件无类型问题 |
| 全量 pytest | 414 passed |
| Eval dry-run gate | 60 / 60 passed |
| Frontend projection self-check | 通过 |
| Frontend production build | 通过 |
| L3 production fidelity | 10 passed |
| Release smoke | PASS |

发布冒烟覆盖：

- 3 次“当下国内AI初创公司”开放研究查询：均为非空 `partial`，Trace Integrity 全部 PASS；
- LangGraph / Temporal / Harness 对比查询：非空 `partial`；
- Agent 落地场景查询：非空 `partial`；
- “Transformer是哪一年提出的？”：快路径 `success`，回答 2017，1 个 worker、1 次合成、Trace Integrity PASS。

报告文件：`output/release_smoke/report.json`。

## 后续边界

- 质量拒绝不是执行失败，但也不得被包装成 success；这是明确的交付语义，不是测试后门。
- 原子事实不允许硬编码答案，必须由检索证据和结构化抽取共同支撑。
- 不恢复 TaskShape、Replan Agent、GapFill Agent 或 ExpandPlan Agent 作为生产控制路径。
- 本轮没有提交 Git commit；所有变更保留在工作区。
