# Agent Eval：Failure-driven + Experiment-driven

> **业界成熟做法不是「给最终答案打一个分」，而是同时评估 Outcome、Trajectory、Reliability、Efficiency，并把失败样本持续回灌成回归集。**
>
> 本项目研究的是 `Plan → Worker → Progress → Replan → Evidence → Finalize` 这套 Harness。只看最终 Accuracy，看不出 Harness 哪里起作用。

权威范围仍是 [ARCHITECTURE.md](./ARCHITECTURE.md)。本文是评测实现说明。

## 一句话架构

Eval 分六层。PR 只跑确定性层；Production-Fidelity、Live 和 BrowseComp 不进每次 CI。

```text
L0  Unit / Invariant          每 PR
L1  Agent Component Eval      每 PR
L1.5 Capability Regression   每 PR
L2  Structural Harness Eval   dry-run 每 PR；live nightly
L3  Production-Fidelity E2E   release / 手工
L4  BrowseComp-Plus           release / 手工
L5  Ablation                  Vanilla / No-Replan / Full
```

对外报告只保留三组指标：

| 组 | 指标 |
| --- | --- |
| Quality | Answer Accuracy、Evidence Support、Citation Precision/Recall |
| Behavior | Gate Pass Rate、Progress 对错、Replan Trigger Precision、Replan Recovery Rate、Failure Taxonomy |
| Efficiency | Tokens、Cost、Tool Calls、Workers、Latency P50/P95 |

`task_success_rate` 现在等于 **Gate Pass Rate**（timeout / invalid schema / 错误答案等硬约束）。不再把 Trajectory 相似度、报告格式分、Memory 召回混进同一个 success。

## Dry-run 的语义

前端 **Eval 面板** 现在叫 **Regression Gate**。它只展示确定性回归结果：

```text
Component  20 / 20
Capability 20 / 20
Scenario   20 / 20
Planner     5 / 5
Progress    6 / 6
Replan      5 / 5
Evidence    4 / 4
Regression  0
Merge Blocked NO
```

Dry-run 不调用真实 LLM、搜索工具，也不产生真实延迟。因此 Grounding、Citation P/R、CCR、Tool Calls、Tokens、Cost、P50/P95、pass@1、pass^k 只能在 **Live E2E / Benchmark** 中展示；在 Regression 面板中必须显示 `N/A`，不能显示 `0`。

Evidence component 里的低 grounding 分数是 **unsupported-claim 检测器样例通过** 的结果，不代表真实 Agent 的 grounding 能力。不要把检测器测试分和产品能力分混在一个 Dashboard。

## 分层

### L0 Deterministic invariants

现有单测覆盖 Planner DAG、Worker isolation、ActionBudget、checkpoint、Evidence 回溯。不要 LLM Judge。

### L1 Component Eval

| 数据集 | 验证什么 |
| --- | --- |
| `tests/eval/datasets/planner_v2.jsonl` | 覆盖维度、可并行、无环、来源策略、交付物 |
| `tests/eval/datasets/progress_v1.jsonl` | Progress assessment 能否判 `sufficient / gap / unknown` 与冲突 |
| `tests/eval/datasets/replan_v1.jsonl` | 是否针对缺口补 task、不重复、不越权、不超预算 |
| `tests/eval/datasets/evidence_v1.jsonl` | claim 是否有 evidence，citation 是否支持 |

入口：`python tests/eval/run_eval.py --component`

### L1.5 Capability Regression

`tests/eval/datasets/capability_v1.jsonl` contains 20 deterministic cases and covers all 15 required capability classes:

```text
单点事实 / 条件过滤聚合 / 对比 / 有迹多跳 / 无提示多跳
长报告 / 时效 / 歧义 / 冲突 / 虚假前提
垂直专业 / 长尾 / 多语言 / 严格引用 / 多轮修正
```

The runner validates semantic contract behavior, not only final text:

- ResearchSpec routing and requirements
- CoverageContract derivation, freshness, conflict, and candidate expansion
- plan shape and task kinds
- coverage status from claims and admitted evidence
- premise, ambiguity, multilingual hints, citation, and follow-up spec versioning

入口：`python tests/eval/runners/capability.py`

### L2 Structural Harness Scenario Eval

`tests/eval/datasets/harness_scenarios_v1.jsonl`（20 条），每条对应一种 Agent failure mode：

Plan / Parallel / Progress false-enough / Conflict / Needless replan / Replan budget / Policy / Evidence / Context / Durability。

Trajectory 评的是 **required / forbidden / if-then / limits**，不是 `A→B→C→D` 固定路径。

### L3 Production-Fidelity E2E

入口：`tests/e2e/test_production_fidelity_synthesis.py` 与 `scripts/release_smoke.py`。

这一层与 L2 的区别是：**L2 证明控制面结构不变，L3 证明生产配置在故障下仍能正确收敛**。

