# Research Agent Harness Architecture

This document is the repository architecture authority. The production workflow is an eight-node, Brief-driven research graph. Legacy semantic planning and coverage modules do not control it.

## Position

The system is a production-oriented Deep Research Agent Harness. The Brief and Supervisor own research semantics; a deterministic runtime owns cost, scheduling, evidence admission, termination, and observability.

```text
User Query
  ↓
StructuredResearchBrief
  ↓
Supervisor research action
  ↓
Budget admission
  ↓
Isolated Researchers
  ↓
Incremental evidence / claim / finding ingest
  ↓
Criterion-based Coverage Judgement
  ├─ gap → Supervisor
  └─ enough / low ROI / budget stop
        ↓
      Grounded Synthesis
      ↓
      Quality Gate
      ↓
    Cited Final / Partial / Failed
```

## Eight-Node Runtime

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

The atomic-fact fast path is not a second workflow. The `brief` node derives strict eligibility and sends eligible facts directly to `researcher`; every other query enters the Supervisor loop.

## Authority Model

| Concern | Authority | Non-authority |
|---|---|---|
| User intent and success criteria | `StructuredResearchBrief` | raw query, planner, TaskShape |
| What to research next | Supervisor | Worker, ControlPolicy, planner |
| Machine task identity | Runtime identity module | Supervisor LLM |
| Whether a request can execute | Budget admission and RuntimePolicy | Supervisor LLM |
| Evidence admission | Evidence admission policy | Worker payload |
| Whether coverage improved | Coverage Judge + Brief + evidence delta | worker count or finding count |
| Final delivery | Citation and grounding gates | synthesis confidence |
| Run history and causality | Recorder, journal, RunStore | frontend state |

## Supervisor and Task Identity

The Supervisor may return only:

- `CONDUCT_RESEARCH`
- `COMPLETE`

A research request declares its objective, target criteria, target gaps, expected evidence, effort, and worker call limits. It never supplies a machine `task_id`.

The runtime derives:

- a semantic fingerprint from the objective, target criteria, and target gaps;
- an execution task ID from wave ID plus fingerprint;
- a stable WorkerResult ID.

Non-retry requests with an already-executed semantic fingerprint are rejected. A retry keeps its attempt identity and is separated from genuinely new research.

## Budget Admission

Budget admission runs before dispatch. It approves, defers, or denies each Supervisor request using:

- remaining research tokens;
- remaining LLM calls;
- remaining research time;
- actual approved wave size;
- semantic novelty;
- configured worker slots.

Research cannot consume the synthesis or quality reserves. When research tokens are exhausted, the runtime forces synthesis instead of launching more workers.

## Workers and Ingest

Workers are bounded leaf researchers. They do not run a second deep-research loop. On timeout or budget stop, artifact-backed evidence is salvaged and the worker becomes a partial result while preserving its exact failure reason.

`ingest_findings` processes only unprocessed WorkerResults for the current wave. Evidence, claims, conflicts, resolutions, findings, and search-query fingerprints are deterministic and idempotent. Replaying a WorkerResult does not duplicate records or findings.

## Coverage

Coverage is judged against Brief key questions and success criteria. Each criterion records supported claim IDs, evidence IDs, missing information, conflicts, and confidence. A criterion is supported only when its required independent evidence is present.

Coverage is monotonic:

- no evidence delta means `gap` cannot become `sufficient`;
- judge failure is fail-closed;
- finding count and worker completion are not progress;
- a closed gap must be traceable to new evidence, a supported claim, criterion closure, or conflict resolution.

## Synthesis and Partial Delivery

Synthesis reads a Brief-native context built from evidence digests, findings, claims, worker limitations, and coverage gaps. It does not read legacy coverage state.

If synthesis tokens are low or the provider fails while usable evidence exists, the runtime renders a deterministic user-readable partial result. Partial content:

- contains recovered facts and evidence links;
- discloses unresolved questions and execution limits;
- never leaks internal task, finding, claim, evidence, gap, or coverage IDs;
- is never empty when usable evidence exists.

## Termination

Termination separates:

- runtime status: `finished | cancelled | crashed`;
- outcome: `success | partial | failed | cancelled`;
- reason: for example `COVERAGE_SUFFICIENT`, `BUDGET_EXHAUSTED`, `NO_USABLE_EVIDENCE`, `MARGINAL_GAIN_LOW`, or `QUALITY_REJECTED`.

Quality failure is never reported as completed research. Budget exhaustion with evidence is a partial result, not an empty failure.

## State and Observability

`ResearchState` is the workflow truth and is checkpointed through LangGraph. It contains the Brief, Supervisor decisions, task state, evidence, claims, findings, coverage judgement, budgets, and terminal projection. Legacy semantic state and semantic wave history are not part of the runtime.

The trace has exactly one `research.run` root. Worker, evidence, coverage, synthesis, quality, and terminal events preserve lineage. Trace Integrity fails when required stages, progress, root spans, lineage, or terminal semantics are missing.

## Evaluation

Regression covers Brief, Coverage, Supervisor, Evidence, capability scenarios, structural scenarios, production fault injection, and release smoke. See [EVALUATION.md](./EVALUATION.md) and [architecture/research-runtime-stabilization-result.md](./architecture/research-runtime-stabilization-result.md).
