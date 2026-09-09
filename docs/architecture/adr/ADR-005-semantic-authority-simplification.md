# ADR-005: Consolidate semantic authority into Brief and Supervisor

## Status

Accepted.

## Context

The previous 18-node graph routed open research through TaskShape, ResearchSpec shape rules, coverage compilation, plan mutation, progress assessment, and ControlPolicy. Multiple deterministic modules interpreted the same user intent, causing rule interaction, query-specific patches, and unpredictable behavior.

## Decision

- `StructuredResearchBrief` is the only user-intent authority.
- `Supervisor` is the only research-strategy authority.
- `RuntimePolicy` is the deterministic budget, retry, safety, and terminal authority.
- The production graph is eight nodes.
- Fast path branches after `brief`; it does not bypass the runtime.
- `GAP_FILL`, `EXPAND_PLAN`, and `REPLAN` are not canonical actions.
- Legacy semantic modules remain only as explicitly marked projections or deprecated adapters.

## Consequences

- Open-ended research behavior is easier to reason about and trace.
- The deterministic runtime remains responsible for production safety.
- Coverage is judged against the Brief and evidence-backed findings.
- Partial delivery remains possible but must disclose its limitations.
- Evaluation now covers Brief, Coverage, Supervisor, and Evidence directly.
