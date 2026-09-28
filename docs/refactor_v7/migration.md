# Answer Contract v2 migration

## Scope

The run creation flag `HARNESS_RESEARCH_ENGINE_VERSION=answer_contract_v2` fixes the engine version for a new run. `.env.example` enables it; an existing deployment without the flag remains on `legacy_v1` until explicitly migrated. Never change the flag during an in-flight run. The v2 path freezes one `AnswerSpec` revision at brief creation, admits evidence and claims, validates `AnswerUnit` objects, derives coverage from those units, renders validated units, and lets `CompletionResultV2` decide the outcome.

## Stored data and API

The state adds `engine_version`, `answer_spec`, `answer_units`, `support_edges`, `completion`, `evidence_version`, and `answer_version`. Existing state keys remain readable. API session snapshots add `schema_version`, `completion`, and `answer_units_summary`; older clients can ignore these fields. A historical snapshot without v2 fields must only be read under its original version.

## Configuration

v2 sets run limits at creation: 240 seconds total, 60 seconds reserved for delivery, 160,000 tokens, 24 model calls, 36 tool calls, 2 parallel workers, 3 waves, and a 75 second worker ceiling. The limits are enforced by the existing budget manager. `.env` supplies the real search and model credentials during live verification; it is ignored by Git and must not be copied into reports.

## Rollout and rollback

Enable v2 in a staging deployment, verify the offline suite, the 10×3 real evaluation, citations, UI replay, and latency before releasing it. If gates fail, keep the feature flag off for new production runs. Rollback changes only the new-run flag; preserve v2 checkpoints and evaluation evidence. The legacy branch still has active generation code and therefore does not yet satisfy the final cleanup gate.
