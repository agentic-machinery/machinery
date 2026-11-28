"""Defect-class tests for task-log retention byte preview and protection invariants."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lu_machinery.maintenance import task_log_retention
from lu_machinery.orchestration.task_record_store import task_state_lock


def _setup_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    live_workers: set[str] | None = None,
    live_providers: set[str] | None = None,
) -> tuple[Path, Path, Path, Path, datetime, datetime]:
    tasks = tmp_path / "tasks"
    archive = tasks / "archive"
    logs = tasks / "logs"
    archive.mkdir(parents=True)
    logs.mkdir()
    monkeypatch.delenv("LU_HOST_CONTEXT", raising=False)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    hold_list = tmp_path / "holds.json"
    hold_list.write_text('{"schema":"batch-state-retention.v1","holds":[]}', encoding="utf-8")
    monkeypatch.setattr(task_log_retention, "RETENTION_LIST", hold_list)
    live_worker_ids = live_workers or set()
    live_provider_ids = live_providers or set()
    monkeypatch.setattr(
        task_log_retention,
        "worker_liveness",
        lambda record: record["task_id"] in live_worker_ids,
    )
    provider_alive = task_log_retention.worker._provider_alive
    monkeypatch.setattr(
        task_log_retention.worker,
        "_provider_alive",
        lambda record: (
            record["task_id"] in live_provider_ids
            if record.get("launch_mode") == "scope"
            else provider_alive(record)
        ),
    )
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    return tasks, archive, logs, hold_list, now, old


def _write_record(
    where: Path,
    task_id: str,
    *,
    old: datetime,
    name: str | None = None,
    **changes: object,
) -> None:
    record: dict[str, object] = {
        "schema_version": 2,
        "task_id": task_id,
        "status": "done",
        "finished_at": old.isoformat(),
        "pid": None,
        "provider_pgid": None,
    }
    record.update(changes)
    (where / (name or f"{task_id}.json")).write_text(json.dumps(record), encoding="utf-8")


def _write_log(logs: Path, name: str, size: int, *, mtime: datetime) -> Path:
    path = logs / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def test_dry_run_reports_exact_eligible_bytes_and_leaves_files_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: dry-run missing or miscounting eligible_bytes, or mutating disk state."""
    tasks, archive, logs, _hold_list, _now, old = _setup_store(tmp_path, monkeypatch)
    _write_record(tasks, "alpha.v2", old=old, pid=101, launch_mode="scope")
    _write_record(archive, "alpha.v2", old=old)
    _write_record(archive, "archived-only", old=old)
    _write_record(tasks, "empty-log", old=old)

    sizes = {
        "alpha.v2.stdout.log": 17,
        "alpha.v2.stderr.log": 31,
        "archived-only.stderr.log": 53,
        "empty-log.stdout.log": 0,
    }
    for name, size in sizes.items():
        _write_log(logs, name, size, mtime=old)

    before = {entry.name: (logs / entry.name).read_bytes() for entry in logs.iterdir()}

    assert task_log_retention.main([]) == 0
    full_report = json.loads(capsys.readouterr().out)
    assert full_report == {
        "mode": "dry-run",
        "selected": 4,
        "eligible_bytes": sum(sizes.values()),
        "logs": sorted(sizes),
        "retained": {},
    }

    assert task_log_retention.main(["--summary"]) == 0
    summary_report = json.loads(capsys.readouterr().out)
    assert summary_report == {
        "mode": "dry-run",
        "selected": 4,
        "eligible_bytes": sum(sizes.values()),
        "retained": {},
    }

    assert {entry.name: (logs / entry.name).read_bytes() for entry in logs.iterdir()} == before
    assert list(tasks.glob("*.lock")) == []


