# Personal Search Assistant

个人搜索助手：日常 Web + File 搜索，复杂问题按需 Deep Research。底层 Harness / StateGraph 保留，企业 DB/KB/MCP 能力已移除。

## 文档

| 文档 | 内容 |
|------|------|
| [Personal Search 产品架构](docs/PERSONAL_SEARCH.md) | Mode Router、Quick/Deep、模块拆分、P0 实施 |
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
用户 → Mode (auto/quick/deep) → Harness Intent/Plan
  → StateGraph → Web/File Worker → Evidence → Answer + Sources
  （显式 Markdown/PDF 请求才走 synthesis 工具）
```

## 测试

```bash
python3 tests/test_harness_phase1.py
python3 tests/test_hybrid_planning.py
python3 tests/test_harness_hitl.py
```

## 下一步（P1）

- `mode_router.py` Quick/Deep 分流子图
- `conversation/store.py` 多轮会话
- `fetch_url.py` 搜索卡片后按需拉正文
