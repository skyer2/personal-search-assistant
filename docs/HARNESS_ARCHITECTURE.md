# Contract-Driven Research StateGraph Runtime

The research StateGraph is the only product control path. `ControlPolicy` decides routing; graph edges only execute that decision.

## Nodes

```text
START
  → compile_spec
  → spec_gate
      → clarify → compile_spec
      → plan
  → plan_validate
  → dispatch
      → research_worker × N
      → dispatch_barrier
      → ingest_semantics
      → assess
      → retry / gap_fill / expand_plan / replan
          → plan_validate or dispatch
      → synthesize
      → quality_gate
          → repair_synthesis
          → gap_fill / replan
          → finalize
  → END
```

Named graph nodes:

1. `compile_spec`
2. `spec_gate`
3. `clarify`
4. `plan`
5. `plan_validate`
6. `dispatch`
7. `research_worker`
8. `dispatch_barrier`
9. `ingest_semantics`
10. `assess`
11. `retry`
12. `gap_fill`
13. `expand_plan`
14. `replan`
15. `synthesize`
16. `quality_gate`
17. `repair_synthesis`
18. `finalize`

The node count intentionally exceeds the SDD's approximate 14-node target. See [ADR-004](architecture/adr/ADR-004-stategraph-node-granularity.md): separate plan operators preserve deterministic action budgets, strategy fingerprints, and observable control semantics.

## Phase and Transition Rules

- `transition_update()` is the only phase transition helper.
- `compile_spec` writes `ResearchSpec` and initial `CoverageContract`.
- `spec_gate` blocks empty objectives, blocking ambiguity, and contradicted premises.
- `plan_validate` enforces plan invariants, task granularity, coverage binding, and candidate-set dependency.
- `dispatch_barrier` is a side-effect-free join; one wave enters one semantic ingest.
- `ingest_semantics` is the only worker-result → semantic-state boundary.
- `assess` produces deterministic assessments and calls `ControlPolicy`.
- `quality_gate` may only repair synthesis, perform a gap-bound control action, or finalize.
- `finalize` is the only terminal transition.

## Worker Contract

Workers own execution, not research completion. Each worker result is scoped to:

- one task ID and plan version
- one attempt
- one dispatch wave
- its own task delta and raw payload

Workers can return findings, facts, candidates, sources, evidence IDs, and confidence. They cannot mutate coverage, close gaps, mark research complete, or decide the next control action.

## Candidate Expansion

```text
discovery task
  → CandidateSet materialized
  → ControlPolicy.EXPAND_PLAN
  → expanded CoverageContract
  → candidate × dimension tasks
```

Before expansion, `candidate_set` is a required discovery unit. After expansion, that transient unit is removed. This prevents a permanently unsatisfiable candidate obligation after concrete candidates exist.

## Synthesis Contract

Synthesis consumes semantic digest records:

- spec objective and delivery requirements
- coverage summary
- admitted evidence digests
- claims and conflict resolutions
- explicit limitations

It cannot search, mutate coverage, add claims, or close gaps. A synthesis timeout may trigger a bounded retry or deterministic partial fallback, but never a silent success.

## Stop and Terminal Semantics

| Condition | Meaning |
|---|---|
| `SUCCESS` | all semantic and quality gates pass |
| `PARTIAL` | bounded useful evidence exists, but full coverage or quality is not met |
| `FAILED` | no usable semantic result or a required gate fails |
| `CANCELLED` | user or policy cancellation |

Budget stop, timeout, and worker `STOPPED` do not automatically map to `FAILED`. ControlPolicy chooses synthesis, partial delivery, or failure from semantic state.

## Runtime Files

- `app/research/runtime/graph.py` — graph topology and pure default nodes
- `app/research/runtime/runner.py` — production runtime nodes and telemetry
- `app/research/runtime/state.py` — canonical state schema
- `app/research/runtime/semantic_ingest.py` — semantic admission boundary
- `app/research/control/policy.py` — sole routing authority
