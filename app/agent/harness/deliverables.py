"""会话交付物落盘：Markdown / PDF。

LLM Worker 经常只写一段「已转换」文字而不调工具。规则引擎认出 pdf 后，
校验与 finalize 必须确定性写盘，不能把文件交付降成聊天摘要。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.utils.word_converter import convert_md_to_pdf as convert_md_to_pdf_via_reportlab

_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\n\r\t]+')
_NON_REPORT_PREFIXES = (
    "成功转换",
    "转换完成",
    "转换失败",
    "缺少依赖",
    "Markdown文件",
    "已成功生成",
    "生成Markdown文件失败",
    "错误：文件不存在",
    "步骤执行超时",
    "查询出现异常",
    "并行步骤执行异常",
)


def list_markdown_files(session_dir: Path) -> list[Path]:
    root = Path(session_dir)
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def list_pdf_files(session_dir: Path) -> list[Path]:
    root = Path(session_dir)
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.pdf") if p.is_file())


def filename_stem_from_query(query: str, default: str = "调研报告") -> str:
    text = _UNSAFE_FILENAME.sub(" ", (query or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    for token in ("pdf", "PDF", "Markdown", "markdown"):
        text = text.replace(token, " ")
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    if len(text) > 40:
        text = text[:40].rstrip()
    return text or default


def usable_report_text(content: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    if any(text.startswith(prefix) or prefix in text[:40] for prefix in _NON_REPORT_PREFIXES):
        return ""
    return text


def best_report_text(*chunks: str) -> str:
    usable = [usable_report_text(chunk) for chunk in chunks]
    usable = [text for text in usable if text]
    if not usable:
        return ""
    return max(usable, key=len)


def all_report_text(*chunks: str) -> str:
    usable = [usable_report_text(chunk) for chunk in chunks]
    return "\n\n".join(text for text in usable if text)


def _step_result_chunks(state: object) -> list[str]:
    chunks: list[str] = [str(getattr(state, "final_content", "") or "")]
    for result in getattr(state, "step_results", None) or []:
        chunks.append(
            str(
                getattr(result, "compressed_content", "")
                or getattr(result, "content", "")
                or ""
            )
        )
    return chunks


def content_from_loop_state(state: object, *, join_all: bool = False) -> str:
    chunks = _step_result_chunks(state)
    if join_all:
        return all_report_text(*chunks)
    return best_report_text(*chunks)


def report_markdown_from_state(state: object, *, abort_reason: str = "") -> str:
    """预算中止时仍拼一份可读 Markdown，避免 PDF 交付变成空白聊天。"""
    title = filename_stem_from_query(
        str(getattr(getattr(state, "intent", None), "raw_query", "") or ""),
        "调研报告",
    )
    reason = abort_reason or str(getattr(state, "abort_reason", "") or "")
    body = content_from_loop_state(state, join_all=True)
    parts: list[str] = []
    if reason:
        parts.append(f"> 本次运行因 `{reason}` 提前结束，下文是已收集材料的部分交付，不是完整终稿。")
    if body.lstrip().startswith("#"):
        parts.append(body)
    elif body:
        parts.append(f"# {title}\n\n{body}")
    else:
        parts.append(f"# {title}\n\n没有可用的研究报告正文。")
    return "\n\n".join(parts).strip()


def session_artifact_names(session_dir: Path) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for path in [*list_markdown_files(session_dir), *list_pdf_files(session_dir)]:
        if path.name in seen:
            continue
        seen.add(path.name)
        names.append(path.name)
    return names


def ensure_requested_deliverables(session_dir: Path, state: object) -> dict[str, Path | None]:
    intent = getattr(state, "intent", None)
    deliverable = str(getattr(intent, "deliverable", "") or "")
    query = str(getattr(intent, "raw_query", "") or "")
    abort_reason = str(getattr(state, "abort_reason", "") or "")
    if deliverable not in {"md", "pdf"}:
        return {"md": None, "pdf": None}
    content = content_from_loop_state(state, join_all=True)
    if abort_reason or not usable_report_text(content):
        content = report_markdown_from_state(state, abort_reason=abort_reason)
    return materialize_requested_files(
        session_dir,
        deliverable=deliverable,
        content=content,
        query=query,
    )


def persist_markdown_if_missing(
    session_dir: Path,
    content: str = "",
    *,
    filename_stem: str = "",
) -> Path | None:
    root = Path(session_dir)
    root.mkdir(parents=True, exist_ok=True)
    existing = list_markdown_files(root)
    if existing:
        return existing[0]
    text = usable_report_text(content)
    if not text:
        return None
    stem = filename_stem_from_query(filename_stem) if filename_stem else "调研报告"
    path = root / f"{stem}.md"
    body = text if text.lstrip().startswith("#") else f"# {stem}\n\n{text}"
    path.write_text(body, encoding="utf-8")
    return path


def ensure_pdf_from_markdown(
    session_dir: Path,
    *,
    content: str = "",
    filename_stem: str = "",
) -> Path | None:
    root = Path(session_dir)
    root.mkdir(parents=True, exist_ok=True)
    existing = list_pdf_files(root)
    if existing:
        return existing[0]
    md_path = persist_markdown_if_missing(
        root,
        content,
        filename_stem=filename_stem,
    )
    if md_path is None:
        return None
    pdf_path = md_path.with_suffix(".pdf")
    convert_md_to_pdf_via_reportlab(md_path, pdf_path)
    if pdf_path.exists():
        return pdf_path
    return None


def materialize_requested_files(
    session_dir: Path,
    *,
    deliverable: str,
    content: str = "",
    query: str = "",
) -> dict[str, Path | None]:
    stem = filename_stem_from_query(query)
    md_path = None
    pdf_path = None
    if deliverable in {"md", "pdf"}:
        md_path = persist_markdown_if_missing(
            session_dir,
            content,
            filename_stem=stem,
        )
    if deliverable == "pdf":
        pdf_path = ensure_pdf_from_markdown(
            session_dir,
            content=content,
            filename_stem=stem,
        )
    return {"md": md_path, "pdf": pdf_path}
