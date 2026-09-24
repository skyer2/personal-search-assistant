# Deep Research Observability & Insight Diagnosis v6

This document records the implementation of the Plan → Worker → Coverage → Repair → Insight → Synthesis → Quality diagnosis contract. The initial code-path and root-cause inventory is in [ROOT_CAUSE_REPORT_OBSERVABILITY_REFACTOR.md](ROOT_CAUSE_REPORT_OBSERVABILITY_REFACTOR.md).

## Before and after

| Area | Before | After |
| --- | --- | --- |
| Plan | Trace exposed task IDs/counts; research scope was opaque. | Each task exposes its question, objective, dimensions, evidence and counter-evidence requirements, source preferences, bounded budgets, priority, and version. Invalid plans are held at the dispatch gate. |
| Worker | Execution status, result status, failure, and evidence were shown as competing status columns. Artifact references could be mixed into Evidence. | The projection derives one `success` / `partial` / `failed` outcome. The default table shows task, duration, objective, canonical Evidence, stop reason, readable budget usage, and real error; Artifact IDs are in advanced details. |
| Coverage | A run-level `gap` / `sufficient` label obscured per-question evidence requirements. | Every key question carries status, blocking state, evidence refs, independent/high-authority/primary-source counts, freshness, missing evidence, conflicts, and a deterministic explanation. |
| Repair | A new research wave had no cohesive before/after explanation. | Repair task metadata records repair ID, question, gap reason, missing evidence, and budget. The next Coverage event records before/after status and reason, evidence delta, and whether the gap closed. |
| Insight | The canonical builder could emit signals but did not construct mechanisms; a separate legacy heuristic made displayed counts inconsistent. | The canonical builder emits evidence-backed signals and extracts a mechanism only from a validated claim with an explicit causal connector. Analytical runs fail the Insight Gate when either required output is absent and do not call normal synthesis. |
| Synthesis | Empty content collapsed to a generic provider error; fallback could look like an ordinary quality pass. | Empty responses are classified from observed metadata (HTTP empty, stream without content, finish without content, or content filtered). Attempt diagnostics are preserved. Deterministic recovery must pass an explicit strict semantic review; otherwise delivery is partial. |
| Claim citations | References were only checked for resolvability and could be excessive or irrelevant. | Claim/evidence edges carry a relevance relation and score. Irrelevant edges are excluded from body citations, and a body claim shows no more than three references. |
| Run diagnosis | Failure origin and detailed events were separate, so a reader had to reconstruct the chain. | `run_diagnosis` reports the earliest quality-degradation stage, ordered failure chain, impact, next action, and stage metrics. |

## Runtime contracts

- `ResearchTaskSpec` preserves key-question coverage, objective, dimensions, evidence needs, counter-evidence needs, source preferences, and bounded task budgets.
- `KeyQuestionCoverage` is explanatory data, not a second terminal authority. It states why a question is covered or what remains missing.
- Repair remains bounded by the existing wave policy. This change adds a diagnostic contract; it does not add workers or increase the global search budget.
- An Insight Gate is required for comparison, trend/forecast, conflict-analysis, and prompts that explicitly ask for a trend, future direction, prediction, mechanism, or driver. Generic recommendations and structured reports are not blocked solely for lacking a causal mechanism. Causal language is surfaced only when it appears in an admitted, validated claim; correlation is not converted into a mechanism.
- Fallback is not a successful primary synthesis. It can produce a complete answer only when strict semantic review returns `PASS`; other fallback deliveries remain partial.
- The UI defaults to user-readable task and stage fields. Span and event identifiers remain in advanced trace views.

## Validation

The implementation is covered by deterministic tests for plan validation and coverage, worker artifact/evidence separation and outcome projection, coverage explanations, repair deltas, insight mechanism extraction from admitted claims/source excerpts, empty-provider diagnostics, irrelevant citation exclusion, top-three body citations, and earliest degradation ordering. The full Python suite completed with **610 passed**. Frontend TypeScript production build passed; Vite reported the existing Ant Design chunk-size warning. `compileall` and `git diff --check` passed.

### Real `.env` acceptance runs (2026-09-23)

The same live query was executed four times while diagnosing the real path. These are diagnostic repeats, not a blind evaluation set:

| Run | Duration | Terminal / quality | Findings / evidence | Synthesis | Acceptance |
| --- | ---: | --- | ---: | --- | --- |
| 1 (before terminal-contract correction) | 498.50 s | `success` / `partial` | 14 / 22 | Insight gate blocked provider; strict review failed | Fail; terminal was incorrectly upgraded, then fixed |
| 2 | 421.61 s | `partial` / `partial` | 15 / 28 | Insight gate blocked provider; no mechanism evidence | Fail; strict review correctly remained partial |
| 3 | 560.83 s | `partial` / `partial` | 8 / 21 | Primary, compact and report repair failed; answer contract incomplete | Fail |
| 4 (after source-excerpt mechanism extraction) | 506.38 s | `success` / `pass` | 15 / 29 | Primary timed out at 90 s; compact retry failed at 30 s; evidence-bound recovery passed strict review with 2 signals / 1 mechanism | Delivered answer passed, but primary-synthesis acceptance failed |

