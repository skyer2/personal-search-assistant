# Semantic Research Agent Harness

A controllable and evaluable harness for long-running research agents.

```text
Query
  → Structured Research Brief
  → bounded research DAG
  → focused workers
  → immutable Evidence Ledger / Evidence Cards
  → Gap Check (diagnostic)
  → Report Writer / report-only repair
  → Completion Contract
```

This project is not a search engine. Search is only an environment tool. The harness studies user-intent compilation, LLM research strategy, evidence admission, coverage judgement, grounded synthesis, durability, and evaluation.

## Core Rules

- `StructuredResearchBrief` is the only user-intent authority.
- `Supervisor` is the only research-strategy authority.
- `RuntimePolicy` is the only budget, retry, safety, and terminal-state authority.
- Task state is execution state only. All workers finishing does not imply coverage.
- Gap Check is diagnostic; the Completion Contract alone decides delivery.
- A research worker is complete only when its final AI JSON contains at least one accepted finding bound to admitted canonical evidence; raw tool output, summary-only, and facts-only results cannot be complete.
- A single tool failure never discards admitted evidence: workers with evidence after timeout, budget stop, or `search_empty` become `partial`, and their evidence still reaches Coverage.
- Supervisor consumes structured Coverage gaps by exact `gap_id` and `criterion_id`.
- Workers reserve 20–45 seconds for soft finalization; first retrieval remains available when no evidence has yet been admitted. Tool errors and worker lifecycle are separate.
- Synthesis consumes a deterministic 8K/4K Evidence Pack with Runtime-resolved evidence digests and structured conflict resolutions. Compact retry and deterministic recovery are diagnostics; a complete grounded answer remains `success`.
- The final answer must satisfy the Completion Contract: every key question has a direct answer and real evidence binding.
- Semantic LLM calls go through one structured invocation boundary; workers cannot reinterpret user intent.
- Worker budgets separate search queries, fetched sources, and logical tool invocations.
- Search success requires at least one valid `http(s)` result; empty provider responses are failures.
- Worker token ceilings are lifetime limits, while run reservations track only in-flight LLM estimates.
- Chat is the primary answer surface. `FILES` lists only explicit user deliverables, never internal run artifacts.

## Execution Path

```text
Atomic fact → Brief eligibility → one bounded researcher → policy-gated structured answer

Other Research
  → brief
  → bounded plan → workers → Evidence Ledger
  → gap check → one targeted repair → report
  → completion contract
  → finalize
```

The production graph has eight nodes. `direct` is only an ablation baseline, not a product route. The optional `deep_debug` mode keeps that graph and the per-worker token ceilings, but raises run admission and stage timeouts for integration. Routing resolves the profile before the actual run budget manager and session are created.

## Evaluation

The deterministic regression gate now has 60 cases:

- 20 component invariants
- 20 capability cases covering all 15 capability classes
- 20 structural scenarios

```bash
.\.venv\Scripts\python.exe tests\eval\run_eval.py --dry-run --fail-on-regression
```

Production fidelity, live scenarios, and BrowseComp-Plus are documented in [docs/EVALUATION.md](docs/EVALUATION.md) and [docs/BROWSECOMP_PLUS_EVAL.md](docs/BROWSECOMP_PLUS_EVAL.md).

Run the ten-query calibration suite against the local real `.env` providers:

```powershell
.\.venv\Scripts\python.exe scripts\live_deep_research_suite.py --mode deep_debug
```

The audit keeps `answer_01.md`–`answer_10.md` and `report.json` under `output/live_deep_research_suite/`. It reports success, partial and failed separately; partial is never counted as a pass. Use `--start N --no-clean` to resume a long live run.

## Documentation

- [Architecture authority](docs/ARCHITECTURE.md)
- [StateGraph runtime](docs/HARNESS_ARCHITECTURE.md)
- [Observability contract](docs/OBSERVABILITY.md)
- [Evaluation system](docs/EVALUATION.md)
- [Deep Research convergence](docs/architecture/deep-research-convergence.md)
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
