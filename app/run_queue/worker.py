"""RunQueueWorker：durable queue 消费者（可独立进程 / 容器部署）。"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from app.run_queue.service import RunQueue, get_run_queue


class RunQueueWorker:
    def __init__(
        self,
        queue: RunQueue | None = None,
        *,
        poll_interval_sec: float = 0.5,
    ):
        self.queue = queue or get_run_queue()
        self.worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.poll_interval_sec = poll_interval_sec
        self._stop = asyncio.Event()

    async def execute_job(self, job: Any) -> None:
        """执行单个 job；生产部署中此方法即 Research Worker Service 入口。"""
        from app.agent.main_agent import run_deep_agent

        await run_deep_agent(
            job.query,
            job.session_id,
            user_id=job.user_id,
            tenant_id=job.tenant_id,
            project_id=job.project_id,
            mode=job.mode,
            run_id=job.job_id,
        )

    async def run_once(self) -> bool:
        job = self.queue.claim_next(self.worker_id)
        if job is None:
            return False
        try:
            await self.execute_job(job)
            self.queue.complete(job.job_id, self.worker_id)
        except asyncio.CancelledError:
            self.queue.fail(job.job_id, self.worker_id, "cancelled")
            raise
        except Exception as exc:
            self.queue.fail(job.job_id, self.worker_id, str(exc))
        return True

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_sec)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()


_WORKER_TASK: asyncio.Task | None = None


async def start_run_queue_worker() -> asyncio.Task:
    global _WORKER_TASK
    if _WORKER_TASK is None or _WORKER_TASK.done():
        worker = RunQueueWorker()
        _WORKER_TASK = asyncio.create_task(worker.run_forever(), name="run-queue-worker")
    return _WORKER_TASK


__all__ = ["RunQueueWorker", "start_run_queue_worker"]
