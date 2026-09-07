"""Phase 7: 多 Agent 编排单元测试（无需 LLM）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.orchestration import (
    check_subagent_binding,
    check_unauthorized_tools,
)
from app.agent.harness.planner import build_plan, finalize_plan, understand_task
from app.agent.harness.state import PlanStep, StepStatus
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


def test_harness_config_orchestration():
    reload_harness_config()
    from app.config.loader import get_harness_config

    cfg = get_harness_config()
    assert cfg.parallel_retrieval_enabled is True
    assert cfg.max_parallel_workers >= 1
    assert cfg.step_timeout_sec >= 10
    assert cfg.enforce_subagent_binding is True
    assert cfg.direct_worker_invoke is True
    assert cfg.progress_eval_enabled is True
    assert cfg.graph_checkpoint_backend == "sqlite"
    assert cfg.max_step_tool_calls >= 1
    print("[OK] orchestration config loaded")


def test_step_status_enum():
    assert StepStatus.DONE.value == "done"
    assert StepStatus.PENDING.value == "pending"
    print("[OK] step status enum")


if __name__ == "__main__":
    test_parallel_group_marking()
    test_binding_and_unauthorized_tools()
    test_harness_config_orchestration()
    test_step_status_enum()
    print("\n=== Orchestration tests passed ===")
