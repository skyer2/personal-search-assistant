# Deep Research Insight-Driven Contract v4

This is the current delivery contract for the insight-driven Deep Research path. It supersedes the planning, repair-budget, source-quality and recovery guidance in `DEEP_RESEARCH_QUALITY_CONTRACT_V3.md`; the v3 document remains as historical context for persisted run diagnostics.

## Bounded plan and repair

The first wave is a bounded DAG generated from `StructuredResearchBrief`. Every key question receives a task with a stable `question_id`; an uncovered question is a deterministic plan-validation error, never an implicit repair candidate. Each analytical task contains three research lanes: primary source, supporting evidence, and counter-evidence/limitations. Task dimensions remain bounded to two and estimated queries remain at most 70% of the worker cap.

There are at most two research waves. The first wave can spend only the `research` stage share (60%). A single targeted repair may spend the separately protected `repair` share (15%), and must stay scoped to one missing question, one gap/dimension, and the repair worker caps. Report (15%) and verification (10%) capacity remain protected. The runtime owns admission: if it rejects a Supervisor research request, the recorded Supervisor decision is rewritten to `COMPLETE` with the runtime-admission reason so the trace cannot claim work that never ran.

## Evidence and final completion

Raw artifacts remain immutable captures. Canonical evidence is admitted from resolvable sources, while broad secondary sources may be retained as research signals. The Completion Contract applies a stricter final rule: every answer to a key question must bind at least one `PRIMARY` or `HIGH_QUALITY_SECONDARY` source (or authority score >= 0.75). The global high-authority ratio remains a quality metric; it is not a substitute for per-question support.

The only business outcomes are `success`, `partial`, `failed`, and `cancelled`. Coverage, worker lifecycle, synthesis fallback and Quality Gate are diagnostics feeding the Completion Contract. A partial worker can still support a successful final answer. A provider fallback only becomes a successful delivery when every key question, citation and authoritative evidence requirement passes.

## Insight-driven answer path

The answer path is:

```text
Finding + canonical Evidence
  -> TrendSignal
  -> multi-signal Mechanism
  -> synthesis / cited final answer
```

Signals are deterministic, evidence-backed compact statements. A mechanism is emitted only when two or more signals share a criterion; it instructs the writer to explain interaction and uncertainty rather than creating a new unverified fact. The writer receives signals, mechanisms, conflict policy, real evidence digests and source citations. It must answer first, then explain reasoning, counter-evidence and limits.

If provider synthesis and compact retry both fail, deterministic recovery produces one concise answer per question from the highest-ranked supported finding. It does not concatenate every finding into an evidence dump. This recovery remains marked `synthesis_degraded` and is still subject to citation, quality and Completion Contract checks.

## Provider telemetry and observability

Every synthesis attempt records model, provider, prompt and digest size, pack tokens, TTFT, provider duration, output token usage, finish reason, failure class and whether retry is permitted. Empty provider content and content removed by the cleaner are explicit retryable provider outcomes. Trace synthesis events also carry signal/mechanism counts; the Trace Viewer displays those counts and the provider failure class.

## Regression and live validation

The deterministic suite covers plan coverage, repair reservation, insight aggregation, provider-empty telemetry and per-question authoritative completion, in addition to the earlier v3 quality fixtures. Run it with:

```powershell
$env:TEMP = (Join-Path (Get-Location) '.tmp-test')
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PYTEST_ADDOPTS = '-p no:cacheprovider'
.\.venv\Scripts\python.exe -m pytest -q
```

Real `.env` validation is required before reporting performance or generalization metrics. The ten-case calibration suite and the blind 20 x 3 suite are distinct from deterministic regression: only successful Completion Contract runs count toward Pass@1 or Pass^3. Do not call a compact retry or deterministic recovery proof of primary-synthesis stability.
