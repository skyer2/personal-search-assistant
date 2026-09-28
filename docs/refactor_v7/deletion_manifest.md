# Deletion manifest

This records the current worktree, not a claim of completed release cleanup. `commit` remains `pending` until an actual commit is made.

| Symbol/path | Category | Production callers before | Replacement / callers after | Tests | Action | Removal gate | Commit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `app/research/planning/coverage.py` | C | No text imports found in app/tests/scripts; dynamic-entry review pending | `coverage/answer_units.py` on v2 | v2 coverage tests; full suite | deleted in worktree | Confirm dynamic imports/checkpoint references | pending |
| `spec/intent.py`, `intent/user_ask.py::classify_ask_type` | B | Brief and ask parsing | Shared classifier in Spec path | contract tests | shared forwarding | Remove legacy alias after old callers migrate | pending |
| `delivery/answer_contract.py::compile_deterministic_answer` (DEL-02/03) | A/B | Legacy runner and `report_repair.py` | v2 `unit_renderer.py`; legacy callers remain | v2 unit tests; legacy tests | retained for legacy execution | Migrate or remove all legacy generation callers | pending |
| `delivery/answer_contract.py::assess_answerability/assess_answer_completeness` (DEL-04/05) | A/B | Legacy runner | v2 unit validator and `CompletionResultV2`; legacy callers remain | contract tests | retained for legacy execution | Migrate all legacy completion callers | pending |
| `evidence/quality.py` URL-based primary scoring (DEL-06) | A | Evidence ingestion | Explicit source relationship, content kind and origin group | v2 source tests | behavior changed | Real provenance audit | pending |
| `domain/completion.py` partial and ref shortcuts (DEL-07/08/09) | A/B | Legacy runner | `completion_v2.py` for v2; legacy caller remains | v2 completion tests | retained for legacy execution | Remove legacy terminal decision path | pending |
| `runtime/fast_synthesis.py` and raw finding recovery (DEL-12) | A | Legacy recovery paths | Validated-unit renderer for v2 | v2 zero-unit test | retained for legacy execution | Migrate remaining legacy callers | pending |
| `domain/termination.py`, `control/terminal_policy.py` (DEL-14) | B | Graph finalizer | Existing terminal policy plus v2 completion projection | offline suite | partial migration | Add and verify transactional CAS terminal commit | pending |

Other DEL items (01, 10, 11, 13) have partial v2 routing changes but still require production-call and behavior audit. No unexamined row is marked deleted.
