# Contract-Driven Research Harness Architecture

This document is the repository architecture authority. Older Brief/Business Gap/replacement-recovery descriptions are historical.

## Position

The system is a **Contract-Driven Adaptive Deep Research Agent Harness**. Given an open research question, it compiles the user's intent into a verifiable success contract, plans and executes evidence collection, assesses semantic coverage, adapts through bounded control actions, and produces a cited answer.

## Canonical Loop

```text
ResearchSpec
  ↓
CoverageContract
  ↓
Adaptive Plan
  ↓
Research Execution
  ↓
Admitted Evidence + Claims
  ↓
Coverage Assessment
  ↓
Semantic Gap
  ↓
Bounded Adaptive Control
  ↓
Grounded Synthesis
  ↓
Quality Gate
```

## Authority Model

| Concern | Authority | Non-authority |
|---|---|---|
| What success means | `ResearchSpec` + `CoverageContract` | Plan, task status, Worker summary |
| How to execute | `ExecutionPlan` + task state | ResearchSpec |
| Whether evidence is admitted | `app/research/evidence/admission.py` | Worker payload |
| Whether semantics are covered | `CoverageState` from Spec + Claims + Evidence | task completion count |
| What the runtime does next | `ControlPolicy` | Planner, Worker, Scheduler |
| What can be delivered | Coverage, conflict, citation, grounding gates | synthesis confidence |
| What happened | `AgentTelemetry` and `RunStore` projections | frontend state |

## Four Control Actions

- `RETRY`: same task scope, same strategy, new attempt after a transient execution failure.
- `GAP_FILL`: focused new tasks bound to explicit `SemanticGap.gap_id`.
- `EXPAND_PLAN`: consume a materialized `CandidateSet`, replace the transient `candidate_set` coverage unit with concrete candidate × dimension units.
- `REPLAN`: change the strategy fingerprint and supersede the old strategy; it is not a retry or a directional prompt patch.

Each action has an independent budget and telemetry event. `ActionBudget` is the canonical state counter.

## TaskShape Versus Capability

TaskShape is only a topology selector:

- `SIMPLE_FACT`
- `DETERMINISTIC_PIPELINE`
- `SINGLE_TOPIC_DEEP_DIVE`
- `BREADTH_HEAVY`
- `DYNAMIC_DISCOVERY`
- `HYBRID_CONFLICT`

Comparison, filtering, freshness, multilingual support, strict citations, conflict handling, and multi-hop needs live in `ResearchSpec` requirements. The system must not create one TaskShape per capability.

## ResearchSpec

`ResearchSpec` contains:

- objective, subjects, dimensions, and language hints
- reasoning requirements
- evidence and source requirements
- freshness policy
- interaction requirements and assumptions
- premises and ambiguity state
- delivery requirements
- measurable success criteria

It is serializable, stable for identical objectives, and versioned across follow-up turns. A contradicted premise or blocking ambiguity stops normal research entry.

## Coverage Model

`compile_coverage_contract(spec)` derives required `CoverageUnit` records from the spec. Each unit declares:

- subject and dimension
- minimum evidence and independent-source count
- authority threshold
- freshness policy
- conflict policy

`assess_coverage(contract, claims, evidence, conflicts, resolutions)` is deterministic and recomputable. Covered, partial, missing, stale, conflicted, and waived states belong to semantic state, not task state.

For discovery, the initial contract contains a transient `candidate_set` unit. After candidates are materialized, the expanded contract removes that unit and creates concrete candidate units; an unsatisfiable discovery unit cannot remain forever.

## Evidence and Claims

Workers return raw results only. The semantic ingest boundary:

1. admits evidence;
2. extracts claims;
3. binds claim IDs to admitted evidence IDs;
4. detects and resolves conflicts;
5. materializes candidate sets;
6. recomputes coverage;
7. opens and closes semantic gaps;
8. records marginal semantic gain.

Unadmitted evidence can never support a final claim.

## Quality and Delivery

Normal success requires:

- minimum required coverage satisfied;
- blocking conflicts resolved or disclosed under an allowed partial mode;
- every final claim bound to existing admitted evidence;
- citations resolvable and precise;
- grounding evidence sufficient for the delivered claim set.

Budget or timeout stop is an execution condition, not automatically a quality failure. With usable partial evidence, the runtime may deliver a clearly bounded partial result.

## State and Durability

`ResearchState` is the only workflow truth and is checkpointed through LangGraph. It contains canonical semantic state:

```text
research_spec
coverage_contract
coverage_state
claims
claim_conflicts
claim_resolutions
evidence_records
semantic_gaps
candidate_set
marginal_gain
```

Task state records execution facts such as pending, running, succeeded, failed, stopped, superseded, attempt, lease, and failure classification. `STOPPED` and `FAILED` are distinct. The old replacement-recovery snapshot and task-owned business-gap authority are removed.

## Code Map

| Concept | Code |
|---|---|
| Spec model/compiler/validator | `app/research/spec/` |
| Coverage model/compiler/assessor/gaps | `app/research/coverage/` |
| Semantic ingest | `app/research/runtime/semantic_ingest.py` |
| Planning operators | `app/research/planning/` |
| Control authority | `app/research/control/policy.py` |
| StateGraph | `app/research/runtime/graph.py` |
| Runtime nodes | `app/research/runtime/runner.py` |
| Evidence admission | `app/research/evidence/admission.py` |
| Claim reconciliation | `app/research/claims/` |
| Quality gates | `app/research/runtime/graph.py` |
| Observability | `app/observability/` |
| Evaluation | `tests/eval/` |

## ADRs

- [ADR-001 ResearchSpec authority](architecture/adr/ADR-001-research-spec-authority.md)
- [ADR-002 Coverage-derived state](architecture/adr/ADR-002-coverage-derived-state.md)
- [ADR-003 Semantic control actions](architecture/adr/ADR-003-semantic-control-actions.md)
- [ADR-004 StateGraph node granularity](architecture/adr/ADR-004-stategraph-node-granularity.md)

## Non-goals

No self-evolving agent, new memory system, MCP platform, multimodal pipeline, UI expansion, new PDF capability, or benchmark-specific runtime behavior.