- 保留生产配置：planner LLM enabled、direct worker invoke、replan 上限、全局 token 预算、synthesis timeout。
- 只替换 provider implementation 与 clock，不修改 HarnessConfig，也不把生产路径降级为假 runtime。
- 注入 rate limit、empty、context overflow、provider unavailable、auth、budget exhausted、timeout 等故障。
- 断言终态、非空输出、Worker 生命周期、root span、lineage、synthesis attempt / fail reason / fallback。
- Q1 release blocker 默认重复 3 次，要求每次都是非空 `partial` 或 `success` 且 Trace Integrity 通过。

发布冒烟：

```bash
python scripts/release_smoke.py --q1-runs 3
```

该命令先运行 L3 fault-injection，再运行 4 条 release query suite。它是发布门禁，不是普通单测；需要完整的 Python 环境，与前端构建无关。

### L4 BrowseComp-Plus

公开坐标系。Retrieval 与 Agent 分开算。离线 surrogate **不冒充**官方 Accuracy。详见 [BROWSECOMP_PLUS_EVAL.md](./BROWSECOMP_PLUS_EVAL.md)。

对照应改成：

```text
Retrieval-only → Vanilla → Harness-NoReplan → Full Harness
```

固定模型、语料、search backend，只改 Harness 变量。

### L5 Ablation

```text
V0 Vanilla          Query → single agent → tools → Answer
V1 Harness-NoReplan Spec → Plan → Workers → Coverage → Answer
V2 Full-Harness     同上，SemanticGap 触发 GapFill / Expand / Replan
```

配置：`tests/eval/variants/{vanilla,no_replan,full}.yml`

```bash
python tests/eval/run_eval.py --live --variant vanilla
python tests/eval/run_eval.py --live --variant no_replan
python tests/eval/run_eval.py --live --variant full
```

报告 ΔAccuracy / ΔCitation / ΔTokens / ΔP95 / ΔToolCalls / Replan Recovery。每个 case 绑定 `case_id` + `variant` + `run_id` + `trace_id`，失败可 drill-down 到 Flight Recorder。

## Judge

| 模块 | 作用 |
| --- | --- |
| `ReportStructureGrader`（原 heuristic judge） | 只评标题/引用标记/参考文献，**不是**答案质量 |
| `QualityJudge` | correctness / completeness / grounding。`--judge` 或 `eval.llm_judge_enabled` 才启用 |
| Human meta-eval | `tests/eval/datasets/judge_calibration_v1.jsonl`（当前 13 条 seed）。目标仍是 30～50 条专家标签 |

QualityJudge 来源：

```text
disabled           默认，不算答案质量
llm                模型 JSON rubric
reference_grader   有 reference / must_include 时的词面 surrogate，不冒充官方 Accuracy
unavailable        已启用但没有模型和 reference
```

不要把 `llm_stub` 当成分数。Judge 校准看的是 human gold vs automatic label 的 agreement / kappa / MAE，而不是假设 Judge 永远正确。

```bash
python tests/eval/run_eval.py --calibrate-judge
```

## Reliability

Live 默认仍是 1 case × 1 run。要看稳定性：

```bash
python tests/eval/run_eval.py --live --variant full --repeat 3 --fixture --limit 5
```

报告字段：

| 指标 | 含义 |
| --- | --- |
| `pass_at_1` | 单次运行成功期望 |
| `pass_at_k` | k 次里至少成功一次 |
| `pass_hat_k` | k 次全部成功。生产型 Agent 更应看这个 |
| latency/token mean/std | 代价波动 |

## Controlled environment

Harness 控制面的 live scenario 用固定语料，避免网页变化污染分数：

```bash
python tests/eval/run_eval.py --live --variant full --fixture --limit 5
```

`HARNESS_EVAL_FIXTURE=1` 时 `internet_search` / `fetch_url` 只读 `tests/eval/fixtures/corpus.json`，未知 URL **不会**回落到真实网络。BrowseComp-Plus 仍走自己的固定 corpus，优先级更高。

真实 Web 只留给 online / capability eval。

## Failure Taxonomy

失败写入 `failure_stage` + `failure_type`：

`planning` · `retrieval` · `worker` · `tool` · `evidence` · `progress` · `replan` · `synthesis` · `runtime`

## CI 分档

```text
PR CI     L0 unit + L1 component + L1.5 capability + L2 dry-run
Nightly   20 条 live scenario × Full + NoReplan（可选 1～3 repeat）
Release   L3 Production-Fidelity + Release Smoke
Benchmark BrowseComp 50 × Vanilla / NoReplan / Full + 官方 judge
```

## 命令

```bash
# PR
python tests/eval/run_eval.py --dry-run --fail-on-regression

# 只跑 component
python tests/eval/run_eval.py --component

# Live ablation（需 LLM；控制面请加 --fixture）
python tests/eval/run_eval.py --live --variant full --limit 5 --fixture
python tests/eval/run_eval.py --live --variant full --repeat 3 --fixture --limit 5
python tests/eval/run_eval.py --calibrate-judge
```

旧 `tasks.jsonl`（数据库 / RAGFlow / 电商 PDF）已删除；当前回归真源是 Component datasets、`capability_v1.jsonl` 与 `harness_scenarios_v1.jsonl`。

基线：`tests/eval/results/baseline.json`（L1+L2 dry-run）。它证明的是 **Planner / Progress / Replan / Evidence invariants 不退化**，不证明线上答案质量。
