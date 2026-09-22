# Research Agent Harness Architecture

This document is the repository architecture authority. The production workflow is an eight-node, Brief-driven research graph. Legacy semantic planning and coverage modules do not control it.

> **v6 truth boundary:** synthesis and delivery receive only admitted claims
> with explicit question lineage.  Artifact salvage remains evidence-only;
> it cannot become a finding or answer without Claim Admission.  The final
> renderer receives a typed AnswerViewModel with point-owned citations.
> Primary synthesis is bounded to 90 seconds; compact retry and report repair
> are bounded to 30 seconds each and remain explicit degraded diagnostics.
>
> **v5 delivery boundary:** synthesis receives normalized Insight Cards,
> Forecast Cards and deterministic Claim–Evidence bindings, not raw Findings
> or search snippets. See [DEEP_RESEARCH_INSIGHT_SYNTHESIS_V5.md](DEEP_RESEARCH_INSIGHT_SYNTHESIS_V5.md).

## Position

The system is a production-oriented Deep Research Agent Harness. The Brief and Supervisor own research semantics; a deterministic runtime owns cost, scheduling, evidence admission, termination, and observability.

```text
User Query
  ↓
StructuredResearchBrief
  ↓
Deterministic bounded plan (every key question mapped)
  ↓
Budget admission
  ↓
Isolated Researchers
  ↓
Incremental evidence / claim / finding ingest
  ↓
Criterion-based Coverage Judgement
  ├─ one blocking gap → targeted repair (one wave, reserved budget)
  └─ enough / low ROI / budget stop
        ↓
      Finding → Signal → Mechanism → Grounded Synthesis
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
| User intent, key questions, and final success criteria | `StructuredResearchBrief` | raw query, planner, TaskShape |
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

`COMPLETE` is not authority to suppress a blocking `CoverageGap`: the runtime
deterministically changes that decision to one bounded targeted repair whenever
the gap is still actionable.  A missing `max_replan_count` in a legacy state
snapshot uses the configured one-repair default; only an explicit zero disables
repair. The deterministic initial plan does not consume that one repair wave.

A research request declares its objective, target criterion, `CoverageGap` ID, missing evidence types, blocking conflict IDs, expected evidence, effort, and worker call limits. It never supplies a machine `task_id`. The deterministic fallback consumes `CoverageJudgement.gaps` directly; it never binds a criterion by array position.

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

Initial research cannot borrow the repair, report or verification reserve. The default stage allocation is research 60%, targeted repair 15%, report 15% and verification 10%. The one repair wave can consume its protected 15% only after a blocking gap; report and verification capacity remain protected. When the permitted research stage is exhausted, the runtime forces synthesis instead of launching more workers.

Worker leases use the actual approved wave size, not the configured maximum. Admission and execution share the same `TaskBudgetProfile`. The default profiles are:

| Effort | Token ceiling | LLM calls | Search queries | Fetch sources | Tool invocations | Output tokens / call |
|---|---:|---:|---:|---:|---:|---:|
| small | 40,000 | 10 | 6 | 10 | 10 | 2,500 |
| medium | 80,000 | 16 | 10 | 16 | 16 | 3,500 |
| large | 120,000 | 24 | 16 | 24 | 24 | 5,000 |

The current baseline intentionally prioritizes completeness over cost: the Run ceiling is 500K tokens, 120 LLM calls, 300 tool calls, and 30 minutes. These are runaway-protection limits, not normal stop conditions. Supervisor requests do not carry duplicate per-worker budget fields; admission, plan metadata, and worker leases all resolve from the same `TaskBudgetProfile`.

A worker lease is an admission record, not a token reservation. Its `token_ceiling` is the worker lifetime limit; run-level reserved tokens include only non-worker in-flight requests and worker in-flight LLM requests. Creating two 80K medium workers therefore reserves zero tokens until each model call is actually authorized. Committing an actual usage smaller than its estimate refunds the difference atomically. Worker token and call ceilings remain enforced on every request.

`deep_debug` is an explicit integration profile. It keeps the agent graph and per-worker token ceilings unchanged, but raises the actual RunBudgetManager ceiling to 1.2M tokens, 240 LLM calls, 600 tool calls, 60 minutes, six Supervisor iterations, and two parallel workers. The route and profile are resolved before this manager and RunSession are created. If a pre-existing manager already has usage, a later profile mismatch is reported as a warning instead of silently mutating live budget authority. This profile never changes the production default.

Its worker stage may use a 300-second integration limit, while primary Synthesis is capped at 180 seconds by default. An explicitly configured `HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC` takes precedence when it is lower; the current real `.env` uses 120 seconds. The compact retry keeps its own shorter timeout. Stage limits bound failures and do not serve as a performance-success metric.

Each call receives `max_output_tokens_per_call`; a small task cannot consume the full research token ceiling on its first LLM call.

The three retrieval resources are independent. `batch_search(N)` consumes `N` search queries and one logical tool invocation, but no fetch budget. `batch_fetch(N)` consumes `N` fetched sources and one logical tool invocation, but no search budget. A worker can therefore execute the intended `batch_search → batch_fetch → structured result` sequence without a search quota accidentally blocking all fetches. Search semantic success requires at least one result with a valid `http(s)` URL; an HTTP success with empty or URL-less results returns `search_empty` and is not counted as a successful query.

Tool authorization has one source. Worker profiles define the tool registry; planning, prompt context, runtime authorization, and worker-profile selection all consume `worker_tools_for_step()`. A research worker therefore sees the same batch search/fetch and JIT read tools that the runtime authorizes. Source constraints still remove web or file tools before the plan is emitted.

## Workers and Ingest

Workers are bounded leaf researchers. They do not run a second deep-research loop and never judge global coverage. A worker stops normally with `local_evidence_sufficient`, `no_more_useful_evidence`, `soft_budget_finalize`, or `soft_deadline_finalize`.

Finalization is armed before an LLM request when projected usage would risk the token ceiling, when final LLM calls must be preserved, or when the worker reaches a soft wall deadline. The token threshold is adaptive between 40% and 80%. The wall deadline reserves 20–45 seconds, giving a 150-second worker its first 120 seconds for research and a 300-second worker its first 255 seconds. With zero retrieval and zero evidence, the first retrieval is still authorized after the soft deadline unless a hard deadline, hard budget, or cancellation has fired. Once evidence exists, the current request receives a Finalization Mode instruction, retrieval is disabled, and remaining capacity is preserved for the structured WorkerResult. Hard token, call, and timeout caps remain runaway-protection ceilings. On a true timeout or hard budget stop, artifact-backed evidence is salvaged and the exact failure reason is preserved.

Research workers deliver evidence-backed findings, not search logs. The final answer must come from a non-tool assistant message and contain JSON with `summary`, `findings`, `gaps`, `conflicts`, and `stop_reason`. Each finding requires `claim` plus verbatim `evidence_ids` or `artifact_ids`; invented IDs are rejected. A single Finalization-only retry uses a model-visible capability limited to existing artifact/evidence reads; search and fetch tools are removed rather than exposed with zero quotas. Runtime ingestion resolves artifact and locator references to admitted canonical evidence IDs, emits accepted/rejected diagnostics, and allows deterministic compression only as a marked partial fallback when facts and admitted evidence both exist.

Worker lifecycle is classified after finalization from structured validity, accepted findings, admitted evidence, and the terminal reason. A local tool failure is not a worker failure: evidence plus accepted findings and a normal stop is complete; evidence with an abnormal stop is stopped/partial and remains ingestible; only zero evidence and zero accepted findings can become a terminal worker failure. `last_tool_error`, `fail_reason`, and `stop_reason` are separate diagnostics.

Objective evidence metadata is runtime-owned. Search providers, fetch tools, and the tool output contract persist `published_at` on the artifact; ingestion reads that metadata when constructing `EvidenceRecord`. Workers do not guess publication dates. Coverage continues to use its existing freshness algorithm, but no longer loses provider-provided dates merely because the final Worker JSON omitted metadata.

Each `researcher` result is ingested immediately with the same deterministic ingestion contract. The graph still waits for required workers at the fan-in boundary, but evidence, claims, and findings become available as each worker returns. `ingest_findings` then processes only WorkerResults that were not already ingested; replay never duplicates records. If partial-wave coverage already satisfies the Brief, only optional or speculative workers may be skipped. Required workers are never cancelled for latency.

## Coverage

Coverage is judged only against Brief key questions. `success_criteria` belongs to the final Quality Gate. Each criterion records supported claim IDs, evidence IDs, independent source IDs, missing evidence types, unresolved conflicts, and confidence. v3 additionally projects each question as `covered`, `partially_covered`, or `uncovered`, with a blocking bit, direct/high-authority support counts, and Worker-failure binding. A blocking question cannot reach `SUCCESS`; it triggers at most one bounded targeted repair.

A criterion is `supported` only when all of the following hold:

- a claim or finding is explicitly bound to the criterion (lexical overlap alone is at most `partial`);
- evidence exists and the required independent source count is met;
- `primary_required` is satisfied when configured;
- `freshness_required` is satisfied with a usable publication/effective date;
- no blocking unresolved conflict is bound to the criterion.

Every non-supported criterion emits a structured `CoverageGap` with `gap_id`, `criterion_id`, current evidence, missing evidence type, blocking conflict IDs, and priority. `missing` and `recommended_next_questions` remain display/eval projections, not control-plane inputs.

Coverage is monotonic:

- no evidence delta means `gap` cannot become `sufficient`;
- judge failure is fail-closed;
- finding count and worker completion are not progress;
- a closed gap must be traceable to new evidence, a supported claim, criterion closure, or conflict resolution;
- a blocking unresolved conflict always keeps coverage insufficient.

## Synthesis and Partial Delivery

Synthesis reads a deterministic Evidence Pack built from Brief criteria, evidence-backed findings, claims, evidence records, and structured conflict resolutions. The pack is criterion-balanced, deduplicated, quality-ranked, and bounded to 8K input tokens normally and 4K on compact retry, with a 30K hard maximum. It does not read legacy coverage state and cannot search. Selected evidence must resolve to a non-empty Runtime-owned digest; otherwise synthesis is skipped and the runtime records `synthesis_evidence_digest_missing`. v3 requires semantic question-based organization, deduplication, and a signal → mechanism → milestone → uncertainty structure for forecasts. Raw `art-web-*` Artifact IDs are never canonical Evidence IDs. `SUCCESS` requires all blocking questions closed, sufficient source quality, valid citations, and a passing Quality Gate; compact or deterministic recovery remains a visible diagnostic rather than proof of primary-path performance.

Canonical evidence IDs are bound to stable citation numbers during ingestion. Synthesis sees those numbers in its prompt; after generation, the runtime projects the selected evidence-backed findings onto numeric sentences and rejects unknown citation numbers. Internal worker JSON is removed from user-facing Markdown and PDF deliverables.

Conflict reconciliation distinguishes `resolved`, `expected_disagreement`, and `unresolved`. An unresolved conflict bound to a required criterion is blocking. Synthesis must use the specified winner for resolved conflicts, explain scope/context for expected disagreement, disclose uncertainty for non-blocking unresolved conflicts, and force degraded mode for blocking unresolved conflicts. It must never choose a winner for an unresolved conflict.

The synthesis model is invoked directly through the LLM gateway with `ainvoke`. Production deployments can select a faster synthesis-only model with `LLM_SYNTHESIS_MODEL` and cap output with `LLM_SYNTHESIS_MAX_TOKENS`; prompt and output limits vary with deliverable depth. Every attempt records the Evidence Pack size, full prompt and digest size, model/provider, time to first token when available, duration, actual input/output tokens, finish reason, and failure reason. Provider rate limiting, provider unavailability, context-length failures, and synthesis timeout may retry once with the smaller compact Evidence Pack. Auth, bad request, content filter, and budget failures do not retry. `LLM_SYNTHESIS_TIMEOUT_SEC`, `HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC`, and `HARNESS_SYNTHESIS_RETRY_TIMEOUT_SEC` override the defaults only when explicitly configured; the retry timeout follows the selected run profile. A successful compact retry remains a successful delivery but sets `synthesis_degraded=true`, `synthesis_retry_count=1`, and `successful_attempt=2`. It does not count toward primary-path stability, which targets P95 under 120 seconds (under 180 seconds for Deep Debug).

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

Budget denials emit one canonical `budget.denied` event with scope, resource, reason, used, reserved, limit, and worker/run snapshots. Soft finalization emits `budget.decided` with `status=finalize` and is not counted as denial. Structured Brief/Supervisor fallbacks emit `semantic.fallback` with error type, message, category, model, and schema. Worker terminal events carry the complete worker budget snapshot, normal stop reason, and LLM events carry phase, task, call index, token estimate, duration, and remaining worker limits.

Root spans never inherit Worker task, plan, or attempt context. Lineage edges are derived from explicit input/output references; matching IDs only fills in the missing side of an edge and never creates duplicate edges.

The frontend is a run-scoped projection only. Events, files, progress, worker statistics, tool statistics, and source statistics are filtered by the current `run_id`; late events from an old run cannot update the new turn.

## Evaluation

Regression covers Brief, Coverage, Supervisor, Evidence, capability scenarios, structural scenarios, production fault injection, and release smoke. See [EVALUATION.md](./EVALUATION.md).
