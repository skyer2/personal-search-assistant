# Research Truth Pipeline & Final Delivery v6

## Why this boundary exists

The delivery path is now an explicit authority chain:

```text
Artifact → EvidenceRecord / SalvageEvidence → ClaimDraft
→ Claim Admission → ValidatedClaim → AnswerViewModel → FinalRenderer
```

An Artifact is tool output.  Evidence is traceable source material.  A Claim
is the only research conclusion allowed to affect Coverage, Answerability or
the final answer.  The renderer only formats a typed answer model and cannot
select evidence or create facts.

## Worker completion and salvage

When a Worker stops because of a timeout, budget limit, or provider failure,
its recoverable artifacts become `SalvageEvidence`.  They retain provenance,
source location, extraction quality and text classification, but have
`publishable_as_claim=false` by default.  Salvage never creates a Finding.

The optional future promotion path must meet all of these conditions before it
constructs a `ClaimDraft`: an official or authoritative-secondary source, a
complete non-navigation sentence, no search snippet, directness at least
0.80, known `ask_id` and `question_id`, and a successful claim extraction.
For dispatched tasks whose target gap exactly matches one Brief research
question, ingestion resolves the corresponding `question_id` and `ask_id`
before admission. Ambiguous or merely similar target text does not acquire
lineage.

## Claim Admission

`app/research/claims/admission.py` is the single publishability gate.  It
requires:

- explicit `ask_id`, `question_id`, and `task_id`;
- evidence references that resolve to canonical ledger records;
- direct and eligible sources;
- structurally complete prose, rather than navigation, SEO, title-only,
  question-shaped, or truncated text;
- a publishability score of at least 0.70, or 0.78 for inference/forecast.

Every admission result is stored in `claim_admission_diagnostics`.  Claims
that pass carry `validated=true`, their score, provenance and reasons.  Only
those rows are passed to Evidence Pack construction, Coverage and
Answerability.

## Question-level delivery

Answerability has no token-overlap authority.  It groups validated claims by
their explicit `question_id` and reports one of `UNANSWERABLE`, `PARTIAL`,
`SUPPORTED`, or `STRONG` for every key question.  Coverage likewise reads
validated claims with an explicit criterion binding; raw Findings and Salvage
Evidence cannot close a gap.

Persisted Briefs that predate `user_asks` remain readable by Coverage. They
receive the legacy criterion diagnostic only and cannot acquire new ask-level
support through that compatibility path. Persisted claim rows without a
`validated` field must still retain explicit question lineage and a resolvable
evidence reference; a row explicitly marked `validated=false` is never
promoted.

## Presentation and recovery

`AnswerViewModel` contains only `AnswerPoint`s, question sections and
canonical references.  Each AnswerPoint owns its citation source IDs.  The
FinalRenderer renders citations and references directly; it does not scan
arbitrary Markdown to inject citations.  Recovery uses the same model.  A
recovery view includes only canonical references used by its rendered answer
points; unused rows in the immutable evidence ledger remain available to
diagnostics but cannot become orphan bibliography entries.  The final
reference-closure check requires every rendered citation to resolve and every
rendered reference to be cited.
partial delivery contains only admitted conclusions and human-readable,
specific evidence gaps.  It never exposes raw excerpts, artifact IDs, worker
failures, budget codes, or recovery implementation terminology.

Evidence-bound recovery preserves the worker-declared claim type. A question
about the future does not turn a present-tense fact into a forecast. The trace
records this as `evidence_bound_recovery` and keeps it separate from primary
synthesis performance. The Completion Contract may still return `success` when
the recovered report directly answers every required ask, every claim resolves
to admitted evidence, citations close, and source/relevance/quality gates pass.
The runtime keeps `synthesis_degraded=true` so this delivery never counts as a
primary-writer success. If retrieval produced source material but no claim
passed admission, the system can issue a truthful partial delivery explaining
the limitation; no recoverable evidence remains `failed`.

Focused repair workers have a bounded lease of up to four regular model calls.
If retrieval exhausts that lease before it can return structured output, the
runtime may grant exactly one additional finalize-only call, subject to the
run-level hard call and token budgets. That call has only artifact/evidence
read capability; it cannot search or fetch. This lets the worker produce the
evidence-bound JSON needed for claim admission without opening an unbounded
retry path.

The retrieval-capable stream is also stopped at the worker's soft deadline,
leaving its reserved tail for that finalize-only call. This inner deadline is
necessary because otherwise the outer worker timeout cancels the coroutine
before structured finalization can start. The outer worker step adds at most a
90-second finalizer window, capped by the remaining run deadline. A timed-out
primary stream with no remaining call lease can receive the same single
bounded finalization grant; finite worker token leases reserve 15% for the
finalize phase. The run-level call and token ceilings still apply.

The canonical citation validator accepts both the historical `参考文献`
heading and the current `参考来源` heading. Both still require body markers to
close exactly against the rendered reference list.
Sentence-level quality checks preserve citation markers that follow Chinese
or English punctuation, so a valid end-of-sentence citation remains bound to
the claim it supports. Ledger-assigned local citation numbers are accepted
only when they were derived from the run's admitted EvidenceRecords.

## Bounded synthesis latency

The primary writer is capped at 90 seconds even if an inherited environment
requests a longer window.  Compact recovery and report-only repair are each
capped at 30 seconds.  Attempt telemetry records pack size, prompt and digest
size, TTFT, duration, token counts and failure reason.  A compact retry is
reported as degraded delivery; it is never counted as a primary success.

## Quality gate

The report-quality gate separately checks source quality, evidence text,
claim publishability, answer shape and presentation.  Navigation text, raw
snippets, broken text, missing question answers and citation/reference closure
fail the delivery contract.  A report quality defect is repairable only from
existing validated claims; it does not reopen search or promote salvage.

## Verification

The focused regression suite covers navigation rejection, lineage, no-overlap
answerability, validated-only Coverage, safe rendering and evidence-only
salvage in `tests/test_truth_pipeline.py`.  Real-provider E2E remains a
separate validation: it verifies the provider, search, artifact persistence,
claim admission, final presentation and trace on the active `.env` without
treating a provider retry as primary-path success.
