"""PDF 交付：意图锁定 + 确定性转 PDF（无需 LLM API）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.deliverables import (
    ensure_pdf_from_markdown,
    ensure_requested_deliverables,
    list_pdf_files,
)
from app.agent.harness.planner import auto_resolve_clarification, build_plan, understand_task
from app.agent.harness.planner_llm import merge_intent_from_llm
from app.agent.harness.state import LoopState, PlanStep, StepResult
from app.agent.harness.validator import ResultValidator

USER_PDF_QUERY = (
    "agent下一跳是什么？ agent目前实际落地进展是什么样的呢？ 输出一个pdf调研文档"
)


def test_user_query_locks_pdf_plan():
    intent = understand_task(USER_PDF_QUERY)
    assert intent.deliverable == "pdf"
    plan = build_plan(intent)
    assert [s.step_type for s in plan.steps][-2:] == ["generate_markdown", "convert_pdf"]


def test_llm_cannot_downgrade_pdf_to_text():
    rule = understand_task(USER_PDF_QUERY)
    merged = merge_intent_from_llm(
        rule,
        {
            "needs_network": True,
            "needs_file_read": False,
            "deliverable": "text",
            "confidence": 0.95,
            "reason": "这是一个问答，对话回答即可",
        },
    )
    assert merged.deliverable == "pdf"
    plan = build_plan(merged)
    assert "convert_pdf" in [s.step_type for s in plan.steps]


def test_auto_resolve_keeps_explicit_pdf():
    intent = understand_task(USER_PDF_QUERY)
    intent.needs_clarification = True
    intent.ambiguity_flags = ["deliverable_ambiguous"]
    resolved = auto_resolve_clarification(intent)
    assert resolved.deliverable == "pdf"
    assert resolved.slots.output_preference == "file_pdf"


def test_ensure_pdf_from_markdown_and_rglob(tmp_path: Path):
    nested = tmp_path / "subdir"
    nested.mkdir()
    md = nested / "行业调研.md"
    md.write_text("# Agent 落地调研\n\nOpenAI Operator 已发布。\n", encoding="utf-8")
    pdf = ensure_pdf_from_markdown(tmp_path)
    assert pdf is not None and pdf.exists()
    assert pdf.read_bytes()[:4] == b"%PDF"
    assert list_pdf_files(tmp_path)


def test_abort_still_writes_partial_pdf(tmp_path: Path):
    intent = understand_task(USER_PDF_QUERY)
    state = LoopState(session_id="pdf-abort")
    state.intent = intent
    state.abort_reason = "budget_tool_calls"
    state.final_content = ""
    state.step_results = [
        StepResult(
            step_type="independent_research",
            content="# 下一跳\n\n多智能体协作、长期记忆与 MCP 互操作是 2026 年主方向。",
        ),
        StepResult(
            step_type="independent_research",
            content="# 落地\n\n软件研发与客服已有可量化工时节省和 ROI 案例。",
        ),
    ]
    written = ensure_requested_deliverables(tmp_path, state)
    assert written["pdf"] is not None and written["pdf"].exists()
    assert written["pdf"].read_bytes()[:4] == b"%PDF"
    markdown = (written["md"].read_text(encoding="utf-8") if written["md"] else "")
    assert "budget_tool_calls" in markdown
    assert "下一跳" in markdown
    assert "落地" in markdown


def test_abort_does_not_convert_working_notes(tmp_path: Path):
    (tmp_path / "working_notes.md").write_text("# scratch\n\n内部笔记，不是报告。\n", encoding="utf-8")
    intent = understand_task(USER_PDF_QUERY)
    state = LoopState(session_id="pdf-notes")
    state.intent = intent
    state.abort_reason = "deadline_exceeded"
    state.final_content = ""
    state.step_results = [
        StepResult(
            step_type="independent_research",
            content="# 长程 agent\n\nMETR 时间视野倍增周期已缩短到约 3 个月。",
        ),
    ]
    written = ensure_requested_deliverables(tmp_path, state)
    assert written["pdf"] is not None and written["pdf"].exists()
    assert written["pdf"].name != "working_notes.pdf"
    assert written["md"] is not None
    assert written["md"].name != "working_notes.md"
    assert not (tmp_path / "working_notes.pdf").exists()
    report_pdfs = list_pdf_files(tmp_path, include_internal=False)
    assert report_pdfs and report_pdfs[0].name != "working_notes.pdf"
    markdown = written["md"].read_text(encoding="utf-8")
    assert "deadline_exceeded" in markdown
    assert "长程 agent" in markdown


def test_working_notes_alone_is_not_a_report_pdf(tmp_path: Path):
    (tmp_path / "working_notes.md").write_text("# notes\n\nscratch\n", encoding="utf-8")
    pdf = ensure_pdf_from_markdown(tmp_path, content="# 调研正文\n\n公开声明刹车踏板。\n", filename_stem="刹车踏板调研")
    assert pdf is not None
    assert pdf.name == "刹车踏板调研.pdf"
    assert not (tmp_path / "working_notes.pdf").exists()
    assert (tmp_path / "刹车踏板调研.md").exists()
    intent = understand_task(USER_PDF_QUERY)
    state = LoopState(session_id="pdf-empty-abort")
    state.intent = intent
    state.abort_reason = "budget_tool_calls"
    written = ensure_requested_deliverables(tmp_path, state)
    assert written["pdf"] is not None and written["pdf"].exists()
    markdown = (written["md"].read_text(encoding="utf-8") if written["md"] else "")
    assert "没有可用的研究报告正文" in markdown


def test_validator_writes_pdf_when_worker_skips_tool(tmp_path: Path):
    intent = understand_task(USER_PDF_QUERY)
    state = LoopState(session_id="pdf-test")
    state.intent = intent
    state.final_content = "成功转换: 假装已经有 PDF"
    state.step_results = [
        StepResult(
            step_type="generate_markdown",
            content="# Agent 落地进展\n\nAnthropic Claude Code 已达十亿美元年化收入。",
        ),
        StepResult(step_type="convert_pdf", content="成功转换: 未真正写盘"),
    ]
    validator = ResultValidator()
    step = PlanStep(step_type="convert_pdf", description="转 PDF")
    outcome = validator.validate_step(
        step,
        state.step_results[-1],
        tmp_path,
        state,
    )
    assert outcome.passed
    finalize = validator.validate_finalize(state, tmp_path)
    assert finalize.passed, finalize.reason
    pdfs = list_pdf_files(tmp_path)
    assert pdfs and pdfs[0].read_bytes()[:4] == b"%PDF"


CJK_LAYOUT_MD = """# 长中文排版

