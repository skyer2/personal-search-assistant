# Deep Research Insight Synthesis & Evidence Presentation v5

## Purpose

v5 changes delivery from a finding-and-snippet compilation into a bounded,
evidence-backed research report. It does not add workers, agents, search-query
budget, graph loops, or a new Completion Contract.

## Before and after

| Area | Before v5 | v5 |
| --- | --- | --- |
| Writer input | Evidence digests and selected Findings, including raw excerpts | Allow-listed Insight Cards, Forecast Cards, normalized Claim–Evidence bindings, coverage limits |
| Abstraction | Writer clustered, reasoned and wrote in one provider call | Deterministic Claim Dedup → Signal → Mechanism → Insight/Forecast Card pipeline |
| Evidence display | Provider chose evidence wording; reference block could expose snippets | Runtime ranks canonical evidence and renders the strongest 2–4 items; all bindings remain in Trace |
| Recovery | `直接回答/关键判断` fallback could resemble a Finding list | Deterministic `总—分—总` report with current conclusions, forecasts, uncertainty and cross-section judgment |
| Quality | Citation, basic duplicates and forecast wording | Adds raw-snippet, internal-title, summary/detail overlap, canonical duplicate, forecast-card and evidence-coverage metrics |
| Source upgrade | Primary-source preference only | Named-original-source candidates are recorded with a domain query hint for the existing primary-source lane; no additional worker or global query budget is created |

## Pipeline and contracts

```text
Canonical Findings
  → deterministic claim deduplication
  → 3–6 semantic signals
  → mechanism assessments
  → InsightCard / ForecastCard
  → Claim–Evidence binding and ranking
  → report writer
  → deterministic citation/reference renderer
  → quality gate
```

`SynthesisRequest._prompt()` must not read raw Findings or EvidenceDigest
content. Its report-writer input is restricted to `insight_cards`,
`forecast_cards`, `claim_evidence_bindings`, limitations and conflict handling.

An Insight Card requires a non-empty mechanism and at least one evidence
reference. A Forecast Card always contains a current signal, mechanism,
directional forecast, observable milestone, and uncertainty. The provider is
instructed not to expose internal `q1/q2/q3`, raw artifact IDs, search snippets
or URL fragments.

## Evidence and source upgrade

Claim–Evidence bindings rank canonical evidence by authority, directness,
freshness and independence. The report body shows only the strongest 2–4
evidence rows for a claim. The full `primary_evidence_refs`,
`supporting_evidence_refs`, counter and limitation binding remains in
`synthesis.completed` and the Trace **Claim–Evidence Map**.

Recommendation Briefs have `primary_required=true` by default: a company,
product or career recommendation cannot become a completed delivery solely
from unverified homepages or generic search results. Clean, bound secondary
claims can still appear in deterministic recovery with an explicit
cross-validation limitation; title, article-frame and search-snippet text is
rejected rather than copied into the report.

When a core secondary/community/unknown claim names Gartner, IDC, OpenAI,
Anthropic, Microsoft, Meta, Salesforce, Google, EU or a government/regulator,
the pipeline creates a source-upgrade record. If a matching primary source is
already canonical it is reconciled as an upgrade. Otherwise its `site:` hint is
queued for the existing primary-source research lane, capped by that lane's
existing worker budget. Synthesis itself has no retrieval capability and cannot
silently create searches.

## Quality metrics and repair

The quality gate reports:

- `summary_detail_overlap` (repairable above `0.85`)
- `duplicate_claim_ratio` after canonicalisation (target `<= 5%`)
- claim/evidence coverage, signal and mechanism counts
- source upgrade attempts/queued candidates
- primary/high-authority source metrics (a release block only when the Brief
  explicitly requires primary sources; otherwise a visible source-quality
  warning)
- forecast milestone and uncertainty coverage
- raw snippet and broken-sentence counts

Raw search snippet exposure is a failure. Repeated summary/detail sentences,
internal answer headings and weak forecast structure are repairable. A report
repair remains report-only and is capped at 30 seconds.

## Default report shape

```markdown
# 结论摘要
# 2026 当前热点
# 未来 1~2 年方向
# 主要不确定性
# 综合判断
# 参考来源
```

The executive summary states abstract conclusions. Detail explains mechanisms,
evidence and constraints. The final judgment combines the sections instead of
repeating either summary or evidence rows.

## Verification

`tests/test_insight_synthesis_v5.py` covers bounded clustering of 20 findings,
writer input isolation, top-N/body vs complete Trace bindings, named-source
upgrade candidates, raw-snippet rejection, summary/detail duplication, and the
forecast milestone/uncertainty contract. Existing synthesis, citation, answer
contract and v4 regression tests remain part of the suite.
