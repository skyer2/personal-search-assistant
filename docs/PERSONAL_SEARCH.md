# Personal Search Assistant — 产品架构与模块拆分

> **定位**：本仓库产品目标从「企业多数据源研搜 Demo」转向「个人 Search Assistant；复杂问题按需升级 Deep Research」。  
> **原则**：底层 Harness / StateGraph **保留**；真正要动的是入口分流、默认能力面、身份/对话、Progress 通用化与 UI。  
> **状态**：架构设计文档（2026-08-31）。**P0 / P1 已落地**；Intent/Plan 方案见 [INTENT_AND_PLAN.md](INTENT_AND_PLAN.md)。

---

## 0. 一句话与两层模式

**一句话：** 做一个能日常用的个人搜索助手；Deep Research 是按需启用的重型执行引擎，不是每条消息的唯一工作流。

```text
SearchMode（新，产品入口）     决定：要不要进 Deep Research
PlanningMode（已有）            决定：进 Deep 之后怎么规划
  DIRECT / TEMPLATE / DYNAMIC
```

| 层 | 目标 | 明确不是 |
|----|------|----------|
| **产品** | 平时像 ChatGPT 搜索一样快；复杂、多实体、要对照/修订/多资料时自动升 Deep | 不是每个问题都跑完整研搜；不是默认出 Markdown/PDF |
| **架构** | 在现有 Runtime **前面**加 Search Mode Router；Quick / Deep 共用 Evidence / Artifact / Citation / Finalize | 不是推翻 StateGraph、Planner、Progress、Checkpoint |
| **能力面** | 个人默认 Web + URL + File；DB / RAGFlow / MCP / HITL 变成可选插件 | 不是删企业能力；面试时仍可打开 Developer Mode 展示 |

---

## 1. 整体架构

### 1.1 分层总览

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 5  体验层                                                          │
│  Search · History · Projects · Saved Sources                             │
│  Auto/Quick/Deep 模式切换 · Answer+Sources 默认 · Developer Mode 折叠    │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ POST /api/task  (mode, project_id, thread_id)
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 4  服务层                                                          │
│  FastAPI · WebSocket stream · Conversation Store · Identity 门禁         │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 3a 产品路由（新增）                                                 │
│  Mode Router: conversation_context → understand → quick | deep           │
│  SearchMode 预算 · 交付物策略 · 来源白名单（personal defaults）            │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
┌──────────────────────────┐        ┌──────────────────────────────────────┐
│ Quick Path（新增子图）     │        │ Deep Path（现有主图，语义收紧）        │
│ search → fetch_url?      │        │ Brief → Plan → DAG → Send Workers    │
│ → Evidence → 轻量综合    │        │ → Progress → Replan → Synthesis      │
│ 无 Progress / 无 Replan  │        │ PlanningMode: DIRECT/TEMPLATE/DYNAMIC│
└────────────┬─────────────┘        └──────────────────┬───────────────────┘
             │                                          │
             └──────────────────┬───────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 3b Domain Harness（保留）                                          │
│  Artifact/Evidence Store · Context Virtualization · Citation · Memory    │
│  Tool Gateway · Checkpoint/Trace · Source Ledger · Eval（Developer）    │
└───────────────────────────────┬─────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 2  Research StateGraph Runtime（保留，扩展 Quick 分支）             │
│  LangGraph · Send · interrupt · SQLite checkpointer                      │
└───────────────────────────────┬─────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 1  工具层                                                          │
│  search_web（卡片）· fetch_url · read_file · read_artifact/evidence      │
│  [可选] DB / KB / MCP Gateway / generate_markdown / convert_pdf          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Quick vs Deep 执行流

