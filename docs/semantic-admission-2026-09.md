# Semantic Admission 设计

## 目标

上一轮解决了 Graph “能否结束”；本轮解决 “结束决策是否可信”。核心不变量：

1. 有 blocking evidence gap 时，Progress 不允许判定 `enough`。
2. Evidence Research Query 的核心维度必须由 required research task 覆盖。
3. Normal Synthesis 必须有可信证据，且不得跳过 required research。
4. Synthesis / Final Answer 是 Derived Output，永远不能注册为自己的 Evidence。
5. Partial / Failed 的终止原因必须是一等 Trace 字段，不受 metadata 截断影响。

## Progress Gap 单一事实源

`ProgressAssessment.gaps` 是唯一 Gap truth；`coverage_gaps`、`missing_dimensions`
和 `open_gap_ids` 只是兼容视图，必须在判定 verdict 前同步：

- Coverage Matrix 输出 `type=coverage_gap`。
- 同一个 gap 同步写入 `coverage_gaps` 的 description。
- `materialize_gaps()` 将 `coverage_gap` 纳入 actionable types。
- verdict 判定使用 blocking + actionable gap 集合，而不是另一份 legacy list。

```text
blocking_gaps > 0 → verdict = gap
blocking_gaps = 0 → 才允许 enough
```

## Plan Required Research Contract

Prompt 只是建议，Validator 是合同执行点：

```text
evidence research query
→ 至少一个非 optional research task
→ Brief 核心维度不能只由 optional task 覆盖
```

新增拒绝原因：

- `no_required_research_task`
- `core_dimension_only_optional:<dimension>`

模板计划同样遵守该合同。检索步骤若无法从 Brief 中识别出明确的
`coverage_keys`，不能因为“未命中维度”被降级为 optional；只有显式
`optional` 或明确 supporting/background 语义才允许 optional。这避免
“唯一检索步骤 optional → Validator 拒绝 → fallback 继续失败”的死路。

## Coverage Evidence Semantics

Coverage Matrix 的最小可信单元是：

```text
dimension-matched fact + external source / evidence_id
```

因此 Worker 合同中的 `facts + sources` 与 `findings + evidence_ids`
等价参与覆盖判定；仅有 derived output 不算证据。Brief 的
`criteria/constraint` 缺口保留为 advisory gap，用于披露质量风险，但不
触发新的 required research wave。

## Synthesis Admission Policy

Synthesis 分为两种模式：

### Normal Synthesis

要求：

- required research 全部 terminal。
- trusted evidence count > 0。
- Progress 没有 blocking gap。

Normal 模式只允许跳过 optional pending research，不允许跳过 required research。

### Emergency Synthesis

仅允许以下触发：

- deadline / research budget exhausted
- provider globally unavailable
- explicit force synthesis
- user cancellation with partial delivery
- `graph_no_progress` 且已有实际 research wave

Emergency 才允许跳过 required pending / running research。若 trusted evidence
count 为 0，则禁止调用 LLM 生成事实性答案，改为 deterministic no-evidence partial。

Normal 模式允许消费“失败 Worker 返回的部分可信证据”，前提是：

- required research 已全部 terminal；
- trusted evidence count > 0；
- Progress 没有 blocking gap。

Runner 的证据计数取 Graph state 与 CitationManager 两者较大值。该计数只作为
“是否存在可信证据”的下限判断，不用于精确去重统计；它确保 CitationManager
暂未注册的 EvidenceStore 引用不会被覆盖为 0。

## Evidence Boundary

Evidence 只能来自：

- retrieval tool
- EvidenceStore span
- file / db / kb 原始输入
- 已存在的可信外部证据

以下 Derived Output 不得注册为 EvidenceSource：

- `summarize`
- `generate_markdown`
- `convert_pdf`
- `finalize`

## Terminal Trace

终止信息必须在 `run.completed / run.failed / run_summary` 事件 attributes 顶层输出：

- `termination`
- `termination_status`
- `termination_reason`
- `termination_stage`

这些控制面字段列入 privacy sanitizer 永久保留，不依赖巨大的 `metadata` dict。

## Span Projection

Tree projection 的缓存有效性必须同时校验：

- event count
- expected span count（由事件中的 `span_id / event_id` 计算）

因此只要事件里有 span identity，投影不可能返回 0 spans。

## UI 语义

进度 100% 只表示生命周期 terminal，不表示全部阶段成功。Partial 显示：

- `运行结束 · 部分完成`
- `Quality Failed`
- `Research Skipped / 未执行`

## 回归测试

1. zero workers + missing coverage → verdict 必须 gap。
2. coverage gap 必须进入 `open_gap_ids`。
3. all optional research plan 必须被 Validator 拒绝。
4. normal enough 不得跳过 required research。
5. trusted evidence = 0 时 normal synthesis 禁止运行。
6. synthesis 长输出不得增加 CitationManager sources。
7. metadata 超过 24 keys 时 terminal event 仍保留 termination reason。
8. span projection 缓存 span count 错误时必须重建。
9. `facts + sources` 命中核心维度时不能被判 missing。
10. emergency 且 trusted evidence = 0 时不得调用 LLM。
11. normal early-stop 不得写 `replan_exhausted`。
