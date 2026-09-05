# P1 / P2 设计与实现文档（2026-09）

## 总览

在 Run Isolation P0（见 `run-isolation-refactor-2026-09.md`）基础上，
本轮完成 P1「Agent 语义补齐」全部七项与 P2「生产化」的第一阶段
（Durable Run Queue + API/Worker 分离），并保持四条核心不变量：

```
Invariant 1  mutable state 唯一属于 run_id
Invariant 2  跨 Run 信息只能通过显式 Context Reference 流动
Invariant 3  Event / Artifact / Evidence / Step 可反查唯一 Run
Invariant 4  UI 状态 = 后端 Run State 的 Projection
```

---

## P1：补齐 Agent 语义

### 1. RunSummary（新增 `app/research/run_summary.py`）

每次 Run 终结时落盘 `runs/{run_id}/run_summary.json`：

```json
{
  "run_id": "...", "session_id": "...", "parent_run_id": null,
  "query": "...", "intent_summary": "...",
  "entities": [], "conclusions": [],
  "key_evidence_refs": [], "artifact_refs": [],
  "unresolved_questions": [], "status": "completed"
}
```

写入点：`_phase_finalize`（`loop.py`）。历史 Run 只暴露 Summary，
不暴露 raw artifact / checkpoint / 聊天全文。

### 2. FollowUpResolver（新增 `app/research/followup.py`）

四类追问与处理策略：

| 类型 | 判定 | 行为 |
|------|------|------|
| standalone | 无指代 + 语义重叠 < 0.25 | 不继承任何历史 |
| semantic_followup | CJK bigram 相关度 ≥ 0.25 | 继承最相关 1~3 个 RunSummary |
| explicit_reference | "上一轮/刚才/前面提到" | 继承最近 Run |
| artifact_followup | "那份 PDF/上一份报告" | 继承最近 Run + artifact 引用 |

中文相关性用字符 bigram 计算（无分词依赖，可解释、可 eval 校准）。

### 3. ContextBuilder 显式继承（`loop.py`）

`_bootstrap_run` 解析 FollowUpContext 存入 metadata；
`_phase_build_context` 把 context_block 作为显式记忆引用注入
（`source="followup_resolver"`，带 selected_runs 观测字段）。
**绝不注入历史 raw artifact 或整段聊天记录。**

### 4. Run-scoped WebSocket（前端 + monitor）

- `task_result` 事件携带 `run_id`；
- 前端 hook：`payload.run_id !== currentRunId` 的实时消息直接忽略
  （旧 Run 迟到的 terminal 事件不再污染新 Run UI）。

### 5. Session CRUD（`session_routes.py` + `service.py`）

``+ET /api/sessions?tenant_id=          # 租户内列表（归档默认隐藏）
POST /api/sessions/{id}/archive        # 归档：不参与默认历史
POST /api/sessions/{id}/unarchive
DELETE /api/sessions/{id}              # tombstone + 级联
DELETE /api/runs/{run_id}              # 删除 Run-owned 数据
```

级联删除：runs 行 + uploads 行 + session 行 +
`output/session_{id}/` + `updated/session_{id}/` + `logs/traces/{id}.jsonl`。

### 6. Retention（新增 `app/run_store/retention.py`）

```
intermediate（artifacts/evidence/state）→ 默认 7 天后清理
deliverables + run_summary.json        → 用户控制，永不自动删
raw trace                              → 90 天（随 Session 删除级联）
```

启动时自动执行 + `POST /api/admin/retention/apply` 手动触发。

### 7. Tenant Ownership（`sqlite.py` + `service.py`）

- sessions / runs 增加 `tenant_id / user_id / project_id`（含旧库自动迁移）；
- `get_run_scoped(run_id, tenant_id)`：跨租户即使知道 id 也返回不可见；
- 归档 / 删除 / 列表全部租户过滤；`/api/task` 创建时写入所有权。

> 部署注意：当前 tenant_id 来自请求参数（单机模式）。企业部署时
> 必须替换为认证中间件注入，禁止信任浏览器传值——接口签名已按此设计。

---

## P2：生产化第一阶段

### Durable Run Queue（新增 `app/run_queue/`）

```
POST /api/task ──(RUN_QUEUE_ENABLED=1)──▶ run_jobs 表（durable row）
                                              │
RunQueueWorker（可独立进程）◀── claim_next ────┘
      │ heartbeat / complete / fail
      ▼
  run_deep_agent
```

- SQLite WAL 持久化：进程崩溃后 job 不丢（pending 可重claim）；
- `enqueue / claim_next / heartbeat / complete / fail` 是稳定接口，
  多实例部署时替换为 Redis Streams / Postgres `SKIP LOCKED` 即可，
  上层语义不变；
- 默认关闭（`RUN_QUEUE_ENABLED=0`），单机直连执行保持兼容；
  开启后 API 与 Worker 可分进程部署。

### 已实现 vs 待外部设施

| 项 | 状态 | 说明 |
|----|------|------|
| Durable job 持久化 | ✅ | SQLite WAL，崩溃不丢 |
| API / Worker 分离 | ✅ | 队列模式 + 独立 worker 入口 |
| Run-scoped 事件 | ✅ | run_id 全链路 |
| 多实例锁 | 🔜 | 单进程互斥已实现；多实例换 Redis/PG |
| Event Bus 扇出 | 🔜 | 当前进程内 fanout；换 Redis Pub/Sub |
| 对象存储 | 🔜 | 本地 run_dir 布局已按对象存储键设计 |

---

## 修改文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `app/research/run_summary.py` | 新增 | RunSummary 模型与读写 |
| `app/research/followup.py` | 新增 | FollowUpResolver（CJK bigram 相关度） |
| `app/run_store/retention.py` | 新增 | 保留策略执行 |
| `app/run_queue/service.py` | 新增 | Durable Run Queue |
| `app/run_queue/worker.py` | 新增 | 队列 Worker |
| `app/agent/harness/loop.py` | 修改 | FollowUp 注入 / RunSummary 落盘 / task_result run_id |
| `app/api/monitor.py` | 修改 | task_result 携带 run_id |
| `app/api/session_routes.py` | 修改 | Session/Run CRUD + retention API |
| `app/api/server.py` | 修改 | 队列模式 + 启动 retention + 所有权写入 |
| `app/run_store/models.py` | 修改 | 租户/归档字段 |
| `app/run_store/sqlite.py` | 修改 | schema 迁移 |
| `app/run_store/service.py` | 修改 | CRUD / 级联 / 租户边界 |
| `frontend/src/hooks/useDeepAgentSession.ts` | 修改 | Run-scoped WS 过滤 |
| `tests/test_run_lifecycle.py` | 新增 | 8 项 P1/P2 回归 |

## 验证结果

```
后端：pytest 25 个测试文件 → 168 passed
前端：tsc -b --noEmit → 0 errors
```

## 后续（P2 第二阶段）

1. Redis Streams 队列适配器（替换 SQLite claim 锁）；
2. WebSocket Gateway 独立部署 + Redis Pub/Sub 事件扇出；
3. Artifact 迁移对象存储（run_dir 键 = session/run/artifact）；
4. Worker 水平扩缩容 + 心跳超时 reclaim；
5. 认证中间件注入 tenant（替换请求参数）。
