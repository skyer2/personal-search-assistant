# Agent Eval：Failure-driven + Experiment-driven

> 成熟做法不是只给最终答案打一个分，而是同时评估 Outcome、Trajectory、Reliability、Efficiency，并把失败样本持续回灌成回归集。

本文是评测实现说明。[ARCHITECTURE.md](./ARCHITECTURE.md) 仍是仓库范围权威。

## 两类 Eval

| 类型 | 问题 | 数据源 |
|---|---|---|
| Regression Eval | 这次代码改动是否破坏确定性合同？ | 组件 / capability / structural dry-run、baseline diff |
| Run Quality Eval | 这个真实 Run 的回答质量如何？ | 该 Run 的 `quality.assessed` / `eval.scored`、grounding、citation、coverage、latency、cost |

前者是开发回归门禁，后者是单次运行质量解释。二者不共用同一个“分数”语义，也不能互相冒充。

## 评测对象

```text
Query → Structured Research Brief → Supervisor
      → Researchers → Evidence-backed Findings
      → Coverage Judge → Synthesis → Quality Gate
```

## 分层

```text
L0  Unit / Invariant          每 PR
L1  Agent Component Eval      每 PR
L1.5 Capability Regression   每 PR
L2  Structural Harness Eval   dry-run 每 PR；live nightly
L3  Production-Fidelity E2E   release / 手工
L4  BrowseComp-Plus           release / 手工
L5  Ablation                  Vanilla / Single-iteration / Full
```

### L1 Component Eval

| 数据集 | 验证什么 |
|---|---|
| `brief_v1.jsonl` | 用户意图、时效、来源、交付物、fast-path 准入 |
| `coverage_v1.jsonl` | Brief 对齐的 Coverage Judgement、缺口、冲突、弱证据 |
| `supervisor_v1.jsonl` | CONDUCT_RESEARCH / COMPLETE、任务边界、预算终止 |
| `evidence_v1.jsonl` | claim-evidence 绑定、引用覆盖、幻觉检测 |

入口：

```bash
.\.venv\Scripts\python.exe tests\eval\run_eval.py --component
```

组件评测不调用真实 LLM、搜索工具，也不产生真实延迟。它证明结构语义，不证明线上答案质量。

### L1.5 Capability Regression

`capability_v1.jsonl` 包含 20 条确定性用例，覆盖 15 类能力：

```text
单点事实 / 条件过滤聚合 / 对比 / 有迹多跳 / 无提示多跳
长报告 / 时效 / 歧义 / 冲突 / 虚假前提
垂直专业 / 长尾 / 多语言 / 严格引用 / 多轮修正
```

该层使用 legacy projection adapter 验证行为，不把 `ResearchSpec` 作为生产 workflow authority。

### L2 Structural Harness Scenario Eval

`harness_scenarios_v1.jsonl` 包含 20 条失败模式：

```text
Brief / Supervisor / Parallel / Coverage / Evidence / Context / Durability / Worker
```

Trajectory 评的是 required / forbidden / if-then / limits，不是固定 `A→B→C→D` 路径。

### L3 Production-Fidelity E2E

入口：

```bash
.\.venv\Scripts\python.exe tests\e2e\test_production_fidelity_synthesis.py
.\.venv\Scripts\python.exe tests\test_runtime_failure_observability.py
.\.venv\Scripts\python.exe scripts\release_smoke.py --q1-runs 3
```

这一层保留生产配置，只替换 provider implementation 与 clock，注入 rate limit、empty、context overflow、provider unavailable、auth、budget exhausted、timeout 等故障，并断言：

- 终态与非空输出；
- Worker 生命周期；
- root span 与 lineage；
- synthesis attempt / fail reason / fallback；
- search-query / fetch-source / logical tool-invocation budget separation；
- `budget.denied` 与 `semantic.fallback` 诊断；
- Trace Integrity；
- repeated run 非空 `partial` 或 `success`。

`release_smoke.py` 还固定执行 3 次主研究查询、两个开放研究查询和 1 个原子事实查询。主查询必须 3 次均为非空 `partial` 或 `success`；原子事实必须命中快路径并回答受证据支持的年份，同时 Trace Integrity 通过。

