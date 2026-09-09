# ADR-004: Retain explicit semantic control nodes

## Status

Accepted deviation from the SDD's approximate 14-node target.

## Context

The SDD recommends about 14 nodes but also requires separate retry/gap-fill/expand/replan actions, independent budgets, strategy fingerprints, and control events. Collapsing these operators into one generic `patch_plan` node would reduce node count but make the canonical action polymorphic and less observable.

## Decision

The graph keeps 18 named nodes. `ControlPolicy` remains the sole routing authority, while `retry`, `gap_fill`, `expand_plan`, and `replan` remain separate nodes so each action can preserve its deterministic state transition, budget, telemetry event, and failure mode.

## Consequences

- Graph topology has more named nodes than the suggested target.
- Control semantics remain explicit and independently testable.
- No new node may be added without proving an existing node cannot own the responsibility.
- `dispatch_barrier` remains side-effect free and is not a semantic assessor.
