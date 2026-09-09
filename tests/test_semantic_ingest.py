from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.runtime.semantic_ingest import ingest_semantics
from app.research.spec.compiler import compile_research_spec


def _state():
    spec = compile_research_spec("OpenAI 的收入是多少？")
    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_openai",
                description="OpenAI revenue",
                objective="OpenAI revenue",
                metadata={"subject_id": "subject_1", "coverage_keys": ["key_fact"]},
            )
        ]
    )
    return {
        "run_id": "r",
        "research_spec": spec.to_dict(),
        "plan": plan.to_dict(),
        "tasks": {"t_openai": {"execution_status": "succeeded"}},
        "worker_results": [
            {
                "task_id": "t_openai",
                "ok": True,
                "summary": "OpenAI revenue 10 亿美元",
                "payload": {
                    "facts": ["OpenAI revenue 10 亿美元"],
                    "sources": ["https://openai.com/revenue", "https://reuters.com/openai"],
                    "confidence": 0.9,
                },
            }
        ],
    }


def test_worker_result_becomes_evidence_claim_and_covered_unit():
    update = ingest_semantics(_state())
    assert len(update["evidence_records"]) == 2
    assert update["claims"]
    assert update["coverage_state"]["covered_ids"]
    assert not update["semantic_gaps"]


def test_rejected_evidence_never_supports_claim():
    state = _state()
    state["worker_results"][0]["payload"]["sources"] = []
    state["worker_results"][0]["payload"]["evidence_ids"] = []
    update = ingest_semantics(state)
    assert update["evidence_records"] == []
    assert update["claims"] == []
    assert update["coverage_state"]["missing_ids"]
    assert update["semantic_gaps"]
