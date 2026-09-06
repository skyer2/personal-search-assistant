# Graph Convergence 设计

## 目标

本轮修复 Research StateGraph 的控制流不变量，而不是提高 `recursion_limit`：

1. Router 只读 Graph State，不做 workflow 状态迁移。
2. `prepare_synthesis` 是唯一把 early-stop 研究任务写成 `skipped` 的节点。
3. Synthesis 后默认进入 Quality Gate，不再无条件回到 Dispatch。
4. `synthesized / partial / completed / aborted` 是 Research 终态，禁止重新 Dispatch。
5. Router 已路由到 Synthesis 但 Node 发现无可执行 Synthesis 时，抛出 Graph invariant 异常。
6. Artifact DAG 必须满足 required consumer 的 producer 可达性。
7. Understand timeout 的规则 Brief 有确定性质量下限。
8. Graph 自带 stagnant-cycle guard，早于 recursion limit 收敛。

## 控制流

```text
Progress
  ├─ run ───────────► Dispatch ─► Worker ─► Progress
  ├─ gap ───────────► Replan ─► Plan Validate
  ├─ abort ─────────► Abort
  └─ enough/terminal
       └───────────► Prepare Synthesis
                      └─► Synthesize
                          └─► Quality Gate
                              └─► Finalize ─► END
```

`Prepare Synthesis` 做三件事：

1. 将尚未开始的 optional research 标为 `skipped`。
2. 如果 early-stop 后仍无 runnable synthesis，则降级跳过剩余 required research。
3. 写回 `plan`、`task_status`、`synthesis_admission=true` 和 `replan_exhausted=true`。

## Admission Policy

Router 与 Synthesis 使用同一持久化准入信号：

```text
synthesis_admission = true
```

`node_synthesize()` 只依据 Graph State 判断：

- `synthesis_admission`
- `force_synthesis`
- `abort_reason`
- Progress verdict / reason
- 已有 evidence / findings

如果已经路由到 Synthesis 但：

```text
没有 pending/running synthesis
或依赖仍不满足
```

则抛出 `GraphInvariantViolation`，禁止返回伪 `synthesized` 再进入 Dispatch。

## Artifact DAG

CandidateSet 从 task dependency 转为 artifact dependency 后，必须保证：

```text
required consumer
→ requires_artifact
→ producer exists
→ producer is required
```

`annotate_candidate_dependencies()` 会在发现 required consumer 时自动把对应 producer
提升为 required。`validate_artifact_dependencies()` 仍独立校验：

- `missing_artifact_producer`
- `required_task_depends_on_optional_artifact`

Discovery 识别同时区分显式生产者和启发式候选：

- `task_kind=discovery` 或 `produces_artifact` 是显式生产者。
- 仅靠文案命中“候选 / 发现 / landscape”等词的任务，只有在无上游依赖、
  未标记 `deep_dive`、未消费 artifact 时才可作为启发式 Discovery。
- 依赖 Discovery 的深挖任务即使文案包含“候选公司”，也必须被归类为 consumer。

## Understand Fallback

规则 fallback 不允许把完整问题当作 entity，也不允许开放性“值得加入的公司”问题
只得到单一“商业化”维度。命中 career/company recommendation 语义时，确定性补充：

```text
技术实力
团队背景
融资与估值
商业化进展
赛道前景
招聘与人才机会
加入风险
```

并把 `freshness` 提升为 `recent`。

## Stagnant Cycle Guard

Progress Node 计算控制面指纹：

```text
plan_version
task_status
progress verdict / reason
replan_count
status
candidate_set status/items
findings/evidence count
```

指纹连续两轮不变时：

```text
stagnant_cycles >= 2
```

Progress 强制进入 `enough / graph_no_progress`，随后 Prepare Synthesis 交付当前证据。
`recursion_limit` 只保留为最后保险丝。

## 回归测试

必须覆盖：

1. Enough → Prepare → Synthesis → Quality → END。
2. Router 不调用状态迁移函数。
3. Prepare 写回 optional skipped 状态。
4. Synthesis routed-but-not-runnable 抛 invariant 异常。
5. Terminal research status 禁止重新 Dispatch。
6. Optional producer + required consumer 自动提升 producer。
7. Missing artifact producer 被 validator 拒绝。
8. Understand fallback Brief 质量下限。
9. 连续停滞两轮触发 `graph_no_progress`。
10. “深挖候选公司”不会被误判为 Discovery producer。
