# Run Isolation + Lifecycle 重构落地文档（2026-09）

## 背景

依据架构评审：系统已有 `run_id`，但它尚未成为完整的执行隔离边界。
同 Session 连续 Run 时存在六类串台：文件工作区、Artifact/Evidence Store、
Leaf Agent checkpoint、WebSocket 终态、UI 文件投影、状态语义。
本次落地评审 P0 全部八项修复。

## 核心不变量

```
Invariant 1  任何 mutable execution state 都必须属于唯一 run_id
Invariant 2  跨 Run 信息只能通过显式 Context Reference 流动
Invariant 3  任何 Event / Artifact / Evidence / Step 都必须能反查唯一 Run
Invariant 4  UI 展示状态必须是后端 Run State 的 Projection
```

## P0 修改明细

### 1. run_dir 物理隔离（`loop.py`）

`_bootstrap_run()` 现在创建：

```
session_dir/
└── runs/{run_id}/
    ├── artifacts/    # ArtifactStore（Run Scope）
    ├── evidence/     # EvidenceStore（Run Scope）
    ├── deliverables/ # Markdown / PDF 交付物（Run Scope）
    └── state/        # StepCheckpointStore（Run Scope）
```

- `HarnessRunContext` 新增 `run_dir / artifact_dir / deliverable_dir`；
- ArtifactStore / EvidenceStore / StepCheckpointStore 全部改挂 Run 子目录，
  不再把 Session 目录历史加载进当前 Run；
- `_phase_finalize` / `validate_finalize` 的交付物写入与枚举全部改为
  `runs/{run_id}/deliverables`；
- working_notes / evidence.json 等 Run 状态文件写入 `run_dir`。

### 2. 交付物复用 Bug（`deliverables.py`）

彻底删除 `persist_markdown_if_missing()` 中的：

```python
existing = list_markdown_files(root)
if existing:
    return existing[0]   # 已删除：Run2 会拿到 Run1 的 Markdown
```

新规则：目标文件不存在 → 新建；存在 → 由当前 Run 决定 overwrite；
绝不回退复用目录里任意已有 Markdown。

### 3. Run Artifacts API 语义（`session_routes.py` / `files.py`）

`GET /api/runs/{run_id}/artifacts` 不再借 `session_id` 列整段 Session 历史，
新增 `list_run_output_files()` 严格只列 `runs/{run_id}/` 下文件；
Run 目录不存在 → 空列表（不回落 Session）。

### 4. 前端 Files 面板（`api.ts` / `useDeepAgentSession.ts`）

新增 `listRunArtifacts(runId)`；`refreshFiles` 优先加载当前 Run 交付物，
失败才回落 Session 历史。Files 面板默认只展示本次回答产生的文件。

### 5. Leaf thread Run 隔离（`window_hygiene.py` / `loop.py`）

```text
旧：{session_id}:step:{index}          ← Run1/Run2 Step0 完全相同
新：{session_id}:{run_id}:step:{index}
```

并行线程同理。同一 Session 不同 Run 的 Leaf Agent 不再命中
全局 InMemorySaver 里的旧 checkpoint。

### 6. 结构化终态（`monitor.py` / `loop.py` / 前端）

`task_result` 事件携带：

```json
{
  "result": "...",
  "status": "partial",
  "termination": {"reason": "budget_tool_calls", "stage": "research"}
}
```

前端 `setServerStatus(payload.data.status)`，partial 不再显示
"100% 全部阶段完成"（`phaseProgress` 仅在 `completed` 时强制 100%）。

### 7. Trace Viewer 降级（`TraceViewer.tsx`）

`/api/meta` 从 `Promise.all` 硬依赖改为 `.catch()` 降级（unknown），
404 只显示版本警告，不再把整块 Trace 拉成空白。

### 8. Budget 止血（`harness.yml`）

```text
max_total_tokens   100000 → 180000
max_tool_calls        40 → 120
max_step_tool_calls    8 → 16
max_run_sec          600 → 900
synthesis_reserve_sec 210 → 120
max_llm_calls_per_run 30 → 45
max_llm_calls_per_worker 8 → 10
max_plan_steps         8 → 10
agent.max_research_tasks 5 → 6
```

## 修改文件清单

| 文件 | 说明 |
|------|------|
| `app/agent/harness/loop.py` | run_dir 引导 / 交付物 Run Scope / thread_id / 结构化终态 |
| `app/agent/harness/window_hygiene.py` | thread_id 加入 run_id |
| `app/agent/harness/deliverables.py` | 删除 existing[0] 复用 |
| `app/agent/harness/validator.py` | validate_finalize 支持交付目录 |
| `app/api/monitor.py` | task_result 结构化 status |
| `app/api/session_routes.py` | Run artifacts 真正 Run Scope |
| `app/run_store/files.py` | list_run_output_files |
| `app/research/runtime/runner.py` | 交付/evidence 写入 Run 目录 |
| `app/research/runtime/worker.py` | working memory 写 run_dir |
| `app/config/harness.yml` | Budget 120/16/900s |
| `frontend/src/lib/api.ts` | listRunArtifacts |
| `frontend/src/hooks/useDeepAgentSession.ts` | Files 当前 Run + 真实终态 |
| `frontend/src/components/TraceViewer.tsx` | /api/meta fail-open |
| `tests/test_run_isolation.py` | 新增：6 项隔离不变量测试 |
| `tests/test_pdf_deliverable.py` | 更新为新交付语义 |
| `tests/test_harness_phase13_guardrails.py` | 预算断言更新 |
| `tests/test_worker_retry_storm.py` | 预算断言更新 |

## 验证结果

```
后端：pytest 21 个直接相关文件 → 160 passed
前端：tsc -b --noEmit → 0 errors
```

剩余失败均为预存环境问题（opentelemetry 缺依赖、
obs trace integrity 命名漂移、Windows SQLite 临时文件锁），
与本次改动无关。

## 未尽事项（P1/P2，按评审优先级）

1. FollowUpResolver + RunSummary（显式跨 Run 上下文继承）；
2. Run-scoped WebSocket subscription（按 run_id 过滤 terminal event）；
3. Tenant / Project 存储所有权与 403 边界；
4. Session / Run CRUD + Retention 级联删除；
5. 分布式 Runtime（Durable Queue + Event Bus + 多 Worker）。
