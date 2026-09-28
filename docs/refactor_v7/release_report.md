# Release verification (in progress)

## Executed

- Baseline before refactor: 614 offline tests passed.
- After the latest v2 worker, renderer, comparison-scope and field-schema changes: 635 offline tests passed (`pytest -q -p no:cacheprovider`).
- Frontend projection self-check and production build passed.
- Type checks on the changed runtime and delivery modules passed.
- `.env` backed E05 real run passed end to end (`output/refactor_v7_smoke25/report.json`): 1/1 valid answer unit, source/citation/relevance quality pass, trace integrity pass, 30.03 seconds. The v2 GLM extraction call uses low reasoning effort; direct source retrieval now finds and verifies a DeepSeek-owned release page.
- A 10-case first-pass live diagnostic (`output/refactor_v7_10x1/report.json`) produced 1 complete success and 9 failures. This is far below the required 24/30 first-pass and 27/30 overall release thresholds, so the 30-run repetition has not been started. The cases with recommendations, comparisons, explanations, current-state and forecasts all failed to produce enough valid answer units.
- After field-type-aware worker prompting and same-unit inference-premise binding, E01 improved from failed to partial (`output/refactor_v7_e01_smoke3/report.json`): 1/5 valid units in 103.70 seconds. Partial does not count toward the full-success gate.
- An experimental search-card candidate scout made E01 worse (two complete failures: `output/refactor_v7_e01_scout1/report.json` and `output/refactor_v7_e01_scout2/report.json`); it was removed. The active worker keeps the earlier bounded retrieval path.
- Comparison contracts now freeze every explicit subject, and the validator rejects one-sided answer units. E04 was rerun with two-subject retrieval (`output/refactor_v7_e04_subjects1/report.json`): 0/1 valid units, failed in 101.05 seconds. This confirms the guard works but does not solve source quality or complete comparison assembly.
- Entity-list assembly now verifies each subject against admitted claims rather than trying to match a serialized list verbatim. E04 was rerun again (`output/refactor_v7_e04_subjects2/report.json`): still 0/1 valid units, failed in 81.56 seconds. The admitted facts covered Claude Code, but did not yield a source-backed Cursor side plus complete comparison dimensions; the terminal failure is correct.
- The configured Tavily API still returns `ForbiddenError`; Bocha is the intended and active search backend in the supplied `.env`.

## Not yet passed

- No 10×3 release evaluation on one frozen commit; G04 and G05 are not satisfied. The 10×1 diagnostic is 1/10 full success.
- Multi-entity and analytical tasks need further retrieval, candidate planning and validated-unit coverage improvements.
- No manual citation review, UI replay, or comparative quality review.
- Deletion and terminal CAS gates remain open; see `deletion_manifest.md`.

The release gate is **blocked**. Do not describe this worktree as fully verified or push it as an approved release. The `.env` file and credentials are excluded from Git.
