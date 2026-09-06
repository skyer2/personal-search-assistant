# Replan Convergence Design

## 1. Problem

The failing trajectory was not worker recursion. It was a control-plane loop:

```text
Progress gap
  → PlanPatch t_gap_1
  → Validator rejects category deep dive
  → replan_exhausted=true, replan_count unchanged
  → Synthesis
  → Quality fail
  → Quality routes to replan
  → same rejected patch
```

LangGraph's recursion limit was only the last symptom. Increasing it would only delay the same loop.

## 2. State Contract

Replan control now has explicit, separate counters:

- `replan_attempts`: every replan node invocation, including rejected and unaffordable proposals.
- `replan_applied_count`: patches that changed the plan.
- `replan_count`: compatibility projection of `replan_applied_count` for legacy LoopState consumers.
- `replan_exhausted`: irreversible control-plane latch. Once true, no research replan may be entered.
- `control_no_progress`: irreversible convergence kill-switch latch.

`max_replan_count` caps attempts as well as applied patches. A rejected proposal therefore consumes an attempt.

## 3. Routing Matrix

`route_progress` and `route_after_quality` both use the same authority:

| Condition | Route |
| --- | --- |
| Quality PASS | finalize |
| `replan_exhausted` or `control_no_progress` | finalize |
| attempts >= max attempts | finalize |
| repairable synthesis issue, first attempt | repair_synthesis |
| research gap, attempts remaining | replan |
| otherwise | finalize as partial |

Synthesis having completed does not grant another research attempt. A quality failure after synthesis can only repair synthesis, replan while attempts remain, or finalize partial.

## 4. Landscape Gap Patch Contract

For `task_kind=landscape_discovery`, follow-up research must be a candidate-scoped `gap_fill`, not a category-wide `deep_dive`.

A valid gap-fill task requires:

- non-empty `resolves_gap_ids`;
- non-empty `coverage_keys`;
- `subject_id` identifying a concrete candidate, not the landscape category;
- `entities=[candidate]`;
- `requires_artifacts=["candidate_set"]`;
- an available CandidateSet produced by a Discovery task.

Example:

```text
t_gap_deepseek_career
task_kind=gap_fill
subject_id=deepseek
coverage_keys=[招聘与人才机会, 加入风险]
resolves_gap_ids=[gap_hiring, gap_risk]
```

If no usable CandidateSet exists, the patch is empty and the run converges to synthesis/partial rather than proposing an invalid category task.

## 5. Control Fingerprint

The control plane fingerprint contains only stable control signals:

- plan version;
- task status;
- open gap IDs;
- replan exhausted / attempts;
- quality reason and repair action;
- synthesis status.

Transient counters such as quality attempts are excluded. Two consecutive identical fingerprints set `control_no_progress`, force `replan_exhausted`, and route to partial finalization. The LangGraph recursion limit is no longer the first defense.

## 6. Budget-Blocked Evidence Salvage

`BudgetReservationError` is a run-control failure, not an evidence failure. When a
worker is blocked after tools have already produced artifacts, the Worker adapter
reuses the existing artifact-backed salvage path:

1. collect artifacts associated with the current task or step;
2. normalize them into partial findings, evidence IDs, and source locators;
3. return them in the blocked `WorkerResult`;
4. project `partial_evidence_count` into the worker row and trace;
5. allow CandidateSet construction, synthesis admission, and Emergency Synthesis
   to consume those findings.

Tool outputs are tagged with the current worker task ID when they enter the
ArtifactStore. Salvage therefore remains worker-scoped even when a tool does not
know its plan step index. Structured tool JSON is parsed from the full artifact
content before falling back to the truncated summary, so the partial report shows
human-readable title/content evidence instead of raw JSON.

A blocked worker with no artifacts remains a failure and is not converted into a
fabricated finding. This distinction is enforced by only deriving findings from
actual artifact IDs and locators.

The deterministic partial report also renders the recovered source locators, so a
budget-limited PDF still distinguishes verifiable evidence from missing coverage.

## 7. Verification

Regression coverage includes:

1. rejected replan cannot be re-entered from quality;
2. rejected replan consumes an attempt;
3. landscape gap patch is candidate-scoped `gap_fill`;
4. broad or metadata-incomplete gap fill is rejected;
5. budget-blocked workers retain artifact-backed partial evidence;
6. worker rows feed CandidateSet and synthesis admission;
7. salvaged tool JSON is rendered readably with source links;
8. full graph converges in fewer than 20 supersteps;
9. example PDF query still produces a valid PDF artifact.
