# Semantic Simplification Result

## Scope

- Baseline commit: `c0535c097e16f3c960fdb4a85c19e4caf1a54694`
- Baseline tests: `444 passed`
- Production cutover target: Brief + Supervisor + thin deterministic RuntimePolicy

## Before / After Graph

### Before

```text
compile_spec
spec_gate
clarify
plan
plan_validate
dispatch
research_worker
dispatch_barrier
ingest_semantics
assess
gap_fill
expand_plan
replan
retry
synthesize
quality_gate
repair_synthesis
finalize
```

18 named nodes.

### After

```text
brief
supervisor
researcher
ingest_findings
coverage_judge
synthesize
quality_gate
finalize
```

8 named nodes.

## Authority Diff

| Metric | Before | After |
|---|---:|---:|
| Graph nodes | 18 | 8 |
| Canonical semantic actions | 3 (`GAP_FILL`, `EXPAND_PLAN`, `REPLAN`) | 0 |
| Semantic authorities | 7 | 2 (`Brief`, `Supervisor`) |
| Runtime authority | split across control modules | 1 (`RuntimePolicy`) |

Before, user intent was interpreted by Mode Router, TaskShape Router, ResearchSpec shape rules, Coverage Compiler, Planner, Progress Assessor / SemanticGap Builder, and ControlPolicy. After, only the Brief and Supervisor interpret open-ended intent.

## Deleted Production Modules

- `app/research/routing/task_shape.py`
- `app/research/control/policy.py`
- `app/research/planning/replan.py`
- `app/research/planning/gap_fill.py`
- `app/research/planning/expansion.py`
- `app/agent/harness/guardrails.py`
- Old runner nodes and bypasses:
  - `_execute_simple_fact_fast_path`
  - `node_compile_spec`
  - `node_spec_gate`
  - `node_clarify`
  - `node_plan`
  - `node_plan_validate`
  - `node_dispatch`
  - `node_retry`
  - `node_ingest_semantics`
  - `node_assess`
  - `node_gap_fill`
  - `node_expand_plan`
  - `node_replan`
  - `node_repair_synthesis`

## Deleted Tests And Data

- TaskShape router tests
- old Replan / GapFill / ExpandPlan control-plane tests
- old dispatch/retry graph-node tests
- old planner-runtime integration tests
- `planner_v2.jsonl`, `progress_v1.jsonl`, `replan_v1.jsonl`
- old Progress and Replan component graders
- outdated architecture ADRs and hard-ceiling design document

## New Canonical Modules

- `app/research/brief/` — user-intent authority
- `app/research/supervisor/` — research-strategy authority
- `app/research/findings/` — evidence-backed finding compression
- `app/research/coverage/judge.py` — Brief-aligned coverage judgement
- `app/research/control/runtime_policy.py` — deterministic execution policy
- `app/research/runtime/legacy.py` — explicit legacy role registry

## Retained Production Capabilities

- Worker isolation and parallel fan-out safety
- Budget manager, worker leases, timeout, and deterministic retry
- Typed task lifecycle and stable IDs
- Checkpoint / resume
- Evidence admission and claim/evidence binding
- Citation management and grounding checks
- Context compression
- Partial delivery with explicit limitations
- Trace / observability / eval harness

## Deprecated Or Projection Modules

`app/research/runtime/legacy.py` is the explicit registry:

```text
structured_brief = canonical
supervisor       = canonical
runtime_policy   = canonical
research_spec    = projection
coverage_contract = projection
coverage_state   = projection
semantic_gaps    = projection
```

Projection modules do not control production workflow. They preserve legacy semantic ingest, replay, and deterministic evaluation until each caller is fully migrated. `task_shape`, `gap_fill`, `expand_plan`, and `replan` are deleted rather than retained as deprecated authorities.

## Runtime Semantics

