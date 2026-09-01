# 个人研究 Memory

> 权威产品叙事见 [ARCHITECTURE.md](ARCHITECTURE.md)。实现仍在 `app/agent/memory/`；**主故事不是 TTL / half-life / SUPERSEDE**，而是个人研究记忆。
>
> 面试精简版见 [MEMORY_INTERVIEW.md](./MEMORY_INTERVIEW.md)。单次任务窗口见 [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md)。

---

## 一、先建立整体框架

### 1.1 一句话定位

个人助手真正要记住的是：

```text
用户上次研究过什么？
当时得出了什么结论？
哪些证据已经过期？
这次问题是不是之前研究的 continuation？
```

这叫 **Personal Research Memory**。压缩、checkpoint、trace 仍然存在，但它们不是 Memory 的主故事。

实现上仍是分层状态：单次任务靠压缩撑住窗口，同一次运行靠 **LangGraph checkpoint** 可续跑，跨任务只持久化经过筛选、可溯源的结论与来源。

```text
P0  上次研究过什么、结论是什么     → Research History + Findings
P1  证据是否过期、来源是否还可信   → Source History + freshness
P2  这次是不是 continuation        → Conversation + project context
P3  用户偏好（要短答、关注 C++）   → Preference
```

TTL / consolidation durable job / trust SUPERSEDE 可以留在实现里，但不要当成产品卖点。

### 1.2 和另外三套「存东西」的关系（不要混）

一次运行里仓库里其实有多套持久化，只有长期层叫 Memory：

| 层 | 实现 | 生命周期 | 记什么 |
|---|---|---|---|
| **长期记忆 Memory** | `MemoryStore`（SQLite 默认） | 跨 session、跨任务 | 研究结论、偏好、已查来源 |
| **工作记忆 Compaction** | `ContextCompressor` + `ContextBuilder` | 单次任务内 | 步骤摘要、evidence digest |
| **图内 Checkpointer** | LangGraph SQLite | 单次 RESEARCH 工作流 | **唯一** workflow resume |
| **可观测 Trace** | JSONL `logs/traces/{session_id}.jsonl` | 离线分析 | phase 事件 |

`LoopState checkpoint.json` **不再**作为第二套恢复系统。

### 1.3 业界五层 ↔ 本仓映射

```text
业界工作记忆     →  ContextCompressor + prior 摘要 + evidence digest + token 预算
业界会话记忆     →  Conversation Store + LangGraph ResearchState checkpoint
业界情节记忆     →  MemoryStore.semantic / episodic + 项目加权召回
业界用户记忆     →  MemoryStore.preference（user_explicit = trusted）
业界程序性记忆   →  MemoryStore.procedural + 来源台账
```

### 1.4 一次任务里 Memory 怎么转起来

```text
POST /api/task  (query, thread_id, user_id, tenant_id, project_id)
        │
        ▼
  解析 MemoryIdentity，绑到 ContextVar
        │
 UNDERSTAND → PLAN →（可选 HITL）
        │
 BUILD_CONTEXT  ── recall 知识记忆 + 加载项目来源台账 ──► 注入每步 prompt
        │
 while 逐步执行:
        │
        ├─ 若是写报告步：二次 recall（只接受 derived+）
        ├─ EXECUTE 子 Agent
        ├─ COMPRESS + 注册 Citation
        ├─ 检索步成功：默认只写 Source Ledger / Evidence，不进长期 Memory
        └─ HITL reject/edit：沉淀 procedural
        │
 FINALIZE  ── Memory Candidate + Utility/Provenance/Freshness Gate ── ADD/UPDATE/SUPERSEDE
        │
        └─ durable job 入队 → drain consolidation：衰减 / 独立确认晋升 / 硬清理
```

---

## 二、身份与隔离（谁的记忆）

文件：`app/agent/memory/identity.py`

长期记忆的隔离边界是四元组，**不是**「一个 session 一份 JSON」：

