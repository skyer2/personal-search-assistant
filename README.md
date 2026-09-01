# Research Agent Harness

A controllable and evaluable harness for long-running research agents.

```text
This project is not a search engine.

Search is only a tool environment used to study:
- planning
- multi-agent orchestration
- progress evaluation
- replanning
- context management
- durability
- evidence grounding
- evaluation
```

**Deep Research 只是 Agent Harness 的 workload。Search 只是 Agent 可调用的一种环境能力。**

权威范围：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 研究什么

1. 复杂任务如何拆成稳定 Research Plan  
2. 多 Worker 如何并行且不污染状态  
3. 如何判定任务真正完成（Progress）  
4. 何时 Replan、如何限制自治  
5. 长任务 Context / Evidence 外置  
6. 崩溃恢复与失败归因  
7. 这些机制相对 Vanilla Agent 有没有增益  

不研究：搜索排序、query rewrite、召回质量、RAG、DB、MCP、个人搜索产品 UX。

## 执行路径

默认只有 **agent**（Harness）：

```text
Task → Brief → Plan → parallel Workers → Progress / Replan → Synthesis → Answer
```

`direct` 只用于对照实验，不是产品能力：

```text
Query → single agent + search tool → Answer
```

环境工具固定且尽量简单：`search(query)`、`fetch(url)`、本地 `file_read`。

## 快速启动

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...
export TAVILY_API_KEY=...   # 仅作为 environment 的 search 实现

uvicorn app.api.server:app --reload --app-dir .
cd frontend && pnpm install && pnpm dev
```

## 测试

```bash
python3 tests/test_architecture_p0.py
python3 tests/test_research_harness.py
python3 tests/test_intent_and_plan.py
python3 tests/test_progress_evaluator.py
python3 tests/test_research_checkpoint.py
python3 tests/test_hybrid_planning.py
```