```text
                         User Message
                              │
                              ▼
                   ┌──────────────────────┐
                   │  Conversation Store   │  L0：最近 4～8 turn + summary
                   └──────────┬───────────┘
                              ▼
                   ┌──────────────────────┐
                   │  Mode Router            │
                   │  Auto / Quick / Deep    │
                   └──────────┬───────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼                                   ▼
     ┌─────────────┐                    ┌─────────────────┐
     │ QUICK        │                    │ DEEP             │
     │ ≤2 searches  │                    │ ≤5 objectives    │
     │ no replan    │                    │ ≤2 replan        │
     │ no parallel  │                    │ parallel Send    │
     │ chat answer  │                    │ Progress Eval    │
     └──────┬──────┘                    └────────┬─────────┘
            │                                    │
            ▼                                    ▼
     search_web (cards)                   Research Brief
            │                                    │
            ▼                                    ▼
     worker picks URLs                   Planning Policy
            │                           Direct/Template/Dynamic
            ▼                                    │
     fetch_url → Artifact                         ▼
            │                              Objective DAG
            ▼                                    │
     Evidence Span                              ▼
            │                              Send Workers
            ▼                                    │
     light synthesis                             ▼
            │                              Progress Evaluator
            └──────────────┬─────────────────────┘
                           ▼
                    Answer + Sources
              (chat 默认；md/pdf 仅显式请求)
```

### 1.3 五层上下文（必须分清）

| 层 | 名称 | 内容 | 生命周期 | 现有模块 |
|----|------|------|----------|----------|
| **L0** | Conversation | 刚才聊了什么 | thread 内 4～8 turn + rolling summary | **新增** `conversation/store.py` |
| **L1** | Research Brief | 这次要查什么（depth / freshness / primary） | 单次 run | `research_brief.py` |
| **L2** | Working Context | 当前 Worker 窗口（JIT 短卡 + ref） | 单步 | `context_builder.py` / `context_selector.py` |
| **L3** | Evidence / Artifact | 事实原文（lossy context, lossless storage） | run + 持久化 | `evidence_store.py` / `artifacts.py` |
| **L4** | Long-term Memory | 偏好、项目结论、Source Ledger | 跨 session | `memory/` |

**硬规则：**

- Conversation ≠ Memory
- Web 检索默认 **不**写入长期 Memory（`step_incremental_write_longterm: false` 保持）
- 值得记住的才进 Memory Candidate，必须带 `as_of` + source
- Source Ledger 按 `source_freshness_days` 决定复用还是重抓

---

## 2. 核心功能

### 2.1 用户侧

| # | 功能 | 说明 |
|---|------|------|
| 1 | **Auto / Quick / Deep** | 默认 Auto；简单事实走 Quick（1～2 次搜索）；比较、修订、多资料、官方对照走 Deep |
| 2 | **对话式回答为默认交付** | 直接出 Answer + Sources；只有用户明确说「生成 Markdown / PDF」才写文件 |
| 3 | **多轮 Conversation** | 「创业板呢？」能接上上一轮主题；会话状态，不是长期 Memory |
| 4 | **Project 工作区** | Inbox / C++ / Agent / KRX 等；记忆和来源台账按项目隔离 |
| 5 | **结果页分层** | 默认 Answer + Sources；展开 Research details 才见 Plan / Workers / Progress / Replan / Evidence |
| 6 | **Developer Mode** | Eval / Trace / Token / Cost 从主界面挪走，需要时再开 |

### 2.2 系统侧（保留的 Harness）

| # | 组件 | 职责 |
|---|------|------|
| 1 | **Mode Router** | `conversation_context → understand → quick \| deep` |
| 2 | **Quick 路径** | search → fetch_url → Evidence → 轻量综合 |
| 3 | **Deep 路径** | 现有 Brief → Plan → DAG → Send Workers → Progress → Replan → Synthesis |
| 4 | **Context Virtualization** | Artifact / Evidence / JIT / 压缩 / token 预算（不动） |
| 5 | **Source Ledger** | 查过的官方源可按 freshness 复用 |
| 6 | **Checkpoint / Trace** | Deep 路径继续可恢复、可审计 |

### 2.3 默认关闭、代码保留

| 能力 | 个人默认 | 启用方式 |
|------|----------|----------|
| MySQL / DB Worker | 关 | `enabled_sources.db=true` |
| RAGFlow / KB Worker | 关 | `enabled_sources.kb=true` |
| MCP Gateway | 关 | `mcp.enabled=true` |
| HITL 计划审批 | 关 | 仅高风险副作用 |
| Memory consolidation | 关 | 记忆变多再开 |
| 导出 Markdown/PDF | 关 | 用户明确要求才挂工具 |

---

## 3. 模块拆分

### 3.1 模块关系图

