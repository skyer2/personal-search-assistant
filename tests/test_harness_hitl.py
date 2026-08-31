"""HITL coordinator 与配置加载测试。"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.hitl import HitlCoordinator
from app.config.loader import reload_harness_config


def test_hitl_config_loaded():
    config = reload_harness_config()
    assert config.hitl_enabled is False
    assert config.hitl_interrupt_on.get("generate_markdown") is True
    assert config.hitl_step_gate_types == []
    print("[OK] hitl config")


def test_hitl_coordinator_flow():
    coordinator = HitlCoordinator()

    async def _run() -> list[dict]:
        async def worker() -> list[dict]:
            payload = {
                "action_requests": [{"name": "generate_markdown", "args": {"filename": "a.md"}}],
                "review_configs": [
                    {"action_name": "generate_markdown", "allowed_decisions": ["approve", "reject"]}
                ],
            }
            return await coordinator.wait_for_decisions("sess-1", payload, timeout_sec=2)

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.05)
        ok = coordinator.submit_decisions("sess-1", [{"type": "approve"}])
        assert ok is True
        decisions = await task
        return decisions

    result = asyncio.run(_run())
    assert result[0]["type"] == "approve"
    print("[OK] hitl coordinator approve flow")


if __name__ == "__main__":
    test_hitl_config_loaded()
    test_hitl_coordinator_flow()
    print("\n=== HITL tests passed ===")
