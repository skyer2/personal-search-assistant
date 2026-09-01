"""Phase 7: 多 Agent 编排单元测试（无需 LLM）。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.orchestration import (
    IdempotencyRegistry,
    StepCheckpointStore,
    check_subagent_binding,
    check_unauthorized_tools,
    find_parallel_batch,
    mark_parallel_retrieval_groups,
    parse_worker_payload,
    step_idempotency_key,
)
from app.agent.harness.planner import build_plan, finalize_plan, understand_task
from app.agent.harness.state import LoopState, Phase, PhaseEvent, PlanStep, StepResult, StepStatus
from app.config.loader import reload_harness_config


def test_parallel_group_marking():
    intent = understand_task("搜索公开资料并读取上传的附件，生成Markdown")
    intent.needs_file_read = True
    intent.needs_network = True
    plan = finalize_plan(build_plan(intent))
    groups = [s.metadata.get("parallel_group") for s in plan.steps if "parallel_group" in s.metadata]
    assert groups, "应标记至少一个并行组"
    assert max(groups) >= 0
    print(f"[OK] parallel groups={groups}")


def test_find_parallel_batch():
    intent = understand_task("搜索公开资料并读取附件，生成报告")
    intent.needs_file_read = True
    intent.needs_network = True
    plan = finalize_plan(build_plan(intent))
    batch = find_parallel_batch(plan.steps, 0, enabled=True)
    assert len(batch) >= 2
    print(f"[OK] parallel batch indices={batch}")


def test_structured_worker_payload():
    raw = json.dumps(
        {
            "ok": True,
            "summary": "机器人行业增速15%",
            "facts": ["2025增速15%"],
            "sources": ["https://example.com"],
            "worker": "网络搜索助手",
            "step_type": "network_search",
        },
        ensure_ascii=False,
    )
    payload = parse_worker_payload(raw, step_type="network_search", subagent="网络搜索助手")
    assert payload.ok is True
    assert "15%" in payload.summary
    assert payload.sources
    print("[OK] structured worker payload")


def test_binding_and_unauthorized_tools():
    step = PlanStep(step_type="network_search", description="搜", subagent="网络搜索助手")
    ok, reason = check_subagent_binding(step, [], enforce=True)
    assert not ok and reason == "wrong_subagent"

    ok2, bad = check_unauthorized_tools(
        step,
        ["generate_markdown"],
        enforce=True,
    )
    assert not ok2 and "generate_markdown" in bad
    ok3, bad3 = check_unauthorized_tools(
        step,
        ["read_artifact", "read_evidence"],
        enforce=True,
    )
    assert ok3 and not bad3
    print("[OK] binding + unauthorized tool checks")


def test_checkpoint_roundtrip(tmp_path: Path):
    store = StepCheckpointStore(tmp_path)
    results = [
        StepResult(step_type="network_search", content="ok", compressed_content="ok"),
    ]
    key = step_idempotency_key("sess1", 0, "network_search")
    store.save(
        session_id="sess1",
        task_fingerprint="abc",
        next_step_index=1,
        step_results=results,
        assistants_called=["网络搜索助手"],
        completed_keys=[key],
        plan_summary="plan",
    )
    data = store.load()
    assert data is not None
    assert data["next_step_index"] == 1
    restored = store.restore_step_results(data)
    assert restored[0].step_type == "network_search"

    registry = IdempotencyRegistry()
    registry.load_from_checkpoint(data, store)
    assert registry.get(key) is not None
    print("[OK] checkpoint + idempotency")


def test_harness_config_orchestration():
    reload_harness_config()
    from app.config.loader import get_harness_config

    cfg = get_harness_config()
    assert cfg.parallel_retrieval_enabled is True
    assert cfg.max_parallel_workers >= 1
    assert cfg.step_timeout_sec >= 10
    assert cfg.enforce_subagent_binding is True
    assert cfg.direct_worker_invoke is True
    assert cfg.persist_loop_state is False
    assert cfg.graph_runtime_enabled is True
    assert cfg.progress_eval_enabled is True
    assert cfg.graph_checkpoint_backend == "sqlite"
    assert cfg.max_step_tool_calls >= 1
    print("[OK] orchestration config loaded")


def test_step_status_enum():
    assert StepStatus.DONE.value == "done"
    assert StepStatus.PENDING.value == "pending"
    print("[OK] step status enum")


def test_parallel_child_delta_merge_is_additive():
    from app.agent.harness.loop import AgentHarness

    parent = LoopState(session_id="parent")
    parent.phase = Phase.PARALLEL_EXECUTE
    parent.tool_calls_count = 2
    parent.obs_structured_checks = 1
    parent.obs_step_message_tokens_peak = 100
    child = LoopState(session_id="parent")
    child.phase = Phase.VALIDATE
    child.tool_calls_count = 3
    child.obs_structured_checks = 2
    child.obs_step_message_tokens_peak = 250
    child.compression_ratios = [0.5]
    child.trace = [PhaseEvent(phase="execute", status="done")]

    AgentHarness._merge_parallel_child_state(parent, child)

    assert parent.phase == Phase.PARALLEL_EXECUTE
    assert parent.tool_calls_count == 5
    assert parent.obs_structured_checks == 3
    assert parent.obs_step_message_tokens_peak == 250
    assert parent.compression_ratios == [0.5]
    assert len(parent.trace) == 1


if __name__ == "__main__":
    import tempfile

    test_parallel_group_marking()
    test_find_parallel_batch()
    test_structured_worker_payload()
    test_binding_and_unauthorized_tools()
    with tempfile.TemporaryDirectory() as td:
        test_checkpoint_roundtrip(Path(td))
    test_harness_config_orchestration()
    test_step_status_enum()
    print("\n=== Orchestration tests passed ===")