```text
                    ┌─────────────────┐
                    │   frontend/      │
                    │ App · Composer   │
                    │ History/Projects │
                    └────────┬────────┘
                             │ mode / project_id / thread_id / user_id=me
                             ▼
                    ┌─────────────────┐
                    │   app/api/       │
                    │ server.py        │
                    │ harness_routes   │
                    └────────┬────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ conversation/   │  │ research/       │  │ agent/harness/  │
│ store.py (新)   │  │ routing/ (新)   │  │ planner         │
│ thread/turns    │  │ mode_router.py  │  │ intent_slots    │
│ rolling summary │  │                 │  │ research_brief  │
└────────┬───────┘  └────────┬───────┘  │ worker_profiles │
         │                   │          │ context_builder │
         │                   ▼          └────────┬───────┘
         │          ┌────────────────┐           │
         └─────────►│ runtime/        │◄──────────┘
                    │ graph.py        │
                    │  ├─ quick_subgraph (新)
                    │  └─ deep_subgraph (现有)
                    │ runner.py · state.py
                    └────────┬───────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ planning/       │  │ workers/        │  │ tools/          │
│ policy.py       │  │ registry.py     │  │ tavily/search   │
│ progress.py     │  │ factory.py      │  │ fetch_url (新)  │
│ lead_planner    │  │                 │  │ artifact_tools  │
└────────────────┘  └────────────────┘  └────────────────┘
```

### 3.2 文件映射（保留 / 改语义 / 新增）

```text
deepsearch-agents/
├── app/
│   ├── api/
│   │   ├── server.py                 # 改：Request 增加 mode / project_id / search policy
│   │   ├── harness_routes.py         # 改：capabilities 暴露 personal defaults
│   │   └── eval_routes.py / trace_*  # 保留：Developer Mode 入口
│   ├── agent/
│   │   ├── prompt/prompts.yml        # 改 P0：去金融/电商/DB 默认语义
│   │   ├── harness/
│   │   │   ├── loop.py               # 保留：生产仍走 graph runtime
│   │   │   ├── planner.py            # 改：understand 默认 chat；接 Conversation
│   │   │   ├── intent_slots.py       # 改 P0：depth / freshness / source preference
│   │   │   ├── research_brief.py     # 改 P1：depth / freshness / prefer_primary
│   │   │   ├── worker_profiles.py    # 改 P0：Web / Document / Research / Synthesis
│   │   │   ├── artifacts.py          # 保留
│   │   │   ├── evidence_store.py     # 保留
│   │   │   ├── context_selector.py   # 改 P2：中文 2-gram / hybrid
│   │   │   ├── context_builder.py    # 改：注入 ConversationContext
│   │   │   └── citations.py          # 保留
│   │   └── memory/                   # 保留代码；个人默认简化
│   ├── research/
│   │   ├── routing/
│   │   │   └── mode_router.py        # 新增 P1：Auto 判定 Quick / Deep
│   │   ├── runtime/
│   │   │   ├── graph.py              # 改 P1：conversation + mode_router + QUICK 分支
│   │   │   ├── runner.py             # 改：按 SearchMode 套 budget / HITL
│   │   │   ├── state.py              # 改：SearchMode、conversation 摘要
│   │   │   ├── scheduler.py          # 保留 Deep 路径
│   │   │   ├── isolation.py          # 保留
│   │   │   └── checkpointer.py       # 保留
│   │   ├── planning/
│   │   │   ├── policy.py             # 改 P0：默认 web/file；official/domain/time
│   │   │   ├── compose.py            # 保留：仅 Deep 使用
│   │   │   ├── lead_planner.py       # 保留：仍按 objective 拆
│   │   │   ├── progress.py           # 改 P1：Brief-driven，去掉商业 hardcode
│   │   │   └── plan_patch.py         # 保留：GAP → 补任务
│   │   └── workers/registry.py       # 改 P0：DB/KB 条件注册
│   ├── tools/
│   │   ├── tavily / internet_search  # 改 P1：search_web 卡片化 + domain/recency
│   │   ├── artifact_tools.py         # 保留
│   │   └── fetch_url.py              # 新增 P1
│   ├── conversation/
│   │   └── store.py                  # 新增 P1：thread / turns / rolling summary
│   ├── mcp/                          # 保留，个人默认 enabled=false
│   └── config/harness.yml            # 改 P0：personal_search budgets；关 HITL/DB/KB/MCP
├── frontend/src/
│   ├── App.tsx                       # 改 P0：History / Projects，去 Demo sidebar
│   ├── components/ChatComposer.tsx   # 改 P0：Auto / Quick / Deep
│   ├── lib/api.ts                    # 改 P0：固定 user_id=me + project_id
│   └── components/EvalPanel.tsx 等   # 挪到 Developer Mode
└── docs/
    ├── RESEARCH_HARNESS.md           # 保留 Harness 权威
    ├── RESEARCH_INTELLIGENCE.md      # 保留 Deep 闭环
    └── PERSONAL_SEARCH.md            # 本文：产品语义 + Mode Router + 五层上下文
```

