# Semantic Research Agent Harness

A controllable and evaluable harness for long-running research agents.

```text
Query
  → Structured Research Brief
  → Supervisor research actions
  → Researcher workers
  → Evidence-backed compressed findings
  → Coverage judgement
  → Grounded synthesis
  → Quality gate
```

This project is not a search engine. Search is only an environment tool. The harness studies user-intent compilation, LLM research strategy, evidence admission, coverage judgement, grounded synthesis, durability, and evaluation.

## Core Rules

- `StructuredResearchBrief` is the only user-intent authority.
- `Supervisor` is the only research-strategy authority.
- `RuntimePolicy` is the only budget, retry, safety, and terminal-state authority.
- Task state is execution state only. All workers finishing does not imply coverage.
- Coverage is judged from evidence-backed findings against the Brief, not from task completion.
- Synthesis consumes semantic digests and cannot change coverage.
- The final answer must pass coverage, conflict, citation, and grounding gates.

## Execution Path

```text
Simple fact → Brief fast path → researcher → answer

Other Research
  → brief
  → supervisor
  → researcher × N
  → ingest findings
  → coverage judge
  → synthesize
  → quality gate
  → finalize
```

The production graph has eight nodes. `direct` is only an ablation baseline, not a product route.

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
- [Semantic simplification result](docs/architecture/semantic-simplification-result.md)
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