- Fast path is derived from the Brief and branches after `brief`; it does not bypass the graph.
- Supervisor retries reset tasks to `pending`; researchers remain the only execution node.
- Supervisor iteration has a hard limit. At the limit, usable evidence produces `partial`; no evidence produces `failed`.
- Budget `cap_tool_calls()` can only lower a tool-call ceiling.
- Worker leases use `session.active_wave_size`, so budgets follow the actual wave.
- Coverage Judge returns actionable missing questions; if the LLM says insufficient but provides no gap, Brief key questions are used.
- Synthesis failure with usable evidence ends as `partial`, never fake `success`.
- Brief deliverable requirements project into `LoopState.intent`, so PDF requests can produce PDF output.

## Observability

Canonical events now include:

```text
brief.compiled
topology.decided
supervisor.started
supervisor.decided
plan.created
progress.assessed
coverage.assessed
finding.compressed
quality.assessed
run.terminated
```

`summarize_trace()` projects:

- Brief and topology;
- Supervisor decisions;
- compressed findings;
- Coverage Judgements;
- progress/actionable gaps;
- workers, evidence, synthesis, lineage, and quality.

Trace Viewer now exposes `Understanding` and `Supervisor / Coverage` panels instead of the old `Progress / Replan` panel.

The final run summary exposes `supervisor_iterations`. Supervisor iteration limit and all terminal control decisions emit `control.decided`, so a bounded failure no longer fails Trace Integrity with `missing_control_decision_event`.

## Eval Diff

Component eval changed from:

```text
Planner / Progress / Replan / Evidence
```

to:

```text
Brief / Coverage / Supervisor / Evidence
```

New datasets:

- `brief_v1.jsonl`
- `coverage_v1.jsonl`
- `supervisor_v1.jsonl`

Live ablation names changed from `no_replan` to `single_iteration`. Metrics now report supervisor iteration and recovery instead of replan trigger/recovery.

## Validation

Final validation is recorded below after the cutover run.

| Check | Result |
|---|---|
| Python type check | PASS (`mypy`: 18 source files, no issues) |
| Python tests | PASS (408 passed) |
| Eval regression dry-run | PASS (60/60, gate 100.0%) |
| Frontend projection self-check | PASS |
| Frontend production build | PASS |
| Release smoke | PASS (L3 10 passed; 6 release queries PASS) |
| Deterministic PDF E2E | PASS (`tests/test_e2e_control_plane_pdf.py`) |
| Live acceptance queries | BLOCKED by local environment |

Live acceptance was attempted through `app.agent.main_agent`. Search and tracing worked, but this checkout's `.env` intentionally contains only Bocha search configuration and no `OPENAI_API_KEY`. Without the LLM credential, open-ended workers fail and converge to `failed`; after the control-event fix, the bounded failure path reports `Trace Integrity: PASS`. This is an environment configuration gap, not a workflow regression. Configure the LLM credential in the deployment environment and rerun the acceptance list.

## Benchmark / Cost / Latency Delta

No comparable pre/post benchmark was run in this cutover. Baseline benchmark, token, cost, and latency data were not recorded at commit `c0535c0`, so this report does not invent a delta.

Required follow-up:

1. run the same live fixture set on baseline and new architecture;
2. record official BrowseComp-Plus accuracy separately from retrieval metrics;
3. report quality, citation, tokens, tool calls, P50/P95, and supervisor iterations.

## Known Limitations

- `max_replan_count` remains the configuration storage name for the supervisor iteration limit.
- The deterministic Brief compiler is conservative and may produce shallow key questions without an LLM.
- Coverage Judge uses a coarse supported-finding count in fallback mode.
- Follow-up Brief delta handling is not yet a full conversation-state model.
- Public benchmark and cost/latency comparison remain pending.

## Remaining Non-blocking Debt

- Rename `max_replan_count` to `max_supervisor_iterations` across config and persistence.
- Add a true `no_coverage_judge` ablation distinct from `single_iteration`.
- Move capability regression fully from legacy projection adapters to Brief-native cases.
- Remove remaining legacy projection modules after all replay and eval callers migrate.
- Add a supervised benchmark report with fixed model, corpus, and search backend.
