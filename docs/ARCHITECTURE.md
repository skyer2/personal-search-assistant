# Semantic Research Agent Harness Architecture

This document is the repository architecture authority. Legacy `ResearchSpec`, `CoverageContract`, and semantic action modules are projections or deprecated adapters; they do not control the production workflow.

## Position

The system is a **Deep Research Agent Harness**. It gives open-ended research reasoning to a Brief compiler and Supervisor, while a thin deterministic runtime owns budgets, execution safety, evidence grounding, observability, and evaluation.

```text
User Query
  ↓
StructuredResearchBrief
  ↓
Supervisor research action
  ↓
Isolated Researchers
  ↓
Evidence-backed compressed findings
  ↓
Coverage Judgement
  ↓
Grounded Synthesis
  ↓
Quality Gate
  ↓
Cited Final / Partial / Failed
```

## Authority Model

| Concern | Authority | Non-authority |
|---|---|---|
| What the user wants | `StructuredResearchBrief` | raw query, TaskShape, plan |
| What research to do next | `Supervisor` | Worker, planner adapter, ControlPolicy |
| Whether evidence is admitted | evidence admission | Worker payload |
| Whether research is enough | Coverage Judge + Brief | task completion count |
| Whether an action can execute | `RuntimePolicy` | Supervisor, Worker |
| What can be delivered | citation and grounding gates | synthesis confidence |
| What happened | `AgentTelemetry` and `RunStore` | frontend state |

## Eight-node Runtime

```text
brief
supervisor
researcher
ingest_findings
coverage_judge
synthesize
quality_gate
finalize
```

Fast path is not a separate workflow. `brief` derives strict eligibility and can send an atomic fact directly to `researcher`; all other research enters the Supervisor loop.

## Supervisor Contract

The Supervisor can return only:

- `THINK`
- `CONDUCT_RESEARCH`
- `COMPLETE`

It receives the Brief, compressed findings, Coverage Judgement, and budget state. It does not receive infinite raw tool history and cannot bypass RuntimePolicy.

## Deterministic Runtime

RuntimePolicy owns:

- tool, token, worker, time, and iteration budgets;
- deterministic retry;
- worker lease and timeout;
- partial delivery eligibility;
- terminal-state validity.

It contains no semantic actions such as `GAP_FILL`, `EXPAND_PLAN`, or `REPLAN`.

## Evidence and Findings

Workers return raw results. The findings boundary:

1. compresses worker results;
2. preserves claims, evidence IDs, confidence, limitations, and unresolved questions;
3. rejects unsupported findings as sufficient evidence;
4. feeds Coverage Judge and synthesis.

Unadmitted evidence can never support a final claim.

## Quality and Delivery

| Outcome | Meaning |
|---|---|
| `success` | coverage, citation, grounding, and content gates pass |
| `partial` | usable evidence exists but coverage or quality remains incomplete; limitations are disclosed |
| `failed` | no usable semantic result or a required gate fails |
| `cancelled` | user or policy cancellation |

## State and Durability

`ResearchState` is the workflow truth and is checkpointed through LangGraph. Legacy fields such as `research_spec`, `coverage_contract`, and `coverage_state` are projections for adapters and observability. Task state records execution facts only.

## Observability

Canonical events include:

```text
brief.compiled
topology.decided
supervisor.decided
plan.created
finding.compressed
coverage.assessed
progress.assessed
quality.assessed
run.terminated
```

See [OBSERVABILITY.md](./OBSERVABILITY.md).

## Evaluation

Component regression covers Brief, Coverage, Supervisor, and Evidence. Production fidelity, live scenarios, and BrowseComp-Plus are separate layers. See [EVALUATION.md](./EVALUATION.md).
