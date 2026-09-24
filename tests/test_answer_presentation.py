"""Final-answer presentation: question grouping, explicit citations, closure."""

from __future__ import annotations

from app.research.delivery.answer_renderer import (
    render_answer,
    validate_reference_closure,
)
from app.research.delivery.answer_view_builder import (
    build_partial_answer_view,
    canonicalize_sources,
)
from app.research.delivery.answer_contract import FinalAnswer, QuestionAnswer
from app.research.delivery.final_renderer import render_final_view
from app.research.delivery.view_builder import build_answer_view
from app.research.delivery.text_normalizer import normalize_claim_text


BRIEF = {
    "key_questions": [
        "2026年9月 Agent 最新热点是什么？",
        "Agent 未来1-2年的发展方向是什么？",
    ],
    "research_questions": [
        {
            "question_id": "q1",
            "ask_id": "A1",
            "text": "2026年9月 Agent 最新热点是什么？",
        },
        {
            "question_id": "q2",
            "ask_id": "A2",
            "text": "Agent 未来1-2年的发展方向是什么？",
        },
    ],
}

SOURCES = [
    {
        "evidence_id": "e1",
        "locator": "https://openai.com/research/agents",
        "publisher": "OpenAI",
        "title": "Agent Research Update",
        "published_at": "2026-09-01",
        "source_type": "primary",
    },
    {
        "evidence_id": "e2",
        "locator": "https://www.reuters.com/technology/agents",
        "publisher": "Reuters",
        "title": "Enterprises Adopt Agents",
        "published_at": "2026-08-20",
        "source_type": "authoritative_secondary",
    },
]


def _view():
    return build_partial_answer_view(
        brief=BRIEF,
        claims=[
            {
                "claim_id": "c1",
                "question_id": "q1",
                "text": "企业级 Agent 正从 Demo 转向真实业务流程部署。",
                "evidence_ids": ["e2"],
                "confidence": 0.82,
            },
            {
                "claim_id": "c2",
                "question_id": "q2",
                "text": "Agent 协议与工具生态正在走向标准化。",
                "evidence_ids": ["e1"],
                "confidence": 0.9,
            },
        ],
        bindings=[],
        coverage={
            "key_question_coverage": [
                {
                    "question_id": "q1",
                    "status": "partial",
                    "missing_requirements": ["primary_source"],
                },
                {"question_id": "q2", "status": "supported"},
            ]
        },
        source_registry=SOURCES,
    )


def test_citation_never_enters_question_or_status_area():
    report = render_answer(_view())
    headings = [
        line
        for line in report.splitlines()
        if line.startswith("#") or line.startswith("**状态")
    ]
    assert headings
    assert all("[" not in line for line in headings)
    assert "2026年9月 Agent 最新热点是什么？[1]" not in report


def test_partial_is_grouped_by_question_with_independent_statuses():
    view = _view()
    assert len(view.sections) == 2
    assert view.sections[0].question_id == "q1"
    assert view.sections[0].status == "partial"
    assert view.sections[1].question_id == "q2"
    assert view.sections[1].status == "confirmed"


def test_raw_search_snippet_and_article_frame_never_enter_body():
    view = build_partial_answer_view(
        brief={"key_questions": ["近期变化是什么？"]},
        claims=[
            {
                "claim_id": "bad",
                "text": "智通财经APP获悉,AI、算力领域近来利好颇多...",
                "evidence_ids": ["e1"],
            },
            {
                "claim_id": "good",
                "text": "企业正在增加对 Agent 可靠性评测的投入。",
                "evidence_ids": ["e1"],
            },
        ],
        bindings=[],
        coverage={},
        source_registry=SOURCES,
    )
    report = render_answer(view)
    assert "智通财经APP获悉" not in report
    assert "企业正在增加" in report


