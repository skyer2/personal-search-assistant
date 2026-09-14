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
- Brief and Supervisor use raw ChatModels through `StructuredLLMGateway`; structured output coercion is outside worker prompt logic.
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
- iteration, worker, token, time, search-query, fetch-source, and logical tool-invocation budgets;
- whether terminal metadata is valid;
- whether partial delivery is allowed because usable evidence exists.

The configuration key `max_replan_count` is retained only as the storage name for the supervisor iteration limit. It does not create a semantic Replan action.

## Worker Contract

Workers own execution, not research completion. Each result is scoped to:

- one task ID and plan version;
- one attempt;
- one dispatch wave;
- its own raw payload and evidence IDs.

Workers can return findings, facts, candidates, sources, evidence IDs, evidence publication metadata, confidence, and a normal stop reason. They cannot mutate coverage, mark research complete, or choose the next strategy.

For `research` and `network_search`, a complete result requires a final assistant message without tool calls, valid JSON, at least one finding with a claim, and at least one verbatim runtime `evidence_ids` / `artifact_ids` reference. `ToolMessage`, raw search JSON, summary-only, and facts-only outputs are not final answers. One Finalization-only retry may read existing artifacts/evidence, but cannot search or fetch. Ingestion resolves accepted references to admitted canonical evidence IDs and records rejected references instead of silently dropping them.

`worker_tools_for_step()` is the single tool-authority bridge. It is consumed by Plan builders, prompt tool context, worker-profile selection, and runtime authorization. Research plans never carry hand-written legacy names such as `web_search` or `fetch`; source constraints are projected through the same registry before a step is emitted.

Worker leases split remaining research capacity by the actual approved wave size and enforce the shared `TaskBudgetProfile`: token ceiling, LLM/search-query/fetch-source/tool-invocation ceilings, and `max_output_tokens_per_call`. `batch_search(N)` and `batch_fetch(N)` each count as one logical tool invocation while consuming only their own resource type. Early fan-in does not cancel required workers; it can only skip optional workers after partial-wave Coverage is sufficient.

`LLM_TIMEOUT_SEC` is the default model-call timeout for every LLM stage. `LLM_BRIEF_TIMEOUT_SEC`, `LLM_SUPERVISOR_TIMEOUT_SEC`, `LLM_WORKER_TIMEOUT_SEC`, `LLM_SYNTHESIS_TIMEOUT_SEC`, `HARNESS_STEP_TIMEOUT_SEC`, `HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC`, and `HARNESS_SYNTHESIS_RETRY_TIMEOUT_SEC` are explicit per-stage overrides. `LLM_WORKER_MODEL` can select a faster retrieval worker while planning keeps the heavier model; `LLM_SYNTHESIS_MODEL` and `LLM_SYNTHESIS_MAX_TOKENS` isolate final synthesis and cap its output. Compact retries use a lower per-mode output limit. The worker wall timeout defaults to at least one model call plus ten seconds.
Per-worker leases are tuned with `HARNESS_TASK_<EFFORT>_*` variables. Production uses a bounded medium profile so retrieval, evidence digestion, and the structured-output retry all fit inside the worker wall timeout.
A transient provider connection failure before any tool work is retried once inside the same worker; failures after work has started are salvaged rather than replayed.

Worker finalization is adaptive. Before an LLM call, the budget boundary projects the call cost and arms `Finalization Mode` when another same-scale call would risk the token ceiling; the threshold is capped at 80% and never starts before 40% of the ceiling. The wall-clock boundary reserves at least one model call plus ten seconds, and the LLM-call boundary reserves the final calls. Once armed, the current request receives a Finalization Mode instruction, new search/fetch is rejected, and the remaining capacity is reserved for structured output. Hard caps remain safety ceilings for runaway workers.

## Stop Semantics

| Condition | Meaning |
|---|---|
| `success` | coverage, synthesis, citation, and grounding gates pass |
| `partial` | useful evidence exists, but coverage or quality remains incomplete; output must disclose the limitation |
| `failed` | no usable semantic result or a required gate fails |
| `cancelled` | user or policy cancellation |

Budget stop, timeout, and worker `STOPPED` do not automatically map to `failed`. RuntimePolicy chooses partial delivery or failure from actual evidence state.

Normal worker stop reasons are `local_evidence_sufficient`, `soft_budget_finalize`, `soft_deadline_finalize`, and `no_more_useful_evidence`. `worker_token_cap`, `worker_llm_call_cap`, and `worker_timeout` remain abnormal stop reasons and should not dominate healthy runs.

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
- `app/agent/harness/step_budget.py` — per-worker search, fetch, and logical invocation budgets
- `app/research/execution/tool_gateway.py` — worker-local retrieval execution scope
