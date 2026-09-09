# ADR-002: Coverage is evidence-derived semantic state

## Status

Accepted.

## Context

Task-centric progress and business-gap ownership made semantic completion dependent on plan execution details. The same evidence could yield different progress depending on which task found it.

## Decision

`CoverageContract` defines required units. `assess_coverage()` deterministically recomputes `CoverageState` from the contract, admitted claims, admitted evidence, conflicts, and resolutions. `SemanticGap` is derived from uncovered, stale, conflicted, or source-quality-deficient units and has a stable subject/dimension/type identity.

## Consequences

- Coverage can be replayed from canonical state.
- Task and claim IDs cannot affect gap identity.
- Synthesis cannot modify coverage.
- Discovery uses a transient `candidate_set` unit that is consumed by expansion.
