# Runtime Failure Observability Result

日期：2026-09-11  
基线：`97c161505171be09d31b92c5c853ade7483e03b6`  
状态：实现完成，全量回归、评测门禁、前端构建、发布冒烟和 PDF 端到端验证通过。

## 目标

本轮修复不改变八节点 Research StateGraph 主流程，而是对齐 Worker 执行策略、预算语义、raw ChatModel 调用、结构化降级诊断和用户可见交付语义。目标是让一次 Run 的 Trace 能直接回答：控制面是否降级、最早失败在哪里、哪个资源耗尽、工具是否被拒绝、LLM 调用进展如何、Synthesis 是本地失败还是 Provider 失败。

## 前后对比

| 领域 | 修改前 | 修改后 |
|---|---|---|
| Worker 检索预算 | 搜索与抓取共用一个模糊 retrieval counter | 拆分为 search queries、fetch sources、logical tool invocations |
| 批量工具 | `batch_search(N)` 可能耗尽整个 retrieval 预算，导致后续 fetch 被拒 | `batch_search(N)` 只扣 N 个搜索和 1 次逻辑调用；`batch_fetch(N)` 只扣 N 个抓取和 1 次逻辑调用 |
| 预算拒绝 | 只能从工具返回或 Worker 失败间接推断 | 统一 `budget.denied` 事件，携带 scope、resource、reason、used、limit、Worker/Run 快照 |
| 拒绝原因 | `research_token_cap` 等语义混杂 | 区分 worker token、research phase token、run token、worker/run LLM call、search、fetch、tool call |
| Synthesis 调用 | raw ChatModel 收到 Agent-state dict，本地校验快速失败 | 收到 `[HumanMessage]` 消息列表；本地错误分类保留 type/message/category |
| 结构化降级 | Brief/Supervisor 静默吞异常 | `semantic.fallback` 保留 error type、message、category、model、schema |
| Worker 终态 | finally 中可能把成功误报为失败 | 先保存 WorkerResult，再统一 emit terminal；成功路径是 `worker.completed` |
| Worker 可观测性 | 缺少完整预算快照 | 终态携带 LLM、token、search、fetch、tool 的 used/limit |
| LLM 可观测性 | 只有总量 | `gen_ai.chat` 携带 phase、task、call index、token estimate、duration、TTFT、Worker 剩余额度 |
| 工具观测 | 参数容易被整体压缩或误传 | `args_meta` 只保留参数名和列表条目数；不记录 query、URL、prompt 或正文 |
| 工具返回 | telemetry 会覆盖非字符串原始结果 | telemetry 旁路记录，工具结果原样透传 |
| Partial 交付 | 内部 runtime code 可能变成研究事实 | 优先保留恢复的结构化 findings；渲染层过滤内部预算/超时错误码 |
| Coverage 恢复 | 保留 findings 时丢失任务目标绑定 | 同时保留 claims、supported criteria 和 target gaps |
| 历史兼容 | Supervisor 仍接受旧 `max_search_calls` 字段和 `THINK` 动作 | 删除旧字段别名和旧动作映射，仅保留 `max_search_queries` 与 `CONDUCT_RESEARCH` / `COMPLETE` |

## 核心实现

- `app/agent/harness/step_budget.py`：Worker 三资源预算与原子扣减。
- `app/agent/harness/budget_events.py`：统一预算拒绝事件。
- `app/research/execution/tool_gateway.py`：Worker 检索执行作用域。
- `app/research/runtime/task_budget.py`：任务预算 Profile。
- `app/research/execution/worker_executor.py`：Worker 快照、terminal 语义与 fast-path 搜索。
- `app/research/execution/synthesis_executor.py`：raw ChatModel 消息列表与失败分类。
- `app/research/execution/structured_llm_gateway.py`：结构化调用与降级诊断。
- `app/research/runtime/ingestion.py`：恢复 findings、claims 与 Coverage 绑定。
- `app/research/delivery/partial_renderer.py`：用户语义的降级部分交付。
- `app/observability/*`：事件词表、Recorder、隐私白名单和 Trace 汇总。
- `app/tools/batch_retrieval.py`、`app/tools/fetch_url.py`、`app/tools/tavily_tool.py`：工具扣费与 telemetry。
- `scripts/release_smoke.py`：发布冒烟适配 raw ChatModel `ainvoke` 与新 ToolGateway 契约。
- 删除 `docs/architecture/runtime-delivery-stabilization-result.md`，避免历史结果与当前架构并存。

## 验证结果

| 验证 | 结果 |
|---|---|
| `mypy` | 通过，20 个源文件无类型问题 |
| 全量 pytest | 421 passed |
| Runtime failure observability contract | 7 passed |
| Simple fact fast path | 25 passed |
| Production fidelity synthesis | 10 passed |
| Timeout salvage + PDF full stack | 1 passed |
| Eval dry-run gate | 60 / 60 passed |
| Frontend projection self-check | 通过 |
| Frontend production build | 通过 |
| Release smoke（Q1 × 3 + Q2-Q4） | PASS；Q1 三次均为非空 partial，Trace Integrity 全部 PASS |
| Atomic fact release query | success，回答 2017，一手 arXiv 来源，Trace Integrity PASS |
| Golden PDF query | `你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？输出结果为pdf` 端到端 success，生成 1 个 PDF |

发布冒烟中的 Synthesis 走 raw ChatModel `ainvoke` 主路径，无 `unknown_provider_error`，开放研究查询为质量语义上的 partial，不是执行失败。

## 边界

- 质量不足时仍输出 partial，不伪造 success。
- 工具预算拒绝是 denied，不是空成功。
- 事件不记录完整 prompt、query、URL、key 或网页正文。
- 本轮不提交 Git commit，所有变更保留在工作区。
