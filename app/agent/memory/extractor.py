"""
【Phase 15】记忆事实提取 — 类型化 fact + 步内增量 + finalize 批量。
【Phase 18】来源锚定抽取：无证据的网页原文不得进入长期记忆。
"""

from __future__ import annotations

from typing import Any, Optional

from app.agent.memory.models import MemoryType, MemoryWriteRequest, WriteSource
from app.agent.memory.provenance import Provenance, TrustTier, classify_trust_tier

EXTRACT_PROMPT = """从以下 Agent 任务结果中提取少量可长期复用的记忆，而不是所有信息。
只保留以后做同类研究时真正值得复用的内容。
优先：用户偏好、明确反馈、程序性知识、经过验证的可复用结论。
不要：网页全文、一次性搜索结果、无出处事实、失败检索、未验证推断。
要求：
1. 每条事实独立、具体、可复用
2. 不要泛泛而谈，保留主题、结论、数据要点和年份（valid time）
3. 每行格式：类型|事实内容
   类型只能是：semantic / episodic / preference / procedural
4. 不要编号
5. 不要把网页原文整段复制

任务结果：
{content}
"""

STEP_EXTRACT_HINTS = {
    "network_search": MemoryType.EPISODIC,
    "file_read": MemoryType.EPISODIC,
}


