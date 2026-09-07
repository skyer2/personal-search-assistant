# Harness 运行时架构（Phase 20–25）

> **权威方案**：[ARCHITECTURE.md](./ARCHITECTURE.md)。本文只补充 StateGraph Runtime 细节。  
> 调度权威固定为 **Research StateGraph**。没有 ANSWER/SEARCH 产品路径，也没有旧 while 调度回退。

对照：[教学版 deepsearch-agents](https://github.com/didilili/deepsearch-agents) 是一次 `create_deep_agent` 黑盒跑完全程。本仓库只保留 Research StateGraph 生产调度路径。

---

## 整体架构

```text
API / UI（实验台，不是搜索产品）
   → Product Route (TaskShape: fast_path / harness)
   → Research Domain (Brief / Planner / Progress / QualityGate)
   → Agent Runtime (StateGraph + SQLite checkpointer)
   → WorkerExecutorV2 / SynthesisExecutor
   → Environment: search / fetch / file  +  Artifact / Evidence
```

研究 Worker 固定走 `WorkerExecutorV2`；综合固定走 `SynthesisExecutor`，综合阶段禁止新增检索。

---

## 状态：Graph 是唯一 workflow truth

### UI 终态语义

| 用户可见终态 | 判定 | 含义 |
|--|--|--|
| 执行失败 | Run status = `failed` | 执行链路异常、超时或崩溃，不是质量结论 |
| 质量拒绝 | Run status = `partial` 且 `quality.passed=false` / `quality_passed=false` / `quality_reason` 非空 | 已执行但质量门禁拒绝低可信结论 |
| 部分可确认 | Run status = `partial`，且没有明确质量失败 | 已保留部分可信结论，但未达到完整交付标准 |
| 无法找到可靠来源 | Run status = `partial` 且 reason = `insufficient_trusted_evidence` / `no_evidence` / `no_content` | 检索完成但证据等级不足，不是执行错误 |

```text
ResearchState  →  LangGraph SQLite checkpointer
LoopState      →  进程内 handles（不 checkpoint 为 workflow）
Artifact/Evidence Store → 原文
```

不要再画「Graph SQLite + LoopState checkpoint.json」双恢复。

任务运行态只存 `ResearchState["tasks"]`。`task_status` 只能通过 `task_status_projection(tasks)` 在 API/UI 输出时动态生成，不再是持久化字段。

控制面阶段迁移必须经过 `transition_update()`；终止必须经过 `FINALIZE/ABORT → TERMINATED`。终止原因采用 first-cause-wins，后续 Quality/Finalize 不得覆盖首个真实原因。

Harness 路径：

```text
TaskShape
  ├── SIMPLE_FACT → single WorkerExecutorV2 search → deterministic answer
  └── other shapes
        → intent → clarify → plan → validate → dispatch
Send(isolated workers via WorkerExecutorV2) → Progress Evaluator
GAP → replan；ENOUGH → synthesis → Quality Gate → finalize
```

`direct` 仅对照实验：single worker + search tool，不进上述节点。

### Simple Fact Fast Path

`SIMPLE_FACT` is a product path, not a recommendation from the retired experiment router:

- one worker, at most two authorized provider searches, and at most three tool calls
- zero planner calls, zero replans, zero progress evaluation, zero compression, and zero synthesis-agent calls
- `render_simple_fact_answer` renders the grounded answer deterministically
- one `PRIMARY` source or two independent `HIGH_QUALITY_SECONDARY` sources are required; community-only evidence returns low confidence instead of success

The fast path still uses the real `RunBudgetManager`, worker lease, `ToolGateway`, `WorkerExecutorV2`, citation manager, validator, and finalizer. It does not create a second control plane.

### History Deletion Cascade

One UI turn maps to one Run. Deleting a turn is therefore a Run-level operation, not a message-level operation:

```text
DELETE /api/runs/{run_id}
  ├── RunStore row
  ├── output/session_{session_id}/runs/{run_id}
  ├── run_summary.json / evidence / artifacts
  ├── run_events + trace projections + payload dir
  ├── JSONL run file
  ├── graph checkpoint thread_id = run_id
  └── memories whose provenance.run_id = run_id
```

`DELETE /api/sessions/{session_id}` applies the same cleanup to every Run in the Session. Archive only changes session visibility and keeps data.

---

## 和教学版的差别

| | [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents) | 本仓库 |
|--|--------------------------------------------------------------------------|--------|
| 入口 | 每问都进主 Agent | 默认全部进 Harness；direct 只做 ablation |
| 编排 | 主 Agent 自己决定调哪个子 Agent | Domain 出 Brief/Plan；StateGraph 调度；WorkerRuntime 执行 |
| 上下文 | 历史全塞 | Brief + JIT；原文在 Artifact Store |
| 进度 | 无任务级 checkpoint | **只有**图内 SQLite checkpointer |
| Search | 产品能力 | **Environment tool only** |

相关代码：`app/research/runtime/graph.py`、`app/research/runtime/worker.py`、`app/research/planning/progress.py`、`app/agent/harness/artifacts.py`、`evidence_store.py`。

权威设计：[ARCHITECTURE.md](./ARCHITECTURE.md)。

面试运维面：`GET /api/harness/capabilities` 返回实验档 `agent|direct`、environment tools 与固定控制面实现。

---

## 工人契约与重试（避免研究空转）

研究步 `allowed_tools` = 来源工具（`internet_search` / `fetch_url` 或 `read_file_content`）**加上** JIT 回读 `read_artifact` / `read_evidence`。越权校验对这两个上下文工具始终放行。

`ResultValidator.no_error` 只认明确执行失败模板（如「步骤执行超时」），不扫摘要里的「失败 / 错误 / 异常」。

缺 JSON 时的 `structured_retry` / 外层 recover：

- **禁止**整步再搜再抓；检索额度置 0，指令要求只输出 JSON
- 产出抽取走最后一条助手正文，跳过 ToolMessage（避免对着 Tavily 文本找 `facts`）
- 仍没有 JSON 时，用本步已存 Artifact 卡片 salvage
- 工人已回 `summary`+`facts` 时压缩不再打 LLM

步内 `budget.max_step_tool_calls`（默认 8）硬限制 `internet_search` / `fetch_url`。会话 `max_tool_calls` 默认 40，给并行研究 + 写报告留余量；简单事实路径被硬收敛到 3。工具 start/end 由 Flight Recorder 统一 emit，Monitor 只做 WebSocket exporter，避免双计。

---

## 证据来源分级

`CitationManager` no longer treats every URL as primary. Evidence is classified as:

| Tier | Examples |
|------|----------|
| `PRIMARY` | official docs/site, official GitHub repository, original paper, government/regulator |
| `HIGH_QUALITY_SECONDARY` | Reuters/Bloomberg/FT/WSJ, major academic or standards bodies |
| `COMMUNITY` | CSDN, blogs, Zhihu, Xueqiu, Medium |
| `UNKNOWN` | all other unverified URLs |

Simple-fact completion uses source tier, not URL presence. Community-only evidence cannot be reported as a high-confidence success.

---

## Agent Flight Recorder

一次 Research Run 只 emit 一次语义事件（`app/observability/`）。JSONL、WebSocket、OTLP/Langfuse、进程内 Counter/Histogram 都是 exporter。并行 Worker span 的 key 是 `phase + task_id + attempt`，不再用 phase 当唯一 key。

看哪里：

- 实时 UI：过程框 / 执行过程
- 因果树：`GET /api/traces/tree/{session_id}` 与 Trace 查看器
- 落盘：`app/logs/traces/{session_id}/{run_id}.jsonl`
- 指标：`GET /api/metrics/summary`、`GET /api/metrics/prometheus`（窗口值是 gauge；`harness_live_*` 才是进程内 counter）

细节与事件词表见 [OBSERVABILITY.md](./OBSERVABILITY.md)。
