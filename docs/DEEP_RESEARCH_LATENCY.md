# Deep Research latency contract

## What changed

The runtime now treats answer delivery and post-run enrichment as separate
paths. `delivery` is the point at which the answer has been persisted and the
final WebSocket event has been published. Memory extraction, consolidation and
OTLP/Langfuse flushing are post-run work and are bounded by
`HARNESS_POST_RUN_TIMEOUT_SEC` (10 seconds by default).

The old `finalize_ms` field remains for compatibility. New runs also expose a
`latency.v2` summary with:

- stage timings (`understand`, `topology`, `plan`, `research`, `synthesis`,
  `quality`, `delivery`, `post_run`);
- stable substeps such as `delivery.persist_result`,
  `delivery.websocket_publish`, and `post_run.memory_extract`;
- synthesis substep aliases for evidence selection, evidence packing, prompt
  construction, provider queue, TTFT, generation, parsing, citation binding
  and validation;
- parallel worker wall time, worker sum and `research_parallel_saved_ms`;
- worker LLM/tool/idle ratios and time-to-first/ enough-evidence metrics;
- first evidence, enough evidence and final answer timestamps relative to run
  start.

Worker latency rows are keyed by `(worker_id, wave_id, attempt)`. Replaying a
checkpoint replaces the same row instead of double-counting it, while a real
retry remains a separate attempt. This keeps `research_worker_sum_ms` and
`research_parallel_saved_ms` stable across resume/replay. If a legacy graph
merge retained only `worker_queue_ms`/`worker_execution_ms`, the reader
reconstructs compatibility worker rows instead of returning a null research
breakdown.

The same summary is present in the run result metadata and the JSONL event
payload. UI consumers should use `latency.stage_ms` and
`latency.substeps` rather than infer bottlenecks from a single finalize row.

## Before / after

| Area | Before | After |
| --- | --- | --- |
| Finalize | Included memory LLM extraction and all post-processing | Delivery is measured separately; enrichment is scheduled after publish |
| Disabled memory | Could still call `memory_extract` before the store rejected writes | Disabled memory skips extraction entirely |
| OTLP/Langfuse | `finish_run()` synchronously called `force_flush()` | Flush is coalesced in a daemon background worker; explicit flush remains available for shutdown/tests |
| Diagnostics | One coarse `finalize_ms` | Stage and substep timings with status, sizes, model and worker ratios |
| Worker parallelism | Queue/execution arrays only | Per-worker wall/LLM/tool/context/compression/evidence/idle metrics when available, plus parallel savings |
| Run outcome | Could be delayed by optional enrichment | Outcome is decided and persisted before post-run work |

## Reading a slow run

1. Check `latency.stage_ms` for the largest stage.
2. If `delivery_ms` is large, inspect `latency.substeps` for artifact writing,
   persistence or WebSocket publishing.
3. If `post_run.memory_extract` is large, check the configured compression model
   and provider timeout. It no longer delays the final answer.
4. Compare `research_worker_sum_ms` with `research_wall_ms`; their difference is
   the work saved by parallel execution.

The performance numbers in this document are instrumentation contracts, not a
claim that every provider or deployment already meets the SDD latency targets.
Those targets require live provider runs and a baseline/optimized A/B report.

## Validation evidence

The real provider smoke produced a successful run at about 481.7 s with
25 admitted evidence records, one synthesis attempt, quality pass and
delivery/finalize in single-digit milliseconds. A second smoke against the
same external provider ended after about 1,765.7 s with no evidence: the
breakdown attributed the time to a 180 s Brief timeout, roughly 539 s of
Supervisor calls and two roughly 17-minute Worker attempts, while delivery
remained 6 ms and finalize 11 ms. This is recorded as provider/control-plane
instability, not as evidence that the SDD P50/P95 targets have been met.

## Control-plane routing

`LLM_SUPERVISOR_MODEL` can select a dedicated fast model for Brief and
Supervisor calls. When it is absent, the configured worker model is reused so
the control plane does not inherit the slowest research model. If coverage is
already sufficient or the runtime budget is exhausted, the Supervisor decision
is made deterministically and no provider call is issued. The Supervisor
prompt receives only bounded finding projections (up to three per criterion),
never raw evidence or worker transcripts.

The live E2E auditor keeps the intermediate `coverage.assessed` value as a
diagnostic (`coverage_event_sufficient`) and gates the run on the final
Completion/quality contract. A transient gap event therefore cannot mark a
grounded, complete delivery as failed.

The final bounded smoke on 2026-09-21 completed in 191.26 s with 15 accepted
findings, 16 admitted evidence records, a grounded complete answer, quality
pass and trace integrity pass (`root_count=1`, no orphan or cycle). Its
latency.v2 breakdown was:

- Brief/understanding: 30.013 s;
- Supervisor: 30.023 s on the slowest attempt (50.686 s accumulated across
  attempts);
- Research wall: 60.035 s for two parallel workers, with 119.995 s summed
  worker wall time and 59.995 s parallel savings;
- Synthesis: 50.019 s, because the 30 s primary and 20 s compact attempts
  timed out and deterministic recovery compiled the final answer;
- Quality: 4 ms; delivery: 8 ms; post-run: 0 ms.

This run deliberately reports `passed=false` in the E2E acceptance report:
the answer was successfully delivered, but primary synthesis stability was
not established. The result is therefore evidence that delivery/finalize is
no longer the bottleneck and that recovery is observable, not evidence that
the provider-dependent P95 target has already been met.

A follow-up bounded provider run after the instrumentation convergence took
260.63 s. It produced 15 findings and 16 evidence records, with Brief 30.014
s, Supervisor 30.033 s on the slowest attempt, a 120.011 s research critical
path, and Synthesis 50.046 s. Both workers reached their configured stop
conditions and the answer still passed grounding, quality and completion
checks; primary synthesis again timed out, so the acceptance result remained
`passed=false`. The persisted trace is therefore a provider-stability
diagnostic, not a release-pass result.
