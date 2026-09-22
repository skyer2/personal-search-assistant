"""Regression suite for the answer-quality main chain (SDD §36).

Case 1  the real drifting query keeps both user asks
Case 2  a Brief LLM timeout still preserves the original asks
Case 3  worker timeout consumes execution retry, not a semantic wave
Case 4  coverage gap can never produce COMPLETE
Case 5  synthesis provider failure yields validated claims only
Case 6  generic boilerplate fails the Relevance Gate
Case 7  tier3-only sources cannot support a core claim
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.research.brief.compiler import (
    compile_structured_brief,
    compile_structured_brief_with_llm,
)
from app.research.brief.models import StructuredResearchBrief
from app.research.control.runtime_policy import decide_control
from app.research.coverage.judge import CoverageJudgement, judge_coverage
from app.research.delivery.answer_contract import (
    assess_answerability,
    compile_evidence_bound_answer,
    render_final_answer,
)
from app.research.domain.failure import classify_failure
from app.research.domain.research_budget import (
    is_execution_failure,
    is_execution_recovery_pass,
)
from app.research.domain.task_state import TaskExecutionStatus, transition_task
from app.research.evidence.source_tier import classify_source_tier, evaluate_source_quality
from app.research.intent import UserAskContract, evaluate_semantic_fidelity
from app.research.quality import evaluate_relevance
from app.research.runtime.state import empty_research_state
from app.research.supervisor.agent import SupervisorAgent
from app.research.supervisor.models import SupervisorAction

DRIFT_QUERY = "2026年9月 agent最新的热点是什么？你觉得agent未来1-2年的发展方向是什么呢？"

# The exact generic questions the old deterministic fallback invented.
FORBIDDEN_TEMPLATE_QUESTIONS = (
    "当前主要关注点和工程路径是什么？",
    "未来一段时间可验证的进展有哪些？",
    "这些判断的主要不确定性和依据是什么？",
)


def _assert_no_template_drift(brief: StructuredResearchBrief) -> None:
    for question in brief.key_questions:
        assert question not in FORBIDDEN_TEMPLATE_QUESTIONS, question


# --------------------------------------------------------------------------- 1


def test_case1_real_query_keeps_both_user_asks():
    brief = compile_structured_brief(DRIFT_QUERY)
    _assert_no_template_drift(brief)

    assert len(brief.user_asks) == 2
    a1, a2 = brief.user_asks
    assert "agent" in a1.text.lower()
    assert "热点" in a1.text
    assert a1.time_scope == "2026年9月"
    assert a1.ask_type == "current_state"

    assert "agent" in a2.text.lower()
    assert "未来" in a2.text
    assert "1-2年" in a2.time_scope
    assert a2.ask_type == "forecast"

    # Every research question keeps lineage back to an ask.
    assert [q.ask_id for q in brief.research_questions] == ["A1", "A2"]
    fidelity = evaluate_semantic_fidelity(
        UserAskContract("c", DRIFT_QUERY, brief.user_asks), brief
    )
    assert fidelity.passed
    assert fidelity.ask_coverage == 1.0


# --------------------------------------------------------------------------- 2


class _TimingOutBriefModel:
    """Stands in for a Brief provider that always times out."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, *_args, **_kwargs):
        self.calls += 1
        raise asyncio.TimeoutError("brief provider timeout")

    def with_structured_output(self, *_args, **_kwargs):
        return self


def test_case2_brief_timeout_falls_back_without_reinventing_questions():
    from app.agent.harness.run_budget import RunBudgetManager

    model = _TimingOutBriefModel()
    brief = asyncio.run(
        compile_structured_brief_with_llm(
            DRIFT_QUERY,
            agent=model,
            budget_manager=RunBudgetManager(
                token_limit=10_000,
                llm_call_limit=4,
                tool_call_limit=4,
                deadline_sec=60,
            ),
        )
    )
    assert brief.compiler_source == "deterministic_fallback"
    _assert_no_template_drift(brief)
    assert len(brief.user_asks) == 2
    assert brief.key_questions[0].startswith("2026年9月")
    assert "未来1-2年" in brief.key_questions[1]