### 3.3 新增模块职责

#### `app/research/routing/mode_router.py`

```python
# 伪接口 — 设计意图，非现行代码
class SearchMode(str, Enum):
    AUTO = "auto"
    QUICK = "quick"
    DEEP = "deep"

@dataclass
class RouteDecision:
    mode: Literal["quick", "deep"]
    confidence: float
    signals: list[str]          # 可解释：为何升/降
    user_override: bool         # 用户显式选 Quick/Deep

def route(
    query: str,
    user_mode: SearchMode,
    conversation_summary: str,
    attachments: list[str],
) -> RouteDecision: ...
```

**分流信号：**

| 信号 | 倾向 |
|------|------|
| 单实体、事实查询、定义、股票代码、休市、API 是什么 | **Quick** |
| 比较、修订差异、多文档综合、要官方对照、多维度、上传多个 PDF | **Deep** |
| 显式「生成报告 / Markdown / PDF」 | Deep 或至少走导出工具，但 **不**因此默认每次都出文件 |

#### `app/conversation/store.py`

```python
# 伪接口
@dataclass
class ConversationTurn:
    role: Literal["user", "assistant"]
    content: str
    run_id: str | None
    sources: list[str]          # citation refs
    timestamp: str

@dataclass
class ConversationThread:
    thread_id: str
    project_id: str
    user_id: str                # 固定 "me"
    turns: list[ConversationTurn]   # 最近 4～8
    rolling_summary: str        # 超窗时压缩

def append_turn(thread_id, turn) -> ConversationThread: ...
def get_context(thread_id, max_turns=8) -> tuple[list[ConversationTurn], str]: ...
```

#### `app/tools/fetch_url.py`

搜索卡片之后按需拉正文进 Artifact：

```text
search_web → 只返回卡片（title/url/snippet/score）
  → 工人决定深挖
  → fetch_url → Artifact
  → Evidence Span → Finding
  → 需要时 read_evidence / read_artifact
```

### 3.4 明确不拆、不重写

以下模块 **保留实现**，仅调整调用边界或默认配置：

- `graph` 的 Deep 子图（intent → plan → dispatch → Send → progress → synthesis）
- Artifact / Evidence Store 与 JIT Context
- Citation 管线
- Checkpoint / Trace / Eval
- Source Ledger
- MCP Gateway（默认关）
- PlanPatch / Lead Planner / Plan Validator

---

## 4. 关键业务规则

### 4.1 分流规则（SearchMode ≠ PlanningMode）

```text
用户显式 Quick / Deep  → 尊重用户
用户 Auto             → Router 判定
```

**Quick 约束：**

| 参数 | 值 |
|------|-----|
| max_search_queries | ≤ 2 |
| max_replan | 0 |
| parallel | 否 |
| progress_eval | 否 |

**Deep 约束：**

| 参数 | 值 |
|------|-----|
| max_research_tasks | ≈ 5 |
| max_replan | ≈ 2 |
| parallel | 是（Send fan-out） |
| progress_eval | 是 |

### 4.2 交付物规则（相对现状要反过来）

**现状问题**（`planner_llm.py` / `intent_slots.py`）：

- 「列出 N 条 + 要来源/链接」→ 自动 `deliverable=md`
- `DIRECT` 仍会走进完整研究链

**目标规则：**

```text
默认 deliverable = chat（直接回答 + Sources）
仅当用户明确说「生成 Markdown / PDF / 报告」才 md/pdf
「列出 N 条 + 要来源」不再自动变 file_md
```

| 用户表达 | deliverable | 工具 |
|----------|-------------|------|
| 普通提问 | `chat` / `text` | 无文件工具 |
| 「附上来源/链接」 | `chat` + citations | 无文件工具 |
| 「生成 Markdown 报告」 | `md` | `generate_markdown` |
| 「导出 PDF」 | `pdf` | `generate_markdown` → `convert_pdf` |