Across these four diagnostic runs, P50 latency was **502.44 s**, P95 was **560.83 s**, and primary synthesis succeeded **0/4**. Thus this change fixes status truthfulness and makes recovery auditable, but it does **not** meet the SDD's P50 ≤ 5 min, P95 ≤ 8 min, or primary-path stability goals. The fourth run's final answer passed Quality through strict evidence-bound recovery and was marked degraded; it is not evidence of a stable primary path. No 20-query × 3-run blind evaluation was performed.

Because the real provider acceptance gate did not pass, this implementation is not marked release-ready and has not been pushed to `main`.

## Performance expectations

The new projection and diagnosis work is local deterministic processing. It adds no model call, search call, worker, websocket protocol, or unbounded timeout. Plan validation, coverage explanation, and diagnosis should remain within the SDD limits of 1 second, 1 second, and 500 milliseconds respectively. Insight extraction is bounded by the existing evidence pack.

### Final `.env` verification (2026-09-24)

One additional live run was made after adding stage-level latency and diagnosis to the acceptance report. It **failed acceptance**: terminal `partial`, quality `partial`, 8 accepted findings, 23 admitted Evidence, Coverage insufficient, no normal/compact synthesis success, and deterministic recovery did not meet the strict semantic contract. Trace integrity passed. Duration was **561.13 s**.

The measured critical path was not Finalize. `research` consumed **500.04 s** of 561.10 s; the two workers in wave 1 and the targeted worker in wave 2 had wall durations of **142.58 s**, **250.02 s**, and **250.02 s**. Their recorded model time totaled **289.57 s**, tool time **3.91 s**, and worker idle ratio was **54.33%**. Brief/understand took **30.03 s** and Supervisor accounted for **30.06 s** total across two decisions. `finalize` took **23 ms**. Therefore optimizing Finalize would not address the observed bottleneck; worker/provider wait and control-stage model calls dominate.

This run also narrows the synthesis diagnosis: the Insight Gate found **1 signal and 0 mechanisms**, so normal synthesis was deliberately blocked with `insight_gate_missing_signal_or_mechanism`. The three displayed synthesis attempts took 0 ms and did not reach the provider. This run is not evidence of another provider `empty_content` response. The strict gate behaved correctly by returning partial when evidence did not support a mechanism; the missing mechanism must not be fabricated from correlation.

The run exposed a separate observability defect: Run Diagnosis displayed zero plan question coverage and zero dimensions because it read a nonexistent `coverage_ratio` key (the plan projection provides `coverage_rate`) and assumed the last plan event always carries task specs. The reducer now selects the latest substantive plan, derives question coverage from explicit `question_id` values, and includes repair task metadata when available. A regression test covers this projection. This local telemetry fix is covered by the test suite, but the live run itself predates this final reducer correction.

The project is **not release-ready** under this SDD: 561.13 s exceeds the 5-minute P50 target on the latest sample, and coverage, mechanism extraction, and complete delivery failed. The tested work remains local and **has not been pushed** to `main`; passing unit tests alone does not override the failed real-provider acceptance run.

### `.env` verification after worker timeout and targeted-repair changes (2026-09-24)

The latest live golden run used the real local `.env` and also **failed acceptance**: `partial`, 540.77 s, 6 accepted findings, 20 admitted evidence records, insufficient coverage, no complete answer, and trace integrity passed. The two user questions did not have equal evidence: q1 had six supported findings, while q2 still had no supporting finding or evidence binding after the repair wave. The answer therefore correctly remained partial; changing the completion gate to call this a success would hide a real evidence gap.

The dominant time was still research and provider waiting, not finalization: research took 360.02 s across three workers (153.14 s, 180.02 s, and 180.00 s); synthesis took 120.05 s, brief/understand 30.02 s, and finalization 24 ms. The primary and compact synthesis calls returned no usable output after about 90 s and 30 s respectively, with zero reported output tokens. The generated fallback was an evidence list and did not answer q2. Thus the release gate is short because answerability, coverage, insight, synthesis, and latency all fail—not because trace integrity or finalization is broken.

This run also exposed a diagnostics defect: the final report-repair invocation replaced the attempt history and overwrote the original failure reason. The implementation now retains synthesis attempts across graph re-entry, preserves the first provider failure, and includes provider failure fields in the live E2E report. The run diagnosis also showed only one plan task and zero question coverage/dimensions even though two initial workers and one repair worker ran. The reducer now counts task IDs across plan revisions and represents missing question/dimension metadata as unavailable (`null`) instead of a misleading zero; regression coverage was added. Updating this diagnosis projection does not change research behavior, so the live run above predates that telemetry-only correction. The two dry-run coverage fixtures that expected trend/comparison success without mechanism evidence were corrected to contain source-bound causal claims; the production coverage gate was not weakened.

Verification after those fixes: **614 Python tests passed**; TypeScript and frontend production build passed (Vite emitted the existing large Ant Design chunk warning); Python compileall and `git diff --check` passed. The real-provider quality and latency gates remain unpassed, so the changes are not release-ready and have not been pushed to `main`.
