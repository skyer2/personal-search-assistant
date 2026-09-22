"""UserAskContract: the verbatim record of what the user asked.

Compilation is intentionally query-preserving. A fallback may under-understand
the request, but it must never invent a different question. Every downstream
research question carries an ``ask_id`` back to one of these asks.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

AskType = Literal[
    "current_state",
    "forecast",
    "comparison",
    "recommendation",
    "explanation",
    "fact",
]

_CLAUSE_SPLIT = re.compile(r"(?<=[?？!！。;；\n])\s*")
_SOFT_SPLIT = re.compile(r"\s*[,，]\s*(?=(?:你|您|那|还有|并且|以及|另外|同时)\S)")

_FORECAST = ("未来", "接下来", "下一步", "趋势", "预测", "前景", "展望", "会怎样", "将会", "发展方向", "方向是")
_CURRENT = ("最新", "当前", "现在", "目前", "截至", "近期", "今年", "热点", "进展", "现状")
_COMPARISON = ("对比", "比较", "区别", "差异", "vs", "versus", "哪个更", "谁更")
_RECOMMENDATION = ("推荐", "值得", "应该选", "该选", "建议", "怎么选", "如何选", "适合")
_EXPLANATION = ("为什么", "为何", "原因", "怎么做到", "如何实现", "机制")
_FACT = ("是谁", "哪一年", "哪年", "什么时候", "多少", "发布时间", "总部")

# Output-format instructions are deliverable constraints, not research asks.
_DELIVERY_ONLY = re.compile(
    r"^(?:请)?(?:输出|生成|导出|保存|写成|转成|以)?\s*(?:结果|内容|报告|文件|答案)?\s*"
    r"(?:为|用|成|是)?\s*(?:pdf|md|markdown|word|excel|csv|html|表格|文档)\b"
    r"|^(?:请)?(?:用|使用)?\s*(?:中文|英文|简体|繁体)\s*(?:回答|输出|撰写|作答)?$"
    r"|^(?:输出|交付)(?:格式|形式)",
    re.IGNORECASE,
)
_CONTINUATION = ("为什么", "为何", "原因", "理由", "why", "怎么讲", "如何解释")

# Chinese numeric horizons: 1-2年 / 一到两年 / 3个月 / 未来两年
_HORIZON = re.compile(
    r"(?:未来|今后|接下来|下一?)\s*"
    r"(?:\d+\s*[-–~到至]?\s*\d*|[一二三四五六七八九十]+\s*[-–~到至]?\s*[一二三四五六七八九十]*)"
    r"\s*(?:年|个月|月|季度|周)"
)
_POINT_IN_TIME = re.compile(
    r"(?:截至|到)?\s*(?:20\d{2})\s*年?\s*(?:0?[1-9]|1[0-2])?\s*月?|(?:20\d{2})-(?:0?[1-9]|1[0-2])"
)

_SUBJECT_STOP = {
    "你觉得", "你觉的", "我想知道", "请问", "请", "帮我", "麻烦", "的", "是什么", "有哪些",
    "呢", "吗", "啊", "吧", "最新", "当前", "未来", "发展", "方向", "热点", "趋势",
    "生成", "输出", "分析", "比较", "对比", "评估", "查找", "整理", "总结", "介绍",
    "报告", "研究", "深度", "一份", "关于", "结果", "内容", "情况", "现状", "公司",
    "潜力", "值得", "加入", "为什么", "哪些",
    # Verbs and deliverable formats are never the research subject.
    "evaluate", "compare", "analyze", "analyse", "summarize", "summarise",
    "find", "list", "explain", "describe", "generate", "write", "review",
    "pdf", "md", "markdown", "word", "excel", "csv", "html", "doc", "docx",
    "report", "research", "about", "role", "the", "for", "and", "with",
}

_SUBJECT_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.+#/-]{1,}|[\u4e00-\u9fff]{2,}")
_MAX_SUBJECT_CHARS = 10
_CJK_STOP_SPLIT = re.compile(
    "|".join(
        sorted(
            (
                re.escape(item)
                for item in (
                    "生成", "输出", "分析", "比较", "对比", "评估", "查找", "整理",
                    "总结", "介绍", "一份", "关于", "深度", "研究", "报告", "结果",
                    "内容", "情况", "现状", "公司", "潜力", "值得", "加入", "哪些",
                    "为什么", "最新", "当前", "未来", "发展", "方向", "热点", "趋势",
                    "有", "的", "是",
                )
            ),
            key=len,
            reverse=True,
        )
    )
)


def _ask_id(index: int) -> str:
    return f"A{index}"


def is_delivery_instruction(text: str) -> bool:
    """A clause that only specifies output format is a constraint, not an ask."""
    blob = str(text or "").strip()
    if not blob or len(blob) > 30:
        return False
    return bool(_DELIVERY_ONLY.search(blob))


def _is_continuation(text: str) -> bool:
    """A short trailing follow-up like '为什么' belongs to the previous ask."""
    blob = str(text or "").strip().rstrip("？?。.！!")
    if not blob or len(blob) > 8:
        return False
    return any(blob.startswith(token) for token in _CONTINUATION)


def split_user_clauses(query: str) -> list[str]:
    """Split a multi-part user query into its original clauses, verbatim.

    Delivery-format instructions are dropped and short follow-up fragments are
    merged into the ask they qualify, so one question never becomes three.
    """
    text = str(query or "").strip()
    if not text:
        return []
    parts: list[str] = []
    for chunk in _CLAUSE_SPLIT.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        for piece in _SOFT_SPLIT.split(chunk):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    merged: list[str] = []
    for piece in parts:
        if is_delivery_instruction(piece):
            continue
        if _is_continuation(piece) and merged:
            merged[-1] = f"{merged[-1].rstrip('？?。.！!')}，{piece}"
            continue
        if len(piece) < 4 and "?" not in piece and "？" not in piece:
            if merged:
                merged[-1] = f"{merged[-1].rstrip('？?。.！!')}，{piece}"
            continue
        merged.append(piece)
    return merged or ([text] if text else [])


def classify_ask_type(text: str) -> AskType:
    """Classify one clause. Ordering favours the most specific signal."""
    blob = str(text or "")
    lowered = blob.lower()
    if any(token in blob for token in _FORECAST):
        return "forecast"
    if any(token in blob or token in lowered for token in _COMPARISON):
        return "comparison"
    if any(token in blob for token in _RECOMMENDATION):
        return "recommendation"
    if any(token in blob for token in _CURRENT):
        return "current_state"
    if any(token in blob for token in _EXPLANATION):
        return "explanation"
    if any(token in blob for token in _FACT):
        return "fact"
    return "current_state"


def extract_time_scope(text: str) -> str:
    """Return the verbatim time scope fragment, or an empty string."""
    blob = str(text or "")
    horizon = _HORIZON.search(blob)
    if horizon:
        return horizon.group(0).strip()
    point = _POINT_IN_TIME.search(blob)
    if point and point.group(0).strip(" 年月-"):
        return point.group(0).strip()
    return ""


def extract_subject(text: str, *, fallback: str = "") -> str:
    """Best-effort subject extraction that never fabricates a new topic.

    Verbs, deliverable formats and time horizons are excluded; among what is
    left the most discriminative token wins (proper nouns, then longest term).
    """
    blob = str(text or "")
    tokens = [
        token
        for token in _SUBJECT_TOKEN.findall(blob)
        if token.lower() not in _SUBJECT_STOP and token not in _SUBJECT_STOP
    ]
    tokens = [token for token in tokens if not _HORIZON.search(token)]
    if not tokens:
        return fallback.strip()
    latin = [token for token in tokens if re.match(r"^[A-Za-z]", token)]
    if latin:
        # Capitalised or all-caps names are stronger signals than lowercase words.
        proper = [token for token in latin if token[:1].isupper()]
        pool = proper or latin
        return max(pool, key=len)
    # Chinese runs arrive as one greedy token; split on stop terms so the subject
    # stays a topic ("量子计算") instead of the whole sentence.
    segments: list[str] = []
    for token in tokens:
        for segment in _CJK_STOP_SPLIT.split(token):
            segment = segment.strip()
            if 2 <= len(segment) <= _MAX_SUBJECT_CHARS:
                segments.append(segment)
    if segments:
        return max(segments, key=len)
    return max(tokens, key=len)[:_MAX_SUBJECT_CHARS]


@dataclass(frozen=True)
class UserAsk:
    ask_id: str
    text: str
    ask_type: AskType = "current_state"
    subject: str = ""
    time_scope: str = ""
    required: bool = True
    answer_requirements: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UserAsk":
        row = data or {}
        ask_type = str(row.get("ask_type") or "current_state")
        if ask_type not in {
            "current_state", "forecast", "comparison",
            "recommendation", "explanation", "fact",
        }:
            ask_type = "current_state"
        return cls(
            ask_id=str(row.get("ask_id") or ""),
            text=str(row.get("text") or ""),
            ask_type=ask_type,  # type: ignore[arg-type]
            subject=str(row.get("subject") or ""),
            time_scope=str(row.get("time_scope") or ""),
            required=bool(row.get("required", True)),
            answer_requirements=tuple(
                str(item) for item in row.get("answer_requirements") or [] if str(item).strip()
            ),
        )


@dataclass(frozen=True)
class UserAskContract:
    contract_id: str
    raw_query: str
    asks: tuple[UserAsk, ...] = field(default_factory=tuple)
    source: str = "deterministic_clause_split"

    @property
    def required_asks(self) -> tuple[UserAsk, ...]:
        return tuple(ask for ask in self.asks if ask.required)

    def ask(self, ask_id: str) -> UserAsk | None:
        return next((item for item in self.asks if item.ask_id == ask_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "raw_query": self.raw_query,
            "asks": [item.to_dict() for item in self.asks],
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UserAskContract":
        row = data or {}
        return cls(
            contract_id=str(row.get("contract_id") or ""),
            raw_query=str(row.get("raw_query") or ""),
            asks=tuple(
                UserAsk.from_dict(item)
                for item in row.get("asks") or []
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ),
            source=str(row.get("source") or "deterministic_clause_split"),
        )


def _answer_requirements(ask_type: AskType, time_scope: str) -> tuple[str, ...]:
    requirements: list[str] = ["direct_answer"]
    if ask_type == "current_state":
        requirements.extend(["dated_evidence", "concrete_entities"])
    elif ask_type == "forecast":
        requirements.extend(["current_signal", "mechanism", "observable_milestone", "uncertainty"])
    elif ask_type == "comparison":
        requirements.extend(["per_subject_evidence", "explicit_differences"])
    elif ask_type == "recommendation":
        requirements.extend(["decision_criteria", "risks"])
    elif ask_type == "explanation":
        requirements.append("causal_evidence")
    else:
        requirements.append("primary_source")
    if time_scope:
        requirements.append("time_scope_respected")
    return tuple(dict.fromkeys(requirements))


def compile_user_ask_contract(query: str, *, conversation_delta: str = "") -> UserAskContract:
    """Deterministically compile the contract while preserving the user's words."""
    raw = " ".join(part for part in (str(query or ""), str(conversation_delta or "")) if part.strip()).strip()
    clauses = split_user_clauses(raw)
    global_subject = extract_subject(raw)
    asks: list[UserAsk] = []
    for index, clause in enumerate(clauses, 1):
        ask_type = classify_ask_type(clause)
        subject = extract_subject(clause, fallback=global_subject)
        time_scope = extract_time_scope(clause) or (
            extract_time_scope(raw) if ask_type != "forecast" else ""
        )
        asks.append(
            UserAsk(
                ask_id=_ask_id(index),
                text=clause,
                ask_type=ask_type,
                subject=subject or global_subject,
                time_scope=time_scope,
                required=True,
                answer_requirements=_answer_requirements(ask_type, time_scope),
            )
        )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return UserAskContract(
        contract_id=f"asks_{digest}",
        raw_query=str(query or ""),
        asks=tuple(asks),
        source="deterministic_clause_split",
    )


__all__ = [
    "AskType",
    "UserAsk",
    "UserAskContract",
    "classify_ask_type",
    "compile_user_ask_contract",
    "extract_subject",
    "extract_time_scope",
    "split_user_clauses",
]