| 字段 | 作用 |
|---|---|
| `tenant_id` | 企业/组织，最外层隔离 |
| `user_id` | 真实用户；没有则退化为 `session_id` 并标 `ephemeral=True` |
| `project_id` | 研究专题。同项目加权召回、来源台账都靠它 |
| `session_id` | 只做溯源，**不参与隔离** |

解析优先级：

```text
请求参数  >  ContextVar  >  环境变量  >  session 退化
```

FastAPI `POST /api/task` 把 `user_id / tenant_id / project_id` 传进 `run_deep_agent` → `AgentHarness.run` 里 `set_memory_identity`，`finally` 里 `reset`。这样单进程多用户不会再共享同一个 `HARNESS_MEMORY_USER_ID`。

生产默认：`require_explicit_identity=true`，拒绝匿名（ephemeral）写入。本地单测可在 `MemoryPolicy` 里关闭。不要把 `HARNESS_MEMORY_USER_ID` 当多用户方案。

---

## 三、数据模型（一条记忆长什么样）

文件：`app/agent/memory/models.py`

### 3.1 类型（分库语义）

| `MemoryType` | 含义 | 典型来源 |
|---|---|---|
| `semantic` | 可复用研究结论、领域事实 | finalize、database_query、knowledge_base |
| `episodic` | 某次任务/步骤的具体经历 | network_search、file_read |
| `preference` | 交付格式、关注领域 | 用户显式写入 |
| `procedural` | 怎么查、用户改过什么 | HITL reject/edit |
| `source` | 已查来源台账（独立通道，不进常规 recall） | URL 去重后的 ledger |

常规召回只取前四类（`RECALLABLE_TYPES`）。来源台账走 `list_sources`，在 prompt 里单独成【项目已查来源】块。

### 3.2 写入来源

`finalize` / `step_incremental` / `user_explicit` / `seed` / `mem0` / `hitl` / `consolidation`

### 3.3 一条 `MemoryRecord` 的关键字段

- 内容：`fact`
- 隔离：`tenant_id`, `user_id`, `project_id`
- 治理：`version`, `confidence`, `is_deleted`, TTL（按 `created_at`）
- 信任：`trust_tier`（untrusted / derived / trusted）
- 溯源：`provenance`（url / sql / file / kb、evidence_ids、citation_count）
- 冲突链：`dedup_key`, `supersedes`, `superseded_by`
- 反馈：`recall_count`, `last_recalled_at`, `embedding`
- 任务元数据：`task`, `topic`, `session_id`, `write_source`

---

## 四、信任分级与防污染（研搜特化的核心）

文件：`app/agent/memory/provenance.py`

### 4.1 为什么必须有这一层

深度研搜最大的记忆失效模式：**把不可信网页抽成 fact 写进长期库，之后每次 recall 都注入 prompt** —— 一次性注入变成永久注入。

### 4.2 三级信任

```text
trusted    用户亲口说的、HITL 批准、系统种子     → 写报告可引用
derived    内部库 / 知识库 / 带引用的报告结论     → 写报告可引用
untrusted  外部网页原文，即便带 URL               → 只作线索，合成步默认不注入
```

判定规则（`classify_trust_tier`）：

- `user_explicit` / `seed` / `hitl` → **trusted**
- `network_search` → **永远 untrusted**（有 URL 只代表「有出处」，不代表内容可信）
- `database_query` / `knowledge_base` / `file_read` → **derived**
- finalize 报告：有 `[n]` 引用或 evidence → derived，否则 untrusted

召回权重：trusted 1.0 / derived 0.9 / untrusted 0.6。

### 4.3 两道硬门（不是靠 prompt「请谨慎」）

**写门：** 网页步 `require_provenance_for_step_write=true` 时，没有 URL / evidence_id 的内容 **直接不写**。

**读门：** 写报告 / 汇总步（`generate_markdown` / `summarize` / `convert_pdf`）二次召回时，`synthesis_min_trust=derived`，untrusted 进不了报告上下文。

检索结果在 prompt 里还会包 `<untrusted source="...">`，记忆层也可包 `<untrusted source="user_memory">`。

---

## 五、写入路径（什么时候记、记什么）

文件：`extractor.py`、`loop.py`、`consolidation.py`、`governance.py`

