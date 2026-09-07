# Control Plane Architecture (2026-09)

## Authority

This is the authoritative control-plane design. Historical phase RFCs were removed; operational and evaluation documents remain separate.

```text
API / UI
  └── Product Route (TaskShape)
        ├── Simple Fact Fast Path
        │     └── WorkerExecutorV2 → deterministic renderer → Quality/Finalize
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
7. Workflow termination is not task success; only a nonempty, grounded final answer that passes quality can be `completed`.

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

Terminal invariants:

```text
synthesis_failed => quality != pass
quality_pass => final_content nonempty && answer_grounded
partial => UI reports partial, never “all stages completed”
aborted/failed => UI reports failure, never “all stages completed”
```

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

For `SIMPLE_FACT`, `WorkerExecutorV2` uses a direct provider-search branch. It may issue a second official-source search when the first response contains no primary source and fewer than two high-quality secondary sources. It reserves the same worker lease and retrieval quota, but does not invoke a leaf LLM. This keeps the fast path inside the production executor and budget boundary instead of adding a parallel agent runtime.

### SynthesisExecutor

`app/research/execution/synthesis_executor.py` is separate from the research worker executor:

- reads existing evidence and artifacts
- uses `LLMGateway(phase="synthesis")`
- sets retrieval quota to zero through `ToolGateway(0)`
- never starts a new search/fetch
- returns the same typed `WorkerResult` boundary

Synthesis no longer falls back to `_run_single_step`.

Synthesis uses coordinator budget scope (`LLMGateway(phase="synthesis")`) and never binds a worker task ID. Research worker leases cannot authorize synthesis, and synthesis cannot consume a worker lease.

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

`tests/test_simple_fact_fast_path.py` is the release gate for the golden path. It runs the real `AgentHarness → ResearchGraphRunner → WorkerExecutorV2 → RunBudgetManager → ToolGateway → CitationManager → validator → finalizer` chain with only the search provider mocked. Five canonical simple facts must pass 5/5:

1. `DeepSeek V3 发布时间？`
2. `Attention Is All You Need 哪年发表？`
3. `LoRA 原论文是什么？`
4. `DeepSeek-R1 发布时间？`
5. `LangChain 首次发布是哪一年？`

Required assertions include `task_shape=simple_fact`, `execution_path=fast_path`, zero planner/replan/synthesis calls, at most three tool calls, a primary source, a grounded answer, no budget reservation error, and `success + quality pass`.

`tests/test_control_plane_e2e.py` runs six scenarios at `recursion_limit=20`:

1. Normal landscape → success
2. Semantic gap → one replan → success
3. Rejected patch → partial, finite termination
4. Worker budget interruption with salvage → worker partial, run continues
5. Research token cap → partial synthesis, reason remains `research_token_cap`
6. Quality failure after replan exhaustion → partial finalize, no second replan

These gates must pass before a real query E2E is considered valid.
