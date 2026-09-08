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

补充文档：

- [docs/HARD_CEILING_ADAPTIVE_EFFORT.md](docs/HARD_CEILING_ADAPTIVE_EFFORT.md) — Hard Ceiling + Adaptive Effort（全局确定性控制 / 局部自治）
- [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) — Agent Flight Recorder（统一 Trace / Replan / Eval）
- [docs/EVALUATION.md](docs/EVALUATION.md) — 五层 Eval（Component / Scenario / BrowseComp / Ablation）
- [docs/CONTEXT_SYSTEM.md](docs/CONTEXT_SYSTEM.md) — History / Context / Memory 分层与遗忘
- [docs/HARNESS_ARCHITECTURE.md](docs/HARNESS_ARCHITECTURE.md) — StateGraph 运行时
- [docs/BROWSECOMP_PLUS_EVAL.md](docs/BROWSECOMP_PLUS_EVAL.md) — BrowseComp-Plus 评测
- [docs/OPENEULER_BARE_METAL.md](docs/OPENEULER_BARE_METAL.md) — 裸机安装、`.env`、启停与 systemd

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
Simple Fact → Fast Path → Source Gate → Answer
Other Task → Brief → research-only Plan → parallel Workers → dispatch barrier → Assessments → ControlPolicy → Synthesis → TerminalPolicy → Answer
```

Replan 使用稳定 Business Gap 与 replacement task：旧任务被 supersede，新任务继承同一 Gap；恢复次数、代数和同 Gap 次数都有硬上限。

`direct` 只用于对照实验，不是产品能力：

```text
Query → single agent + search tool → Answer
```

环境工具固定且尽量简单：`search(query)`、`fetch(url)`、本地 `file_read`。

## History / Context / Memory

```text
UI History         显示 Session 下全部 Run
Model Context      当前 Run + 相关 RunSummary Top-K + Memory Top-K
Long-term Memory   带 provenance / trust tier 的跨任务记忆
```

前端支持：

- 删除单个问答（对应 Run）
- 多选删除
- 清空当前会话
- 归档当前会话
- Memory 管理：查看、单条遗忘、按会话遗忘、清空用户记忆

删除 Run 会级联清理 RunStore、run 目录、Trace / Projection / Payload、Graph checkpoint、RunSummary 和 `provenance.run_id` 派生记忆。

## 快速启动

**openEuler 裸机（Python 3.12、密钥、防火墙、systemd）逐步说明：** [docs/OPENEULER_BARE_METAL.md](docs/OPENEULER_BARE_METAL.md)

已有 3.12 与 Node 的开发机：

```bash
cp .env.example .env          # 填写 OPENAI_* 与搜索 Provider 配置
pip install -r requirements.txt
uvicorn app.api.server:app --reload --app-dir . --host 0.0.0.0 --port 8000
cd frontend && pnpm install && pnpm dev
```

## 测试

```bash
python3 tests/test_flight_recorder.py
python3 tests/test_architecture_p0.py
python3 tests/test_research_harness.py
python3 tests/test_environment_tools.py
python3 tests/test_intent_and_plan.py
python3 tests/test_control_policy.py
python3 tests/test_research_checkpoint.py
python3 tests/test_hybrid_planning.py
python3 tests/eval/test_eval_dry_run.py
python3 tests/eval/run_eval.py --dry-run
```

发布闭环（生产配置故障矩阵 + 4 条 release query）：

```bash
python scripts/release_smoke.py --q1-runs 3
```
