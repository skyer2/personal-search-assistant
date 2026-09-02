"""
结果校验

支持 per-step 校验与 finalize 终态校验。
【Phase 6】增加 Citation-First 引用覆盖率校验。
"""

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from app.agent.harness.deliverables import (
    best_report_text,
    content_from_loop_state,
    ensure_pdf_from_markdown,
    filename_stem_from_query,
    list_markdown_files,
    list_pdf_files,
    persist_markdown_if_missing,
)
from app.agent.harness.state import (
    ExecutionPlan,
    LoopState,
    PlanStep,
    StepResult,
    ValidationOutcome,
)

if TYPE_CHECKING:
    from app.agent.harness.citations import CitationManager

# 只匹配明确的执行失败模板，避免研究报告里的「失败模式 / 错误处理」误伤。
EXECUTION_ERROR_MARKERS = (
    "查询出现异常",
    "没有可用的表",
    "步骤执行超时",
    "并行步骤执行异常",
)
SQL_EMPTY_KEYWORDS = ("没有可用的", "0 条", "空", "未找到")


class ResultValidator:
    def validate_step(
        self,
        step: PlanStep,
        result: StepResult,
        session_dir: Path,
        state: LoopState,
    ) -> ValidationOutcome:
        content = result.compressed_content or result.content or ""

        if result.metadata.get("unauthorized_tools"):
            return ValidationOutcome(False, "unauthorized_tool", "error")
        if "tool_gateway" in (result.content or ""):
            return ValidationOutcome(False, "tool_gateway_denied", "error")

        if result.metadata.get("step_timeout"):
            return ValidationOutcome(False, "step_timeout", "error")

        if result.metadata.get("structured_ok") is False:
            code = str(result.metadata.get("error_code") or "worker_failed")
            return ValidationOutcome(False, code, "error")

        if result.metadata.get("invalid_structured_output"):
            return ValidationOutcome(False, "invalid_structured_output", "error")

        if any(marker in content for marker in EXECUTION_ERROR_MARKERS):
            return ValidationOutcome(False, "no_error", "error")

        if step.step_type == "network_search":
            if len(content) < 120:
                return ValidationOutcome(False, "search_too_short", "error")
            assistants_seen = list(
                result.metadata.get("step_assistants_called") or state.assistants_called
            )
            if step.subagent and step.subagent not in assistants_seen:
                return ValidationOutcome(False, "wrong_subagent", "error")

        if step.step_type in ("generate_markdown", "summarize"):
            if not content.strip():
                return ValidationOutcome(False, "no_content", "error")

        query = str(getattr(getattr(state, "intent", None), "raw_query", "") or "")
        filename_stem = filename_stem_from_query(query) if query else ""

        if step.step_type == "generate_markdown":
            persist_markdown_if_missing(
                session_dir,
                best_report_text(content, content_from_loop_state(state)),
                filename_stem=filename_stem,
            )
            md_files = list_markdown_files(session_dir, include_internal=False)
            if not md_files:
                return ValidationOutcome(False, "no_file_generated", "warning")

        if step.step_type == "convert_pdf":
            ensure_pdf_from_markdown(
                session_dir,
                content=best_report_text(content, content_from_loop_state(state)),
                filename_stem=filename_stem,
            )
            pdf_files = list_pdf_files(session_dir, include_internal=False)
            if not pdf_files:
                return ValidationOutcome(False, "no_file_generated", "error")

        return ValidationOutcome(True)

    def validate_finalize(
        self,
        state: LoopState,
        session_dir: Path,
        citation_manager: Optional["CitationManager"] = None,
        min_citation_coverage: float = 0.2,
    ) -> ValidationOutcome:
        intent = state.intent
        content = state.final_content or ""

        if not content.strip() and not state.step_results:
            return ValidationOutcome(False, "no_content", "error")

        if any(marker in content for marker in EXECUTION_ERROR_MARKERS):
            return ValidationOutcome(False, "no_error", "error")

        if intent and intent.deliverable in ("md", "pdf"):
            from app.agent.harness.deliverables import (
                content_from_loop_state,
                materialize_requested_files,
            )

            materialize_requested_files(
                session_dir,
                deliverable=intent.deliverable,
                content=content_from_loop_state(state) or content,
                query=intent.raw_query,
            )
            md_files = list_markdown_files(session_dir, include_internal=False)
            if not md_files:
                return ValidationOutcome(False, "no_file_generated", "error")

        if intent and intent.deliverable == "pdf":
            pdf_files = list_pdf_files(session_dir, include_internal=False)
            if not pdf_files:
                return ValidationOutcome(False, "no_file_generated", "error")

        coverage = self.validate_plan_coverage(state.plan, state.assistants_called)
        if not coverage.passed:
            return coverage

        failed_steps = [v for v in state.step_validation_results if not v.get("passed")]
        if failed_steps and state.metadata.get("strict_validation", False):
            return ValidationOutcome(False, "step_validation_failed", "error")

        # 【Phase 6】Citation-First finalize 校验
        if citation_manager is not None and intent and intent.deliverable in ("md", "pdf", "text"):
            ok, reason = citation_manager.validate_citations(content, min_citation_coverage)
            if not ok:
                return ValidationOutcome(False, reason, "warning")

        return ValidationOutcome(True)

    def validate_plan_coverage(
        self,
        plan: ExecutionPlan | None,
        assistants_called: list[str],
    ) -> ValidationOutcome:
        if not plan:
            return ValidationOutcome(True)
        for step in plan.steps:
            if step.subagent and step.subagent not in assistants_called:
                return ValidationOutcome(False, "wrong_subagent", "error")
        return ValidationOutcome(True)
