"""Session-scoped artifact and upload listing — no frontend absolute paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_INTERNAL_STEMS = {"working_notes", "evidence", "checkpoint"}


def _artifact_sort_key(item: dict[str, Any]) -> tuple:
    name = str(item.get("name") or "")
    lower = name.lower()
    internal = 1 if Path(name).stem.lower() in _INTERNAL_STEMS else 0
    pdf = 0 if lower.endswith(".pdf") else 1
    markdown = 0 if lower.endswith(".md") else 1
    return (internal, pdf, markdown, -float(item.get("mtime") or 0))


def session_output_dir(output_root: Path, session_id: str) -> Path:
    return Path(output_root) / f"session_{session_id}"


def run_output_dir(output_root: Path, session_id: str, run_id: str) -> Path:
    return session_output_dir(output_root, session_id) / "runs" / run_id


def session_upload_dir(updated_root: Path, session_id: str) -> Path:
    return Path(updated_root) / f"session_{session_id}"


def list_output_files(output_root: Path, session_id: str) -> list[dict[str, Any]]:
    root = session_output_dir(output_root, session_id)
    if not root.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "type": "file",
                "path": rel,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    files.sort(key=_artifact_sort_key)
    return files


def list_run_output_files(
    output_root: Path,
    session_id: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """严格 Run Scope：只列 runs/{run_id}/ 下的文件；不存在则空（不回落 Session）。"""
    root = run_output_dir(output_root, session_id, run_id)
    if not root.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "type": "file",
                "path": rel,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    files.sort(key=_artifact_sort_key)
    return files


def resolve_output_file(output_root: Path, session_id: str, relative_name: str) -> Path:
    root = session_output_dir(output_root, session_id).resolve()
    target = (root / relative_name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path escapes session output directory")
    return target


def list_upload_files_from_disk(updated_root: Path, session_id: str) -> list[dict[str, Any]]:
    root = session_upload_dir(updated_root, session_id)
    if not root.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "uploaded_at": "",
                "server_path": path.name,
            }
        )
    return files