def test_case2_llm_brief_dropping_an_ask_is_rejected():
    """A structurally valid Brief that loses an ask must not pass the gate."""
    fallback = compile_structured_brief(DRIFT_QUERY)
    drifted = StructuredResearchBrief.from_dict(
        {
            **fallback.to_dict(),
            "key_questions": ["当前主要关注点和工程路径是什么？"],
            "research_questions": [
                {"question_id": "q1", "ask_id": "A1", "text": "当前主要关注点和工程路径是什么？"}
            ],
        }
    )
    fidelity = evaluate_semantic_fidelity(
        UserAskContract("c", DRIFT_QUERY, fallback.user_asks), drifted
    )
    assert not fidelity.passed
    assert "A2" in fidelity.missing_asks
    assert "missing_required_ask" in fidelity.issues


# --------------------------------------------------------------------------- 3


def _state_with_failed_worker(code: str) -> dict:
    state = empty_research_state(
        run_id="budget-split", session_id="budget-split", task_query=DRIFT_QUERY
    )
    running = transition_task(
        state["tasks"], "task_1", execution_status=TaskExecutionStatus.RUNNING
    )
    state["tasks"] = transition_task(
        running,
        "task_1",
        execution_status=TaskExecutionStatus.FAILED,
        attempt=1,
        failure=dict(classify_failure(code)),
    )
    return state


def test_case3_worker_timeout_is_execution_not_semantic():
    state = _state_with_failed_worker("timeout")
    assert is_execution_failure(state["tasks"]["task_1"]["failure"])
    assert is_execution_recovery_pass(state)
    # Runtime recovers by retrying, it does not ask for a new research strategy.
    assert decide_control(state).action == "retry"


def test_case3_semantic_gap_does_not_count_as_execution_failure():
    state = _state_with_failed_worker("coverage_gap")
    assert not is_execution_failure(state["tasks"]["task_1"]["failure"])
    assert not is_execution_recovery_pass(state)


def test_case3_execution_retry_does_not_increment_semantic_iteration():
    """An execution-recovery pass keeps the semantic iteration unchanged."""
    state = _state_with_failed_worker("timeout")
    previous_iteration = 1
    state["supervisor"] = {"iteration": previous_iteration}
    state["plan"] = {"steps": []}

    recovery = is_execution_recovery_pass(state)
    semantic_iteration = previous_iteration + (
        1 if state.get("plan") and not recovery else 0
    )
    execution_retries = int(state.get("execution_retries") or 0) + (1 if recovery else 0)

    assert semantic_iteration == previous_iteration
    assert execution_retries == 1


# --------------------------------------------------------------------------- 4


def _gap_judgement() -> CoverageJudgement:
    brief = compile_structured_brief(DRIFT_QUERY)
    judgement = judge_coverage(brief, [], evidence=[], claims=[])
    assert not judgement.sufficient
    return judgement


def test_case4_budget_exhaustion_with_gap_is_stop_not_complete():
    supervisor = SupervisorAgent(agent=None)
    action = supervisor.fallback_action(
        compile_structured_brief(DRIFT_QUERY),
        _gap_judgement(),
        {"exhausted": True},
    )
    assert action.action == "STOP_BUDGET_PARTIAL"
    assert action.action != "COMPLETE"


def test_case4_model_complete_is_downgraded_when_coverage_has_a_gap():
    """Even if the Supervisor model says COMPLETE, a gap forbids completion."""
    resolved = SupervisorAgent.enforce_completion_invariant(
        SupervisorAction("COMPLETE", "model thinks it is done"),
        _gap_judgement(),
        budget={},
    )
    assert resolved.action == "STOP_FAILURE"
    assert "coverage_gap_blocks_complete" in resolved.reason


