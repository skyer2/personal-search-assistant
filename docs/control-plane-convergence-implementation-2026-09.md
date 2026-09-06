# Control Plane Convergence Implementation (2026-09)

## Goal

This change implements the RFC's Phase 0–5 path without adding product surface area:

1. LangGraph remains the only global control plane.
2. `ResearchState` is the only workflow truth.
3. `RunSession` holds runtime handles only.
4. Plan steps no longer carry runtime status.
5. The default worker/synthesis path no longer enters `_run_single_step`.
6. LLM and tool authorization go through explicit execution gateways.

## Design

### Phase 0 — Feature Freeze

No memory, MCP, new agent, new tool, or frontend capability was added. All changes are runtime correctness and control-plane convergence.

### Phase 1 — Typed Contracts and FSM

`app/research/domain/contracts.py` now defines:

- `WorkflowPhase`
- `OutcomeStatus`
- `TaskStatus`
- `ProgressDecision`
- `QualityDecision`
- `ReplanState`
- `TerminationReason`
- `TaskRuntimeState`
- `Termination`

`app/research/control/transitions.py` owns the allowed phase transition table. `app/research/control/policy.py` centralizes dispatch, progress, and quality routing. Runtime lifecycle (`phase`) and business result (`outcome`) are now separate fields, and termination is a typed dictionary rather than an overloaded status string.

### Phase 2 — Plan / Task Runtime Separation

Runtime task state now lives in `ResearchState["tasks"]`; `ResearchState["task_status"]` is only a compatibility projection for older UI/checkpoint consumers. Scheduler reads and updates task maps instead of `PlanStep.metadata["status"]`. Skipping pending tasks no longer mutates the immutable plan.

`app/research/runtime/project.py` now exposes `sync_legacy_execution_scratch()` for legacy domain services. It no longer writes task status into plan metadata. The main runner no longer calls `apply_graph_to_loop()`.

### Phase 3 — WorkerExecutorV2

`app/research/execution/worker_executor.py` implements the new executor:

- input: `ResearchTask`
- output: `WorkerResult`
- execution: registered leaf worker via `WorkerRegistry`
- context: task-level JIT context
- budget: atomic worker lease and release
- evidence: artifact/evidence store ingestion plus citation binding
- result assembly: only the enriched `worker_payload` is consumed after artifact/evidence salvage
- recovery: timeout, budget interruption, and worker exceptions salvage already-collected artifact evidence before declaring failure
- telemetry: actual tool-call usage is synchronized from the typed executor and budget authority
- subagent accounting: direct V2 dispatch records the selected assistant for legacy quality checks
- synthesis timeout: synthesis steps use a dedicated budget-bounded timeout instead of the research-step timeout

The default `WORKER_EXECUTOR_V2=true` path does not call `_run_single_step` or `snapshot_worker_loop_state`. Setting the flag to `false` retains the legacy executor for A/B rollback.

### Phase 4 — LoopState Power Removal

`RunSession` may hold execution handles and a legacy scratch projection for old domain functions, but it is no longer a workflow authority. The graph worker path does not create or snapshot child `LoopState`. `apply_graph_to_loop()` remains only as a compatibility alias for the legacy fallback.

### Phase 5 — Unified Gateways

`LLMGateway` binds budget authorization, phase, and worker scope around model-backed execution. `ToolGateway` owns per-worker retrieval authorization. The V2 executor invokes both gateways and does not call providers or tools directly.

## Invariants and Tests

`tests/test_control_plane_convergence.py` adds architecture and behavior gates:

- typed task state initialization and projection
- valid/invalid phase transitions, including terminal immutability
- no-progress and replan-exhausted routing cannot re-enter replan
- V2 executor source has no `_run_single_step`, `snapshot_worker_loop_state`, or `LoopState`
- runner no longer references `apply_graph_to_loop`
- scheduler no longer writes `metadata["status"]`
- V2 execution returns a typed result and releases its budget lease
- V2 consumes the post-enrichment payload and syncs real tool-call usage
- progress and quality routing have no duplicate fallback policy branch
- direct subagent dispatch is visible to the legacy finalizer, preventing false `wrong_subagent` quality failures

## Rollout

The production default is `WORKER_EXECUTOR_V2=true`. To temporarily compare or roll back, set:

```text
WORKER_EXECUTOR_V2=false
```

The legacy branch is deliberately retained behind this flag until fixed-case evaluation and real E2E results are stable.
