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

## Timeout Contract

`LLM_TIMEOUT_SEC` is the default model-call timeout for Brief, Supervisor, Worker, and Synthesis. Stage-specific variables such as `LLM_WORKER_TIMEOUT_SEC`, `LLM_SYNTHESIS_TIMEOUT_SEC`, `HARNESS_STEP_TIMEOUT_SEC`, `HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC`, and `HARNESS_SYNTHESIS_RETRY_TIMEOUT_SEC` override it only when explicitly configured. Without a worker-stage override, the worker wall timeout reserves one model call plus ten seconds. The shared resolution lives in `app/config/timeouts.py`; hard-coded 20/30/60-second stage ceilings are not valid.

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
