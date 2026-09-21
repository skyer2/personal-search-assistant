# Deep Research 控制面收敛 v2

本次改造把控制面从“Brief → Supervisor → Plan → Worker”的重复路径收敛为：

```text
Intent Router（确定性）
  → Brief（一次模型调用）
  → Brief + bounded initial plan（确定性）
  → 并行 Worker wave 1
  → Gap Precheck（确定性）
      → 可执行阻塞缺口：最多一次 Targeted Supervisor/Repair wave 2
      → 其他情况：直接 Synthesis
  → Minimal Quality Gate → Delivery
```

## 代码变化

### 之前

- 开放问题先编译 Brief，再调用 Supervisor 生成第一轮研究计划。
- Coverage 不足时无条件进入 Supervisor。
- Worker 任务缺少统一的 question/hypothesis/evidence contract。
- 控制面耗时和研究耗时混在同一个阶段，无法判断第二次 Supervisor 是否有价值。

### 现在

- `app/research/routing/intent_router.py` 在任何模型调用前做确定性意图路由：
  `simple_fact`、`simple_search`、`deep_research`、`comparison`、`mechanism`、`trend`、`forecast`、`file_only`。
- `app/research/planning/brief_plan.py` 从 canonical Brief 生成 bounded plan。每个任务包含 `question_id`、`hypothesis`、`evidence_needed`、`counter_evidence_needed`、`search_hints`、`entities`、`dimensions`、`max_queries`。
- `app/research/control/gap_precheck.py` 在第二次 Supervisor 前检查 coverage、预算、波次和可执行缺口。无阻塞缺口、预算耗尽、达到两波或已有足够证据时不会发起 Supervisor LLM 调用。
- 语义动作统一投影为 `TARGETED_RESEARCH` 或 `SYNTHESIZE`，同时保留兼容字段 `CONDUCT_RESEARCH` / `COMPLETE`；事件带有 `runtime_action` 与 `override_reason`。
- Quality Gate 始终运行，并记录 `insight_density`、Completion Contract、citation 和 grounding 诊断。
- Latency summary 新增 `intent_router`、`brief_plan`、`plan_validate`、`gap_precheck`，并继续保留 worker critical path、LLM/tool/idle 拆分。

## 有界规则

- 初始计划最多两个 focused worker；每个 worker 最多两个实体、两个维度、七个查询。
- 研究波次最多两轮；第二轮只能针对一个阻塞缺口，最多一个实体、一个维度、五个查询（由现有 admission/budget 层继续约束）。
- 没有可绑定证据的 finding 不进入最终回答；事实、推断和预测沿用现有 claim/answer contract。
- provider 或 telemetry 失败不会阻止 deterministic recovery 和 Quality Gate。

## 验证

新增 `tests/test_control_plane_v2.py`，覆盖：

- deterministic intent route；
- bounded Brief + Plan contract；
- counter-evidence 约束；
- sufficient coverage 跳过 Supervisor；
- actionable gap 允许一次 repair；
- max wave 阻止第三轮。

完整测试仍需使用项目的真实 `.env` 做 live provider 验证。provider 超时或空响应时，报告必须区分 primary synthesis failure 和 deterministic recovery；不能把 fallback 当作 primary 稳定性的证明。

## 最终 `.env` 实测（2026-09-22）

命令：

```text
.venv/Scripts/python scripts/live_deep_research_e2e.py --runs 1 --mode agent --max-replans 1 --output output/latency-live-smoke-v2-acceptance
```

结果：

| 指标 | 结果 |
| --- | ---: |
| 运行状态 | `success` |
| 总耗时 | 363.96 s |
| accepted findings / evidence | 17 / 18 |
| Completion Contract | PASS |
| Citation validity | PASS |
| Grounding / Quality | PASS |
| Trace root / orphan / cycle | 1 / 0 / 0 |
| synthesis attempts | 2 |
| synthesis mode | `deterministic_recovery` |
| `synthesis_degraded` | `true` |
| evaluator `passed` | `true` |

控制面阶段为：Intent Router 145 ms、Brief 30.004 s、Research critical path 123.344 s、Gap Precheck 0 ms、Supervisor 8 ms、Synthesis 210.061 s、Quality 4 ms、Delivery 7 ms。日志中的 Supervisor COMPLETE 来源为 `deterministic_gap_precheck`，`supervisor_calls_avoided=1`，没有为第二次决策发起模型请求。

这次 provider 的 primary synthesis 没有稳定完成，经过一次 retry 后由证据驱动 deterministic recovery 交付，最终质量契约通过。因此本次证明了“失败可恢复、状态可解释、证据可追溯”，不能据此宣称 primary synthesis P95 已达标；这仍需要稳定 provider 后做多次 live eval。
