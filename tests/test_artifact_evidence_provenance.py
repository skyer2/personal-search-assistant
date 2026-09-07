from __future__ import annotations

from app.agent.harness.artifacts import ArtifactStore, set_artifact_store
from app.agent.harness.tool_contract import apply_tool_output_contract
from app.research.execution.tool_gateway import ToolGateway
from app.research.runtime.worker import salvage_worker_evidence


def test_search_artifact_has_task_step_and_run_provenance():
    store = ArtifactStore()
    set_artifact_store(store)
    raw = {
        "query": "AI startup",
        "results": [
            {
                "title": "AI startup landscape",
                "url": "https://example.com/landscape",
                "content": "Candidate companies and funding milestones.",
                "raw_content": "Candidate companies and funding milestones.",
            }
        ],
    }
    with ToolGateway(None).execution_scope(
        worker_task_id="t_landscape",
        step_index=0,
        run_id="run-artifact",
        session_id="session-artifact",
    ):
        apply_tool_output_contract(
            raw,
            tool_name="internet_search",
            step_type="network_search",
        )

    artifact = next(iter(store.iter_artifacts()))
    assert artifact.step_index == 0
    assert artifact.metadata["task_id"] == "t_landscape"
    assert artifact.metadata["run_id"] == "run-artifact"
    assert artifact.metadata["session_id"] == "session-artifact"


def test_salvage_by_task_id_and_step_index():
    store = ArtifactStore()
    set_artifact_store(store)
    task_artifact = store.put(
        "task provenance",
        kind="web",
        locator="https://example.com/task",
        metadata={"task_id": "t_landscape"},
        step_index=7,
    )
    step_artifact = store.put(
        "step provenance",
        kind="web",
        locator="https://example.com/step",
        metadata={"task_id": "other_task"},
        step_index=0,
    )

    by_task = salvage_worker_evidence(task_id="t_landscape", step_index=99)
    by_step = salvage_worker_evidence(task_id="not_this_task", step_index=0)
    assert by_task["evidence_refs"] == [task_artifact.artifact_id]
    assert by_step["evidence_refs"] == [step_artifact.artifact_id]
