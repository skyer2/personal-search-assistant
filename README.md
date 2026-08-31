# Personal Search Assistant

个人搜索助手：日常 Web + File 搜索，复杂问题按需 Deep Research。底层 Harness / StateGraph 保留，企业 DB/KB/MCP 能力已移除。

## 文档

| 文档 | 内容 |
|------|------|
| [Personal Search 产品架构](docs/PERSONAL_SEARCH.md) | Mode Router、Quick/Deep、模块拆分 |
| [Intent 与 Plan 方案](docs/INTENT_AND_PLAN.md) | Deep 路径 Brief / 混合规划 / Brief-driven Progress |
| [Harness 运行时架构](docs/HARNESS_ARCHITECTURE.md) | StateGraph + Worker Profile |
| [Research Intelligence](docs/RESEARCH_INTELLIGENCE.md) | Progress / Replan（Deep 路径） |
| [上下文工程](docs/CONTEXT_SYSTEM.md) | Artifact / Evidence / JIT |
| [Memory 系统](docs/MEMORY_SYSTEM.md) | 身份四元组、Source Ledger |

## P0 已实现

- 默认 **chat 交付**（带来源不再自动转 Markdown）
- 来源策略 **web + file** only
- Worker：**web / file / mixed / synthesis**
- `harness.yml` **personal_search** 预算段；HITL/DB/KB/MCP 关闭
- 前端 **Auto / Quick / Deep** + 固定身份 `user_id=me`
- API `mode` / `project_id` / `tenant_id=local`
- 删除 MySQL、RAGFlow、MCP 全栈代码

## P1 已实现

- **Mode Router**：`auto/quick/deep`；用户显式选择优先；Auto 按比较/报告/多 PDF 升 Deep
- **Quick 子图**：`conversation → mode_router → quick_search → quick_fetch → quick_synthesize → finalize`，不进 Progress/Replan
- **Conversation Store**：按 `user/project/thread` 存最近 4～8 turn + rolling summary；短追问改写
- **`fetch_url`**：搜索只出卡片，按需拉正文进 Artifact/Evidence

## Intent / Plan（Deep）

- Intent = **Research Brief**（目标、实体、维度、depth/freshness、官方优先、成功标准）
- Plan 按 Brief 选 DIRECT / TEMPLATE / DYNAMIC；比较题按实体拆 DAG
- Progress 对照 Brief 判缺口，不再写死营收/量产词

## 快速启动

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...
export TAVILY_API_KEY=...

# 后端
uvicorn app.api.server:app --reload --app-dir .

# 前端
cd frontend && pnpm install && pnpm dev
```

## 架构（个人版）

```text
用户 → Conversation → Mode Router (auto/quick/deep)
  ├─ Quick：search 卡片 → fetch_url → Answer + Sources（无 Progress/Replan）
  └─ Deep：Intent → Plan → DAG Workers → Progress → Replan → Synthesis
```

## 测试

```bash
python3 tests/test_intent_and_plan.py
python3 tests/test_personal_search_p1.py
python3 tests/test_research_harness.py
python3 tests/test_harness_phase1.py
python3 tests/test_hybrid_planning.py
python3 tests/test_harness_hitl.py
```

## 下一步（P2）

- History / Projects / Saved Sources UI
- Developer Mode 折叠 Eval/Trace
- 中文 2-gram hybrid context selector
