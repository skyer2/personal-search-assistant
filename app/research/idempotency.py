"""
外部副作用幂等。

LangGraph checkpointer 管 workflow state，不保证 tool/DB/file 恰好一次。
interrupt() 恢复后 node 会从头执行，副作用必须带 action key。
"""

from __future__ import annotations


def action_idempotency_key(
    *,
    run_id: str,
    plan_version: int,
    task_id: str,
    action_id: str = "execute",
) -> str:
    return f"{run_id}:p{int(plan_version)}:{task_id}:{action_id}"
