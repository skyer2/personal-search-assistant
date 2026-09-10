# Research Runtime Stabilization Result

## Scope

This change stabilizes the eight-node research runtime without adding a second planner or semantic state machine. It makes task identity deterministic, turns budgets into dispatch constraints, makes coverage evidence-delta driven, makes ingestion idempotent, protects synthesis capacity, and separates partial delivery from failure.

## Before / After

### Before

```text
Supervisor emits 4 broad tasks
  ↓
all workers dispatch and consume research budget
  ↓
Coverage remains gap
  ↓
second wave repeats the same semantic tasks
  ↓
coverage becomes sufficient without new evidence
  ↓
synthesis hits budget_tokens
  ↓
debug-style semantic digest
  ↓
Quality fail but research_completed
  ↓
Trace missing root / progress
```

### After

```text
Brief
  ↓
Supervisor proposes focused research
  ↓
runtime derives semantic fingerprints and machine IDs
  ↓
Budget admission approves only affordable, novel tasks
  ↓
bounded workers return partial evidence on budget stop
  ↓
incremental ingest deduplicates evidence / claims / findings
  ↓
criterion coverage requires an evidence delta
  ↓
enough / low ROI / budget stop
  ↓
LLM synthesis or deterministic partial
  ↓
quality gate
  ↓
success | partial | failed with exact reason
```

## Task Identity

- Supervisor cannot control machine `task_id`.
- `semantic_fingerprint(objective, target_gaps, target_criteria)` is stable and normalized.
- `task_{wave_id}_{fingerprint}` is the execution identity.
- `worker_result_id` is derived from task, wave, attempt, summary, and evidence refs.
- Non-retry duplicate semantic tasks are denied with `duplicate_semantic_task`.

## Budget Admission

`admit_dispatch` is the only dispatch gate. It checks:

- research token cap;
- run LLM-call cap;
- research time reserve;
- actual approved wave size;
- configured and active task slots;
- semantic novelty.

Research, supervisor, synthesis, and quality each expose a dedicated remaining-budget API. Research cannot spend the synthesis or quality reserve. Worker leases split capacity by the actual approved wave size, not the configured maximum.

## Coverage Monotonicity

Coverage now emits `CriterionSupport` and `CoverageDelta` for every Brief criterion:

- supported claim IDs;
- evidence IDs;
- missing reason;
- conflicts;
- confidence.

`gap → sufficient` requires new evidence, a supported claim, a closed criterion, or a resolved conflict. LLM judge failure returns the deterministic fail-closed judgement and cannot improve coverage.

## Incremental Ingest

`ingest_new_worker_results`:

- ignores already processed WorkerResult IDs;
- only processes the current dispatch wave;
- deterministically admits evidence;
- extracts and binds claims;
- detects and resolves conflicts;
- compresses findings;
- tracks duplicate search-query fingerprints and a low-ROI value signal.

Replay produces no additional evidence, claim, finding, or search fingerprint.

## Partial and Termination

- Worker budget stop with evidence becomes stopped/partial and keeps `research_token_cap`.
- Low synthesis budget skips the LLM and renders deterministic partial content.
- Provider synthesis failure with usable evidence renders deterministic partial content.
- No usable evidence terminates as failed with `NO_USABLE_EVIDENCE`.
- Runtime status, outcome, and reason are separate.
- Quality failure never marks research as completed.
- User-facing content is scrubbed of internal IDs.

## Golden Query Comparison

Golden query:

```text
你觉得当下国内 AI 初创有潜力值得加入的公司有哪些？为什么？
```

| Metric | Historical failure | Stabilized deterministic release smoke |
|---|---:|---:|
| Total tokens | 282,000 | 7,995 average per run |
| LLM calls | 30 | 8 |
| Worker count | 4 + repeated second wave | 3 |
| Wall time | about 15 minutes | 361 ms average |
| Supervisor iterations | repeated broad waves | 2 |
| Coverage transition | gap → sufficient without delta | gap; no unsupported transition |
| Final outcome | failed / empty-style digest | non-empty partial |
| Trace integrity | missing root / progress | PASS |
| Root spans | missing or ambiguous | exactly 1 |

Release smoke ran the golden query three times: all runs were non-empty `partial`, all had 3 workers, 3 evidence items, 18 lineage edges, one root span, and Trace Integrity PASS.

## New Invariants and E2E

`tests/test_research_runtime_stabilization.py` covers SDD cases A–H:

- worker budget stop with recovered evidence;
- coverage judge failure stays fail-closed;
- duplicate Supervisor task rejection;
- low synthesis budget skips LLM synthesis;
- synthesis failure with evidence yields readable partial;
- no evidence yields failed / `NO_USABLE_EVIDENCE`;
- WorkerResult replay is idempotent;
- actual wave size splits worker leases correctly.

The golden E2E also asserts:

- unique task IDs;
- no internal IDs in final content;
- one `research.run` root;
- gap → sufficient transitions require intervening evidence;
- task/evidence/synthesis lineage exists;
- Trace Integrity PASS.

## Removed Legacy Hot Path

Deleted:

- `app/research/runtime/semantic_ingest.py`
- `app/research/coverage/gaps.py`
- `app/research/assessment/marginal_gain.py`
- `app/research/planning/marginal_gain.py`
- `app/research/runtime/legacy.py`
- their obsolete tests and historical architecture documents.

`ResearchState` no longer carries `research_spec`, `coverage_contract`, `coverage_state`, `semantic_gaps`, `semantic_wave_gains`, or old marginal-gain state. `ResearchSpec` remains only in non-production evaluation/planning adapters.

## Validation

Completed on 2026-09-10:

- `python -m mypy`: PASS.
- `python -m pytest -q tests -p no:cacheprovider`: 407 passed.
- `pnpm --dir frontend test:projection`: PASS.
- `pnpm --dir frontend build`: PASS.
- `python tests/eval/run_eval.py --dry-run --fail-on-regression`: 60/60 passed.
- `python scripts/release_smoke.py --q1-runs 3`: PASS.

## Known Limitations and Remaining Debt

- Deterministic release smoke proves runtime correctness, not live search-provider answer quality.
- Coverage fallback uses conservative keyword matching; it can under-accept semantically equivalent phrasing, but never fail open.
- Supervisor and Coverage structured output still use provider JSON plus strict parsing; native structured output can be adopted later without changing authorities.
- Live nightly and external benchmark suites were not executed in this local validation.
