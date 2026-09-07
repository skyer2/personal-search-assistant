"""
【Phase 15】JSON 记忆后端 — 开发降级；无 embedding / 审计 / 软删除完整能力。
【Phase 18】对齐新接口签名；写入走同一套 consolidation 决策。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.agent.memory.backend.base import MemoryBackend
from app.agent.memory.consolidation import (
    ConsolidationAction,
    apply_update,
    apply_validity_fields,
    decide_write_action,
)
from app.agent.memory.models import (
    MemoryRecord,
    MemoryType,
    MemoryWriteRequest,
    RecallResult,
    SourceLedgerEntry,
)
from app.agent.memory.policy import MemoryPolicy
from app.agent.memory.recall.hybrid import hybrid_recall
from app.agent.memory.security import contains_pii, redact_pii
from app.agent.memory.validity import record_is_expired


class JsonMemoryBackend(MemoryBackend):
    def __init__(self, storage_dir: Path, policy: MemoryPolicy):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy

    def _path(self, tenant_id: str, user_id: str) -> Path:
        safe_t = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)
        safe_u = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self.storage_dir / f"{safe_t}__{safe_u}.json"

    def _ledger_path(self, tenant_id: str, user_id: str, project_id: str) -> Path:
        safe_t = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)
        safe_u = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        safe_p = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_id)
        return self.storage_dir / f"{safe_t}__{safe_u}__{safe_p}.sources.json"

    def _load(self, tenant_id: str, user_id: str) -> list[MemoryRecord]:
        path = self._path(tenant_id, user_id)
        if not path.exists():
            return []
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        records: list[MemoryRecord] = []
        for item in raw:
            if isinstance(item, str):
                records.append(MemoryRecord(fact=item, tenant_id=tenant_id, user_id=user_id))
            elif isinstance(item, dict):
                rec = MemoryRecord.from_dict(item)
                rec.tenant_id = tenant_id
                rec.user_id = user_id
                records.append(rec)
        return records

    def _save(self, tenant_id: str, user_id: str, records: list[MemoryRecord]) -> None:
        import json

        path = self._path(tenant_id, user_id)
        payload = [r.to_dict() for r in records]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
        records = self._load(tenant_id, user_id)
        return await hybrid_recall(
            query,
            records,
            policy=self.policy,
            top_k=top_k,
            memory_types=memory_types,
            project_id=project_id,
            target_step_type=target_step_type,
        )

    async def remember_writes(
        self,
        writes: list[MemoryWriteRequest],
        *,
        tenant_id: str,
        user_id: str,
        project_id: str = "",
    ) -> int:
        records = self._load(tenant_id, user_id)
        saved = 0
        now = datetime.now(timezone.utc).isoformat()
        for write in writes[: self.policy.max_facts_per_remember]:
            if project_id and not write.project_id:
                write.project_id = project_id
            fact = write.fact.strip()
            if len(fact) < self.policy.min_fact_chars:
                continue
            if self.policy.pii_redact_enabled and contains_pii(fact):
                fact = redact_pii(fact)
                write.fact = fact
            if len(fact) < self.policy.min_fact_chars:
                continue
            decision = decide_write_action(write, records, policy=self.policy)
            if decision.action == ConsolidationAction.NOOP:
                continue
            if decision.action == ConsolidationAction.UPDATE and decision.target:
                apply_update(decision.target, write)
                apply_validity_fields(decision.target, write)
                saved += 1
                continue
            record = MemoryRecord(
                tenant_id=tenant_id,
                user_id=user_id,
                fact=fact,
                memory_type=write.memory_type,
                confidence=write.confidence,
                write_source=write.write_source,
                task=write.task,
                topic=write.topic,
                session_id=write.session_id,
                metadata=write.metadata,
                created_at=now,
                updated_at=now,
                source="remember",
                project_id=write.project_id or "default",
                trust_tier=write.resolved_trust_tier(),
                provenance=write.resolved_provenance(),
                dedup_key=write.dedup_key,
                idempotency_key=write.idempotency_key,
            )
            apply_validity_fields(record, write)
            if decision.action == ConsolidationAction.SUPERSEDE and decision.target:
                decision.target.is_deleted = True
                decision.target.superseded_by = record.id
                record.supersedes = [decision.target.id]
            records.append(record)
            saved += 1
        self._save(tenant_id, user_id, records)
        return saved

    def list_records(
        self,
        *,
        tenant_id: str,
        user_id: str,
        include_deleted: bool = False,
        project_id: str = "",
    ) -> list[MemoryRecord]:
        records = self._load(tenant_id, user_id)
        if not include_deleted:
            records = [r for r in records if not r.is_deleted]
        if project_id and project_id != "default":
            preferred = [r for r in records if r.project_id == project_id]
            others = [r for r in records if r.project_id != project_id]
            records = preferred + others
        return [r for r in records if include_deleted or not record_is_expired(r, self.policy)]

    async def delete_record(
        self,
        record_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        records = self._load(tenant_id, user_id)
        changed = False
        for record in records:
            if record.id == record_id and not record.is_deleted:
                record.is_deleted = True
                record.updated_at = datetime.now(timezone.utc).isoformat()
                changed = True
                break
        if changed:
            self._save(tenant_id, user_id, records)
        return changed

    async def delete_all(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        records = self._load(tenant_id, user_id)
        count = 0
        now = datetime.now(timezone.utc).isoformat()
        for record in records:
            if not record.is_deleted:
                record.is_deleted = True
                record.updated_at = now
                count += 1
        if count:
            self._save(tenant_id, user_id, records)
        return count

    async def delete_records_for_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> int:
        records = self._load(tenant_id, user_id)
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for record in records:
            if not record.is_deleted and record.provenance and record.provenance.run_id == run_id:
                record.is_deleted = True
                record.updated_at = now
                count += 1
        if count:
            self._save(tenant_id, user_id, records)
        return count

    async def delete_records_for_session(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> int:
        records = self._load(tenant_id, user_id)
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for record in records:
            if not record.is_deleted and record.session_id == session_id:
                record.is_deleted = True
                record.updated_at = now
                count += 1
        if count:
            self._save(tenant_id, user_id, records)
        return count

    async def delete_source_ledger(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str = "",
        session_id: str = "",
    ) -> int:
        if not run_id and not session_id:
            return 0
        import json

        removed = 0
        project_root = self.storage_dir
        if not project_root.exists():
            return 0
        for path in project_root.glob("*.sources.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            kept = []
            for item in raw if isinstance(raw, list) else []:
                if not isinstance(item, dict):
                    continue
                if run_id and item.get("run_id") == run_id:
                    removed += 1
                    continue
                if session_id and item.get("session_id") == session_id:
                    removed += 1
                    continue
                kept.append(item)
            path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        return removed

    async def upsert_source_ledger(self, entries: list[SourceLedgerEntry]) -> int:
        if not entries:
            return 0
        import json

        saved = 0
        grouped: dict[tuple[str, str, str], list[SourceLedgerEntry]] = {}
        for entry in entries:
            key = (entry.tenant_id, entry.user_id, entry.project_id)
            grouped.setdefault(key, []).append(entry)
        for (tid, uid, pid), group in grouped.items():
            path = self._ledger_path(tid, uid, pid)
            existing: dict[str, dict] = {}
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    existing = {item["id"]: item for item in raw if isinstance(item, dict) and item.get("id")}
                except Exception:
                    existing = {}
            now = datetime.now(timezone.utc).isoformat()
            for entry in group:
                prev = existing.get(entry.id)
                if prev:
                    prev["hit_count"] = int(prev.get("hit_count") or 1) + 1
                    prev["last_used_at"] = now
                    prev["session_id"] = entry.session_id
                    prev["run_id"] = entry.run_id
                    if entry.quality != "unknown":
                        prev["quality"] = entry.quality
                else:
                    payload = entry.to_dict()
                    payload["first_seen_at"] = now
                    payload["last_used_at"] = now
                    existing[entry.id] = payload
                saved += 1
            path.write_text(
                json.dumps(list(existing.values()), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return saved

    def list_source_ledger(
        self,
        *,
        tenant_id: str,
        user_id: str,
        project_id: str,
        limit: int = 8,
    ) -> list[SourceLedgerEntry]:
        import json

        path = self._ledger_path(tenant_id, user_id, project_id)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        entries = [SourceLedgerEntry.from_dict(item) for item in raw if isinstance(item, dict)]
        entries.sort(key=lambda e: e.last_used_at, reverse=True)
        return entries[:limit]
