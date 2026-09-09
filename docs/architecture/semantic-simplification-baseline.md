# Semantic Simplification Baseline

## Baseline

- Commit: `c0535c097e16f3c960fdb4a85c19e4caf1a54694`
- Test result: `444 passed`
- Test command: `python -m pytest -q tests -p no:cacheprovider`

## Before Graph

The agent graph has 18 nodes:

```text
compile_spec
spec_gate
clarify
plan
plan_validate
dispatch
research_worker
dispatch_barrier
ingest_semantics
assess
gap_fill
expand_plan
replan
retry
synthesize
quality_gate
repair_synthesis
finalize
```

Canonical semantic branches still present in `ControlPolicy`:

```text
GAP_FILL
EXPAND_PLAN
REPLAN
```

## Before Semantic Authorities

```text
Mode Router
TaskShape Router
ResearchSpec Shape Rules
Coverage Compiler
Planner
Progress Assessor / SemanticGap Builder
ControlPolicy
```

Open-ended user intent is therefore interpreted by multiple deterministic modules before synthesis.

## Routing Distribution

| Query class | `mode_router` | `ResearchSpec.task_shape` | Dimensions | Known issue |
|---|---|---|---:|---|
| GPT-4 release date | `fast_path` | `SIMPLE_FACT` | 1 | Correct fast path |
| Agent trend + forecast | `harness` | `SIMPLE_FACT` | 1 | Semantic misclassification |
| Domestic AI startup landscape | `harness` | `DYNAMIC_DISCOVERY` | 8 | Broad mandatory dimension matrix |
| GPT / Gemini / DeepSeek comparison | `harness` | `BREADTH_HEAVY` | 7 | Broad mandatory dimension matrix |
| 2026 BCI long report | `harness` | `SIMPLE_FACT` | 1 | Numeric year causes semantic misclassification |
| Agent ROI conflict | `harness` | `HYBRID_CONFLICT` | 1 | Correct intent, legacy branch machinery |
| Fresh coding-agent progress | `harness` | `DYNAMIC_DISCOVERY` | 8 | Freshness not consistently represented as a Brief constraint |
| Follow-up narrowing | `harness` | `BREADTH_HEAVY` | 7 | Follow-up rebuilt from raw query rather than Brief delta |

## Target Cutover

```text
StructuredResearchBrief = user intent authority
Supervisor = semantic research strategy authority
RuntimePolicy = execution safety and budget authority
```

The target agent graph must have at most eight semantic/runtime nodes and must not route production workflow from `TaskShape`.
