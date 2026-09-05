"""Phase 20: while 外环 + 按步工人 + LoopState 落库（无需 LLM）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.citations import CitationManager, EvidenceSource
from app.agent.harness.context_builder import ContextBuilder
from app.agent.harness.loop_state_store import deserialize_loop_state, serialize_loop_state
from app.agent.harness.orchestration import StepCheckpointStore, check_subagent_binding
from app.agent.harness.state import (
    ExecutionPlan,
    LoopState,
    Phase,
    PlanStep,
    StepResult,
    TaskIntent,
)
from app.agent.harness.worker_runtime import (
    resolve_execute_target,
    worker_tools_for_step,
)
from app.config.loader import get_harness_config, reload_harness_config


def test_resolve_direct_worker_not_main():
    main = object()
    worker = object()
    agent, mode = resolve_execute_target(
        "network_search",
        workers={"network_search": worker},
        main_agent=main,
        direct_invoke=True,
    )
    assert agent is worker
    assert mode == "direct"

    agent2, mode2 = resolve_execute_target(
        "generate_markdown",
        workers={"network_search": worker, "generate_markdown": worker},
        main_agent=main,
        direct_invoke=True,
    )
    assert agent2 is worker
    assert mode2 == "direct"

    agent3, mode3 = resolve_execute_target(
        "network_search",
        workers={"network_search": worker},
        main_agent=main,
        direct_invoke=False,
    )
    assert agent3 is main
    assert mode3 == "main"
    print("[OK] resolve_execute_target dispatch")


def test_worker_tool_isolation_table():
    assert "internet_search" in worker_tools_for_step("network_search")
    assert "generate_markdown" not in worker_tools_for_step("network_search")
    assert "read_file_content" in worker_tools_for_step("file_read")
    assert "internet_search" not in worker_tools_for_step("file_read")
    print("[OK] worker tool isolation table")


def test_loop_state_roundtrip():
    state = LoopState(session_id="s1", phase=Phase.EXECUTE)
    state.task_fingerprint = "fp"
    state.intent = TaskIntent(raw_query="搜索并写报告", summary="搜索")
    state.intent.needs_network = True
    state.intent.deliverable = "md"
    state.plan = ExecutionPlan(
        summary="两步",
        steps=[
            PlanStep(step_type="network_search", description="搜索", subagent="网络搜索助手"),
            PlanStep(step_type="generate_markdown", description="写报告"),
        ],
    )
    state.step_index = 1
    state.step_results = [
        StepResult(
            step_type="network_search",
            content="搜索结果",
            metadata={"worker_dispatch": "direct"},
        )
    ]
    state.assistants_called = ["网络搜索助手"]
    state.working_notes = "已确认库存 12"
    state.metadata["hitl_waiting"] = {"gate_type": "step", "step_index": 1}

    payload = serialize_loop_state(state)
    restored = deserialize_loop_state(payload)
    assert restored.session_id == "s1"
    assert restored.plan is not None
    assert restored.plan.steps[0].subagent == "网络搜索助手"
    assert restored.working_notes.startswith("已确认")
    assert restored.metadata["hitl_waiting"]["gate_type"] == "step"
    assert restored.step_results[0].metadata["worker_dispatch"] == "direct"
    print("[OK] LoopState serialize/deserialize")


def test_loop_state_metadata_is_workflow_safe():
    state = LoopState(session_id="runtime-metadata")
    state.metadata["_run_budget_manager"] = object()
    state.metadata["followup_context"] = {"safe": True}

    payload = serialize_loop_state(state)

    assert "_run_budget_manager" not in payload["metadata"]
    assert payload["metadata"]["followup_context"] == {"safe": True}

    state.metadata["callback_manager"] = object()
    try:
        serialize_loop_state(state)
    except TypeError as exc:
        assert "metadata.callback_manager" in str(exc)
    else:
        raise AssertionError("non-serializable metadata must fail explicitly")
    print("[OK] loop state metadata is workflow-safe")


def test_checkpoint_contains_loop_state(tmp_path: Path):
    store = StepCheckpointStore(tmp_path)
    state = LoopState(session_id="sess", phase=Phase.PLAN)
    state.task_fingerprint = "abc"
    state.plan = ExecutionPlan(summary="p", steps=[PlanStep("network_search", "搜")])
    from app.agent.harness.loop_state_store import serialize_loop_state

    store.save(
        session_id="sess",
        task_fingerprint="abc",
        next_step_index=0,
        step_results=[],
        assistants_called=[],
        completed_keys=[],
        plan_summary="p",
        loop_state=serialize_loop_state(state),
        citation_snapshot={"sources": [], "fact_bindings": [], "counter": 0},
    )
    data = store.load()
    assert data is not None
    assert data.get("authority") == "loop_state"
    assert data["loop_state"]["plan"]["summary"] == "p"
    print("[OK] checkpoint.json 含 LoopState")


def test_citation_snapshot_roundtrip():
    mgr = CitationManager()
    mgr.sources.append(
        EvidenceSource(
            source_id="src-1",
            step_index=0,
            step_type="network_search",
            source_kind="url",
            locator="https://example.com",
            excerpt="增速 15%",
        )
    )
    mgr._counter = 1
    mgr.fact_bindings.append({"fact": "增速 15%", "source_id": "src-1"})
    snap = mgr.checkpoint_snapshot()
    other = CitationManager()
    other.load_from_snapshot(snap)
    assert other.sources[0].locator == "https://example.com"
    assert other.fact_bindings[0]["source_id"] == "src-1"
    print("[OK] citation snapshot")


def test_direct_binding_prompt():
    builder = ContextBuilder()
    step = PlanStep(step_type="network_search", description="搜", subagent="网络搜索助手")
    text = builder.build_subagent_binding_instruction(
        step, enforce=True, dispatch_mode="direct"
    )
    assert "直调工人" in text
    assert "task" in text.lower() or "不要" in text
    legacy = builder.build_subagent_binding_instruction(
        step, enforce=True, dispatch_mode="main"
    )
    assert "task 工具" in legacy
    print("[OK] binding prompt 区分直调 / task")


def test_direct_dispatch_satisfies_binding():
    step = PlanStep(step_type="network_search", description="搜", subagent="网络搜索助手")
    ok, _ = check_subagent_binding(step, ["网络搜索助手"], enforce=True)
    assert ok
    print("[OK] 直调后 stamp 助手名可通过绑定校验")


def test_config_phase20_flags():
    reload_harness_config()
    cfg = get_harness_config()
    assert cfg.direct_worker_invoke is True
    assert cfg.persist_loop_state is False
    assert cfg.graph_runtime_enabled is True
    print("[OK] phase20 config flags")


if __name__ == "__main__":
    import tempfile

    test_resolve_direct_worker_not_main()
    test_worker_tool_isolation_table()
    test_loop_state_roundtrip()
    with tempfile.TemporaryDirectory() as td:
        test_checkpoint_contains_loop_state(Path(td))
    test_citation_snapshot_roundtrip()
    test_direct_binding_prompt()
    test_direct_dispatch_satisfies_binding()
    test_config_phase20_flags()
    print("\n=== Phase 20 runtime tests passed ===")
