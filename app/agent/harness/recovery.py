"""
失败恢复

根据校验失败原因生成结构化恢复提示，供下一轮执行使用。
"""

from app.agent.harness.state import LoopState, PlanStep


class RecoveryManager:
    RECOVERY_HINTS: dict[str, str] = {
        "no_file_generated": (
            "上次执行未生成预期文件。请先调用子智能体获取信息，"
            "再使用 generate_markdown 生成 Markdown，如需 PDF 再调用 convert_md_to_pdf。"
        ),
        "file_min_size": "上次生成的文件内容过短，请补充更多检索信息后再生成完整报告。",
        "search_too_short": "上次网络搜索结果不足，请换关键词重新搜索。",
        "wrong_subagent": (
            "上次未命中计划指定的工人。检索步应由 Harness 直调对应工人；"
            "请确认本步工人图可用，并只使用已绑定工具。"
        ),
        "citation_coverage_low": (
            "引用覆盖率不足。请在报告中为关键结论添加 [n] 引用，并确保文末有参考文献块。"
        ),
        "no_content": "上次未产生有效回答，请重新执行任务。",
        "no_error": "上次执行返回了错误信息，请分析原因后重试。",
        "step_timeout": (
            "上次步骤执行超时。请缩小检索范围、减少工具调用次数，"
            "或拆分更具体的子任务后重试。"
        ),
        "unauthorized_tool": (
            "上次调用了本步骤禁止的工具。请严格遵守 Harness 计划绑定，"
            "只调用当前步骤允许的子 Agent 和工具。"
        ),
        "worker_failed": "工人返回 ok=false，请根据 error_code 调整检索策略后重试。",
        "empty_worker_result": "工人未返回有效内容，请重新委派子 Agent 并确保结构化 JSON 回传。",
        "invalid_structured_output": (
            "工人未返回含 facts/sources 的 JSON。请严格按【工人结构化回传】格式重试。"
        ),
    }

    def build_recovery_hint(self, reason: str, state: LoopState) -> str:
        hint = self.RECOVERY_HINTS.get(
            reason,
            "上次执行未通过校验，请根据执行计划重新完成任务。",
        )
        if state.plan:
            plan_text = "\n".join(
                f"{i + 1}. {step.description}"
                for i, step in enumerate(state.plan.steps)
            )
            hint += f"\n\n【执行计划】\n{plan_text}"
        return hint

    def get_retry_step(self, reason: str, plan_steps: list[PlanStep]) -> PlanStep | None:
        mapping = {
            "no_file_generated": "generate_markdown",
            "search_too_short": "network_search",
            "citation_coverage_low": "generate_markdown",
        }
        step_type = mapping.get(reason)
        if not step_type:
            return None
        for step in plan_steps:
            if step.step_type == step_type:
                return step
        return None
