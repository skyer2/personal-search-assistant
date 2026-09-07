"""Memory backend 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from app.agent.memory.models import (
    MemoryRecord,
    MemoryType,
    MemoryWriteRequest,
    RecallResult,
    SourceLedgerEntry,
)


class MemoryBackend(ABC):
    @abstractmethod
    async def recall(
        self,
        query: str,
        *,
        tenant_id: str,
        user_id: str,
        top_k: int,
        project_id: str = "",
        memory_types: Optional[list[MemoryType]] = None,
        target_step_type: str = "",
    ) -> RecallResult:
        raise NotImplementedError

    @abstractmethod
    async def remember_writes(
        self,
        writes: list[MemoryWriteRequest],
        *,
        tenant_id: str,
        user_id: str,
        project_id: str = "",
    ) -> int:
        raise NotImplementedError

    @abstractmethod
    def list_records(
        self,
        *,
        tenant_id: str,
        user_id: str,
        include_deleted: bool = False,
        project_id: str = "",
    ) -> list[MemoryRecord]:
        raise NotImplementedError

    @abstractmethod
    async def delete_record(
        self,
        record_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def delete_all(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        raise NotImplementedError

    async def delete_records_for_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> int:
        return 0

    async def delete_records_for_session(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> int:
        return 0

    async def delete_source_ledger(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str = "",
        session_id: str = "",
    ) -> int:
        return 0

    async def mark_recalled(
        self,
        record_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
        session_id: str = "",
    ) -> None:
        return None

    async def upsert_source_ledger(
        self,
        entries: list[SourceLedgerEntry],
    ) -> int:
        return 0

    def list_source_ledger(
        self,
        *,
        tenant_id: str,
        user_id: str,
        project_id: str,
        limit: int = 8,
    ) -> list[SourceLedgerEntry]:
        return []

    async def consolidate(
        self,
        *,
        tenant_id: str,
        user_id: str,
        project_id: str = "",
    ) -> dict[str, int]:
        return {"decayed": 0, "promoted": 0, "purged": 0, "examined": 0}

    def enqueue_job(self, job: Any) -> str:
        return ""

    async def drain_jobs(self, limit: int = 8) -> dict[str, int]:
        return {"claimed": 0, "done": 0, "failed": 0}

    def enqueue_consolidation_job(self, *, tenant_id: str, user_id: str, project_id: str = "") -> str:
        return ""
