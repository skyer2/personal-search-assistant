"""Run Isolation 不变量回归测试。

对应评审文档第十八节的系统级用例：同 Session 多 Run 时，
交付物 / 文件列表 / Leaf thread / 终态语义不得串 Run。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.deliverables import materialize_requested_files
from app.agent.harness.window_hygiene import (
    parallel_graph_thread_id,
    step_graph_thread_id,
)
from app.api.monitor import monitor
from app.config.loader import reload_harness_config
from app.run_store import RunStore
from app.run_store.files import list_run_output_files, list_output_files


def test_markdown_not_reused_across_runs(tmp_path):
    run1 = tmp_path / "runs" / "run_001" / "deliverables"
    run2 = tmp_path / "runs" / "run_002" / "deliverables"
    run1.mkdir(parents=True)
    run2.mkdir(parents=True)

    materialize_requested_files(
        run1, deliverable="md", content="Run1 报告正文", query="AI Coding 能否彻底解决软件开发"
    )
    run2_written = materialize_requested_files(
        run2, deliverable="md", content="Run2 报告正文", query="当前最好的大模型排序"
    )
    run2_md = run2_written.get("md")

    assert run2_md is not None
    assert run2_md.read_text(encoding="utf-8") != run1.joinpath(
        "AI Coding 能否彻底解决软件开发.md"
    ).read_text(encoding="utf-8")
    assert "Run2 报告正文" in run2_md.read_text(encoding="utf-8")
    print("[OK] run2 never reuses run1 markdown")


def test_persist_markdown_missing_never_falls_back_to_existing(tmp_path):
    from app.agent.harness.deliverables import persist_markdown_if_missing

    (tmp_path / "旧报告.md").write_text("# 旧报告", encoding="utf-8")
    path = persist_markdown_if_missing(
        tmp_path, content="新报告正文", filename_stem="新报告"
    )
    assert path is not None
    assert path.name == "新报告.md"
    assert "新报告正文" in path.read_text(encoding="utf-8")
    print("[OK] no arbitrary existing[0] fallback")


def test_run_artifacts_listing_is_run_scoped(tmp_path):
    session_root = tmp_path / "session_s1"
    legacy = session_root / "legacy.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    for run_id, name in (("run_001", "A.pdf"), ("run_002", "B.pdf")):
        run_deliverables = session_root / "runs" / run_id / "deliverables"
        run_deliverables.mkdir(parents=True)
        (run_deliverables / name).write_bytes(name.encode())

    run2_files = list_run_output_files(tmp_path, "s1", "run_002")
    names = [f["name"] for f in run2_files]
    assert names == ["B.pdf"]
    assert "A.pdf" not in names
    assert "legacy.pdf" not in names

    # Session 级列表仍是完整历史（历史视图），但 Run 级严格隔离
    session_files = list_output_files(tmp_path, "s1")
    session_names = {f["name"] for f in session_files}
    assert {"A.pdf", "B.pdf", "legacy.pdf"} <= session_names
    print("[OK] run endpoint lists only run-owned files")


def test_bootstrap_output_files_are_current_run_scoped(tmp_path):
    output_root = tmp_path / "output"
    store = RunStore(tmp_path / "run.sqlite")
    store.create_run(run_id="run_001", session_id="s1", query="q1")
    store.create_run(run_id="run_002", session_id="s1", query="q2")

    session_root = output_root / "session_s1"
    legacy = session_root / "legacy.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    for run_id, name in (("run_001", "A.pdf"), ("run_002", "B.pdf")):
        deliverables = session_root / "runs" / run_id / "deliverables"
        deliverables.mkdir(parents=True)
        (deliverables / name).write_bytes(name.encode())

    boot = store.bootstrap("s1", output_root=output_root)
    assert boot.current_run is not None
    assert boot.current_run.run_id == "run_002"
    assert [item["name"] for item in boot.output_files] == ["B.pdf"]

    upload_only = RunStore(tmp_path / "upload-only.sqlite")
    upload_only.add_upload("s2", "paper.pdf", 12)
    (output_root / "session_s2" / "legacy.pdf").parent.mkdir(parents=True)
    (output_root / "session_s2" / "legacy.pdf").write_bytes(b"legacy")
    assert upload_only.bootstrap("s2", output_root=output_root).output_files == []
    print("[OK] bootstrap lists only current run files")


def test_leaf_graph_thread_ids_differ_across_runs():
    t1 = step_graph_thread_id("s1", 0, "run_001")
    t2 = step_graph_thread_id("s1", 0, "run_002")
    assert t1 != t2
    assert "run_001" in t1 and "run_002" in t2

    p1 = parallel_graph_thread_id("s1", 0, "run_001")
    p2 = parallel_graph_thread_id("s1", 0, "run_002")
    assert p1 != p2
    assert p1 != t1
    print("[OK] leaf thread ids include run_id")


def test_task_result_carries_structured_status():
    captured: list[dict] = []
    original = monitor._emit

    def _capture(event: str, message: str, data: dict) -> None:
        captured.append({"event": event, "message": message, "data": data})

    monitor._emit = _capture
    try:
        monitor.report_task_result(
            "部分交付正文",
            status="partial",
            termination_reason="budget_tool_calls",
            termination_stage="research",
        )
    finally:
        monitor._emit = original

    assert captured, "task_result event must be emitted"
    payload = captured[0]
    assert payload["event"] == "task_result"
    assert payload["data"]["status"] == "partial"
    assert payload["data"]["termination"]["reason"] == "budget_tool_calls"
    print("[OK] task_result carries real status")


def test_budget_ceiling_raised_for_deep_research():
    cfg = reload_harness_config()
    assert cfg.max_tool_calls == 120
    assert cfg.max_step_tool_calls == 16
    assert cfg.max_run_sec == 900
    assert cfg.max_total_tokens == 180000
    print("[OK] budget ceiling 120/16/900s")


if __name__ == "__main__":
    print("run with pytest: pytest tests/test_run_isolation.py -q")
