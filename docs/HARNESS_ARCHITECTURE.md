# Semantic Research StateGraph Runtime

The research StateGraph is the only product control path. It deliberately keeps semantic authority in two LLM-facing components and hard execution authority in `RuntimePolicy`.

```text
StructuredResearchBrief = user-intent authority
Supervisor             = research-strategy authority
RuntimePolicy          = budget / retry / safety / terminal authority
```

## Nodes

```text
START
  → brief
      → researcher                # simple-fact fast path
      → supervisor                 # open research loop
          → researcher × N
          → ingest_findings
          → coverage_judge
              → supervisor         # actionable coverage gap
              → synthesize         # enough evidence
      → synthesize
      → quality_gate
          → synthesize             # bounded repairable retry
          → finalize
  → END
```

Named graph nodes:

1. `brief`
2. `supervisor`
3. `researcher`
4. `ingest_findings`
5. `coverage_judge`
6. `synthesize`
7. `quality_gate`
8. `finalize`

## Authority Boundaries

- `brief` compiles the query, conversation delta, entities, key questions, source constraints, freshness, and deliverable into a `StructuredResearchBrief`.
- Fast-path eligibility is derived from the Brief. A simple fact does not bypass the main graph; it branches after `brief`.
- `supervisor` turns the Brief and the latest Coverage Judgement into `CONDUCT_RESEARCH` or `COMPLETE` actions. It owns task granularity and research strategy.
- `researcher` executes one isolated task attempt, immediately ingests its own typed result, and returns the idempotent ingestion update. It cannot declare coverage complete.
- `ingest_findings` performs the final wave reconciliation and processes only WorkerResults that were not already ingested.
- `coverage_judge` compares findings with Brief requirements and emits actionable missing questions, weak claims, and conflicts.
- `synthesize` consumes Brief, findings, admitted evidence, claims, conflicts, and limitations. It cannot search or mutate coverage.
- `quality_gate` validates citation, grounding, coverage, and final content. It can only request a bounded synthesis retry or finalize.
- `finalize` is the only terminal transition and records `success`, `partial`, `failed`, or `cancelled`.

## Runtime Policy

`RuntimePolicy` is deterministic and intentionally contains no semantic actions. It decides only:

- dispatch, retry, synthesize, partial delivery, wait, or stop;
- iteration, worker, tool-call, token, and time budgets;
- whether terminal metadata is valid;
- whether partial delivery is allowed because usable evidence exists.

The configuration key `max_replan_count` is retained only as the storage name for the supervisor iteration limit. It does not create a semantic Replan action.

## Worker Contract

Workers own execution, not research completion. Each result is scoped to:

- one task ID and plan version;
- one attempt;
- one dispatch wave;
- its own raw payload and evidence IDs.

Workers can return findings, facts, candidates, sources, evidence IDs, and confidence. They cannot mutate coverage, mark research complete, or choose the next strategy.

Worker leases split the remaining research capacity by the actual approved wave size and enforce the task-level LLM-call ceiling. Early fan-in does not cancel required workers; it can only skip optional workers after partial-wave Coverage is sufficient.

## Stop Semantics

| Condition | Meaning |
|---|---|
| `success` | coverage, synthesis, citation, and grounding gates pass |
| `partial` | useful evidence exists, but coverage or quality remains incomplete; output must disclose the limitation |
| `failed` | no usable semantic result or a required gate fails |
| `cancelled` | user or policy cancellation |

Budget stop, timeout, and worker `STOPPED` do not automatically map to `failed`. RuntimePolicy chooses partial delivery or failure from actual evidence state.

## Runtime Files

- `app/research/brief/` — user-intent authority
- `app/research/supervisor/` — research-strategy authority
- `app/research/findings/` — compressed, evidence-backed findings
- `app/research/coverage/judge.py` — coverage judgement
- `app/research/control/runtime_policy.py` — deterministic execution policy
- `app/research/control/transitions.py` — phase transitions
- `app/research/domain/termination.py` — terminal-state policy
- `app/research/runtime/graph.py` — eight-node graph topology
- `app/research/runtime/runner.py` — production graph nodes and telemetry
- `app/research/runtime/state.py` — canonical state schema
