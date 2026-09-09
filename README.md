# Contract-Driven Research Agent Harness

A controllable and evaluable harness for long-running research agents.

```text
Query
  → ResearchSpec
  → CoverageContract
  → Adaptive Plan
  → Workers
  → Evidence / Claims
  → Coverage Assessment
  → Semantic Gaps
  → ControlPolicy
  → Grounded Synthesis
  → Quality Gate
```

This project is not a search engine. Search is only an environment tool. The harness studies contract compilation, adaptive control, evidence admission, semantic coverage, grounded synthesis, durability, and evaluation.

## Core Rules

- `ResearchSpec` is the only success contract. A plan explains how to research, not when research is complete.
- `CoverageContract` and evidence-derived `CoverageState` decide semantic completion.
- Task state is execution state only. All workers finishing does not imply coverage.
- `ControlPolicy` is the only routing authority.
- `RETRY`, `GAP_FILL`, `EXPAND_PLAN`, and `REPLAN` are separate bounded actions.
- `SemanticGap` IDs are stable and independent of task or claim IDs.
- Synthesis consumes semantic digests and cannot change coverage.
- The final answer must pass coverage, conflict, citation, and grounding gates.

## Execution Path

```text
Simple Fact → Fast Path → Source Gate → Answer

Other Research
  → compile_spec → spec_gate
  → plan → plan_validate → dispatch
  → research_worker × N → dispatch_barrier
  → ingest_semantics → assess
  → ControlPolicy
       dispatch / retry / gap_fill / expand_plan / replan
       synthesize / partial delivery / finalize
  → quality_gate → finalize
```

`direct` is only an ablation baseline. It is not a product route.

## Evaluation

The deterministic regression gate now has 60 cases:

- 20 component invariants
- 20 capability cases covering all 15 capability classes
- 20 structural scenarios

```bash
python tests/eval/run_eval.py --dry-run --fail-on-regression
```

Production fidelity, live scenarios, and BrowseComp-Plus are documented in [docs/EVALUATION.md](docs/EVALUATION.md) and [docs/BROWSECOMP_PLUS_EVAL.md](docs/BROWSECOMP_PLUS_EVAL.md).

## Documentation

- [Architecture authority](docs/ARCHITECTURE.md)
- [StateGraph runtime](docs/HARNESS_ARCHITECTURE.md)
- [Observability contract](docs/OBSERVABILITY.md)
- [Evaluation system](docs/EVALUATION.md)
- [BrowseComp-Plus](docs/BROWSECOMP_PLUS_EVAL.md)
- [Context and memory boundaries](docs/CONTEXT_SYSTEM.md)
- [Deployment](docs/OPENEULER_BARE_METAL.md)

## Quick Start

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn app.api.server:app --reload --app-dir . --host 0.0.0.0 --port 8000
cd frontend && pnpm install && pnpm dev
```

Run tests on systems with restrictive global temporary directories:

```powershell
New-Item -ItemType Directory -Force -Path .tmp\pytest-tmp | Out-Null
$env:TMP=(Resolve-Path .tmp\pytest-tmp).Path
$env:TEMP=$env:TMP
python -m pytest -q -p no:cacheprovider --disable-warnings
```
