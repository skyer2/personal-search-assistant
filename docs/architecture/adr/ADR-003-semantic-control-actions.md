# ADR-003: Separate retry, gap fill, expansion, and replan

## Status

Accepted.

## Context

The previous recovery path mixed transient task failures, missing evidence, dynamic candidate expansion, and strategy changes under replacement recovery. This obscured root cause and allowed retries to masquerade as replans.

## Decision

ControlPolicy emits four semantically distinct actions:

- `RETRY`: same task, same scope, transient execution failure.
- `GAP_FILL`: focused tasks bound to a stable `gap_id`.
- `EXPAND_PLAN`: consume a materialized `CandidateSet`.
- `REPLAN`: produce a new strategy fingerprint and supersede the old strategy.

Each action has independent counters and telemetry.

## Consequences

- A retry cannot expand plan scope.
- A gap-fill task must carry `resolves_gap_ids`.
- Expansion cannot leave an unsatisfiable `candidate_set` unit.
- Replan must change strategy or be rejected.