def test_evidence_ledger_is_not_rendered_even_with_many_records():
    records = [
        {
            "evidence_id": f"e{index}",
            "locator": f"https://source{index}.example/report",
            "source_type": "secondary",
            "excerpt_ref": f"内部摘录 {index}",
            "supported_claim_ids": [f"c{index}"],
        }
        for index in range(1, 25)
    ]
    view = build_partial_answer_view(
        brief={"key_questions": ["问题"]},
        claims=[
            {
                "claim_id": "c1",
                "text": "这是一个能够展示的完整研究结论。",
                "evidence_ids": ["e1"],
            }
        ],
        bindings=[],
        coverage={},
        source_registry=records,
    )
    report = render_answer(view)
    assert "## 证据" not in report
    assert "摘录：" not in report
    assert "支持信息：" not in report
    assert "内部摘录" not in report
    assert len(view.references) == 1


def test_reference_closure_rejects_missing_and_orphan_numbers():
    report = render_answer(_view())
    closure = validate_reference_closure(report)
    assert closure.passed
    assert closure.body_citations == closure.reference_numbers

    missing = report.replace("[1] Reuters", "[9] Reuters")
    assert not validate_reference_closure(missing).passed
    orphan = report + "\n[99] Orphan，《Unused》，https://unused.example\n"
    assert not validate_reference_closure(orphan).passed


def test_community_only_claim_cannot_be_confirmed():
    view = build_partial_answer_view(
        brief={"key_questions": ["热点是什么？"]},
        claims=[
            {
                "claim_id": "c1",
                "text": "某社区认为 Agent 市场正在快速扩张。",
                "evidence_ids": ["community"],
            }
        ],
        bindings=[],
        coverage={
            "key_question_coverage": [
                {"question_id": "q1", "status": "supported"}
            ]
        },
        source_registry=[
            {
                "evidence_id": "community",
                "locator": "https://blog.csdn.net/example",
                "source_type": "community",
            }
        ],
    )
    assert view.sections[0].status == "partial"
    assert view.sections[0].answer_points[0].evidence_quality == "weak"
    assert "社区或未知来源" in (view.sections[0].gap_note or "")


def test_syndicated_sources_canonicalize_to_one_reference():
    records = [
        {
            "evidence_id": "e1",
            "locator": "https://original.example/news/agent",
            "title": "企业 Agent 开始进入生产流程",
            "excerpt_ref": "企业 Agent 开始进入生产流程并增加可靠性投入。",
            "source_type": "authoritative_secondary",
        },
        {
            "evidence_id": "e2",
            "locator": "https://repost.example/a",
            "title": "企业Agent开始进入生产流程",
            "excerpt_ref": "企业 Agent 开始进入生产流程并增加可靠性投入。",
            "source_type": "secondary",
        },
        {
            "evidence_id": "e3",
            "locator": "https://repost-two.example/b",
            "title": "企业 Agent 开始进入生产流程",
            "excerpt_ref": "企业 Agent 开始进入生产流程并增加可靠性投入。",
            "source_type": "community",
        },
    ]
    canonical, aliases = canonicalize_sources(records)
    assert len(canonical) == 1
    assert len({aliases["e1"], aliases["e2"], aliases["e3"]}) == 1


def test_chinese_punctuation_and_quotes_are_normalized():
    normalized = normalize_claim_text(
        "市场还在问“Agent 能做什么”,下半年..."
    )
    assert normalized == "市场还在问“Agent 能做什么”，下半年……。"


def test_provider_citation_numbers_are_removed_before_runtime_binding():
    normalized = normalize_claim_text("市场还在问 Agent 能做什么[7][8]。")
    assert normalized == "市场还在问 Agent 能做什么。"


def test_answer_contract_ignores_model_citation_numbers_and_caps_body_refs():
    records = [
        {"evidence_id": f"e{i}", "locator": f"https://source{i}.example/report", "title": f"Source {i}"}
        for i in range(1, 6)
    ]
    answer = FinalAnswer(
        objective="Q1",
        answers=[QuestionAnswer(
            question_id="q1",
            direct_answer="基于可验证来源的直接结论[97][98]。",
            evidence_refs=[f"e{i}" for i in range(1, 6)],
        )],
        overall_summary="Summary",
        synthesis_mode="evidence_bound_recovery",
        synthesis_degraded=True,
    )
    view = build_answer_view(answer=answer, evidence_records=records, questions=["Q1"])
    rendered = render_final_view(view)
    answer_line = next(line for line in rendered.splitlines() if line.startswith("**回答**"))
    assert answer_line.count("[") == 3
    assert "[97]" not in rendered and "[98]" not in rendered