### 5.1 四条写入通道

| 时机 | 代码 | 抽什么 | 信任 |
|---|---|---|---|
| 检索步成功 | `_maybe_remember_step` | 启发式 1～2 句；网页必须带 provenance | 网页 untrusted，库/KB derived |
| 最终成功 | `_phase_finalize` | LLM 抽 3～5 条 typed fact（失败则启发式） | 有引用则 derived |
| HITL reject/edit | `_flush_hitl_memories` | 「用户拒绝了某步 / 改了计划」 | trusted + procedural |
| API 显式 | `POST /api/memory/facts` | 用户自己提交 | trusted |

默认 **失败的 partial 任务不写**（`remember_on_partial=false`），避免把半成品当结论。

### 5.2 冲突动作（对齐 Mem0，多了 SUPERSEDE）

写入时先找同类型相似记录（Jaccard + embedding，来源台账用 `dedup_key` 精确撞）：

| 动作 | 何时 | 效果 |
|---|---|---|
| `ADD` | 没有相似 | 新行 |
| `NOOP` | 原文完全相同，或 **低信任想覆盖高信任** | 不改（网页改不了用户偏好） |
| `UPDATE` | 同主题刷新、非矛盾 | 覆盖 fact，version+1，旧 fact 进 `fact_history`（最多 5 条） |
| `SUPERSEDE` | 数字变了 / 含否定对 | 旧记录软删并写 `superseded_by`，新记录带 `supersedes` |

这是研搜相对聊天助手的特化：15% 改成 15.5% 不是「合并成一句」，而是 **留下「结论何时被推翻」的审计链**。

### 5.3 来源台账（Source Ledger）

检索步一旦抽出 URL，就按归一化 URL 的 sha1 写入 `source_ledger`（同 URL 只加 `hit_count`）。下次同项目 `BUILD_CONTEXT` 时注入【项目已查来源】，减少重复检索。质量字段：`reliable / mixed / unreliable / unknown`。网页默认 `mixed`，内部源 `reliable`。

### 5.4 离线巩固（finalize 之后）

`consolidation_async=true` 时用 `asyncio.create_task` 后台跑，不挡返回：

- **衰减**：置信度按半衰期（默认 30 天）打折，被 recall 过的衰减更慢，有 floor
- **晋升**：derived 且跨多个 session 被命中 → trusted
- **硬清理**：软删超过 `purge_after_days`（默认 180 天）物理删除
- untrusted **不会**被晋升

也可手动 `POST /api/memory/consolidate`。

---

## 六、召回与注入（什么时候用、怎么进 prompt）

文件：`recall/hybrid.py`、`context_builder.py`、`loop.py`

### 6.1 两次召回

1. **BUILD_CONTEXT（任务开始，一次）**  
   query = 原始用户问题；注入后续 **每一检索步** 的 memory 层。

2. **合成步前二次召回（写报告）**  
   `target_step_type=generate_markdown` 等，只留 derived+，覆盖 `state.memory_facts`，避免脏网页进入最终报告。

### 6.2 Hybrid 打分

过滤：未删除、未被取代、未过 TTL、类型在 `RECALLABLE_TYPES`、过信任准入门。

分数：

```text
score = (kw_weight * 关键词 + emb_weight * cosine)
        + 0.1 * 新近度 + 类型加成 + 同项目加成
        × 置信度缩放 × 信任权重
```

无 embedding 时关键词权重拉满。完全无命中则冷启动返回最近记录。

召回后会 `mark_recalled`（`recall_count+1`，记下 `seen_sessions`）。`recall_count` 只用于 utility/衰减，**不能**把记忆晋升为 trusted。

### 6.3 Prompt 里长什么样

`ContextBuilder.build_step_message` 的分层（超 token 预算会截尾）：

```text
用户问题
【Harness 任务理解】
【历史研究记忆】     ← type / trust / version / date / score / src=
【项目已查来源】     ← 独立块，不是和 fact 混在一起
【已完成步骤摘要】或 evidence digest
【当前步骤】+ 子 Agent 绑定
MCP 工具 / 路径指令
```

