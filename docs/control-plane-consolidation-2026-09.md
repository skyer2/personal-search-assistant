# Control Plane Consolidation Design

## 1. Problem

The current runtime has three overlapping control systems:

- LangGraph owns the macro workflow.
- `AgentHarness._run_single_step()` still owns a worker-local execute / compress / validate / recover / retry loop.
- `RunBudgetManager`, adaptive effort, and step budgets each enforce only a slice of resource policy.

That split allowed a run to report `deadline_exceeded` when the real cause was token exhaustion, and allowed parallel workers to start before their shared token ceiling was reserved. It also let category aliases fan out into duplicate workers and let a partial result be rewritten as `completed` during finalization.

## 2. Target Architecture

```text
API
  └── Run Coordinator
        └── LangGraph Control Plane
              ├── Understand / Brief
              ├── Plan / Plan Validator
              ├── Dispatch Wave
              ├── Progress / Replan
              ├── Synthesis
              ├── Quality Gate / Repair
              └── Finalize
                    └── WorkerExecutor Execution Plane
                          ├── bounded TaskSpec
                          ├── tool / LLM loop
                          ├── structured output repair
                          └── Finding + Evidence projection
```

Authority rules:

1. `ResearchState` is the workflow truth and checkpoint source.
2. `LoopState` is only a process-local adapter for legacy domain services.
3. `RunBudgetManager` is the only resource authorizer.
4. Workers receive a lease and return a structured `WorkerResult`; they cannot change run-level status.
5. Evidence, subjects, tasks, and findings cross process boundaries as validated IR, not arbitrary dictionaries.
6. Quality and termination are explicit state-machine outcomes.

## 3. Semantic Research Contract

### Research Subject

`ResearchSubject` now carries:

- `canonical`: display name.
- `subject_id`: stable normalized identity.
- `aliases`: lexical aliases that normalize to the same identity.

Canonicalization applies Unicode NFKC, lowercases ASCII, removes whitespace and separators, and maps well-known category phrases to stable IDs such as `china_ai_startups`. `"国内 AI 初创公司"` and `"国内AI初创公司"` therefore resolve to one subject.

### Task Kind

`ResearchBrief.task_kind` is one of:

- `named_entity_deep_dive`
- `landscape_discovery`
- `comparison`

Open-ended category questions must generate a Discovery task that produces a `candidate_set`. The control plane may later replan deep dives for selected candidates. A generic category must not become one fake entity deep dive.

### Plan Shape

- Planner fan-out uses canonical `brief.subjects`, never raw `brief.entities`.
- Duplicate subject / task-kind / coverage tasks are rejected.
- Horizontal comparison is synthesis work. It must not create a retrieval worker such as `t_compare`.

## 4. Budget Governor

The budget manager now uses atomic reservations rather than check-then-act accounting.

### Worker Lease

Before a worker executes:

1. The manager syncs real usage.
2. The worker reserves a lease with a fair research-token ceiling, a per-worker LLM call ceiling, and a stable lease ID.
3. The lease is released in a `finally` block.

The sum of active worker ceilings cannot exceed the research token pool. This prevents parallel workers from each observing “budget available” and then collectively overcommitting.

### LLM Call Reservation

At `on_llm_start`:

1. Estimate prompt tokens conservatively (CJK-aware) plus output reserve.
2. Atomically reserve one LLM call and its token estimate.
3. Reject the call before the provider request if the hard ceiling, research pool, global call cap, or worker lease would be exceeded.

At `on_llm_end`, the reservation is converted to actual usage. Errors release the reservation. Usage sync remains a reconciliation path, not the authorization path.

### Tool Call Reservation

Retrieval tools reserve tool-call quota before invocation. A failed reservation returns the existing structured stop message instead of performing the search.

### Exact Exhaustion Reason

Valid reasons include `budget_tokens`, `research_token_cap`, `budget_llm_calls`, `budget_tool_calls`, `synthesis_time_reserve`, and `deadline_exceeded`.

`deadline_exceeded` is allowed only when the wall deadline is genuinely exhausted. Budget degradation is resolved from the manager snapshot and metadata, never defaulted to a deadline.

## 5. Finding IR

Worker findings are normalized before entering `ResearchState.findings`:

```text
finding_id
task_id
subject_id
dimension
claim
summary (compatibility alias for claim)
evidence_ids
confidence
status = supported | partial | conflicted
```

Legacy `summary`, `facts`, and artifact-backed findings are accepted as input and converted. Invalid findings are rejected into worker diagnostics and never enter workflow state.

## 6. Quality And Termination State Machine

Quality no longer unconditionally falls through to success finalization.

```text
Quality PASS
  → Finalize success

Quality FAIL + repairable synthesis issue + repair budget
  → reset synthesis step → resynthesize once

Quality FAIL + evidence gap + research/replan budget
  → Replan

Quality FAIL + no repair budget / not repairable
  → Finalize partial
```

Quality events and state expose `quality.reason`, `quality.repairable`, and `quality.repair_action`.

Finalization preserves `HarnessResult.status`. In particular, `partial` remains `partial`; it is never rewritten to `completed`.

## 7. Implementation Phases

### Phase 1 — Safety Boundary (this change)

- Add canonical subjects and task-kind IR.
- Force category queries through Discovery.
- Remove comparison retrieval work.
- Add atomic budget reservations.
- Preserve partial status.
- Add conditional quality repair routing.
- Add Finding normalization and validation.
- Add exact termination attribution.
- Add control-plane invariant tests.

### Phase 2 — Worker Executor Extraction

Replace the worker-local call to `AgentHarness._run_single_step()` with a dedicated `WorkerExecutor`:

```text
TaskSpec + Lease → tool/LLM loop → structured repair → WorkerResult
```

Recover, replan, and final quality decisions move fully to LangGraph. This is intentionally not mixed into Phase 1 because it requires migrating all worker prompts and structured-output tests without changing resource semantics at the same time.

### Phase 3 — Production Evaluation

CI uses fake LLM/search/failure fixtures to verify plan shape, budget, evidence, progress, termination, and trace integrity. Live evaluation remains separate and measures answer quality, citation accuracy, latency, cost, budget overrun, partial rate, and span projection health.

## 8. Invariants

- I1: actual plus reserved tokens never authorize above the hard ceiling.
- I2: canonical aliases never create duplicate subject workers.
- I3: category questions create Discovery, not fake entity deep dives.
- I4: comparison work is synthesis, not retrieval.
- I5: required tasks are executed, skipped with a reason, or covered by an explicit termination cause.
- I6: partial results keep partial status and exact reason.
- I7: quality failure cannot finalize as success.
- I8: invalid findings cannot enter workflow state.
- I9: worker timeout or provider failure produces a bounded worker result, not a run crash.
- I10: events with valid span identity project to a non-empty span tree.
