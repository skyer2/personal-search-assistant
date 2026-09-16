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
| `coverage_v1.jsonl` | Brief key questions 对齐的 Coverage Judgement、primary/freshness、缺口、冲突、弱证据 |
| `supervisor_v1.jsonl` | CONDUCT_RESEARCH / COMPLETE、任务边界、预算终止 |
| `evidence_v1.jsonl` | claim-evidence 绑定、引用覆盖、幻觉检测 |

入口：

```bash
.\.venv\Scripts\python.exe tests\eval\run_eval.py --component
```

组件评测不调用真实 LLM、搜索工具，也不产生真实延迟。它证明结构语义，不证明线上答案质量。

收敛闭环契约由 `tests/test_deep_research_convergence.py` 单独覆盖：

- lexical-only 匹配只能是 `partial`；
- primary / freshness 要求会阻止 `supported`；
- blocking unresolved conflict 永远阻止 `sufficient`；
- Supervisor 必须精确复制 `gap_id` / `criterion_id`；
- soft deadline 禁止继续检索且不产生 hard denial；
- bounded semantic conflict 会绑定 criterion；
- Synthesis prompt 必须消费结构化 conflict resolution。

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
.\.venv\Scripts\python.exe -m pytest tests\e2e\test_budget_generous_baseline_full_stack.py
.\.venv\Scripts\python.exe tests\test_runtime_failure_observability.py
.\.venv\Scripts\python.exe scripts\release_smoke.py --q1-runs 3
```

这一层保留生产配置，只替换 provider implementation 与 clock，注入 rate limit、empty、context overflow、provider unavailable、auth、budget exhausted、timeout 等故障，并断言：

- 终态与非空输出；
- Worker 生命周期；
- root span 与 lineage；
- synthesis attempt / fail reason / fallback；
- search-query / fetch-source / logical tool-invocation budget separation；
- `budget.decided` Finalization、`budget.denied` 与 `semantic.fallback` 诊断；
- Worker 正常 stop reason 与无主要 hard-cap 结束；
- Trace Integrity；
- repeated run 非空 `partial` 或 `success`。

`release_smoke.py` 还固定执行 3 次主研究查询、两个开放研究查询和 1 个原子事实查询。主查询必须 3 次均为非空 `partial` 或 `success`；原子事实必须命中快路径并回答受证据支持的年份，同时 Trace Integrity 通过。

`test_budget_generous_baseline_full_stack.py` 固定执行 Budget 宽松基线的两个 Golden Query，走真实 `batch_search → batch_fetch → 补搜索 → structured result` 适配器，并断言证据链和 Coverage 可用、无预算硬上限异常、Trace Integrity 通过、终端事件携带完整 used / reserved / limit 与 stop reason 快照。单个 Worker 的 `partial` 可以继续向 Coverage 贡献证据；最终质量按 Delivery 和 grounding 判断。

### 真实 `.env` Golden Deep Research E2E

入口：

```powershell
.\.venv\Scripts\python.exe scripts\live_deep_research_e2e.py --runs 3 --mode deep_debug
```

真实评测以 Completion Contract 为准：每个 key question 都有直接回答和可解析证据绑定，引用与 Trace 完整，最终业务状态只能是 `success`、`partial`、`failed` 或 `cancelled`。compact 或 deterministic recovery 只写入 `synthesis_degraded` 等诊断字段，完整回答仍计为 `success`；`partial` 不计通过。盲测套件使用独立查询集和重复运行，单次 fallback 只能证明恢复路径，不代表 primary 性能达标。

本轮十题校准集可通过 `scripts/live_deep_research_suite.py` 顺序执行。脚本每题使用独立 session，清理并重建 `output/live_deep_research_suite/`，保存 `answer_01.md` 至 `answer_10.md` 和 `report.json`；它不会把 Coverage 标签当作终态，最终以 Completion Contract、Quality 和 Trace 完整性联合审计。十题校准结果不等同于 SDD 要求的 20 题 × 3 次盲测发布门槛。

### Blind 20 × 3 Live Eval

`scripts/live_blind_eval.py` 使用独立的 20 题查询集，每题连续运行 3 次，默认走
`agent` 模式和本地真实 `.env` provider。查询覆盖热点、框架比较、协议、Coding
Agent、Kubernetes、记忆、评测、安全、市场、研究综述和职业决策等类型；它不复用十题
校准集，也不把 `partial` 计为通过。

```powershell
.\.venv\Scripts\python.exe scripts\live_blind_eval.py --mode agent --repeat 3 --output output\blind20x3
```

单次运行默认硬上限 900 秒，可用 `--run-timeout-sec` 调整；超时会保留为
`live_run_timeout` 失败样本，避免评测进程无限等待。真实 provider 压测建议顺序运行，
不要把多个分片并发到同一个账号，否则限流和队列延迟会污染稳定性结论。

每次运行都会写入 `answer_<case>_<attempt>.md` 和 `report.json`。报告包含：

- `complete_success`、`pass_at_1`、`pass_hat_3`；
- `success`、`partial`、`failed`；
- evidence/citation/trace integrity 通过率；
- P50/P95 latency；
- 每个案例和每次 attempt 的终态、回答长度、证据数、合成降级和 trace 诊断。

发布判定只使用 Completion Contract：必须是 `success`、回答完整、引用和证据有效，
且 trace 无 root/orphan/cycle 问题。`partial`、fallback 或单次成功不能替代三次一致通过。

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