### L4 BrowseComp-Plus

公开坐标系。Retrieval 与 Agent 分开算，离线 surrogate 不冒充官方 Accuracy。详见 [BROWSECOMP_PLUS_EVAL.md](./BROWSECOMP_PLUS_EVAL.md)。

### L5 Ablation

```text
V0 Vanilla           Query → single agent → tools → Answer
V1 Single-iteration  Brief → Supervisor → Workers → Findings → Coverage → Answer
V2 Full              Coverage gap 可触发下一轮 Supervisor 研究
```

配置：

```bash
.\.venv\Scripts\python.exe tests\eval\run_eval.py --live --variant vanilla --fixture
.\.venv\Scripts\python.exe tests\eval\run_eval.py --live --variant single_iteration --fixture
.\.venv\Scripts\python.exe tests\eval\run_eval.py --live --variant full --fixture
```

报告 ΔAccuracy / ΔCitation / ΔTokens / ΔP95 / ΔToolCalls / Supervisor Iteration / Supervisor Recovery。每个 case 绑定 `case_id` + `variant` + `run_id` + `trace_id`，失败可 drill-down 到 Flight Recorder。

## 指标

| 组 | 指标 |
|---|---|
| Quality | Answer Accuracy、Evidence Support、Citation Precision/Recall |
| Behavior | Gate Pass Rate、Coverage Judge、Supervisor Iteration、Failure Taxonomy |
| Efficiency | Tokens、Cost、Tool Calls、Workers、Latency P50/P95 |

`task_success_rate` 等于 Gate Pass Rate，不混入 Trajectory 相似度、报告格式分或 Memory 召回。

## Judge

| 模块 | 作用 |
|---|---|
| `ReportStructureGrader` | 只评标题/引用标记/参考文献，不是答案质量 |
| `QualityJudge` | correctness / completeness / grounding；`--judge` 或配置启用 |
| Human meta-eval | `judge_calibration_v1.jsonl`，目标 30～50 条专家标签 |

```bash
.\.venv\Scripts\python.exe tests\eval\run_eval.py --calibrate-judge
```

## Reliability

```bash
.\.venv\Scripts\python.exe tests\eval\run_eval.py --live --variant full --repeat 3 --fixture --limit 5
```

| 指标 | 含义 |
|---|---|
| `pass_at_1` | 单次运行成功期望 |
| `pass_at_k` | k 次里至少成功一次 |
| `pass_hat_k` | k 次全部成功；生产型 Agent 更应看这个 |
| latency/token mean/std | 代价波动 |

## Controlled Environment

`HARNESS_EVAL_FIXTURE=1` 时 `internet_search` / `fetch_url` 只读 `tests/eval/fixtures/corpus.json`，未知 URL 不回落到真实网络。BrowseComp-Plus 仍走自己的固定 corpus。

## Failure Taxonomy

失败写入 `failure_stage` + `failure_type`：

```text
brief · supervisor · retrieval · worker · tool · evidence
coverage · synthesis · runtime
```

## CI 分档

```text
PR CI     L0 unit + L1 component + L1.5 capability + L2 dry-run
Nightly   live scenario × Full + Single-iteration
Release   L3 Production-Fidelity + Release Smoke
Benchmark BrowseComp-Plus + 官方 judge
```

## 命令

```bash
# PR
.\.venv\Scripts\python.exe tests\eval\run_eval.py --dry-run --fail-on-regression

# 只跑 component
.\.venv\Scripts\python.exe tests\eval\run_eval.py --component

# Live / fixture
.\.venv\Scripts\python.exe tests\eval\run_eval.py --live --variant full --limit 5 --fixture
.\.venv\Scripts\python.exe tests\eval\run_eval.py --live --variant full --repeat 3 --fixture --limit 5
.\.venv\Scripts\python.exe tests\eval\run_eval.py --calibrate-judge
# Release
.\.venv\Scripts\python.exe scripts\release_smoke.py --q1-runs 3
```

当前回归真源：`brief_v1`、`coverage_v1`、`supervisor_v1`、`evidence_v1`、`capability_v1`、`harness_scenarios_v1`。基线只证明结构不变量不退化，不证明线上答案质量。