### 4.3 来源与工具规则

**个人默认允许：** `web`、`file`（含 URL / 上传）

**个人默认禁止：** `db`、`kb`、MCP

**官方优先：** `prefer_primary=true` 时限制/加权 `preferred_domains`

搜索次数由 **信息充分度** 控制，禁止 prompt 写死「至少搜 3 个角度」。

### 4.4 Progress 规则（Deep only，且必须通用）

**现状问题**（`progress.py`）：

- `_COMMERCIAL_DIM` 硬编码「收入 / 营收 / 量产 / 商业化」
- 与 Brief 维度未完全对齐

**目标规则：**

```text
coverage = Brief.dimensions ∪ Brief.success_criteria
对照 Worker findings
missing / conflict / low_confidence / stale → GAP → PlanPatch
全部覆盖且无冲突 → ENOUGH → Synthesis
```

Quick **不**进 Progress Evaluator。

### 4.5 HITL 规则

| 模式 | HITL |
|------|------|
| Quick | 永不 |
| Deep | 默认自动跑完 |
| 任何模式 | 仅文件覆盖、外部副作用、高风险工具才审批 |
| 搜索 / 读网页 / 总结 | 不审批 |

### 4.6 身份规则（个人版最小集）

```text
tenant_id  = "local"
user_id    = "me"          # 前端必须显式发送，禁止再退化成 session_id
project_id = Inbox|C++|Agent|KRX|...
```

`require_explicit_identity=true` 仍然成立，靠固定个人身份满足，而不是关掉门禁。

**个人 Memory 只强调三类：**

1. Preference（偏好）
2. Project Research Memory（项目研究记忆）
3. Source Ledger（来源台账）

Episodic 次要；网页 Fact 尽量不进长期。Consolidation 默认关。

### 4.7 UI 规则

| 用户看到 | 用户看不到（Developer Mode） |
|----------|------------------------------|
| Search / History / Projects / Saved Sources | 网络搜索助手 / 数据库助手 / RAGFlow 助手 |
| Answer + Sources（默认） | Eval / Trace / Token / Cost（默认） |
| Research details（折叠） | Agent 拓扑 Demo sidebar |

### 4.8 预算规则（按模式，不再一套全局）

| | Quick | Deep |
|--|-------|------|
| max_tool_calls | 3 | 15 |
| max_search / research | 2 queries | 5 objectives |
| max_replan | 0 | 2 |
| parallel | 否 | 是 |
| progress_eval | 否 | 是 |
| 压缩 | 否 | 是 |

**配置落点：** `harness.yml` 新增 `personal_search.quick` / `personal_search.deep` 段，runner 按 SearchMode 选用。

---

## 5. 与现状对照

### 5.1 已有 vs 缺失

| 已有（Deep Research Runtime） | 缺失（产品外壳） |
|-------------------------------|------------------|
| Plan / Evidence / Progress / Replan / Checkpoint | Search Mode Router |
| Hybrid Planning DIRECT/TEMPLATE/DYNAMIC | Quick 子图 |
| Artifact/Evidence + JIT Context | Conversation Store |
| Source Ledger + Memory 门禁 | 个人身份 + Project 工作区 |
| MCP / DB / KB 全能力 | 默认 chat 交付 |
| Eval / Trace / HITL 全开 | 通用 Brief-driven Progress |
| Demo 控制台 UI（Agents 拓扑） | 个人 Search UI |

### 5.2 关键代码现状 → 目标

| 模块 | 现状 | 目标 |
|------|------|------|
| `planner.py` | 金融/电商/DB 关键词默认触发多源 | 默认 web+file；chat 交付 |
| `intent_slots.py` | `require_citations` → 倾向 file_md | citations 在 chat 内嵌 |
| `policy.py` | 四源全开 | 个人默认 web/file |
| `worker_profiles.py` | web/db/kb/file/mixed 五 Profile | web/document/research/synthesis；db/kb 条件注册 |
| `progress.py` | 商业维度 hardcode | Brief.dimensions 驱动 |
| `graph.py` | 所有请求走 Deep 链 | mode_router 前置 Quick 分支 |
| `harness.yml` | 全局 budget + HITL/DB/KB 开 | personal_search 分段配置 |
| `App.tsx` | Agents 拓扑 + Eval 主界面 | History/Projects + Developer Mode |

