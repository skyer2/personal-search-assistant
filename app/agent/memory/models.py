"""
【Phase 15】生产级记忆模型 — 类型化 fact、版本、租户、召回分数。
【Phase 18】分层记忆模型 — 项目域、信任等级、溯源、去重键、取代链、召回反馈。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.agent.memory.provenance import Provenance, TrustTier, coerce_trust_tier


class MemoryType(str, Enum):
    """长期记忆类型（分库语义）。"""

    SEMANTIC = "semantic"  # 可复用研究结论、领域事实
    EPISODIC = "episodic"  # 某次任务/步骤的具体经历
    PREFERENCE = "preference"  # 用户偏好（交付物、风格等）
    PROCEDURAL = "procedural"  # 流程/操作习惯，含「用户改过什么」
    SOURCE = "source"  # 【Phase 18】来源台账：查过哪些源、质量如何


# 常规召回只面向知识型记忆；来源台账走独立通道注入
RECALLABLE_TYPES: frozenset[MemoryType] = frozenset(
    {
        MemoryType.SEMANTIC,
        MemoryType.EPISODIC,
        MemoryType.PREFERENCE,
        MemoryType.PROCEDURAL,
    }
)


class WriteSource(str, Enum):
    """写入来源。"""

    FINALIZE = "finalize"
    STEP_INCREMENTAL = "step_incremental"
    USER_EXPLICIT = "user_explicit"
    SEED = "seed"
    MEM0 = "mem0"
    HITL = "hitl"  # 【Phase 18】人工审批/编辑沉淀的程序性记忆
    CONSOLIDATION = "consolidation"  # 【Phase 18】离线巩固产物
    CONFIRMATION = "confirmation"  # 独立证据或人工确认，不靠 recall_count


@dataclass
class MemoryRecord:
    fact: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tenant_id: str = "default"
    user_id: str = ""
    memory_type: MemoryType = MemoryType.SEMANTIC
    version: int = 1
    confidence: float = 0.8
    write_source: WriteSource = WriteSource.FINALIZE
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = "remember"  # 向后兼容旧字段
    task: str = ""
    topic: str = ""
    session_id: str = ""
    is_deleted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    recall_score: Optional[float] = None
    embedding: Optional[list[float]] = field(default=None, repr=False)
    # 【Phase 18】分层与治理
    project_id: str = "default"
    trust_tier: TrustTier = TrustTier.DERIVED
    provenance: Provenance = field(default_factory=Provenance)
    dedup_key: str = ""
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str = ""
    recall_count: int = 0
    last_recalled_at: str = ""
    # 时效：写入时间 ≠ 事实发生时间
    as_of: str = ""
    valid_from: str = ""
    valid_to: str = ""
    last_verified_at: str = ""
    observed_at: str = ""
    source_updated_at: str = ""
    # 独立确认（truth），与 recall_count（utility）分离
    confirmed_by_source_ids: list[str] = field(default_factory=list)
    confirmation_count: int = 0
    human_confirmed: bool = False
    idempotency_key: str = ""
    entity: str = ""
    attribute: str = ""
    value_text: str = ""
    valid_time: str = ""

    def age_days(self) -> int:
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError:
            return 0
        return max(0, (datetime.now(timezone.utc) - created).days)

    def is_expired(self, ttl_days: int) -> bool:
        if ttl_days <= 0:
            return False
        try:
            datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return self.age_days() > ttl_days

    def type_label(self) -> str:
        return self.memory_type.value if isinstance(self.memory_type, MemoryType) else str(self.memory_type)

    def trust_label(self) -> str:
        return coerce_trust_tier(self.trust_tier).value

    def to_dict(self) -> dict[str, Any]:
        mt = self.memory_type.value if isinstance(self.memory_type, MemoryType) else str(self.memory_type)
        ws = self.write_source.value if isinstance(self.write_source, WriteSource) else str(self.write_source)
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "fact": self.fact,
            "memory_type": mt,
            "version": self.version,
            "confidence": self.confidence,
            "write_source": ws,
            "trust_tier": self.trust_label(),
            "provenance": self.provenance.to_dict(),
            "dedup_key": self.dedup_key,
            "supersedes": list(self.supersedes),
            "superseded_by": self.superseded_by,
            "recall_count": self.recall_count,
            "last_recalled_at": self.last_recalled_at,
            "as_of": self.as_of,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "last_verified_at": self.last_verified_at,
            "observed_at": self.observed_at,
            "source_updated_at": self.source_updated_at,
            "confirmed_by_source_ids": list(self.confirmed_by_source_ids),
            "confirmation_count": self.confirmation_count,
            "human_confirmed": self.human_confirmed,
            "idempotency_key": self.idempotency_key,
            "entity": self.entity,
            "attribute": self.attribute,
            "value_text": self.value_text,
            "valid_time": self.valid_time,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "task": self.task,
            "topic": self.topic,
            "session_id": self.session_id,
            "is_deleted": self.is_deleted,
            "metadata": self.metadata,
            "recall_score": self.recall_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        mt_raw = data.get("memory_type", MemoryType.SEMANTIC.value)
        try:
            memory_type = MemoryType(str(mt_raw))
        except ValueError:
            memory_type = MemoryType.SEMANTIC
        ws_raw = data.get("write_source", WriteSource.FINALIZE.value)
        try:
            write_source = WriteSource(str(ws_raw))
        except ValueError:
            write_source = WriteSource.FINALIZE
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            tenant_id=str(data.get("tenant_id") or "default"),
            user_id=str(data.get("user_id") or ""),
            project_id=str(data.get("project_id") or "default"),
            fact=str(data.get("fact", "")),
            memory_type=memory_type,
            version=int(data.get("version") or 1),
            confidence=float(data.get("confidence") or 0.8),
            write_source=write_source,
            trust_tier=coerce_trust_tier(data.get("trust_tier")),
            provenance=Provenance.from_dict(data.get("provenance")),
            dedup_key=str(data.get("dedup_key") or ""),
            supersedes=[str(s) for s in (data.get("supersedes") or [])],
            superseded_by=str(data.get("superseded_by") or ""),
            recall_count=int(data.get("recall_count") or 0),
            last_recalled_at=str(data.get("last_recalled_at") or ""),
            as_of=str(data.get("as_of") or ""),
            valid_from=str(data.get("valid_from") or ""),
            valid_to=str(data.get("valid_to") or ""),
            last_verified_at=str(data.get("last_verified_at") or ""),
            observed_at=str(data.get("observed_at") or data.get("as_of") or ""),
            source_updated_at=str(data.get("source_updated_at") or ""),
            confirmed_by_source_ids=[str(s) for s in (data.get("confirmed_by_source_ids") or [])],
            confirmation_count=int(data.get("confirmation_count") or 0),
            human_confirmed=bool(data.get("human_confirmed", False)),
            idempotency_key=str(data.get("idempotency_key") or ""),
            entity=str(data.get("entity") or ""),
            attribute=str(data.get("attribute") or ""),
            value_text=str(data.get("value_text") or ""),
            valid_time=str(data.get("valid_time") or ""),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
            updated_at=str(data.get("updated_at") or data.get("created_at") or datetime.now(timezone.utc).isoformat()),
            source=str(data.get("source", "remember")),
            task=str(data.get("task", "")),
            topic=str(data.get("topic", "")),
            session_id=str(data.get("session_id", "")),
            is_deleted=bool(data.get("is_deleted", False)),
            metadata=dict(data.get("metadata") or {}),
            recall_score=data.get("recall_score"),
        )


@dataclass
class MemoryWriteRequest:
    fact: str
    memory_type: MemoryType = MemoryType.SEMANTIC
    confidence: float = 0.8
    write_source: WriteSource = WriteSource.FINALIZE
    task: str = ""
    topic: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # 【Phase 18】写入侧的分层与溯源；trust_tier 为 None 时按 provenance 自动判级
    project_id: str = ""
    trust_tier: Optional[TrustTier] = None
    provenance: Optional[Provenance] = None
    dedup_key: str = ""
    as_of: str = ""
    valid_from: str = ""
    valid_to: str = ""
    valid_time: str = ""
    last_verified_at: str = ""
    observed_at: str = ""
    source_updated_at: str = ""
    idempotency_key: str = ""
    expected_version: Optional[int] = None
    entity: str = ""
    attribute: str = ""
    value_text: str = ""
    human_confirmed: bool = False

    def resolved_provenance(self) -> Provenance:
        return self.provenance or Provenance()

    def resolved_trust_tier(self) -> TrustTier:
        if self.trust_tier is not None:
            return coerce_trust_tier(self.trust_tier)
        from app.agent.memory.provenance import classify_trust_tier

        prov = self.resolved_provenance()
        return classify_trust_tier(
            write_source=self.write_source,
            step_type=prov.step_type or str(self.metadata.get("step_type", "")),
            provenance=prov,
        )


@dataclass
class RecallResult:
    records: list[MemoryRecord]
    mean_recall_score: float = 0.0
    keyword_hits: int = 0
    embedding_used: bool = False
    # 【Phase 18】准入门与分层可观测
    candidates: int = 0
    trust_filtered: int = 0
    by_trust: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    # 兼容旧字段名：这不是 IR Recall@K
    recall_at_k: float = 0.0

    def __post_init__(self) -> None:
        if self.mean_recall_score and not self.recall_at_k:
            self.recall_at_k = self.mean_recall_score
        elif self.recall_at_k and not self.mean_recall_score:
            self.mean_recall_score = self.recall_at_k

    def to_metrics(self) -> dict[str, Any]:
        return {
            "recalled": len(self.records),
            "mean_recall_score": self.mean_recall_score,
            "recall_at_k": self.mean_recall_score,  # 兼容旧观测名，语义是 mean score
            "keyword_hits": self.keyword_hits,
            "embedding_used": self.embedding_used,
            "candidates": self.candidates,
            "trust_filtered": self.trust_filtered,
            "by_trust": dict(self.by_trust),
            "by_type": dict(self.by_type),
        }


@dataclass
class SourceLedgerEntry:
    """项目级「已查来源」台账，避免同项目重复检索同一批 URL。"""

    id: str
    tenant_id: str
    user_id: str
    project_id: str
    source_kind: str
    locator: str
    quality: str = "unknown"  # reliable | mixed | unreliable | unknown
    hit_count: int = 1
    last_used_at: str = ""
    first_seen_at: str = ""
    last_checked_at: str = ""
    content_fingerprint: str = ""
    query_purpose: str = ""
    session_id: str = ""
    run_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "source_kind": self.source_kind,
            "locator": self.locator,
            "quality": self.quality,
            "hit_count": self.hit_count,
            "last_used_at": self.last_used_at,
            "first_seen_at": self.first_seen_at,
            "last_checked_at": self.last_checked_at,
            "content_fingerprint": self.content_fingerprint,
            "query_purpose": self.query_purpose,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceLedgerEntry":
        return cls(
            id=str(data.get("id") or ""),
            tenant_id=str(data.get("tenant_id") or "default"),
            user_id=str(data.get("user_id") or ""),
            project_id=str(data.get("project_id") or "default"),
            source_kind=str(data.get("source_kind") or "url"),
            locator=str(data.get("locator") or ""),
            quality=str(data.get("quality") or "unknown"),
            hit_count=int(data.get("hit_count") or 1),
            last_used_at=str(data.get("last_used_at") or ""),
            first_seen_at=str(data.get("first_seen_at") or ""),
            last_checked_at=str(data.get("last_checked_at") or data.get("last_used_at") or ""),
            content_fingerprint=str(data.get("content_fingerprint") or ""),
            query_purpose=str(data.get("query_purpose") or ""),
            session_id=str(data.get("session_id") or ""),
            run_id=str(data.get("run_id") or ""),
            metadata=dict(data.get("metadata") or {}),
        )
