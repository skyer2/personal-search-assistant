# Traceability

| Requirement | Implementation | Verification status |
| --- | --- | --- |
| INV: one frozen user goal | `spec/compiler.py`, `spec/intent.py`, `brief/compiler.py` | Unit tests pass; live scope still under investigation |
| INV: only validated answer units are deliverable | `delivery/unit_models.py`, `unit_validator.py`, `unit_renderer.py`, `coverage/answer_units.py` | Zero-unit and partial-unit tests pass |
| INV: source and claim binding | `evidence/quality.py`, `runtime/ingestion.py`, `claims/models.py` | URL mismatch regression passes; manual citation review pending |
| INV: completion authority | `domain/completion_v2.py`, `runtime/runner.py` | Unit tests pass; end-to-end success not yet verified |
| D: bounded runtime | `runtime/runner.py`, `execution/worker_executor.py` | Live E05 runs within 240 seconds; success target unmet |
| DEL-01–DEL-14 | See `deletion_manifest.md` | Cleanup gate not met |
| T: deterministic regression | `tests/test_answer_contract_v2_refactor.py` and existing suite | 627 passed on current worktree before latest timeout edit; rerun required |
| G: live 10×3 evaluation | `scripts/live_deep_research_suite.py` | Not started as release run; E05 diagnostics failed |
| G: UI replay and manual citation review | API projection and frontend types | Pending |
