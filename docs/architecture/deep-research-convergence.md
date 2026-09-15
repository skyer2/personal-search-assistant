# Deep Research Convergence

## Goal

The research loop converges through explicit local and global contracts:

- Workers decide only when their own task has enough evidence.
- Coverage decides only what the whole Brief still lacks.
- Conflict reconciliation decides whether a contradiction is resolved, expected, or blocking.
- Supervisor consumes exact `CoverageGap` objects.
- Synthesis organizes evidence and discloses uncertainty; it never adjudicates an unresolved conflict.

## Data Flow

```text
Brief.key_questions
  → Supervisor ResearchTask(criterion_id, gap_id)
  → Worker local research / soft finalization
  → Claim / Finding / Evidence
  → Conflict resolution
  → CoverageJudgement(criteria, gaps)
  → targeted gap research or synthesis
```

## Coverage Contract

Research criteria come from `brief.key_questions`. `brief.success_criteria` is reserved for the final Quality Gate. A criterion is `supported` only with explicit criterion binding, admitted evidence, the required independent sources, primary and freshness requirements satisfied, and no blocking unresolved conflict. Lexical matching alone is at most `partial`.

Every open criterion produces:

```text
CoverageGap(
  gap_id,
  criterion_id,
  description,
  current_evidence_ids,
  missing_evidence_type,
  blocking_conflict_ids,
  priority,
)
```

The legacy `missing` and `recommended_next_questions` fields are UI/eval projections only.

## Worker Finalization

A worker enters Finalization Mode adaptively: at no later than 80% and no earlier than 40% of its token ceiling, when the projected next calls would risk the ceiling, when the final LLM calls are needed, or when the wall clock must reserve one model call plus ten seconds. The instruction is injected before the current LLM request, retrieval is disabled, and the remaining capacity is reserved for a structured WorkerResult.

## Tool and Metadata Authorities

Tool authorization flows from one worker registry into Plan `allowed_tools`, prompt tool context, worker-profile selection, and runtime authorization. Research steps include `internet_search`, `fetch_url`, `batch_search`, `batch_fetch`, `read_file_content`, `read_artifact`, and `read_evidence`, unless a source constraint removes web or file tools.

Search success is semantic, not transport-level. A query succeeds only when its provider row contains at least one result with a valid `http(s)` URL. Empty results, URL-less rows, and provider exceptions return `ok=false`; a batch succeeds only when at least one query has usable results and `ok_count` counts only those queries.

Publication metadata is owned by tools and runtime. Search provider dates and fetched-page dates are persisted on artifacts and copied into `EvidenceRecord.published_at`; workers only bind claims to evidence IDs and never guess dates. Coverage freshness remains unchanged.

## Worker Finding Contract

A research worker is complete only when its last non-empty assistant message without tool calls contains valid JSON with at least one finding. Every accepted finding has a non-empty `claim` and at least one `evidence_ids` or `artifact_ids` reference returned by the runtime or a tool. References must be copied verbatim; workers must not invent `E1`, `E2`, or `source1`.

If the first final answer is missing, prose-only, summary-only, facts-only, or lacks evidence references, the executor performs one Finalization-only retry. The retry forbids `internet_search`, `fetch_url`, `batch_search`, and `batch_fetch`; it permits `read_artifact` and `read_evidence`.

Ingestion resolves references to admitted canonical evidence IDs in this order: exact evidence ID, artifact reference, locator/source. Rejected findings remain visible as diagnostics with `reason` and raw references. A deterministic compressed finding is allowed only as a `partial` fallback when both facts and admitted evidence exist; it is never treated as a model-produced complete finding.

Task completion is decided after ingestion:

```text
accepted finding >= 1 + admitted evidence >= 1 + valid structure + normal stop
  -> complete
admitted evidence >= 1 but structure, findings, budget, timeout, or search stopped early
  -> stopped / partial; evidence and findings remain eligible for ingestion
admitted evidence == 0 and accepted finding == 0
  -> failed / no_usable_evidence (or the specific blocking reason)
```

A terminal tool result such as `search_empty`, `budget_denied`, fetch failure, or provider timeout describes only that action. It never discards already admitted evidence. If evidence exists after an abnormal stop, the worker is partial and its evidence still reaches Coverage.

## Evidence Pack and Synthesis Retry

Before synthesis, the runtime builds a deterministic Evidence Pack. Findings are grouped by Brief criterion, deduplicated, quality-ranked by primary source, source tier, freshness, confidence, and evidence count, then limited to three findings per criterion normally and two on compact retry. Resolved winners, expected disagreements, and blocking unresolved conflicts are preserved.

The normal pack is capped at 8K input tokens and the compact retry at 4K, with a 30K hard maximum. A synthesis timeout retries once with the strictly smaller compact pack; the retry input cannot be identical to the first attempt. The retry timeout follows the run profile (30 seconds in production, 120 seconds in `deep_debug`). If both attempts fail, deterministic partial delivery remains the final fallback.

Evidence Pack selection and digest resolution are separate contracts. The pack selects canonical evidence IDs; `SynthesisContextBuilder` resolves each ID through its `EvidenceRecord.artifact_ref` and citation aliases and reads the real artifact summary/content as the excerpt. Synthesis is not invoked when selected findings, evidence references, or non-empty digests are missing; the runtime returns a deterministic partial result with `synthesis_evidence_digest_missing`.

Ingestion binds canonical evidence records to the citation manager before synthesis. The synthesis prompt receives stable `[n]` citation numbers, and the runtime projects selected findings onto numeric report sentences after generation. Model-invented citation numbers are replaced, unknown numbers fail the quality gate, and internal worker JSON payloads are stripped before delivery.

## Timeout Contract

`LLM_TIMEOUT_SEC` is the default model-call timeout for Brief, Supervisor, Worker, and Synthesis. Stage-specific variables such as `LLM_WORKER_TIMEOUT_SEC`, `LLM_SYNTHESIS_TIMEOUT_SEC`, `HARNESS_STEP_TIMEOUT_SEC`, `HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC`, and `HARNESS_SYNTHESIS_RETRY_TIMEOUT_SEC` override it only when explicitly configured. `LLM_SYNTHESIS_MODEL` and `LLM_SYNTHESIS_MAX_TOKENS` allow the final report to use a faster bounded model without changing planning or worker behavior. Without a worker-stage override, the worker wall timeout reserves one model call plus ten seconds. The shared resolution lives in `app/config/timeouts.py`; hard-coded 20/30/60-second stage ceilings are not valid.

Normal stop reasons:

```text
local_evidence_sufficient
soft_budget_finalize
soft_deadline_finalize
no_more_useful_evidence
```

Hard caps remain abnormal runaway protection:

```text
worker_token_cap
worker_llm_call_cap
worker_timeout
```

## Conflict Contract

Claims carry an explicit `criterion_id`. Numeric and bounded semantic-polarity conflicts reconcile into:

- `resolved`: use the authoritative winner;
- `expected_disagreement`: explain the scope or benchmark difference;
- `unresolved`: disclose uncertainty.

An unresolved conflict bound to a required criterion is blocking. It creates a `CoverageGap`, prevents `sufficient`, and forces degraded synthesis when no research budget remains.

## Validation

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_deep_research_convergence.py
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m tests.eval.run_eval --dry-run --fail-on-regression
```

The convergence contract also participates in production-fidelity E2E and release smoke through worker completion, stop reason, budget, coverage, and trace-integrity assertions.