def test_case4_complete_survives_when_coverage_is_sufficient():
    sufficient = CoverageJudgement(sufficient=True, status="sufficient")
    resolved = SupervisorAgent.enforce_completion_invariant(
        SupervisorAction("COMPLETE", "coverage ok"), sufficient, budget={}
    )
    assert resolved.action == "COMPLETE"


# --------------------------------------------------------------------------- 5


def _forecast_brief() -> StructuredResearchBrief:
    return compile_structured_brief(DRIFT_QUERY)


def test_case5_recovery_outputs_only_validated_claims():
    brief = _forecast_brief()
    findings = [
        {
            "finding_id": "f1",
            "claim": "MCP 1.2 在 2026 年 7 月加入 streaming tool result",
            "evidence_ids": ["e1"],
            "question_ids": ["q1"],
        }
    ]
    answerability = assess_answerability(
        brief=brief,
        findings=findings,
        evidence_records=[{"evidence_id": "e1"}],
    )
    recovered = compile_evidence_bound_answer(
        objective=brief.objective,
        brief=brief,
        findings=findings,
        answerability=answerability,
    )
    assert recovered.synthesis_mode == "evidence_bound_recovery"
    rendered = render_final_answer(recovered, citation_numbers={"e1": 1})

    # The recovered claim is present verbatim.
    assert "MCP 1.2" in rendered
    # None of the invented template conclusions may appear.
    for forbidden in (
        "更可能成为可验收交付的一部分",
        "基于当前证据，我判断",
        "可以形成方向性判断",
        "现有研究证据显示",
        "竞争重点正在从",
    ):
        assert forbidden not in rendered, forbidden
    # The forecast ask had no forecast evidence, so it stays unanswered.
    assert recovered.unresolved_questions
    assert "降级部分交付" in rendered


def test_case5_recovery_never_passes_the_completion_contract():
    from app.research.domain.completion import evaluate_completion

    brief = _forecast_brief()
    contract = {
        "final_answer": {
            "objective": brief.objective,
            "synthesis_mode": "evidence_bound_recovery",
            "answers": [
                {"question_id": "q1", "direct_answer": "A", "evidence_refs": ["e1"]},
                {"question_id": "q2", "direct_answer": "B", "evidence_refs": ["e1"]},
            ],
        }
    }
    completion = evaluate_completion(
        brief=brief,
        answer_contract=contract,
        evidence_records=[{"evidence_id": "e1"}],
        final_content="报告正文",
    )
    assert not completion.passed
    assert completion.failure_reason == "synthesis_recovered_not_model_written"
    assert completion.outcome == "partial"


# --------------------------------------------------------------------------- 6


BOILERPLATE_ANSWER = """# 报告
能力与产品形态：现有研究证据显示该方向已从单点观察转为需要持续验证的实践议题。
企业结果导向：现有研究证据显示该方向已从单点观察转为需要持续验证的实践议题。
可靠性与治理：竞争重点正在从单点能力展示转向能否在真实工作流中长期可靠交付结果。
未来 1-2 年，企业结果导向更可能成为可验收交付的一部分。
可观察里程碑：跨团队生产部署。
"""

CONCRETE_ANSWER = """# 2026年9月 agent 热点
Anthropic 在 2026 年 8 月发布 Claude Code 2.0，支持长任务断点续跑 [1]。
MCP 1.2 规范在 2026 年 7 月加入 streaming tool result [2]。
agent 未来1-2年方向：企业 runtime 标准化，当前信号为多家厂商公布 2027 路线图 [3]。
"""


def _contract_for(brief: StructuredResearchBrief, answer: str) -> dict:
    return {
        "objective": brief.objective,
        "answers": [
            {"question_id": "q1", "direct_answer": answer, "evidence_refs": ["e1"]},
            {"question_id": "q2", "direct_answer": answer, "evidence_refs": ["e2"]},
        ],
    }


