# ADR-001: ResearchSpec is the success authority

## Status

Accepted.

## Context

The previous runtime inferred research completion from completed tasks and a mutable Research Brief. Task success leaked into semantic completion, causing false success, over-planning, and untraceable replanning.

## Decision

`ResearchSpec` is the only success contract. It defines the objective, subjects, dimensions, reasoning and evidence requirements, freshness, assumptions, premises, delivery requirements, and measurable criteria. `CoverageContract` is its derived, machine-verifiable form. Plans and tasks describe execution only.

## Consequences

- All workers finishing is insufficient for success.
- Replanning must update strategy, not merely add a prompt directive.
- Follow-up turns increment spec version while retaining stable identity.
- Old Brief authority is compatibility-only for trace projections and has no workflow authority.