class MemoryExtractor:
    def __init__(self, model: Any = None):
        self.model = model

    async def extract_writes(
        self,
        content: str,
        *,
        max_facts: int = 5,
        write_source: WriteSource = WriteSource.FINALIZE,
        task: str = "",
        topic: str = "",
        session_id: str = "",
        project_id: str = "",
        provenance: Optional[Provenance] = None,
        trust_tier: Optional[TrustTier] = None,
    ) -> list[MemoryWriteRequest]:
        if not content.strip():
            return []
        from app.agent.memory.policy import get_memory_policy

        policy = get_memory_policy()
        cap = min(max_facts, policy.max_facts_per_remember)
        prov = provenance or Provenance()
        resolved_trust = trust_tier or classify_trust_tier(
            write_source=write_source,
            step_type=prov.step_type,
            provenance=prov,
        )

        if self.model is None:
            raw_facts = self._heuristic_extract(content, cap)
            return [
                self._to_write(
                    fact=f,
                    memory_type=MemoryType.SEMANTIC,
                    write_source=write_source,
                    task=task,
                    topic=topic,
                    session_id=session_id,
                    project_id=project_id,
                    provenance=prov,
                    trust_tier=resolved_trust,
                    confidence=0.75,
                )
                for f in raw_facts
            ]

        try:
            from app.agent.harness.usage_tracker import tracked_ainvoke

            response = await tracked_ainvoke(
                self.model,
                EXTRACT_PROMPT.format(content=content[:8000]),
                session_id=session_id,
                phase="memory_extract",
            )
            text = getattr(response, "content", str(response))
            if isinstance(text, list):
                text = "\n".join(str(item) for item in text)
            return self._parse_typed_lines(
                str(text),
                cap=cap,
                write_source=write_source,
                task=task,
                topic=topic,
                session_id=session_id,
                project_id=project_id,
                provenance=prov,
                trust_tier=resolved_trust,
            )
        except Exception as exc:
            print(f"[MemoryExtractor] LLM extract failed: {exc}")
            raw_facts = self._heuristic_extract(content, cap)
            return [
                self._to_write(
                    fact=f,
                    memory_type=MemoryType.SEMANTIC,
                    write_source=write_source,
                    task=task,
                    topic=topic,
                    session_id=session_id,
                    project_id=project_id,
                    provenance=prov,
                    trust_tier=resolved_trust,
                    confidence=0.7,
                )
                for f in raw_facts
            ]

    async def extract_facts(self, content: str, max_facts: int = 5) -> list[str]:
        writes = await self.extract_writes(content, max_facts=max_facts)
        return [w.fact for w in writes]

    def extract_step_writes(
        self,
        content: str,
        step_type: str,
        *,
        max_facts: int = 2,
        session_id: str = "",
        task: str = "",
        project_id: str = "",
        provenance: Optional[Provenance] = None,
    ) -> list[MemoryWriteRequest]:
        if not content.strip():
            return []
        from app.agent.memory.policy import get_memory_policy

        policy = get_memory_policy()
        cap = min(max_facts, 2)
        memory_type = STEP_EXTRACT_HINTS.get(step_type, MemoryType.EPISODIC)
        prov = provenance or Provenance(step_type=step_type)
        if not prov.step_type:
            prov.step_type = step_type

        # 外部网页：没有 URL / 证据 ID 就不写长期记忆，防止脏结论持久化
        if (
            policy.require_provenance_for_step_write
            and step_type == "network_search"
            and not prov.has_evidence
        ):
            return []

        trust = classify_trust_tier(
            write_source=WriteSource.STEP_INCREMENTAL,
            step_type=step_type,
            provenance=prov,
        )
        sentences = self._heuristic_extract(content, cap * 2)
        writes: list[MemoryWriteRequest] = []
        for sentence in sentences[:cap]:
            if len(sentence.strip()) < policy.min_fact_chars:
                continue
            writes.append(
                self._to_write(
                    fact=sentence.strip(),
                    memory_type=memory_type,
                    write_source=WriteSource.STEP_INCREMENTAL,
                    task=task,
                    topic="",
                    session_id=session_id,
                    project_id=project_id,
                    provenance=prov,
                    trust_tier=trust,
                    confidence=0.65,
                    extra_metadata={"step_type": step_type},
                )
            )
        return writes

    def _to_write(
        self,
        *,
        fact: str,
        memory_type: MemoryType,
        write_source: WriteSource,
        task: str,
        topic: str,
        session_id: str,
        project_id: str,
        provenance: Provenance,
        trust_tier: TrustTier,
        confidence: float,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> MemoryWriteRequest:
        return MemoryWriteRequest(
            fact=fact,
            memory_type=memory_type,
            write_source=write_source,
            task=task,
            topic=topic,
            session_id=session_id,
            project_id=project_id,
            metadata=extra_metadata or {},
            confidence=confidence,
            trust_tier=trust_tier,
            provenance=provenance,
        )

    def _parse_typed_lines(
        self,
        text: str,
        *,
        cap: int,
        write_source: WriteSource,
        task: str,
        topic: str,
        session_id: str,
        project_id: str = "",
        provenance: Optional[Provenance] = None,
        trust_tier: Optional[TrustTier] = None,
    ) -> list[MemoryWriteRequest]:
        from app.agent.memory.policy import get_memory_policy

        policy = get_memory_policy()
        writes: list[MemoryWriteRequest] = []
        prov = provenance or Provenance()
        resolved_trust = trust_tier or classify_trust_tier(
            write_source=write_source, provenance=prov
        )
        for line in text.splitlines():
            line = line.strip("-• ").strip()
            if not line:
                continue
            memory_type = MemoryType.SEMANTIC
            fact = line
            if "|" in line:
                type_part, fact_part = line.split("|", 1)
                type_part = type_part.strip().lower()
                fact = fact_part.strip()
                try:
                    memory_type = MemoryType(type_part)
                except ValueError:
                    memory_type = MemoryType.SEMANTIC
            if memory_type == MemoryType.SOURCE:
                continue
            if len(fact) < policy.min_fact_chars:
                continue
            writes.append(
                self._to_write(
                    fact=fact,
                    memory_type=memory_type,
                    write_source=write_source,
                    task=task,
                    topic=topic,
                    session_id=session_id,
                    project_id=project_id,
                    provenance=prov,
                    trust_tier=resolved_trust,
                    confidence=0.85,
                )
            )
            if len(writes) >= cap:
                break
        return writes

    def _heuristic_extract(self, content: str, max_facts: int) -> list[str]:
        sentences = [
            s.strip()
            for s in content.replace("\n", "。").split("。")
            if len(s.strip()) >= 20
        ]
        return sentences[:max_facts]