def test_case6_boilerplate_answer_fails_relevance_gate():
    brief = compile_structured_brief(DRIFT_QUERY)
    metrics = evaluate_relevance(
        brief=brief,
        answer_contract=_contract_for(brief, BOILERPLATE_ANSWER),
        final_content=BOILERPLATE_ANSWER,
    )
    assert not metrics.passed
    assert not metrics.partial_passed
    assert "boilerplate_heavy" in metrics.blocking_reasons
    assert "low_specificity" in metrics.blocking_reasons
    assert metrics.boilerplate_ratio > 0.5


def test_case6_concrete_answer_passes_relevance_gate():
    brief = compile_structured_brief(DRIFT_QUERY)
    metrics = evaluate_relevance(
        brief=brief,
        answer_contract=_contract_for(brief, CONCRETE_ANSWER),
        final_content=CONCRETE_ANSWER,
    )
    assert metrics.passed
    assert metrics.ask_answer_rate == 1.0
    assert metrics.boilerplate_ratio == 0.0


def test_case6_unanswered_required_ask_blocks_success_but_allows_partial():
    brief = compile_structured_brief(DRIFT_QUERY)
    contract = {
        "objective": brief.objective,
        "answers": [
            {"question_id": "q1", "direct_answer": CONCRETE_ANSWER, "evidence_refs": ["e1"]},
            {"question_id": "q2", "direct_answer": "当前证据不足，无法可靠回答这一问题。"},
        ],
    }
    metrics = evaluate_relevance(
        brief=brief, answer_contract=contract, final_content=CONCRETE_ANSWER
    )
    assert not metrics.passed
    assert "required_ask_without_direct_answer" in metrics.blocking_reasons
    assert metrics.partial_passed


# --------------------------------------------------------------------------- 7


def test_case7_tier3_only_sources_cannot_support_a_core_claim():
    records = [
        {"evidence_id": "e1", "locator": "https://blog.csdn.net/a/article/details/1"},
        {"evidence_id": "e2", "locator": "https://zhuanlan.zhihu.com/p/2"},
    ]
    assert all(classify_source_tier(row) == "tier3" for row in records)
    result = evaluate_source_quality(records, require_core_support=True)
    assert not result.passed
    assert not result.partial_passed
    assert result.reason == "core_claim_supported_only_by_tier3"


def test_case7_primary_or_two_independent_secondary_supports_core_claim():
    official = evaluate_source_quality(
        [{"evidence_id": "e1", "locator": "https://openai.com/index/agents"}],
        require_core_support=True,
    )
    assert official.passed

    secondary = evaluate_source_quality(
        [
            {"evidence_id": "e1", "locator": "https://www.reuters.com/tech/a"},
            {"evidence_id": "e2", "locator": "https://techcrunch.com/b"},
        ],
        require_core_support=True,
    )
    assert secondary.passed

    single_secondary = evaluate_source_quality(
        [{"evidence_id": "e1", "locator": "https://www.reuters.com/tech/a"}],
        require_core_support=True,
    )
    assert not single_secondary.passed
    # One reputable source is still good enough for a degraded partial.
    assert single_secondary.partial_passed


def test_case7_partial_delivery_requires_an_answerable_ask():
    """SDD §10/§31: bare evidence must not unlock partial delivery."""
    state = empty_research_state(run_id="r", session_id="s", task_query=DRIFT_QUERY)
    state["budget"]["exhausted"] = True
    state["evidence_records"] = [{"evidence_id": "e0"}]
    assert decide_control(state).action == "finalize_failure"

    state["answerability"] = {
        "answerable": True,
        "question_status": [
            {"question_id": "q1", "ask_id": "A1", "answerable": True, "supporting_evidence": ["e0"]}
        ],
    }
    assert decide_control(state).action == "deliver_partial"


def _run_module_tests() -> None:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"[OK] {name}")


if __name__ == "__main__":
    _run_module_tests()
    print("\n=== answer quality chain regressions passed ===")
