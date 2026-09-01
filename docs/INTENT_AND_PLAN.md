# Intent 与 Plan

> **权威范围**：[ARCHITECTURE.md](ARCHITECTURE.md)。全部任务走 Harness（Brief → Plan → Progress）。  
> `direct` 仅对照实验，不走 Intent/Plan。Search 不是产品路径。

---

## 0. 为何要改

旧 Intent 泄漏实现细节（`needs_network` / `needs_file_read` / `deliverable`），Plan 按数据源排成流水线，Progress 用「营收/量产」等商业词硬编码判缺口。这适合企业研报 Demo，不适合个人搜索助手。

个人场景要：

- 默认 **对话回答 + 来源**，只有用户明确要文件才写 md/pdf
- 来源只有 **web + file**
- 比较题按 **实体/维度** 拆 DAG，不按「先搜网再读库」拆
- Progress 对照 **这份 Brief**，而不是写死的商业指标
- 概念题在 Task Router 就 **ANSWER** 结束；事实题走 **SEARCH**，不烧规划

---

## 1. 三层，不要混

```text
① Task Router           ANSWER / SEARCH / RESEARCH（见 ARCHITECTURE）
② Intent = Research Brief   用户要什么、怎样算够     ← 仅 RESEARCH
③ Plan = 目标 DAG           先做哪些任务、依赖、合成
     └─ 执行中 Progress 对照 Brief → enough / gap / replan
```

| 层 | 输入 | 输出 | 谁消费 |
|----|------|------|--------|
| Route | 用户 mode + 问句 | `answer` / `search` / `research` | 图入口 |
| Intent | 改写后的 query + 附件 + 禁令 | `TaskIntent`（内嵌 Brief） | Plan、Context、Progress、写稿 |
| Plan | Intent | `ExecutionPlan` | dispatch / 工人 / Replan |

**ANSWER / SEARCH 跳过 ②③。** 用户显式三档优先于 Auto。旧 API `quick`/`deep` 仍映射到 search/research。

---

## 2. Intent：Brief 是合同，来源标志是派生

### 2.1 权威对象：`ResearchBrief`

| 字段 | 含义 | 个人默认 |
|------|------|----------|
| `objective` | 一句话目标 | 改写后的问句 / 规则摘要 |
| `entities` | 比较或关注对象 | 比较题抽出；否则可空 |
| `dimensions` | 要覆盖的评价维度 | 从问句抽；没有则「关键事实」 |
| `time_range` | 年份或区间 | 问句中的 20xx |
| `freshness` | `any` / `recent` / 约束串 | 出现「最新/今天」→ `recent` |
| `depth` | `shallow` / `standard` / `thorough` | Deep 默认 `standard`；比较/官方/报告 → `thorough` |
| `prefer_primary` | 是否优先官方/一手 | 出现「官方/白皮书/官网/一手」才 true |
| `preferred_domains` | 提示域名（可选） | 空 |
| `source_policy` | `web` / `file` / `web+file` | 由禁令 + 附件派生 |
| `deliverable` | `text` / `md` / `pdf` | **text**；明确报告才升 |
| `constraints` | 禁止来源、排除旧资料等 | 规则填 |
| `success_criteria` | 怎样算做完 | 可追溯引用；冲突数字并列 |

`TaskIntent.needs_network` / `needs_file_read` **保留**，供旧校验与工人绑定，但必须从 Brief + SourcePolicy **派生**，不再当产品语义。

### 2.2 怎么抽出（分点）

1. **会话改写先于理解**  
   Conversation Store 把「创业板呢？」拼成带上一轮主题的 `resolved_query`。Intent 只看改写后的句子。

2. **规则打底（不调模型）**  
   - 槽位：条数、年份、要不要来源、topic  
   - 交付物：PDF 关键词 → `pdf`；Markdown → `md`；「生成+报告」→ `md`；其余 `text`  
   - 「列出 N 条 + 来源」**仍是 text**  
   - 来源禁令：「不要联网」关 web；「不要读附件」关 file  
   - 无来源时默认 web（个人助手默认上网）  
   - 比较标记 / `A / B / C` → entities  
   - 维度词：比较、商业化、技术、竞争、风险、监管…  
   - 深度 / 新鲜度 / 官方优先：见上表

