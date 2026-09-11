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

## Atomic Fact Fast Path

An atomic fact is eligible only when the Brief identifies one subject, one question, no clarification, a brief text deliverable, and point-in-time freshness. The fast path performs one bounded search, admits evidence with `SIMPLE_FACT_EVIDENCE_POLICY`, extracts a structured answer bound to supporting source IDs, and either delivers a cited answer or explicitly reports that the fact cannot be reliably confirmed.

Insufficient atomic-fact evidence is terminal. It never enters an open-research Supervisor loop and never retries synthesis with the same evidence.

## Authority Model

| Concern | Authority | Non-authority |
|---|---|---|
| User intent and success criteria | `StructuredResearchBrief` | raw query, planner, TaskShape |
| What to research next | Supervisor | Worker, ControlPolicy, planner |
| Machine task identity | Runtime identity module | Supervisor LLM |
| Whether a request can execute | Budget admission and RuntimePolicy | Supervisor LLM |
| Structured LLM call envelope | `StructuredLLMGateway` | ad-hoc JSON parsing in agents |
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

Worker leases use the actual approved wave size, not the configured maximum. Admission and execution share the same `TaskBudgetProfile`. The default profiles are:

| Effort | Token ceiling | LLM calls | Search queries | Fetch sources | Tool invocations | Output tokens / call |
|---|---:|---:|---:|---:|---:|---:|
| small | 12,000 | 3 | 3 | 6 | 4 | 1,500 |
| medium | 24,000 | 4 | 4 | 8 | 6 | 2,000 |
| large | 40,000 | 6 | 6 | 12 | 8 | 2,500 |

Each call receives `max_output_tokens_per_call`; a small task cannot consume the full research token ceiling on its first LLM call.

The three retrieval resources are independent. `batch_search(N)` consumes `N` search queries and one logical tool invocation, but no fetch budget. `batch_fetch(N)` consumes `N` fetched sources and one logical tool invocation, but no search budget. A worker can therefore execute the intended `batch_search → batch_fetch → structured result` sequence without a search quota accidentally blocking all fetches.

## Workers and Ingest

Workers are bounded leaf researchers. They do not run a second deep-research loop. On timeout or budget stop, artifact-backed evidence is salvaged and the worker becomes a partial result while preserving its exact failure reason.

Each `researcher` result is ingested immediately with the same deterministic ingestion contract. The graph still waits for required workers at the fan-in boundary, but evidence, claims, and findings become available as each worker returns. `ingest_findings` then processes only WorkerResults that were not already ingested; replay never duplicates records. If partial-wave coverage already satisfies the Brief, only optional or speculative workers may be skipped. Required workers are never cancelled for latency.

## Coverage

Coverage is judged against Brief key questions and success criteria. Each criterion records supported claim IDs, evidence IDs, missing information, conflicts, and confidence. A criterion is supported only when its required independent evidence is present.

Coverage is monotonic:

- no evidence delta means `gap` cannot become `sufficient`;
- judge failure is fail-closed;
- finding count and worker completion are not progress;
- a closed gap must be traceable to new evidence, a supported claim, criterion closure, or conflict resolution.

## Synthesis and Partial Delivery

Synthesis reads a Brief-native context built from evidence digests, findings, claims, worker limitations, and coverage gaps. It does not read legacy coverage state.

The synthesis model is invoked directly through the LLM gateway with `ainvoke`. A normal synthesis attempt is capped at 60 seconds. Only provider rate limiting, provider unavailability, and context-length failures may retry once in degraded mode with a 30-second cap. Timeout, auth, bad request, content filter, and budget failures do not retry.

If synthesis tokens are low or the provider fails while usable evidence exists, the runtime renders a deterministic user-readable partial result. Partial content:

- contains recovered facts and evidence links;
- discloses unresolved questions and execution limits;
- never leaks internal task, finding, claim, evidence, gap, or coverage IDs;
- never renders internal runtime codes such as token, search, fetch, tool, timeout, or synthesis failure reasons as research facts;
- is never empty when usable evidence exists.

## Termination

Termination separates:

- runtime status: `finished | cancelled | crashed`;
- outcome: `success | partial | failed | cancelled`;
- reason: for example `COVERAGE_SUFFICIENT`, `BUDGET_EXHAUSTED`, `NO_USABLE_EVIDENCE`, `MARGINAL_GAIN_LOW`, or `QUALITY_REJECTED`.

Quality failure is never reported as completed research. Budget exhaustion with evidence is a partial result, not an empty failure.

## Delivery

Chat is the primary delivery surface. A report request may additionally produce `.md`, `.pdf`, `.xlsx`, or `.docx`. The `FILES` panel is run-scoped and excludes `run_summary.json`, `evidence.json`, working notes, raw web artifacts, checkpoints, and other internal execution files. Atomic-fact answers do not create a file deliverable.

## State and Observability

`ResearchState` is the workflow truth and is checkpointed through LangGraph. It contains the Brief, Supervisor decisions, task state, evidence, claims, findings, coverage judgement, budgets, and terminal projection. Legacy semantic state and semantic wave history are not part of the runtime.

The trace has exactly one `research.run` root. Worker, evidence, coverage, synthesis, quality, and terminal events preserve lineage. Trace Integrity fails when required stages, progress, root spans, lineage, or terminal semantics are missing.

Budget denials emit one canonical `budget.denied` event with scope, resource, reason, used, limit, and worker/run snapshots. Structured Brief/Supervisor fallbacks emit `semantic.fallback` with error type, message, category, model, and schema. Worker terminal events carry the complete worker budget snapshot, and LLM events carry phase, task, call index, token estimate, duration, and remaining worker limits.

Root spans never inherit Worker task, plan, or attempt context. Lineage edges are derived from explicit input/output references; matching IDs only fills in the missing side of an edge and never creates duplicate edges.

The frontend is a run-scoped projection only. Events, files, progress, worker statistics, tool statistics, and source statistics are filtered by the current `run_id`; late events from an old run cannot update the new turn.

## Evaluation

Regression covers Brief, Coverage, Supervisor, Evidence, capability scenarios, structural scenarios, production fault injection, and release smoke. See [EVALUATION.md](./EVALUATION.md).