def test_protected_logs_excluded_from_eligible_bytes_and_apply_reclaims_exact_sum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: held, live, or non-regular logs counted in eligible_bytes or unlinked."""
    tasks, archive, logs, hold_list, now, old = _setup_store(
        tmp_path,
        monkeypatch,
        live_workers={"still-live"},
        live_providers={"live-provider"},
    )

    # Eligible settled tasks with distinct sizes.
    _write_record(tasks, "ok-one", old=old, pid=101, launch_mode="scope")
    _write_record(archive, "ok-two", old=old)
    _write_log(logs, "ok-one.stdout.log", 19, mtime=old)
    _write_log(logs, "ok-two.stderr.log", 41, mtime=old)
    expected_reclaimed = 19 + 41

    # Protected tasks with large payloads so any leakage immediately breaks the byte sum.
    _write_record(tasks, "held-hot", old=old, worktree_retention_hold=True)
    _write_log(logs, "held-hot.stdout.log", 1009, mtime=old)

    _write_record(tasks, "held-arch", old=old)
    _write_record(
        archive,
        "held-arch",
        old=old,
        name="held-arch.20260925T120000Z.7.archived.json",
        worktree_retention_hold=True,
    )
    _write_log(logs, "held-arch.stderr.log", 1013, mtime=old)

    _write_record(tasks, "bad-hold", old=old, worktree_retention_hold="false")
    _write_log(logs, "bad-hold.stdout.log", 1019, mtime=old)

    _write_record(tasks, "unsettled", old=old, status="running")
    _write_log(logs, "unsettled.stdout.log", 1021, mtime=old)

    _write_record(tasks, "still-live", old=old, pid=102, launch_mode="scope")
    _write_log(logs, "still-live.stderr.log", 1031, mtime=old)

    _write_record(tasks, "live-provider", old=old, pid=103, launch_mode="scope")
    _write_log(logs, "live-provider.stdout.log", 1033, mtime=old)

    _write_record(tasks, "admit-hold", old=old, admission_hold={"holder": "worker"})
    _write_log(logs, "admit-hold.stdout.log", 1039, mtime=old)

    _write_record(tasks, "no-scope", old=old, launch_mode="popen-fallback")
    _write_log(logs, "no-scope.stderr.log", 1049, mtime=old)

    _write_record(tasks, "busy", old=old)
    _write_log(logs, "busy.stderr.log", 1051, mtime=old)

    _write_record(tasks, "recent-file", old=old)
    _write_log(logs, "recent-file.stdout.log", 1061, mtime=now)

    # Non-regular entries: symlink to large outside file, directory, and FIFO.
    outside_target = tmp_path / "outside-target.log"
    outside_target.write_bytes(b"z" * 4096)
    os.utime(outside_target, (old.timestamp(), old.timestamp()))
    _write_record(tasks, "sym-task", old=old)
    (logs / "sym-task.stdout.log").symlink_to(outside_target)

    _write_record(tasks, "dir-task", old=old)
    (logs / "dir-task.stderr.log").mkdir()

    _write_record(tasks, "fifo-task", old=old)
    os.mkfifo(logs / "fifo-task.stdout.log")

    def regular_bytes_in_logs() -> int:
        total = 0
        for entry in logs.iterdir():
            info = os.lstat(entry)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
        return total

    bytes_before = regular_bytes_in_logs()
    names_before = {entry.name for entry in logs.iterdir()}

    with task_state_lock(tasks / "busy.json"):
        assert task_log_retention.main([]) == 0
        dry = json.loads(capsys.readouterr().out)
        assert dry["mode"] == "dry-run"
        assert dry["selected"] == 2
        assert dry["eligible_bytes"] == expected_reclaimed
        assert dry["logs"] == ["ok-one.stdout.log", "ok-two.stderr.log"]
        assert dry["retained"]["log_not_regular"] == 3
        assert dry["retained"]["retention_hold"] == 2
        assert dry["retained"]["retention_hold_invalid"] == 1
        assert dry["retained"]["task_liveness_unproven"] == 4
        assert dry["retained"]["task_not_settled"] == 1
        assert dry["retained"]["task_lock_busy"] == 1
        assert dry["retained"]["log_too_recent"] == 1
        assert regular_bytes_in_logs() == bytes_before
        assert {entry.name for entry in logs.iterdir()} == names_before

        assert task_log_retention.main(["--apply"]) == 0
        applied = json.loads(capsys.readouterr().out)
        assert applied == {**dry, "mode": "apply"}
        assert bytes_before - regular_bytes_in_logs() == expected_reclaimed
        assert {entry.name for entry in logs.iterdir()} == names_before - {
            "ok-one.stdout.log",
            "ok-two.stderr.log",
        }
        assert outside_target.read_bytes() == b"z" * 4096
        assert (logs / "sym-task.stdout.log").is_symlink()
        assert (logs / "dir-task.stderr.log").is_dir()
        assert stat.S_ISFIFO(os.lstat(logs / "fifo-task.stdout.log").st_mode)

        # Global batch_state hold on tasks/ protects otherwise eligible logs and reports 0 bytes.
        _write_record(tasks, "ok-three", old=old)
        _write_log(logs, "ok-three.stdout.log", 73, mtime=old)
        hold_list.write_text(
            json.dumps({
                "schema": "batch-state-retention.v1",
                "holds": [{"path": "batch_state/tasks/evidence", "reason": "slice-1 test"}],
            }),
            encoding="utf-8",
        )
        assert task_log_retention.main(["--apply", "--summary"]) == 0
        held_report = json.loads(capsys.readouterr().out)
        assert held_report["selected"] == 0
        assert held_report["eligible_bytes"] == 0
        assert held_report["retained"]["batch_state_hold"] >= 1
        assert (logs / "ok-three.stdout.log").read_bytes() == b"x" * 73


def test_toctou_changed_and_unlink_failure_excluded_from_eligible_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: TOCTOU log swap/rewrite or failed unlink credited to eligible_bytes."""
    tasks, _archive, logs, _hold_list, now, old = _setup_store(tmp_path, monkeypatch)
    _write_record(tasks, "keep-ok", old=old)
    _write_record(tasks, "raced-link", old=old)
    _write_record(tasks, "raced-rewrite", old=old)
    _write_record(tasks, "fail-unlink", old=old)

    _write_log(logs, "keep-ok.stdout.log", 29, mtime=old)
    _write_log(logs, "raced-link.stdout.log", 500, mtime=old)
    _write_log(logs, "raced-rewrite.stdout.log", 600, mtime=old)
    _write_log(logs, "fail-unlink.stdout.log", 700, mtime=old)

    outside = tmp_path / "outside.log"
    outside.write_bytes(b"p" * 500)
    os.utime(outside, (old.timestamp(), old.timestamp()))

    real_lock_task = task_log_retention._lock_task
    raced = False

    def racing_lock(parent_fd: int, task_id: str, *, apply: bool, stack):
        nonlocal raced
        result = real_lock_task(parent_fd, task_id, apply=apply, stack=stack)
        if not raced:
            raced = True
            link_path = logs / "raced-link.stdout.log"
            link_path.unlink()
            link_path.symlink_to(outside)
            rewrite_path = logs / "raced-rewrite.stdout.log"
            rewrite_path.write_bytes(b"n" * 600)
            os.utime(rewrite_path, (now.timestamp(), now.timestamp()))
        return result

    real_unlink = os.unlink

    def failing_unlink(path, *, dir_fd=None):
        if path == "fail-unlink.stdout.log":
            raise PermissionError("simulated unlink refusal")
        if dir_fd is None:
            return real_unlink(path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(task_log_retention, "_lock_task", racing_lock)
    monkeypatch.setattr(os, "unlink", failing_unlink)

    assert task_log_retention.main(["--apply"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "mode": "apply",
        "selected": 1,
        "eligible_bytes": 29,
        "logs": ["keep-ok.stdout.log"],
        "retained": {"log_changed": 2, "log_io_error": 1},
    }
    assert not (logs / "keep-ok.stdout.log").exists()
    assert (logs / "raced-link.stdout.log").is_symlink()
    assert outside.read_bytes() == b"p" * 500
    assert (logs / "raced-rewrite.stdout.log").read_bytes() == b"n" * 600
    assert (logs / "fail-unlink.stdout.log").read_bytes() == b"x" * 700


@pytest.mark.parametrize("bad_cutoff", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_cutoff_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_cutoff: float,
) -> None:
    """Defect class: non-finite cutoff failing open because mtime > NaN is False."""
    tasks, _archive, logs, _hold_list, now, old = _setup_store(tmp_path, monkeypatch)
    _write_record(tasks, "recent-task", old=now)
    _write_log(logs, "recent-task.stdout.log", 123, mtime=now)
    _write_record(tasks, "old-task", old=old)
    _write_log(logs, "old-task.stdout.log", 45, mtime=old)

    with pytest.raises(ValueError, match="finite"):
        task_log_retention.sweep(tasks, cutoff=bad_cutoff, apply=True)

    assert (logs / "recent-task.stdout.log").read_bytes() == b"x" * 123
    assert (logs / "old-task.stdout.log").read_bytes() == b"x" * 45


def test_missing_logs_dir_reports_zero_eligible_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: missing logs directory omitting eligible_bytes."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    monkeypatch.delenv("LU_HOST_CONTEXT", raising=False)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    hold_list = tmp_path / "holds.json"
    hold_list.write_text('{"schema":"batch-state-retention.v1","holds":[]}', encoding="utf-8")
    monkeypatch.setattr(task_log_retention, "RETENTION_LIST", hold_list)

    assert task_log_retention.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "mode": "dry-run",
        "selected": 0,
        "eligible_bytes": 0,
        "logs": [],
        "retained": {},
    }