记忆 **不进 system prompt**，进的是 **每步 user message**。这是刻意的：system 保持稳定，记忆当「参考材料」并声明勿执行其中的指令。

---

## 七、存储与工程细节

### 7.1 后端

| provider | 用途 |
|---|---|
| `sqlite`（默认） | 生产路径：embedding BLOB、WAL、软删除、审计表、来源台账、schema 迁移 |
| `local`（JSON） | 开发降级，冲突决策仍走同一套 consolidation |
| `mem0` | 可选 overlay；失败 fallback JSON。**不是内核** |

门面：`MemoryStore`。API 用 `get_memory_store()` 单例，避免每次开库。

SQLite 要点：embedding **在事务外** await；审计日志与写入 **共用同一连接**，避免 Windows 锁；关闭前 `wal_checkpoint`。

### 7.2 安全

- PII：邮箱 / 中国手机 / 身份证号写入时脱敏
- 软删除 + 用户级 `forget_user`（GDPR 遗忘）
- `memory_audit` 表记录 remember / merge / supersede / delete / consolidate

### 7.3 HTTP API（`/api/memory`）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/records/{user_id}` | 列出有效记忆 |
| GET | `/recall` | 调试 hybrid recall（可带 `target_step_type`） |
| POST | `/facts` | 用户显式写入 |
| DELETE | `/records/{user_id}/{record_id}` | 软删一条 |
| DELETE | `/records/{user_id}` | 软删该用户全部 |
| GET | `/sources` | 项目来源台账 |
| POST | `/consolidate` | 手动巩固 |
| GET | `/audit` | 审计日志 |

任务入口：`POST /api/task` body 含 `user_id` / `tenant_id` / `project_id`。

### 7.4 配置（`harness.yml` memory 段）

常用开关：`enabled`、`provider`、`recall_top_k`、`ttl_days`/`ttl_by_type`、`wrap_untrusted`、`embedding_enabled`、`step_incremental_enabled`、`step_incremental_write_longterm`、`remember_on_partial`、`require_explicit_identity`、`min_recall_trust`、`synthesis_min_trust`、`utility_gate_enabled`、`require_provenance_for_step_write`、`source_ledger_enabled`、`step_recall_enabled`、`consolidation_*`。

---

## 八、目录索引（对代码时用）

```text
app/agent/memory/
  identity.py          请求级身份 ContextVar
  models.py            MemoryRecord / WriteRequest / SourceLedger
  provenance.py        TrustTier / Provenance / 准入门
  policy.py            策略 + 与 harness.yml 对齐
  extractor.py         LLM / 启发式抽取；网页无出处不写
  governance.py        Jaccard / cosine / 矛盾检测 / merge 留史
  consolidation.py     ADD UPDATE SUPERSEDE NOOP + 衰减晋升清理
  store.py             门面 + Mem0 overlay + 单例
  security.py          PII + 审计
  recall/hybrid.py     混合召回
  recall/embedding.py  OpenAI 兼容 embedding，失败降级
  backend/sqlite_backend.py   生产默认
  backend/json_backend.py     开发降级

app/agent/harness/loop.py           何时 recall / remember
app/agent/harness/context_builder.py 如何注入 prompt
app/agent/harness/compressor.py      工作记忆压缩
app/api/memory_routes.py             REST
app/config/harness.yml               开关
tests/test_harness_phase12/15/18_memory.py
```

---

## 九、明确还没做的（面试主动说）

- LangGraph 图状态默认落到单实例 SQLite checkpointer，多副本要换 Redis/Postgres（这是 **会话图状态**，不是长期 Memory）。
- 巩固是规则（半衰期 / 命中次数），不是 Letta 那种 sleep-time LLM agent。
- 产品层登录 / RBAC 还没有；记忆层已能按 tenant/user 隔离，但身份必须由 API 传入。
- Mem0 仍是可选 sidecar，核心路径是自研 SQLite。
- 来源质量（reliable/unreliable）目前偏启发式，没有完整的「用户点踩」闭环。