3. **LLM 补丁（可关、可失败回退）**  
   压缩模型输出 JSON，覆盖 `deliverable`、来源开关、Brief 字段。`confidence < 0.5` 或解析失败 → 纯规则。Prompt 写死：默认 chat，禁止把「带来源」理解成必须出文件。

4. **编译 Brief 并挂到 Intent**  
   `understand_task` / `understand_intent` 结束时调用 `compile_research_brief`，写入 `intent.brief`。后续 Context / Progress **优先用这份**，不要从原始对话重猜。

5. **澄清（个人默认不打断）**  
   HITL 关；`clarification_auto_resolve=true`。歧义时强制 `deliverable=text`，Brief 用已抽出的实体继续。不在个人路径弹「请选择 Markdown 还是对话」。

6. **用户覆盖**  
   显式 Deep 仍走 Intent；显式只要对话 / 不要文件 → Brief.deliverable 保持 text。

### 2.3 不做的

- 不上 100 类 NLU taxonomy  
- Brief 不写死工具名（不出现 `internet_search`）  
- 不在 Intent 阶段决定停不停、派哪个工人

---

## 3. Plan：按 Brief 选模式，按目标拆 DAG

### 3.1 PlanningMode（≠ SearchMode）

| 模式 | 何时 | 计划形状 |
|------|------|----------|
| **DIRECT** | 单一来源（只网或只文件），且不是比较 | `检索 1 步 → 合成 1 步` |
| **TEMPLATE** | 网+文件都要，且不是比较 | 两步检索再合成 |
| **DYNAMIC** | Brief 有 ≥2 实体，或问句是比较/多维度，或 `depth=thorough` 且多维度 | 按 **实体（+维度）** 拆 research 任务，系统再追加合成 |

合成步由 **deliverable** 决定，工人不选：

- `text` → `summarize`（对话）
- `md` → `generate_markdown`
- `pdf` → `generate_markdown` → `convert_pdf`

### 3.2 怎么生成（分点）

1. **`select_planning_mode(intent)`**  
   先看 Brief.entities / 比较标记；再看来源个数。禁止「有网就 DYNAMIC」。

2. **DIRECT / TEMPLATE**  
   仍用 `build_plan()` 骨架（按是否需要 file/web 排队）。适合「搜一下 XX」。给每步带上 `allowed_tools`（web=`internet_search`+`fetch_url`）。把 `plan.research_brief` 写成 Brief.objective，供工人 prompt。

3. **DYNAMIC**  
   - 有 LLM 且 `dynamic_lead_planner=true`：Lead Planner **只输出** `{research_brief, tasks[{task_id, objective, depends_on, allowed_sources}]}`。禁止工具、禁止写报告步、禁止 `db`/`kb`。  
   - 失败或校验不过：启发式 — 每个 entity 一个并行 `research` 步，再一个 `t_compare`（depends_on 全部实体），objective 带上 Brief.dimensions；`prefer_primary` 时 objective 追加「优先官方/一手」。  
   - 系统 `append_synthesis`，合成步 depends_on 所有研究任务。

4. **校验（机器，不靠模型自觉）**  
   空计划、环、超步数、超 `max_research_tasks`、缺依赖、禁用来源、缺交付合成步 → 拒绝并降级 TEMPLATE。

5. **finalize**  
   打 `task_id` / `depends_on` / 并行检索组。无依赖的 research 可 fan-out。

6. **HITL 计划审批默认关**  
   个人助手不弹「请改计划」。高风险以后再开。

7. **Replan**  
   Progress=`gap` 且未耗尽 `max_replan` 时，PlanPatch **只加 research 任务**，来源必须落在 policy.allowed（web/file），条数 ≤ `max_plan_patch_tasks`。

