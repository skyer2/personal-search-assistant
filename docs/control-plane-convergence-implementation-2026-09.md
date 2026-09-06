# Control Plane Architecture (2026-09)

## Authority

This is the authoritative control-plane design. Historical phase RFCs were removed; operational and evaluation documents remain separate.

```text
API / UI
  └── LangGraph Research Control Plane
        ├── Understand / Clarify / Plan / Plan Validate
        ├── Dispatch → Worker Execute → Progress
        ├── Replan / Prepare Synthesis
        ├── Synthesis → Quality
        └── Finalize / Abort → Terminated
              ├── WorkerExecutorV2 (research execution)
              ├── SynthesisExecutor (evidence-only synthesis)
              ├── LLMGateway
              └── ToolGateway
```

Authority rules:

1. `ResearchState` is the only persisted workflow truth.
2. `ResearchState["tasks"]` is the only task runtime state.
3. `PlanStep.metadata` is semantic metadata, never runtime status.
4. `LoopState` is a process-local compatibility adapter, not a control authority.
5. Control nodes mutate state only through typed transition helpers.
6. Execution cannot set terminal outcomes directly.

## Typed Contracts

`app/research/domain/contracts.py` defines the canonical lifecycle:

- `WorkflowPhase`
- `OutcomeStatus`
- `TaskStatus`: pending, running, done, failed, skipped, partial, blocked, cancelled
- `ProgressDecision`, `QualityDecision`, `ReplanState`
- `TerminationReason`
- `TaskRuntimeState`
- `Termination`

`TaskStatus.PARTIAL` means useful evidence was salvaged but the task did not finish normally. `BLOCKED` means execution cannot proceed under current budget policy. Neither is collapsed into `DONE` or `FAILED`.

`task_status_projection(tasks)` is a read-only projection for UI/API serialization. `ResearchState` no longer contains a `task_status` field and graph nodes no longer persist that projection.

## State Machine

`app/research/control/transitions.py` owns the allowed transition table and exposes:

- `transition_update(state, target, payload)` for normal nodes
- `terminal_update(...)` for `FINALIZE → TERMINATED` and `ABORT → TERMINATED`

Nodes do not assign `phase` directly. Dispatch, Worker, Quality, and Guardrail abort can no longer jump straight to `TERMINATED`:

```text
worker reject / guardrail abort:
  current phase → ABORT → TERMINATED

normal completion:
  QUALITY → FINALIZE → TERMINATED
```

The production graph uses `recursion_limit=20`. The limit is a final safety fuse, not a way to hide control-loop defects.

Two control-plane details are mandatory:

- Worker `Send` payloads carry the parent phase (normally `DISPATCH`) so the isolated worker can legally execute `DISPATCH → EXECUTE` without resetting control state.
- A successful replan flows `REPLAN → PLAN_VALIDATED`, not directly back to `DISPATCH`. This preserves plan validation and task initialization after every patch.
- `terminal_update()` validates both `current → ABORT/FINALIZE` and `ABORT/FINALIZE → TERMINATED`; callers cannot mask the current phase to bypass the FSM.
- The `direct` ablation graph has an explicit `BOOTSTRAP → DIRECT → FINALIZE → TERMINATED` path and never reuses the research control phases.

## Termination

Termination uses first-cause-wins semantics:

1. The first terminal cause records `outcome`, `reason`, `origin_stage`, `cause_event_id`, and detection facts.
2. Later synthesis/quality/finalize updates may only fill missing lifecycle facts such as `synthesis_attempted` and `quality_attempted`.
3. Finalize never rewrites the reason.

Canonical budget and control reasons include:

- `budget_tokens`
- `research_token_cap`
- `budget_llm_calls`
- `budget_tool_calls`
- `deadline_exceeded`
- `synthesis_time_reserve`
- `replan_exhausted`
- `control_plane_no_progress`
- `no_trusted_evidence`
- `quality_failed`
- `provider_policy`
- `provider_unavailable`

## Progress And Replan

`app/research/control/policy.py` is the sole admission authority:

- `decide_dispatch`
- `decide_progress`
- `decide_after_quality`
- `decide_replan`

`node_replan()` no longer calls legacy `can_replan(LoopState)`. Replan is admitted only through `decide_replan()`, followed by affordability checks.

Rejected plan patches are hashed canonically and stored in `ResearchState["rejected_patch_hashes"]`. An identical rejected patch cannot be proposed again. Replan exhaustion and control no-progress force synthesis/finalize and can never re-enter replan.

## Execution Plane

### WorkerExecutorV2

Research tasks execute one immutable task through:

```text
ResearchTask
  → worker lease
  → LLMGateway
  → ToolGateway
  → structured WorkerResult
```

Timeouts, exceptions, and budget interruptions salvage existing artifact evidence before returning failure. Budget interruption with salvage returns `WorkerResult(status="partial")`; no-salvage budget blocking returns `status="blocked"`.

### SynthesisExecutor

`app/research/execution/synthesis_executor.py` is separate from the research worker executor:

- reads existing evidence and artifacts
- uses `LLMGateway(phase="synthesis")`
- sets retrieval quota to zero through `ToolGateway(0)`
- never starts a new search/fetch
- returns the same typed `WorkerResult` boundary

Synthesis no longer falls back to `_run_single_step`.

Synthesis failures are classified before entering termination (`provider_content_filter`, provider rate/auth/context errors, synthesis timeout, or a typed executor failure). If synthesis fails after research produced findings, those findings are copied into `state.metadata["partial_findings"]`; the deterministic partial report renders them instead of discarding salvaged evidence.

### Gateways

`LLMGateway` is the only model-call boundary and rejects execution when no budget manager is bound. `ToolGateway` owns retrieval quota and exposes an explicit authorize/call boundary. Provider callbacks remain telemetry and usage reconciliation, not a second admission policy.

## Legacy Boundary

`LoopState` remains only where old domain services still need process-local scratch:

- Understand / Clarify / Plan / Plan Validate
- Dispatch
- legacy worker rollback
- HITL bridges

Progress, Replan, Prepare Synthesis, Quality, and Finalize no longer call `sync_legacy_execution_scratch()`.

The legacy research worker remains behind `WORKER_EXECUTOR_V2=false` for rollback only. Synthesis always uses `SynthesisExecutor`.

## Deterministic E2E Gates

`tests/test_control_plane_e2e.py` runs six scenarios at `recursion_limit=20`:

1. Normal landscape → success
2. Semantic gap → one replan → success
3. Rejected patch → partial, finite termination
4. Worker budget interruption with salvage → worker partial, run continues
5. Research token cap → partial synthesis, reason remains `research_token_cap`
6. Quality failure after replan exhaustion → partial finalize, no second replan

These gates must pass before a real query E2E is considered valid.
