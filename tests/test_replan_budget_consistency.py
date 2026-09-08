from app.config.loader import get_harness_config
from app.research.domain.contracts import replan_budget_from_state
from app.research.routing.mode_router import budget_for_mode
from app.research.routing.task_shape import (
    TaskShape,
    execution_profile_for_shape,
)
from app.research.runtime.graph import intent_node
from app.research.runtime.state import empty_research_state


def test_breadth_heavy_replan_budget_has_single_effective_limit():
    config = get_harness_config()
    profile = execution_profile_for_shape(TaskShape.BREADTH_HEAVY)
    mode_budget = budget_for_mode("agent", config.personal_search)

    assert profile["max_replan_count"] == 2
    assert mode_budget["max_replan_count"] >= 2
    assert config.max_replan_count >= 2

    hard_limit = min(
        int(mode_budget["max_replan_count"]),
        int(config.max_replan_count),
    )
    state = empty_research_state(
        run_id="r-budget",
        session_id="s-budget",
        task_query="对比 OpenAI、Anthropic 和 Google 的 deep research 架构",
        max_replan_count=hard_limit,
    )
    update = intent_node(state)
    effective_state = {**state, **update}

    assert update["budget"]["max_replan_count"] == 2
    assert replan_budget_from_state(effective_state)["max_attempts"] == 2
