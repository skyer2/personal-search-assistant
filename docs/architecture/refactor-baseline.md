# Contract-Driven Research Harness Refactor Baseline

- Baseline commit: `a09fda7`
- Baseline tests: 391 passed with `pytest -q -p no:cacheprovider`
- Current graph nodes: `intent`, `clarify`, `plan`, `plan_validate`, `dispatch`,
  `research_worker`, `dispatch_barrier`, `progress`, `retry`, `replan`,
  `synthesize`, `quality_gate`, `repair_synthesis`, `finalize`
- Current control actions: `DISPATCH`, `WAIT`, `RETRY`, `REPLAN`, `SYNTHESIZE`,
  `REPAIR_SYNTHESIS`, `DELIVER_PARTIAL`, `FINALIZE_SUCCESS`, `FINALIZE_FAILURE`, `CANCEL`
- Current state authorities: task-centric `business_gaps`, `recovery_snapshot`,
  `stalled_cycles`, and `brief`
- Current recovery path: `build_replacement_patch` replacement tasks
- Current progress source: task execution state plus business-gap ownership
- Benchmark smoke: not executed at baseline because it requires live provider
  credentials; deterministic and offline regressions are covered by the test suite

## Known architecture gaps

1. `ResearchBrief` remains the runtime understanding authority.
2. Coverage is inferred from task completion rather than a contract.
3. Gaps are task-owned and use ephemeral task IDs.
4. Retry, replacement recovery, gap fill, and replan are not separated.
5. Candidate discovery is not an explicit two-stage control loop.
6. Worker results bypass a semantic admission/ingest boundary.
7. Synthesis and quality do not consume a canonical semantic digest.
8. Budget stop and execution failure are not cleanly separated.
