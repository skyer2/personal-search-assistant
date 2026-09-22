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

## Presentation and recovery

`AnswerViewModel` contains only `AnswerPoint`s, question sections and
canonical references.  Each AnswerPoint owns its citation source IDs.  The
FinalRenderer renders citations and references directly; it does not scan
arbitrary Markdown to inject citations.  Recovery uses the same model.  A
partial delivery contains only admitted conclusions and human-readable,
specific evidence gaps.  It never exposes raw excerpts, artifact IDs, worker
failures, budget codes, or recovery implementation terminology.

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
