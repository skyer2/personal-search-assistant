# Deep Research Quality Contract v3

This document describes the release contract implemented after the quality-recovery refactor.

## Delivery rule

`SUCCESS` requires all of the following: every required key question is covered, no blocking gap remains, citations resolve, no broken finding was admitted, the minimum source-quality requirement passes, and the final report passes Quality Gate v3. A response with usable evidence but an unresolved blocking question is `PARTIAL`; provider, persistence, or execution failure with no usable evidence is `FAILED`.

`coverage=gap` cannot become `SUCCESS`. A Worker failure on its bound `question_id` is a blocking gap until a successful targeted repair covers that same question. The runtime allows one repair wave only; each repair is one question and one missing dimension, with at most five queries, five fetches, and two model calls. The run budget reserves 15% for that repair phase.

## Evidence contract

The pipeline is `Artifact → validated Finding → canonical Evidence`. An `art-web-*` identifier is a raw capture reference and never a canonical evidence identifier. The canonical ledger uses generated evidence IDs and records artifact lineage separately.

Source scoring records authority, directness, freshness, independence, completeness, and a source type: `primary`, `authoritative_secondary`, `secondary`, `community`, or `unknown`. Community and unknown sources can aid discovery but cannot enter the canonical evidence ledger for a core claim. Completion requires at least 60% high-authority evidence for Deep Research delivery.

Findings fail admission when their sentence is visibly truncated: dangling punctuation, connectives, numbers without a complete unit, unclosed delimiters, or ellipsis. Raw Artifact preservation does not bypass this gate.

## Coverage and repair

Each coverage assessment emits one `KeyQuestionCoverage` object per question:

- `covered`, `partially_covered`, or `uncovered`;
- blocking state;
- support, direct evidence, independent source, high-authority source, and counter-evidence counts;
- unresolved conflicts, missing evidence types, and confidence.

Coverage is a truthful research diagnostic and feeds the Completion Contract; it is no longer a loose label that can be ignored by a supervisor shortcut.

## Synthesis and quality

Synthesis is instructed to organize around the user’s actual questions and semantic section names. It must not use `q1/q2/q3` headings or concatenate source excerpts. Trend and forecast claims require a current signal, mechanism, observable milestone, and uncertainty.

Quality Gate v3 has deterministic rules for raw Artifact citations, broken sentences, duplicate claims, evidence dumps, missing answers, weak sources, unsupported strong claims, and incomplete forecasts. It emits `PASS`, `REPAIRABLE`, or `FAIL`; a repairable report gets one report-only retry. The Quality Gate metrics include primary/high-authority ratios, broken-sentence count, duplicate-claim ratio, unsupported-claim count, forecast-uncertainty coverage, and source-domain concentration.

## Observability and tests

`coverage.assessed` includes key-question coverage, blocking-gap count, and source-quality metrics. The Trace Viewer renders them in the Coverage table. Artifact-to-canonical-Evidence lineage is explicit, preserving one-root trace integrity without treating artifacts as citations.

The regression suite includes curated bad outputs under `tests/quality/bad_outputs/`. Every fixture must receive a non-`PASS` Quality Gate verdict. The suite also verifies blocking coverage, failed Worker repair triggering, and broken-sentence rejection.

Run the deterministic suite with:

```powershell
$env:TEMP = (Join-Path (Get-Location) '.tmp-test')
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PYTEST_ADDOPTS = '-p no:cacheprovider'
.\.venv\Scripts\python.exe -m pytest -q
```

For live validation, use `scripts/live_deep_research_e2e.py` with the local `.env`. Do not treat a compact or deterministic recovery as proof of normal primary synthesis performance; record it through `synthesis_degraded`.