这是一段没有空格的中文长句用来验证自动换行是否会把文字挤出页边或者叠成一团。智能体编排、长期记忆与工具调用是当前落地主线。""" * 4 + """

## 对照表

| 维度 | 说明 | 证据 | 缺口 | 下一步 |
| --- | --- | --- | --- | --- |
| 编排 | 多智能体协作需要可恢复工作流与明确的阶段门。 | 公开报告 | 缺少一手访谈 | 补访谈 |
| 记忆 | 长期记忆仍依赖外部存储与检索。 | 论文 | 实现细节不足 | 复现实验 |

1. 第一项结论需要完整展示编号
2. 第二项结论同样不能丢

> 引用块用来检查左边栏和换行。

- 无序列表甲
- 无序列表乙

[来源](https://example.com/research/agent-harness/very-long-path)

---

```text
print("code-fence")
```
"""


def test_pdf_cjk_wrap_and_table_fits_page(tmp_path: Path):
    from reportlab.platypus import HRFlowable, Paragraph, Table

    from app.utils.word_converter import (
        _CONTENT_WIDTH,
        _build_styles,
        _format_inline,
        _markdown_to_story,
        convert_md_to_pdf,
    )

    styles = _build_styles()
    assert styles["body"].wordWrap == "CJK"
    assert styles["cell"].wordWrap == "CJK"
    assert styles["h1"].wordWrap == "CJK"
    assert styles["body"].fontName in {"PSA-CJK", "STSong-Light"}

    long_zh = "这是一段没有空格的中文长句用来验证自动换行。" * 20
    wrapped = Paragraph(_format_inline(long_zh), styles["body"])
    used_width, used_height = wrapped.wrap(_CONTENT_WIDTH, 4000)
    assert used_width <= _CONTENT_WIDTH + 0.5
    assert used_height > 60

    story = _markdown_to_story(CJK_LAYOUT_MD, styles)
    tables = [item for item in story if isinstance(item, Table)]
    assert len(tables) >= 1
    col_widths = list(tables[0]._colWidths)
    assert len(col_widths) == 5
    assert abs(sum(col_widths) - _CONTENT_WIDTH) < 1.5
    assert any(isinstance(item, HRFlowable) for item in story)
    assert "<link href=" in _format_inline("[来源](https://example.com/x)")

    md_path = tmp_path / "layout.md"
    pdf_path = tmp_path / "layout.pdf"
    md_path.write_text(CJK_LAYOUT_MD, encoding="utf-8")
    result = convert_md_to_pdf(md_path, pdf_path)
    assert "成功转换" in result
    assert pdf_path.read_bytes()[:4] == b"%PDF"
    assert pdf_path.stat().st_size > 1500


if __name__ == "__main__":
    test_user_query_locks_pdf_plan()
    test_llm_cannot_downgrade_pdf_to_text()
    test_auto_resolve_keeps_explicit_pdf()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_ensure_pdf_from_markdown_and_rglob(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_validator_writes_pdf_when_worker_skips_tool(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_abort_still_writes_partial_pdf(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_abort_without_content_still_writes_stub_pdf(Path(d))
    print("[OK] pdf deliverable tests")
