"""P1/P2 生命周期回归：RunSummary / FollowUp / Session CRUD / Retention / Queue。"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.research.followup import FollowupType, resolve_followup
from app.research.run_summary import RunSummary, load_session_run_summaries, save_run_summary
from app.run_queue.service import RunQueue
from app.run_store.retention import RetentionPolicy, apply_retention
from app.run_store.service import RunStore


def _make_summary(run_id: str, query: str, *, created_at: str = "2026-09-05T10:00:00+00:00") -> RunSummary:
    return RunSummary(
        run_id=run_id,
        session_id="s1",
        query=query,
        intent_summary=f"关于{query}的调研",
        entities=[query],
        conclusions=[f"{query}的结论A"],
        artifact_refs=[f"{run_id}.pdf"],
        unresolved_questions=[f"{query}还有什么没解决"],
        created_at=created_at,
    )


def test_run_summary_roundtrip(tmp_path):
    run_dir = tmp_path / "runs" / "run_001"
    save_run_summary(run_dir, _make_summary("run_001", "AI Coding 前景"))
    summaries = load_session_run_summaries(tmp_path, limit=5)
    assert len(summaries) == 1
    assert summaries[0].run_id == "run_001"
    assert summaries[0].conclusions == ["AI Coding 前景的结论A"]
    print("[OK] run summary roundtrip")


def test_followup_explicit_reference(tmp_path):
    save_run_summary(tmp_path / "runs" / "run_001", _make_summary("run_001", "大模型排序"))
    ctx = resolve_followup("上一轮提到的排序再展开讲讲", tmp_path)
    assert ctx.followup_type == FollowupType.EXPLICIT_REFERENCE
    assert "run_001" in ctx.selected_run_ids
    assert "previous_run run_001" in ctx.context_block
    print("[OK] explicit reference inherits summary")


def test_followup_semantic_overlap(tmp_path):
    save_run_summary(tmp_path / "runs" / "run_001", _make_summary("run_001", "大模型排序 评测"))
    ctx = resolve_followup("大模型排序有哪些变化", tmp_path)
    assert ctx.followup_type == FollowupType.SEMANTIC_FOLLOWUP
    assert ctx.selected_run_ids
    print("[OK] semantic followup selects relevant summary")


def test_followup_standalone_no_inherit(tmp_path):
    save_run_summary(tmp_path / "runs" / "run_001", _make_summary("run_001", "量子计算科普"))
    ctx = resolve_followup("今天上海天气怎么样", tmp_path)
    assert ctx.followup_type == FollowupType.STANDALONE
    assert ctx.selected_run_ids == []
    assert ctx.context_block == ""
    print("[OK] standalone question inherits nothing")


def test_session_tenant_and_archive(tmp_path):
    store = RunStore(tmp_path / "store.sqlite")
    store.ensure_session_owned("sA", tenant_id="tenantA", user_id="u1")
    store.create_run(
        run_id="rA1", session_id="sA", query="q", tenant_id="tenantA", user_id="u1"
    )
    store.ensure_session_owned("sB", tenant_id="tenantB", user_id="u2")

    sessions_a = store.list_sessions(tenant_id="tenantA")
    assert [s.session_id for s in sessions_a] == ["sA"]

    assert store.set_session_archived("sA", archived=True, tenant_id="tenantB") is None
    assert store.set_session_archived("sA", archived=True, tenant_id="tenantA") is not None
    assert store.get_run_scoped("rA1", "tenantB") is None
    assert store.get_run_scoped("rA1", "tenantA") is not None
    print("[OK] tenant ownership boundary")


def test_delete_session_and_run_cascade(tmp_path):
    store = RunStore(tmp_path / "store.sqlite")
    store.ensure_session_owned("s1")
    store.create_run(run_id="r1", session_id="s1", query="q1")
    store.create_run(run_id="r2", session_id="s1", query="q2")
    session_dir = tmp_path / "output" / "session_s1"
    for run_id in ("r1", "r2"):
        deliverable = session_dir / "runs" / run_id / "deliverables"
        deliverable.mkdir(parents=True)
        (deliverable / "report.pdf").write_bytes(b"%PDF")

    result = store.delete_run("r1", output_root=tmp_path / "output")
    assert result["deleted"] is True
    assert not (session_dir / "runs" / "r1").exists()
    assert (session_dir / "runs" / "r2" / "deliverables" / "report.pdf").exists()
    assert store.get_run("r1") is None
    assert store.get_run("r2") is not None

    result = store.delete_session("s1", output_root=tmp_path / "output")
    assert result["deleted"] is True
    assert result["runs_deleted"] >= 1
    assert not session_dir.exists()
    assert store.get_session("s1") is None
    print("[OK] delete session/run cascades")


def test_retention_purges_intermediate_keeps_deliverables(tmp_path):
    store = RunStore(tmp_path / "store.sqlite")
    store.ensure_session_owned("s1")
    store.create_run(run_id="old", session_id="s1", query="q")
    old_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store.db.execute(
        "UPDATE runs SET status='completed', ended_at=? WHERE run_id='old'", (old_time,)
    )
    run_dir = tmp_path / "output" / "session_s1" / "runs" / "old"
    for sub in ("artifacts", "evidence", "state", "deliverables"):
        (run_dir / sub).mkdir(parents=True)
        (run_dir / sub / "x.bin").write_bytes(b"x")
    save_run_summary(run_dir, _make_summary("old", "q"))

    result = apply_retention(
        store,
        policy=RetentionPolicy(intermediate_retention_days=7),
        output_root=tmp_path / "output",
    )
    assert result["purged_runs"] == 1
    assert not (run_dir / "artifacts").exists()
    assert not (run_dir / "evidence").exists()
    assert not (run_dir / "state").exists()
    assert (run_dir / "deliverables" / "x.bin").exists()
    assert (run_dir / "run_summary.json").exists()
    print("[OK] retention keeps deliverables and summary")


def test_durable_run_queue_cycle(tmp_path):
    queue = RunQueue(tmp_path / "queue.sqlite")
    queue.enqueue(job_id="job1", session_id="s1", query="q", mode="agent")
    assert queue.get_job("job1").status == "pending"

    job = queue.claim_next("worker-1")
    assert job is not None and job.job_id == "job1"
    assert job.status == "running"
    assert queue.claim_next("worker-2") is None

    queue.heartbeat("job1", "worker-1")
    queue.complete("job1", "worker-1")
    assert queue.get_job("job1").status == "completed"

    queue.enqueue(job_id="job2", session_id="s1", query="q2")
    queue.claim_next("worker-1")
    queue.fail("job2", "worker-1", "boom")
    assert queue.get_job("job2").status == "failed"
    assert queue.get_job("job2").error == "boom"
    queue.close()
    print("[OK] durable run queue claim/complete/fail")


if __name__ == "__main__":
    print("run with pytest: pytest tests/test_run_lifecycle.py -q")