---

## 6. 实施优先级

### P0 — 产品语义与默认面（不动 graph 结构）

| 任务 | 文件 |
|------|------|
| Intent 默认 chat；去金融/DB 默认语义 | `planner.py`, `intent_slots.py`, `prompts.yml` |
| 来源策略默认 web/file | `policy.py`, `harness.yml` |
| Worker 条件注册 DB/KB | `workers/registry.py`, `worker_profiles.py` |
| personal_search 预算段 | `harness.yml`, `runner.py` |
| 前端 mode 切换 + 固定 identity | `ChatComposer.tsx`, `api.ts`, `App.tsx` |
| capabilities 暴露 personal defaults | `harness_routes.py` |

### P1 — 路由与 Quick 路径

| 任务 | 文件 | 状态 |
|------|------|------|
| Mode Router | `research/routing/mode_router.py` | **已落地** |
| Quick 子图 | `runtime/graph.py`, `runtime/state.py`, `runtime/quick.py` | **已落地** |
| Conversation Store | `conversation/store.py` | **已落地** |
| fetch_url 工具 | `tools/fetch_url.py` | **已落地** |
| search_web 卡片化 | `tools/tavily_tool.py` | **已落地** |
| API mode/thread/project | `api/server.py`（P0 已接 mode；thread=session_id） | **已落地** |
| Brief depth/freshness/primary | `research_brief.py` | **已落地**（见 [INTENT_AND_PLAN.md](INTENT_AND_PLAN.md)） |
| Brief-driven Progress | `planning/progress.py` | **已落地** |

### P2 — 体验 polish

| 任务 | 文件 |
|------|------|
| 中文 2-gram hybrid context selector | `context_selector.py` |
| History / Projects / Saved Sources UI | `frontend/` |
| Developer Mode 折叠 Eval/Trace | `App.tsx`, `EvalPanel.tsx` |
| Research details 折叠层 | 新组件 |
| `PERSONAL_SEARCH.md` 与 golden eval 对齐 | `tests/eval/` |

---

## 7. API 契约（设计）

### 7.1 请求

```json
{
  "query": "创业板今天为什么跌？",
  "mode": "auto",
  "thread_id": "thr_abc123",
  "project_id": "Inbox",
  "user_id": "me",
  "tenant_id": "local",
  "attachments": []
}
```

### 7.2 响应（stream 事件摘要）

```json
{
  "mode_resolved": "quick",
  "answer": "...",
  "sources": [{"title": "...", "url": "...", "evidence_id": "E12"}],
  "research_details": null
}
```

Deep 模式时 `research_details` 含 plan / progress / workers（默认折叠）。

### 7.3 capabilities（personal defaults）

```json
{
  "search_modes": ["auto", "quick", "deep"],
  "enabled_sources": {"web": true, "file": true, "db": false, "kb": false},
  "default_deliverable": "chat",
  "developer_mode": false,
  "projects": ["Inbox", "C++", "Agent", "KRX"]
}
```

---

## 8. 文档地图

| 文档 | 职责 |
|------|------|
| **PERSONAL_SEARCH.md（本文）** | 产品目标、Mode Router、Quick/Deep 分流、五层上下文、模块拆分、业务规则 |
| [RESEARCH_HARNESS.md](./RESEARCH_HARNESS.md) | Harness 决策边界、谁是 Agent、LangGraph 权威 |
| [RESEARCH_INTELLIGENCE.md](./RESEARCH_INTELLIGENCE.md) | Deep 路径 Progress / Replan / 真并行 |
| [HARNESS_ARCHITECTURE.md](./HARNESS_ARCHITECTURE.md) | 运行时五层、一次任务怎么跑 |
| [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md) | Artifact/Evidence/JIT/token 预算 |
| [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md) | 身份四元组、Source Ledger、信任分级 |

---

## 9. 面试叙事（更新版）

> 在 Deep Research Harness 之上，我加了 **Search Mode Router**：日常问题走 Quick（≤2 次搜索、对话交付），复杂问题才升 Deep Research（现有 StateGraph + Progress + Replan）。底层 Evidence / Artifact / Citation / Checkpoint **全部复用**；DB/KB/MCP 变成可插拔能力，个人默认只开 Web+File。Conversation 与 Memory 分层：会话上下文管多轮追问，长期记忆只管偏好和项目结论。
