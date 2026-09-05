"""
【Phase 6】Citation-First Research — 证据链与引用管理

每个 step 注册 EvidenceSource，finalize 生成参考文献块并计算 CCR / 幻觉率。
"""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_PATTERN = re.compile(r"https?://[^\s\]\)\"'<>]+", re.IGNORECASE)
CITATION_MARKER_PATTERN = re.compile(r"\[(\d+)\]")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。！？.!?])\s+")
CITATION_ONLY_PATTERN = re.compile(r"^(?:\[\d+\])+\s*$")


@dataclass
class EvidenceSource:
    """单条可追溯证据。"""

    source_id: str
    step_index: int
    step_type: str
    source_kind: str  # url | sql | file | text | kb
    locator: str
    excerpt: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    bound_fact: str = ""
    artifact_id: str = ""
    evidence_id: str = ""
    start_offset: int = 0
    end_offset: int = 0


@dataclass
class Claim:
    """带引用的断言（可选，用于细粒度校验）。"""

    text: str
    source_ids: list[str] = field(default_factory=list)


class CitationManager:
    """管理证据注册、参考文献生成与引用指标。"""

    def __init__(self) -> None:
        self.sources: list[EvidenceSource] = []
        self._counter = 0
        self.fact_bindings: list[dict[str, Any]] = []
        self._admission_keys: set[str] = set()
        self.max_sources_per_step = 6

    @staticmethod
    def _canonical_locator(locator: str) -> str:
        value = str(locator or "").strip().rstrip(".,;")
        if not value.lower().startswith(("http://", "https://")):
            return value
        parts = urlsplit(value)
        query = urlencode(
            sorted(
                (k, v)
                for k, v in parse_qsl(parts.query)
                if not k.lower().startswith("utm_")
            )
        )
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/") or "/",
                query,
                "",
            )
        )

    def _admit(self, src: EvidenceSource) -> bool:
        if (
            sum(1 for old in self.sources if old.step_index == src.step_index)
            >= self.max_sources_per_step
        ):
            return False
        src.locator = self._canonical_locator(src.locator)
        content_hash = hashlib.sha256(
            (src.excerpt or src.bound_fact).strip().lower().encode()
        ).hexdigest()
        keys = {f"locator:{src.locator}", f"content:{content_hash}"}
        if any(key in self._admission_keys for key in keys):
            return False
        self._admission_keys.update(keys)
        self.sources.append(src)
        return True

    def _next_id(self) -> str:
        self._counter += 1
        return f"src-{self._counter}"

    def register_from_step(
        self,
        step_index: int,
        step_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[EvidenceSource]:
        """从 step 产出中提取并注册证据源。"""
        if not content or not content.strip():
            return []

        registered: list[EvidenceSource] = []
        meta = metadata or {}

        for url in URL_PATTERN.findall(content)[:20]:
            excerpt = _excerpt_around(content, url, 400)
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind="url",
                locator=url.rstrip(".,;"),
                excerpt=excerpt,
                artifact_id=str(meta.get("artifact_id") or ""),
            )
            if self._admit(src):
                registered.append(src)

        if step_type == "file_read":
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind="file",
                locator=meta.get("filename", "uploaded_file"),
                excerpt=content[:800].replace("\n", " "),
                artifact_id=str(meta.get("artifact_id") or ""),
            )
            if self._admit(src):
                registered.append(src)

        if not registered and len(content.strip()) >= 80:
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind="text",
                locator=f"step:{step_index}:{step_type}",
                excerpt=content[:800].replace("\n", " "),
                artifact_id=str(meta.get("artifact_id") or ""),
            )
            if self._admit(src):
                registered.append(src)

        if registered:
            try:
                from app.observability import EventType, get_recorder

                recorder = get_recorder()
                if recorder.is_active:
                    for src in registered:
                        claim_id = f"c_{src.source_id}"
                        finding_id = f"f_step_{src.step_index}"
                        recorder.emit(
                            EventType.EVIDENCE_REGISTERED,
                            phase="execute",
                            status="ok",
                            attributes={
                                "finding_id": finding_id,
                                "claim_id": claim_id,
                                "evidence_id": src.source_id,
                                "source_id": src.source_id,
                                "artifact_id": src.artifact_id,
                                "source_kind": src.source_kind,
                                "support_type": "direct",
                                "source_quality": (
                                    "primary"
                                    if src.source_kind == "url"
                                    else "secondary"
                                ),
                                "freshness": "",
                                "step_index": src.step_index,
                                "step_type": src.step_type,
                                "locator": src.locator,
                            },
                            input_refs=[
                                {"type": "finding", "id": finding_id},
                                {"type": "claim", "id": claim_id},
                            ],
                            output_refs=[
                                item
                                for item in [
                                    {"type": "evidence", "id": src.source_id},
                                    (
                                        {"type": "artifact", "id": src.artifact_id}
                                        if src.artifact_id
                                        else None
                                    ),
                                ]
                                if item
                            ],
                        )
            except Exception:
                import logging as _log

                _log.getLogger("observability").debug("obs emit skipped", exc_info=True)

        return registered

    def bind_worker_facts(
        self,
        step_index: int,
        step_type: str,
        facts: list[str],
        sources: list[str],
    ) -> list[EvidenceSource]:
        """把工人 JSON 的 fact 与 source 绑成可回读证据，避免只靠段落序号贴 [n]。"""
        registered: list[EvidenceSource] = []
        locators = [str(s).strip() for s in (sources or []) if str(s).strip()][:10]
        kind = (
            "url"
            if step_type == "network_search"
            else ("file" if step_type == "file_read" else "text")
        )
        for i, fact in enumerate((facts or [])[:10]):
            text = str(fact).strip()
            if not text:
                continue
            locator = (
                locators[i]
                if i < len(locators)
                else (locators[0] if locators else f"step:{step_index}:{step_type}")
            )
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind=kind,
                locator=locator,
                excerpt=text[:800],
                bound_fact=text,
            )
            if not self._admit(src):
                continue
            self.fact_bindings.append(
                {
                    "fact": text,
                    "source_id": src.source_id,
                    "locator": locator,
                    "step_index": step_index,
                }
            )
            registered.append(src)
        return registered

    def bind_evidence_spans(
        self, spans: list[Any], findings: list[Any] | None = None
    ) -> list[EvidenceSource]:
        """把 EvidenceStore span/finding 登记为可引用 source。"""
        registered: list[EvidenceSource] = []
        claim_by_eid: dict[str, str] = {}
        for finding in findings or []:
            claim = getattr(finding, "claim", None) or (
                finding.get("claim") if isinstance(finding, dict) else ""
            )
            ids = getattr(finding, "evidence_ids", None)
            if ids is None and isinstance(finding, dict):
                ids = finding.get("evidence_ids") or []
            for eid in ids or []:
                claim_by_eid[str(eid)] = str(claim or "")
        for span in spans or []:
            if isinstance(span, dict):
                eid = str(span.get("evidence_id") or "")
                locator = str(span.get("locator") or "")
                text = str(span.get("text") or "")
                kind = str(span.get("source_kind") or "text")
                artifact_id = str(span.get("artifact_id") or "")
                step_index = int(span.get("step_index") or 0)
                step_type = str(span.get("step_type") or "")
                start = int(span.get("start_offset") or 0)
                end = int(span.get("end_offset") or 0)
            else:
                eid = str(getattr(span, "evidence_id", "") or "")
                locator = str(getattr(span, "locator", "") or "")
                text = str(getattr(span, "text", "") or "")
                kind = str(getattr(span, "source_kind", "text") or "text")
                artifact_id = str(getattr(span, "artifact_id", "") or "")
                step_index = int(getattr(span, "step_index", 0) or 0)
                step_type = str(getattr(span, "step_type", "") or "")
                start = int(getattr(span, "start_offset", 0) or 0)
                end = int(getattr(span, "end_offset", 0) or 0)
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind=kind,
                locator=locator or eid,
                excerpt=text[:800],
                bound_fact=claim_by_eid.get(eid, text[:240]),
                artifact_id=artifact_id,
                evidence_id=eid,
                start_offset=start,
                end_offset=end,
            )
            if not self._admit(src):
                continue
            if src.bound_fact:
                self.fact_bindings.append(
                    {
                        "fact": src.bound_fact,
                        "source_id": src.source_id,
                        "locator": src.locator,
                        "evidence_id": src.evidence_id,
                        "artifact_id": src.artifact_id,
                    }
                )
            registered.append(src)
        return registered

    def build_lookup_block(
        self, *, max_items: int = 12, excerpt_chars: int = 480
    ) -> str:
        """写报告步可回读的证据目录（digest 之外的原文摘录）。"""
        if not self.sources:
            return ""
        id_to_num = self.source_number_map()
        lines = [
            "    【可回读证据 — 按 [n] 核对，勿使用未列出的精确数字】",
            "    需要更长摘录时请读取工作目录 evidence.json / working_notes.md。",
        ]
        for src in self.sources[:max_items]:
            num = id_to_num[src.source_id]
            excerpt = (src.bound_fact or src.excerpt or "")[:excerpt_chars]
            lines.append(
                f"  [{num}] {src.source_id} ({src.source_kind}) {src.locator}"
                + (f" artifact={src.artifact_id}" if src.artifact_id else "")
                + (f" evidence={src.evidence_id}" if src.evidence_id else "")
            )
            if excerpt:
                lines.append(f"      {excerpt}")
        return "\n".join(lines)

    def _extract_sql_hint(self, content: str) -> str:
        for line in content.splitlines():
            lower = line.lower()
            if "select" in lower or "from" in lower or "表" in line:
                return line.strip()[:120]
        return ""

    def source_number_map(self) -> dict[str, int]:
        """source_id → 引用编号 [1][2]…"""
        return {src.source_id: idx + 1 for idx, src in enumerate(self.sources)}

    def build_references_block(self) -> str:
        if not self.sources:
            return ""
        lines = ["", "## 参考文献", ""]
        id_to_num = self.source_number_map()
        for src in self.sources:
            num = id_to_num[src.source_id]
            kind_label = {
                "url": "网络",
                "sql": "数据库",
                "file": "文件",
                "kb": "知识库",
                "text": "步骤产出",
            }.get(src.source_kind, src.source_kind)
            excerpt = src.excerpt[:120] + ("…" if len(src.excerpt) > 120 else "")
            lines.append(
                f"[{num}] ({kind_label}) {src.locator} — "
                f"Step {src.step_index + 1}/{src.step_type}: {excerpt}"
            )
        return "\n".join(lines)

    def inject_inline_citation_hints(self, content: str) -> str:
        """只在段落命中已绑定 fact 时补 [n]，不再按段落序号盲贴。"""
        if not self.sources or not content.strip():
            return content

        id_to_num = self.source_number_map()
        header = (
            "> **Evidence-First 报告**：正文含数字的结论应标注已登记的 [n]；"
            "完整来源见文末参考文献。未核实断言不要编造引用。\n\n"
        )
        if content.startswith("> **Evidence-First"):
            header = ""

        bindings = self.fact_bindings or [
            {"fact": src.bound_fact or src.excerpt, "source_id": src.source_id}
            for src in self.sources
            if (src.bound_fact or src.excerpt)
        ]

        paragraphs = [p for p in content.split("\n\n") if p.strip()]
        if not paragraphs:
            return header + content

        enriched: list[str] = []
        for para in paragraphs:
            if para.startswith("##") or para.startswith("> **Evidence"):
                enriched.append(para)
                continue
            if CITATION_MARKER_PATTERN.search(para):
                enriched.append(para)
                continue
            matched_nums: list[int] = []
            para_lower = para.lower()
            for bind in bindings:
                fact = str(bind.get("fact") or "").strip()
                if len(fact) < 8:
                    continue
                needle = fact[:40].lower()
                if needle and needle in para_lower:
                    num = id_to_num.get(str(bind.get("source_id")))
                    if num and num not in matched_nums:
                        matched_nums.append(num)
            if matched_nums:
                hint = "".join(f"[{n}]" for n in matched_nums[:3])
                stripped = para.rstrip()
                if stripped[-1:] in "。！？.!?":
                    enriched.append(stripped[:-1] + f" {hint}{stripped[-1]}")
                else:
                    enriched.append(stripped + f" {hint}")
            else:
                enriched.append(para)
        return header + "\n\n".join(enriched)

    def build_cited_report(self, raw_content: str) -> str:
        """生成带引用提示与参考文献块的最终报告。"""
        body = self.inject_inline_citation_hints(raw_content)
        refs = self.build_references_block()
        if refs and refs.strip() not in body:
            return body.rstrip() + "\n" + refs + "\n"
        return body

    def compute_metrics(self, final_content: str) -> dict[str, float]:
        """CCR：优先统计「含数字的句子」中带 [n] 的比例，避免把标题句算进幻觉。"""
        if not self.sources:
            return {
                "citation_coverage_rate": 0.0,
                "numeric_citation_coverage": 0.0,
                "hallucination_rate": 1.0,
                "cited_markers": 0,
                "registered_sources": 0,
            }

        cited_nums = set(int(m) for m in CITATION_MARKER_PATTERN.findall(final_content))
        registered = len(self.sources)
        source_mention = min(1.0, len(cited_nums) / registered) if registered else 0.0

        sentences = [
            s
            for s in _split_report_sentences(final_content)
            if len(s) >= 12
            and not s.startswith("#")
            and not s.startswith(">")
            and "参考文献" not in s
            and "Evidence-First" not in s
        ]
        numeric_sentences = [
            s for s in sentences if re.search(r"\d", s) and "http" not in s.lower()[:12]
        ]
        pool = numeric_sentences or sentences
        uncited = sum(1 for s in pool if not CITATION_MARKER_PATTERN.search(s))
        numeric_coverage = (len(pool) - uncited) / len(pool) if pool else source_mention
        hallucination = uncited / len(pool) if pool else (1.0 - source_mention)
        coverage = numeric_coverage if numeric_sentences else source_mention

        return {
            "citation_coverage_rate": round(coverage, 3),
            "numeric_citation_coverage": round(numeric_coverage, 3),
            "source_mention_rate": round(source_mention, 3),
            "hallucination_rate": round(min(1.0, hallucination), 3),
            "cited_markers": len(cited_nums),
            "registered_sources": registered,
            "numeric_sentence_count": len(numeric_sentences),
        }

    def validate_citations(
        self, final_content: str, min_coverage: float = 0.2
    ) -> tuple[bool, str]:
        metrics = self.compute_metrics(final_content)
        if metrics["registered_sources"] == 0:
            return True, ""
        if (
            "## 参考文献" not in final_content
            and metrics["citation_coverage_rate"] < min_coverage
        ):
            return False, "citation_coverage_low"
        if metrics["citation_coverage_rate"] < min_coverage:
            return False, "citation_coverage_low"
        return True, ""

    def to_dict_list(self) -> list[dict[str, Any]]:
        return [asdict(src) for src in self.sources]

    def checkpoint_snapshot(self) -> dict[str, Any]:
        return {
            "sources": self.to_dict_list(),
            "fact_bindings": list(self.fact_bindings),
            "counter": self._counter,
        }

    def load_from_snapshot(self, payload: dict[str, Any] | None) -> None:
        if not payload:
            return
        rows = payload.get("sources") or []
        self.sources = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            self.sources.append(
                EvidenceSource(
                    source_id=str(row.get("source_id") or ""),
                    step_index=int(row.get("step_index") or 0),
                    step_type=str(row.get("step_type") or ""),
                    source_kind=str(row.get("source_kind") or "text"),
                    locator=str(row.get("locator") or ""),
                    excerpt=str(row.get("excerpt") or ""),
                    timestamp=str(row.get("timestamp") or datetime.now().isoformat()),
                    bound_fact=str(row.get("bound_fact") or ""),
                    artifact_id=str(row.get("artifact_id") or ""),
                    evidence_id=str(row.get("evidence_id") or ""),
                    start_offset=int(row.get("start_offset") or 0),
                    end_offset=int(row.get("end_offset") or 0),
                )
            )
        self.fact_bindings = [
            dict(item)
            for item in (payload.get("fact_bindings") or [])
            if isinstance(item, dict)
        ]
        self._admission_keys = set()
        for src in self.sources:
            locator = self._canonical_locator(src.locator)
            content_hash = hashlib.sha256(
                (src.excerpt or src.bound_fact).strip().lower().encode()
            ).hexdigest()
            self._admission_keys.update(
                {f"locator:{locator}", f"content:{content_hash}"}
            )
        counter = payload.get("counter")
        if counter is not None:
            self._counter = int(counter)
        else:
            self._counter = len(self.sources)

    def save_evidence_json(
        self, session_dir: Path, *, run_id: str | None = None
    ) -> Path | None:
        if not self.sources:
            return None
        path = (
            session_dir / "evidence.json"
            if not run_id
            else session_dir / "runs" / run_id / "evidence.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sources": self.to_dict_list(),
            "generated_at": datetime.now().isoformat(),
            "run_id": run_id,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path


def _split_report_sentences(text: str) -> list[str]:
    """切句后把紧跟的 [n] 并回上一句，避免「数字句 / 引用标记」被拆开导致 CCR=0。"""
    parts = [p.strip() for p in SENTENCE_SPLIT_PATTERN.split(text or "") if p.strip()]
    merged: list[str] = []
    leading_cite = re.compile(r"^((?:\[\d+\])+)\s*(.*)$", re.DOTALL)
    for part in parts:
        match = leading_cite.match(part)
        if merged and match:
            merged[-1] = f"{merged[-1].rstrip()} {match.group(1)}"
            rest = (match.group(2) or "").strip()
            if rest:
                merged.append(rest)
        else:
            merged.append(part)
    return merged


def _excerpt_around(content: str, needle: str, width: int = 400) -> str:
    text = (content or "").replace("\n", " ")
    if not needle:
        return text[:width]
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return text[:width]
    left = max(0, idx - width // 4)
    right = min(len(text), idx + len(needle) + width)
    return text[left:right]
