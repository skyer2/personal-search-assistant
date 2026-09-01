# Personal Search Assistant

Personal Search Assistant 是一个 adaptive research agent：简单问题直接回答，需要最新信息时搜索，复杂问题进入可恢复的 Research StateGraph；Research Domain 负责分解、证据充分性和 Replan，Worker Runtime 负责局部自主执行，所有事实通过 Evidence/Artifact 可追溯。

权威方案：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 文档

| 文档 | 内容 |
|------|------|
| [架构方案](docs/ARCHITECTURE.md) | 四层边界、唯一 ResearchState、WorkerRuntime、三档路由 |
| [Intent 与 Plan](docs/INTENT_AND_PLAN.md) | RESEARCH 路径 Brief / 混合规划 / Progress |
| [Harness 运行时](docs/HARNESS_ARCHITECTURE.md) | StateGraph Runtime 细节 |
| [上下文工程](docs/CONTEXT_SYSTEM.md) | Artifact / Evidence / JIT |
| [Memory](docs/MEMORY_SYSTEM.md) | 个人研究记忆（上次结论、过期证据、continuation） |

## 三档，而不是每问都研搜

```text
用户 → Conversation → Task Router
  ├─ ANSWER（直答）：概念题，LLM，不检索
  ├─ SEARCH（搜索）：最新事实，search → fetch → 综合
  └─ RESEARCH（研搜）：Brief → Plan → 并行 Worker → Progress → Replan
```

例：

- `std::apply 是什么` → ANSWER
- `glibc 2.42 release notes 改了什么` → SEARCH
- `对比 LangGraph、Temporal 和 DeepSeek Harness 的 durable workflow` → RESEARCH

## 状态模型

- **唯一 workflow truth**：`ResearchState` → LangGraph SQLite checkpointer
- **原文外置**：Artifact Store / Evidence Store（Claim → Evidence → Artifact → Source）
- `LoopState` 只是进程内 handles，**不再**作为第二套 `checkpoint.json` 恢复系统

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

身份固定：`user_id=me`，`tenant_id=local`，`project_id=Inbox`。

## 测试

```bash
python3 tests/test_architecture_p0.py
python3 tests/test_personal_search_p1.py
python3 tests/test_intent_and_plan.py
python3 tests/test_research_harness.py
python3 tests/test_harness_phase1.py
python3 tests/test_hybrid_planning.py
python3 tests/test_progress_evaluator.py
python3 tests/test_research_checkpoint.py
```