### 3.3 Planner 无运行时权力

Lead Planner 不能 `Send` 工人、不能停图。工人是叶子：research 只搜/读；synthesis 只写，禁止再搜。

---

## 4. Progress：对照 Brief，去掉商业 hardcode

`assess_progress` 在检索 READY 清零之后：

1. 失败 / 空结果 / 工人自报 gaps → `coverage_gaps`  
2. 同指标数字冲突 → `conflicts`  
3. **过时**：Brief.freshness=`recent` 或 time_range 有目标年，证据最大年份 ≤ 目标年-2 → `stale_evidence`  
4. **缺维度**：DYNAMIC 下，叶子任务正文未覆盖 Brief.dimensions（不再写死「营收/量产」）→ `missing_dimensions`  
5. **缺一手**：`prefer_primary` 且已有 URL 但没有任何官方/一手线索 → `coverage_gaps`  
6. DIRECT/TEMPLATE：没有失败/冲突时，**不**因启发式维度再搜（避免简单题被拖进 Deep 循环）

`enough` 才进合成；`gap` 才 Replan。

---

## 5. 端到端例子

**A. Quick（不进本方案）**  
「今天纳斯达克休市了吗」→ Router=quick → 卡片 + fetch_url → Answer+Sources。

**B. DIRECT chat**  
「2026 年 AI 电商趋势有哪些？附来源」  
- Brief: deliverable=text, depth=standard, entities≈空, source=web  
- Plan: `network_search → summarize`

**C. DYNAMIC 比较**  
「比较 Tesla / Figure / Unitree 2026 商业化进度」  
- Brief: entities 三个, dimensions 含横向比较+商业化, time=2026, depth=thorough  
- Plan: `t_entity_1 ∥ t_entity_2 ∥ t_entity_3 → t_compare → summarize`  
- 某实体只有 2024 数据 → stale；某实体完全没有商业化线索 → missing_dimensions → gap → 最多补 2 个 research

**D. 显式报告**  
「搜索 Tesla 2026 动态，生成 Markdown 报告」  
- deliverable=md, Plan 末步 `generate_markdown`  
- 比较+报告 → DYNAMIC + md 合成

**E. 禁网**  
「不要联网，只读我上传的文件」  
- Brief.source_policy=file, needs_network=false  
- Plan 无 `network_search`；PlanPatch 也加不进 `internet_search`

---

## 6. 文件落点

| 文件 | 职责 |
|------|------|
| `app/agent/harness/research_brief.py` | Brief  schema、规则编译（depth/freshness/primary） |
| `app/agent/harness/state.py` | `TaskIntent.brief` |
| `app/agent/harness/planner.py` | 规则 Intent + 挂 Brief |
| `app/agent/harness/planner_llm.py` | LLM 补丁含 Brief 字段 |
| `app/agent/harness/intent_slots.py` | 槽位 / 默认 chat |
| `app/research/planning/policy.py` | 来源禁令 + 按 Brief 选 PlanningMode |
| `app/research/planning/compose.py` | 组装并盖 Brief.objective |
| `app/research/planning/lead_planner.py` | DYNAMIC：LLM 或启发式实体 DAG |
| `app/research/planning/validator.py` | 计划机器校验 |
| `app/research/planning/progress.py` | Brief-driven 缺口 |
| `app/research/planning/plan_patch.py` | 补任务只允许 web/file |
| `app/research/runtime/graph.py` / `runner.py` | Progress 带上 intent/brief |
| `tests/test_intent_and_plan.py` | 本方案单测 |

---

## 7. 验收

- 规则路径 **不依赖 LLM/Tavily** 可测  
- 「列出 N 条 + 来源」仍是 chat  
- 比较题 DYNAMIC 且实体来自 Brief  
- Progress 缺的是 Brief.dimensions，不是写死的营收词  
- `prefer_primary` 才检查一手来源  
- 禁网计划与 patch 都不含 `internet_search`
