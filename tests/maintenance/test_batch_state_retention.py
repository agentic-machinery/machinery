"""Temp-dir proof for read-only snapshot digests and the batch_state sweep (#8783)."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lu_machinery.maintenance import batch_state_retention as retention
from lu_machinery.maintenance.batch_state_retention import DEFAULT_ONE_OFF_MIN_BYTES, plan_retention
from lu_machinery.maintenance.claude_session_scratch import sweep_sessions
from lu_machinery.orchestration import scheduled_worktree_cleanup, stale_task_records
from lu_machinery.orchestration.scheduled_worktree_cleanup import batch_state_retention_reports

from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots")

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def test_retention_loads_without_legacy_dispatcher() -> None:
    code = """
import importlib.abc
import sys
class RefuseLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'delegate', 'scripts.delegate', 'scripts.orchestration.stale_task_records'}:
            raise AssertionError(f'legacy dependency: {fullname}')
sys.meta_path.insert(0, RefuseLegacy())
from lu_machinery.maintenance.batch_state_retention import read_only_snapshot_digest
assert read_only_snapshot_digest(None, {})['pre']['entries'] == 0
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr


def test_snapshot_digest_preserves_canonical_bytes() -> None:
    assert retention.read_only_snapshot_digest(None, {}) == {
        "pre": {"sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", "entries": 0},
        "post": {"sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", "entries": 0},
    }
    assert retention.read_only_snapshot_digest({"a.txt": " M"}, {"b.txt": "??"}) == {
        "pre": {"sha256": "ea911a282a1c72b28c1c3cdcf15a1d55888caaaab924e194ec1a8f97775e1e8c", "entries": 1},
        "post": {"sha256": "681bcaef1742eae0239b147abe4bc18c6944df0f7c607bd6cdd7ec8c1ee3287d", "entries": 1},
    }


@pytest.mark.parametrize("record", [{"worktree_path": "checkout"}, {"cwd": "checkout"}, {}])
def test_retention_contends_on_the_shared_worktree_lock(tmp_path: Path, record: dict, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    record = {key: str(checkout) for key in record}
    target = retention._record_lock_target(record, tmp_path / "task.json")
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(retention.worktree_claims, "repository_lock_dir", lambda _root: lock_dir)
    with retention.worktree_claims.worktree_lock(target, lock_dir=lock_dir):
        with pytest.raises(retention.worktree_claims.WorktreeLockReentry):
            with retention.worktree_lock(target, timeout_s=0):
                pytest.fail("retention acquired a lock held by another record writer")


def test_retention_record_age_and_primary_checkout_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    path = tmp_path / "task.json"
    assert retention._record_lock_target({"cwd": str(tmp_path)}, path) == str(path)
    checkout = tmp_path / "fleet-checkout"
    relative = Path(".worktrees") / "dispatch" / "kept"
    (checkout / relative).mkdir(parents=True)
    decoy = tmp_path / "caller-cwd"
    (decoy / relative).mkdir(parents=True)
    monkeypatch.chdir(decoy)
    monkeypatch.setattr(
        stale_task_records, "_repository_checkout", lambda slug: checkout if slug == "owner/repo" else None
    )
    anchored = {"repository": "owner/repo", "worktree_path": relative.as_posix()}
    assert retention._record_lock_target(anchored, path) == str(checkout / relative)
    with pytest.raises(retention.WorktreeLockError):
        retention._record_lock_target({"repository": "owner/missing", "worktree_path": relative.as_posix()}, path)
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(
        tasks, "rel-unknown", status="done", age_days=10, repository="owner/missing", worktree_path=relative.as_posix()
    )
    snapshot = _write_full_sidecars(tasks, "rel-unknown", entries=1)
    _write_record(tasks, "listed", status="done", age_days=10)
    listed = tasks / "listed.json"
    listed.write_text(json.dumps({**json.loads(listed.read_text()), "status": ["done"]}), encoding="utf-8")
    _write_full_sidecars(tasks, "listed", entries=1)
    _write_record(
        tasks, "nul-path", status="done", age_days=10, repository="owner/repo",
        worktree_path=relative.as_posix() + "\0",
    )
    nul_snapshot = _write_full_sidecars(tasks, "nul-path", entries=1)
    digests = {"n": 0}
    original_digest = retention._digest_bytes

    def _counting_digest(*args, **kwargs):
        digests["n"] += 1
        return original_digest(*args, **kwargs)

    monkeypatch.setattr(retention, "_digest_bytes", _counting_digest)
    unknown_report = plan_retention(root, min_age_days=0, apply=True, now=NOW)
    by_task = {row["task_id"]: row for row in unknown_report["selected"]}
    assert set(by_task) == {"rel-unknown", "nul-path"}
    assert by_task["rel-unknown"]["skip_reason"] == "worktree path state unknown"
    assert by_task["nul-path"]["skip_reason"] == "worktree path state unknown"
    assert by_task["rel-unknown"]["reclaimable_bytes"] == 0
    assert digests["n"] == 0
    assert _names(snapshot) == {"read_only_checkout_pre.json", "read_only_checkout_post.json"}
    assert _names(nul_snapshot) == {"read_only_checkout_pre.json", "read_only_checkout_post.json"}
    assert _names(tasks / "listed.snapshots") == {"read_only_checkout_pre.json", "read_only_checkout_post.json"}
    _write_record(
        tasks, "rel-race", status="done", age_days=10, repository="owner/repo", worktree_path=relative.as_posix()
    )
    race_snapshot = _write_full_sidecars(tasks, "rel-race", entries=1)

    def lose_checkout() -> None:
        record_path = tasks / "rel-race.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["repository"] = "owner/missing"
        record_path.write_text(json.dumps(record), encoding="utf-8")

    race_report = plan_retention(root, min_age_days=0, apply=True, now=NOW, on_before_lock=lose_checkout)
    race_rows = {row["task_id"]: row for row in race_report["selected"]}
    assert race_rows["rel-race"]["action"] == "skipped"
    assert race_rows["rel-race"]["skip_reason"] == "worktree path state unknown"
    assert _names(race_snapshot) == {"read_only_checkout_pre.json", "read_only_checkout_post.json"}
    mtime = int((NOW - timedelta(days=3)).timestamp() * 1e9)
    for finished in (None, "bad", " "):
        assert retention._record_age_days({"finished_at": finished}, mtime, NOW) == 3
    for finished in ("2026-09-23T12:00:00Z", "2026-09-23T12:00:00"):
        assert retention._record_age_days({"finished_at": finished}, mtime, NOW) == 2


@pytest.fixture(autouse=True)
def _isolated_claude_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduled apply tests must never sweep host session scratch."""
    monkeypatch.setattr(
        scheduled_worktree_cleanup,
        "sweep_sessions",
        lambda **kwargs: sweep_sessions(tmp_path / f"claude-{os.getuid()}", **kwargs),
    )


def test_large_one_off_artifacts_report_size_age_without_deletion(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    nested = root / "one-off-output"
    nested.mkdir()
    files = [root / "manifest_example.json", nested / "output.bin"]
    for path in files:
        with path.open("wb") as destination:
            destination.truncate(DEFAULT_ONE_OFF_MIN_BYTES + 1)
        timestamp = (NOW - timedelta(days=12)).timestamp()
        os.utime(path, (timestamp, timestamp))
    (root / "small.json").write_bytes(b"small")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_bytes(b"untouched")
    (root / "linked-output").symlink_to(outside, target_is_directory=True)
    (root / "linked-manifest.json").symlink_to(files[0])
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
    for apply in (False, True):
        report = plan_retention(root, apply=apply, now=NOW)
        assert report["one_off_artifacts"] == [
            {
                "path": "manifest_example.json",
                "bytes": DEFAULT_ONE_OFF_MIN_BYTES + 1,
                "age_days": 12.0,
                "action": "report_only",
            },
            {
                "path": "one-off-output/output.bin",
                "bytes": DEFAULT_ONE_OFF_MIN_BYTES + 1,
                "age_days": 12.0,
                "action": "report_only",
            },
        ]
        assert {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in files} == before
        assert (root / "small.json").read_bytes() == b"small"
        public = scheduled_worktree_cleanup.build_public_summary({"batch_state_retention": [{"dry_run": report}]})
        assert public["batch_state_retention"][0]["one_off_artifacts"] == 2
        assert public["batch_state_retention"][0]["one_off_bytes"] == 2 * (DEFAULT_ONE_OFF_MIN_BYTES + 1)
        assert "manifest_example" not in json.dumps(public)


def _batch(tmp_path: Path) -> Path:
    root = tmp_path / "batch_state"
    (root / "tasks").mkdir(parents=True)
    return root


def _write_record(tasks: Path, name: str, *, status: str, age_days: float, **fields: object) -> None:
    record = {
        "task_id": name,
        "status": status,
        "finished_at": (NOW - timedelta(days=age_days)).isoformat(),
        "read_only_mutation_paths": [],
        "read_only_checkout_snapshot_error": None,
        **fields,
    }
    (tasks / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


def _write_full_sidecars(tasks: Path, name: str, *, entries: int = 40) -> Path:
    snapshot_dir = tasks / f"{name}.snapshots"
    snapshot_dir.mkdir()
    payload = json.dumps({f"path-{index}.txt": " M" for index in range(entries)}, separators=(",", ":"))
    for phase in ("pre", "post"):
        (snapshot_dir / f"read_only_checkout_{phase}.json").write_text(payload, encoding="utf-8")
    return snapshot_dir


def _names(snapshot_dir: Path) -> set[str]:
    return {path.name for path in snapshot_dir.iterdir()}


def test_dry_run_changes_nothing_and_apply_shrinks_only_allowlisted_terminal_sidecars(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "old-clean", status="done", age_days=10)
    clean = _write_full_sidecars(tasks, "old-clean")
    _write_record(
        tasks,
        "old-mutation",
        status="failed",
        age_days=10,
        read_only_mutation_paths=["tracked.txt"],
    )
    mutated = _write_full_sidecars(tasks, "old-mutation")
    _write_record(tasks, "still-running", status="running", age_days=30)
    running = _write_full_sidecars(tasks, "still-running")
    _write_record(tasks, "young-clean", status="done", age_days=1)
    young = _write_full_sidecars(tasks, "young-clean")
    other = root / "open-model-data"
    other.mkdir()
    (other / "blob.bin").write_bytes(b"x" * 128)
    stray = root / "stray.snapshots"
    stray.mkdir()
    (stray / "read_only_checkout_pre.json").write_text("{}", encoding="utf-8")

    before = {
        "clean": _names(clean),
        "mutated": _names(mutated),
        "running": _names(running),
        "young": _names(young),
        "other": (other / "blob.bin").read_bytes(),
        "stray": _names(stray),
    }
    dry = plan_retention(root, min_age_days=7, apply=False, now=NOW)

    assert dry["mode"] == "dry-run"
    assert _names(clean) == before["clean"]
    assert _names(mutated) == before["mutated"]
    assert (other / "blob.bin").read_bytes() == before["other"]
    by_name = {row["name"]: row for row in dry["subtrees"]}
    assert by_name["tasks"]["reclaimable_bytes"] > 0
    assert by_name["open-model-data"]["bytes"] == 128
    assert by_name["open-model-data"]["reclaimable_bytes"] == 0
    assert by_name["stray.snapshots"]["reclaimable_bytes"] == 0
    assert dry["totals"]["reclaimable_bytes"] == by_name["tasks"]["reclaimable_bytes"]
    assert [row["snapshot_dir"] for row in dry["selected"]] == ["tasks/old-clean.snapshots"]

    applied = plan_retention(root, min_age_days=7, apply=True, now=NOW)

    assert applied["selected"][0]["action"] == "digested"
    assert _names(clean) == {"digest.json"}
    digest = json.loads((clean / "digest.json").read_text(encoding="utf-8"))
    assert digest["pre"]["entries"] == 40
    assert len(digest["pre"]["sha256"]) == 64
    assert _names(mutated) == before["mutated"]
    assert _names(running) == before["running"]
    assert _names(young) == before["young"]
    assert _names(stray) == before["stray"]
    assert (other / "blob.bin").read_bytes() == before["other"]
    record = json.loads((tasks / "old-clean.json").read_text(encoding="utf-8"))
    assert record["read_only_snapshot_retention"] == "digest"
    assert "read_only_checkout_pre" not in record


def test_sweep_refuses_a_directory_that_is_not_batch_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_state"):
        plan_retention(tmp_path, apply=True, now=NOW)


def test_archive_restore_of_a_digest_only_task(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    _write_record(tasks, "digested", status="done", age_days=20, read_only_snapshot_retention="digest")
    snapshot_dir = tasks / "digested.snapshots"
    snapshot_dir.mkdir()
    digest = {"pre": {"sha256": "ab" * 32, "entries": 3}, "post": {"sha256": "cd" * 32, "entries": 3}}
    (snapshot_dir / "digest.json").write_text(json.dumps(digest), encoding="utf-8")

    archived = stale_task_records.archive_terminal(tasks, min_age_days=14, apply=True, now=NOW)
    assert archived["records"][0]["action"] == "archived"
    assert not snapshot_dir.exists()
    stored = tasks / "archive" / "digested.snapshots" / "digest.json"
    assert json.loads(stored.read_text(encoding="utf-8")) == digest

    restored = stale_task_records.restore_archived(tasks, ["digested"], apply=True)
    assert restored["records"][0]["action"] == "restored"
    assert json.loads((snapshot_dir / "digest.json").read_text(encoding="utf-8")) == digest
    assert not (snapshot_dir / "read_only_checkout_pre.json").exists()


def test_missing_clean_verdict_keys_are_kept(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    finished = (NOW - timedelta(days=10)).isoformat()
    for name, extra in (
        ("no-keys", {}),
        ("no-error-key", {"read_only_mutation_paths": []}),
        ("no-mutation-key", {"read_only_checkout_snapshot_error": None}),
    ):
        (tasks / f"{name}.json").write_text(
            json.dumps({"task_id": name, "status": "done", "finished_at": finished, **extra}),
            encoding="utf-8",
        )
        _write_full_sidecars(tasks, name)

    report = plan_retention(root, min_age_days=0, apply=True, now=NOW)

    assert report["selected"] == []
    for name in ("no-keys", "no-error-key", "no-mutation-key"):
        assert _names(tasks / f"{name}.snapshots") == {
            "read_only_checkout_pre.json",
            "read_only_checkout_post.json",
        }
        record = json.loads((tasks / f"{name}.json").read_text(encoding="utf-8"))
        assert "read_only_snapshot_retention" not in record


def test_status_flip_between_check_and_act_leaves_sidecars_untouched(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "reused", status="done", age_days=10, run_nonce="old-run")
    snapshot = _write_full_sidecars(tasks, "reused", entries=2)
    record_path = tasks / "reused.json"
    fresh = json.dumps({"fresh.txt": "??"}, separators=(",", ":"))

    def _redispatch() -> None:
        # Still a released, explicitly clean record, so age and verdict checks
        # would allow the rewrite. Only the status/nonce recheck must refuse.
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["status"] = "failed"
        record["run_nonce"] = "new-run"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        (snapshot / "read_only_checkout_pre.json").write_text(fresh, encoding="utf-8")

    report = plan_retention(root, min_age_days=0, apply=True, now=NOW, on_before_lock=_redispatch)

    assert report["selected"] == []
    assert (snapshot / "read_only_checkout_pre.json").read_text(encoding="utf-8") == fresh
    assert (snapshot / "read_only_checkout_post.json").is_file()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["run_nonce"] == "new-run"
    assert "read_only_snapshot_retention" not in record


def test_symlink_phase_file_is_refused(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "linked", status="done", age_days=10, run_nonce="same")
    snapshot = _write_full_sidecars(tasks, "linked", entries=1)
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret.txt": " M"}', encoding="utf-8")
    phase = snapshot / "read_only_checkout_pre.json"
    phase.unlink()
    phase.symlink_to(outside)
    outside_record = tmp_path / "outside-record.json"
    outside_record.write_text(
        json.dumps(
            {
                "task_id": "record-link",
                "status": "done",
                "finished_at": (NOW - timedelta(days=10)).isoformat(),
                "read_only_mutation_paths": [],
                "read_only_checkout_snapshot_error": None,
            }
        ),
        encoding="utf-8",
    )
    (tasks / "record-link.json").symlink_to(outside_record)
    _write_full_sidecars(tasks, "record-link", entries=1)

    report = plan_retention(root, min_age_days=0, apply=True, now=NOW)

    assert report["selected"] == []
    assert outside.read_text(encoding="utf-8") == '{"secret.txt": " M"}'
    assert phase.is_symlink()
    assert (snapshot / "read_only_checkout_post.json").is_file()
    assert "read_only_snapshot_retention" not in json.loads(outside_record.read_text(encoding="utf-8"))
    assert _names(tasks / "record-link.snapshots") == {
        "read_only_checkout_pre.json",
        "read_only_checkout_post.json",
    }


def _crash_case(tmp_path: Path, name: str) -> tuple[Path, Path]:
    root = tmp_path / name
    root.mkdir()
    batch = _batch(root)
    tasks = batch / "tasks"
    _write_record(tasks, "task", status="done", age_days=3, run_nonce="same")
    return batch, _write_full_sidecars(tasks, "task", entries=2)


def test_crash_between_digest_record_and_delete_stays_consistent(tmp_path: Path) -> None:
    digest_root, digest_dir = _crash_case(tmp_path, "after-digest")
    with pytest.raises(RuntimeError, match="crash after digest"):
        plan_retention(digest_root, min_age_days=0, apply=True, now=NOW, crash_after="digest")

    assert (digest_dir / "digest.json").is_file()
    assert (digest_dir / "read_only_checkout_pre.json").is_file()
    assert (digest_dir / "read_only_checkout_post.json").is_file()
    digest_record = json.loads((digest_dir.parent / "task.json").read_text(encoding="utf-8"))
    assert "read_only_snapshot_retention" not in digest_record

    record_root, record_dir = _crash_case(tmp_path, "after-record")
    with pytest.raises(RuntimeError, match="crash after record"):
        plan_retention(record_root, min_age_days=0, apply=True, now=NOW, crash_after="record")

    stored = json.loads((record_dir / "digest.json").read_text(encoding="utf-8"))
    assert stored["pre"]["entries"] == 2
    assert (record_dir / "read_only_checkout_pre.json").is_file()
    assert (record_dir / "read_only_checkout_post.json").is_file()
    record = json.loads((record_dir.parent / "task.json").read_text(encoding="utf-8"))
    assert record["read_only_snapshot_retention"] == "digest"


def test_hygiene_cadence_dry_run_then_apply_keeps_the_allowlist(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    finished = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    (tasks / "old-clean.json").write_text(
        json.dumps(
            {
                "task_id": "old-clean",
                "status": "done",
                "finished_at": finished,
                "run_nonce": "same",
                "read_only_mutation_paths": [],
                "read_only_checkout_snapshot_error": None,
            }
        ),
        encoding="utf-8",
    )
    snapshot = _write_full_sidecars(tasks, "old-clean", entries=1)
    other = root / "open-model-data"
    other.mkdir()
    (other / "blob.bin").write_bytes(b"keep")

    dry = batch_state_retention_reports([tmp_path], apply=False)
    assert dry[0]["dry_run"]["mode"] == "dry-run"
    assert "apply" not in dry[0]
    assert _names(snapshot) == {"read_only_checkout_pre.json", "read_only_checkout_post.json"}
    assert dry[0]["dry_run"]["allowlist"] == [
        "tasks/*.snapshots",
        "tasks/archive/*.snapshots",
        "tasks/*.artifacts",
    ]

    applied = batch_state_retention_reports([tmp_path], apply=True)
    assert applied[0]["apply"]["selected"][0]["action"] == "digested"
    assert _names(snapshot) == {"digest.json"}
    assert (other / "blob.bin").read_bytes() == b"keep"


def test_archive_between_digest_and_record_does_not_recreate_the_hot_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "moving", status="done", age_days=20, run_nonce="same")
    snapshot = _write_full_sidecars(tasks, "moving", entries=2)
    record_path = tasks / "moving.json"

    # Relax retention's checkout lock to force the interleaving. Archive
    # still acquires and verifies the actual lock before moving the record.
    monkeypatch.setattr(retention, "worktree_lock", lambda *_args, **_kwargs: contextlib.nullcontext())

    def _archive_midway() -> None:
        stale_task_records.archive_terminal(tasks, min_age_days=0, apply=True, now=NOW)

    report = plan_retention(root, min_age_days=0, apply=True, now=NOW, on_after_digest=_archive_midway)

    assert not record_path.exists()
    assert report["selected"] == []
    assert not snapshot.exists()
    archived = json.loads((tasks / "archive" / "moving.json").read_text(encoding="utf-8"))
    assert "read_only_snapshot_retention" not in archived
    assert (tasks / "archive" / "moving.snapshots").is_dir()


def test_parent_swap_to_symlink_between_check_and_open_touches_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "swap", status="done", age_days=10, run_nonce="same")
    _write_full_sidecars(tasks, "swap", entries=1)
    outside = tmp_path / "outside"
    outside_snap = outside / "swap.snapshots"
    outside_snap.mkdir(parents=True)
    payload = json.dumps({"stolen.txt": " M"}, separators=(",", ":"))
    for phase in ("pre", "post"):
        (outside_snap / f"read_only_checkout_{phase}.json").write_text(payload, encoding="utf-8")
    (outside_snap / "digest.json").write_text("OUTSIDE-DIGEST", encoding="utf-8")
    (outside / "swap.json").write_text((tasks / "swap.json").read_text(encoding="utf-8"), encoding="utf-8")
    (outside / "untouched.txt").write_bytes(b"leave-me")
    before = {path.relative_to(outside).as_posix(): path.read_bytes() for path in outside.rglob("*") if path.is_file()}
    swapped = False
    real_open = os.open

    def _swap() -> None:
        saved = tmp_path / "tasks.real"
        os.rename(tasks, saved)
        tasks.symlink_to(outside, target_is_directory=True)

    def _wrapped_open(path, flags, mode=0o600, *, dir_fd=None):
        nonlocal swapped
        target = Path(path)
        if dir_fd is None and not swapped and target != root and root in target.parents:
            swapped = True
            _swap()
        fd = real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is None and not swapped and target == root:
            swapped = True
            _swap()
        return fd

    monkeypatch.setattr(os, "open", _wrapped_open)

    report = plan_retention(root, min_age_days=0, apply=True, now=NOW)

    assert report["selected"] == []
    after = {path.relative_to(outside).as_posix(): path.read_bytes() for path in outside.rglob("*") if path.is_file()}
    assert after == before


def test_help_is_two_lines_and_has_no_host_path() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "lu_machinery.maintenance.batch_state_retention", "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    text = proc.stdout
    collapsed = " ".join(text.split())
    assert "/home/" not in text
    assert "forensic value after a day" in collapsed
    assert "replace explicitly clean terminal snapshot sidecars with a digest." in collapsed
    assert "do not use it to delete any other batch_state subtree." in collapsed
    assert ".venv/bin/python lu_machinery/maintenance/batch_state_retention.py" in text
    assert "artifacts_reclaimable_bytes" in text
    assert "tasks/<id>.artifacts.tar.xz" in text
    assert "never deleted or replaced" in collapsed


def test_restore_after_digest_does_not_recreate_the_archived_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    archive = tasks / "archive"
    archive.mkdir()
    _write_record(archive, "moving", status="done", age_days=20, run_nonce="same")
    _write_full_sidecars(archive, "moving", entries=2)
    locked: list[Path] = []

    @contextlib.contextmanager
    def _record_lock(path: Path):
        locked.append(path)
        yield

    monkeypatch.setattr(stale_task_records, "task_state_lock", _record_lock)

    def _restore_midway() -> None:
        stale_task_records.restore_archived(tasks, ["moving"], apply=True)

    report = plan_retention(root, min_age_days=0, apply=True, now=NOW, on_after_digest=_restore_midway)

    assert locked == [archive / "moving.json"]
    assert not (archive / "moving.json").exists()
    assert report["selected"] == []
    restored = json.loads((tasks / "moving.json").read_text(encoding="utf-8"))
    assert "read_only_snapshot_retention" not in restored


def test_stale_pid_temp_does_not_block_or_get_deleted(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "old-clean", status="done", age_days=3, run_nonce="same")
    snapshot = _write_full_sidecars(tasks, "old-clean", entries=1)
    stale = snapshot / f".digest.json.tmp.{os.getpid()}"
    stale.write_text("killed-run", encoding="utf-8")

    report = plan_retention(root, min_age_days=0, apply=True, now=NOW)

    assert report["selected"][0]["action"] == "digested"
    assert (snapshot / "digest.json").is_file()
    assert stale.read_text(encoding="utf-8") == "killed-run"
    assert not list(snapshot.glob(".digest.json.*.tmp"))


def _hygiene_receipt(root: Path, receipt_dir: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(
        scheduled_worktree_cleanup,
        "build_receipt",
        lambda *_args, **_kwargs: {
            "schema_version": 2,
            "observed_at": "2026-09-25T12:00:00+00:00",
            "mode": "apply",
            "summary": {"errors": 0},
            "repositories": [],
        },
    )
    monkeypatch.setattr(scheduled_worktree_cleanup.home_session_retention_check, "build_report", lambda: {})
    monkeypatch.setattr(scheduled_worktree_cleanup.home_session_retention_check, "warning_lines", lambda _report: [])
    assert (
        scheduled_worktree_cleanup.main(["--repo-root", str(root.parent), "--apply", "--receipt-dir", str(receipt_dir)])
        == 0
    )
    receipts = list(receipt_dir.glob("*.json"))
    assert len(receipts) == 1
    return json.loads(receipts[0].read_text(encoding="utf-8"))


@pytest.mark.parametrize("code", [errno.ELOOP, errno.EACCES, errno.EEXIST, errno.ENOSPC])
def test_one_record_oserror_does_not_abort_the_sweep_or_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "bad", status="done", age_days=3, run_nonce="same")
    bad = _write_full_sidecars(tasks, "bad", entries=1)
    _write_record(tasks, "good", status="done", age_days=3, run_nonce="same")
    good = _write_full_sidecars(tasks, "good", entries=1)
    real_open = os.open

    def _open(path, flags, mode=0o600, *, dir_fd=None):
        if path == "bad.json.lock":
            raise OSError(code, os.strerror(code))
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", _open)
    receipt = _hygiene_receipt(root, tmp_path / "receipts", monkeypatch)

    applied = receipt["batch_state_retention"][0]["apply"]["selected"]
    by_dir = {row["snapshot_dir"]: row for row in applied}
    assert by_dir["tasks/bad.snapshots"]["action"] == "error"
    assert by_dir["tasks/bad.snapshots"]["error"] == errno.errorcode[code]
    assert by_dir["tasks/good.snapshots"]["action"] == "digested"
    assert _names(bad) == {"read_only_checkout_pre.json", "read_only_checkout_post.json"}
    assert _names(good) == {"digest.json"}
    public = scheduled_worktree_cleanup.build_public_summary(receipt)
    assert public["batch_state_retention"][0]["errors"] == {errno.errorcode[code]: 1}
    dumped = json.dumps(public["batch_state_retention"])
    assert "bad.snapshots" not in dumped
    assert "good.json" not in dumped


def test_public_summary_counts_apply_errors_by_errno_and_ignores_the_dry_run() -> None:
    receipt = {
        "batch_state_retention": [
            {
                "dry_run": {
                    "mode": "dry-run",
                    "allowlist": ["tasks/*.snapshots"],
                    "totals": {"reclaimable_bytes": 10},
                    "selected": [
                        {"action": "would_digest", "snapshot_dir": "tasks/a.snapshots"},
                        {"action": "error", "error": "EACCES", "snapshot_dir": "tasks/ignored.snapshots"},
                    ],
                },
                "apply": {
                    "mode": "apply",
                    "selected": [
                        {"action": "digested", "snapshot_dir": "tasks/ok.snapshots"},
                        {"action": "error", "error": "EACCES", "snapshot_dir": "tasks/a.snapshots"},
                        {"action": "error", "error": "ENOSPC", "snapshot_dir": "tasks/b.snapshots"},
                        {"action": "error", "error": "EACCES", "snapshot_dir": "tasks/c.snapshots"},
                    ],
                },
            }
        ]
    }
    public = scheduled_worktree_cleanup.build_public_summary(receipt)
    assert public["batch_state_retention"] == [
        {
            "mode": "apply",
            "reclaimable_bytes": 10,
            "selected": 2,
            "allowlist": ["tasks/*.snapshots"],
            "errors": {"EACCES": 2, "ENOSPC": 1},
        }
    ]
    dumped = json.dumps(public["batch_state_retention"])
    assert "ignored.snapshots" not in dumped
    assert "a.snapshots" not in dumped


def test_sweep_reaps_its_own_stale_temps_and_reports_them(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    archive = tasks / "archive"
    archive.mkdir()
    _write_record(tasks, "old-clean", status="done", age_days=3, run_nonce="same")
    snapshot = _write_full_sidecars(tasks, "old-clean", entries=1)

    def _plant(directory: Path, name: str, pid: int, *, age_s: float) -> Path:
        path = directory / f".{name}.{pid}.{'ab' * 8}.tmp"
        path.write_text("killed", encoding="utf-8")
        stamp = (NOW - timedelta(seconds=age_s)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    stale_record = _plant(tasks, "old-clean.json", 4242, age_s=3700)
    stale_digest = _plant(snapshot, "digest.json", 4243, age_s=7200)
    stale_archive = _plant(archive, "moved.json", 4244, age_s=3700)
    young = _plant(snapshot, "digest.json", 7, age_s=600)
    young.write_text("live", encoding="utf-8")
    os.utime(young, ((NOW - timedelta(seconds=600)).timestamp(),) * 2)
    foreign = snapshot / f".digest.json.tmp.{os.getpid()}"
    foreign.write_text("other-writer", encoding="utf-8")
    os.utime(foreign, ((NOW - timedelta(hours=5)).timestamp(),) * 2)
    outside = tmp_path / "outside-secret"
    outside.write_text("secret", encoding="utf-8")
    linked = snapshot / f".digest.json.9.{'ef' * 8}.tmp"
    linked.symlink_to(outside)
    os.utime(linked, ((NOW - timedelta(hours=5)).timestamp(),) * 2, follow_symlinks=False)

    expected = [
        {"dir": "tasks", "name": stale_record.name},
        {"dir": "tasks/old-clean.snapshots", "name": stale_digest.name},
        {"dir": "tasks/archive", "name": stale_archive.name},
    ]
    dry = plan_retention(root, min_age_days=0, apply=False, now=NOW)

    assert stale_record.read_text(encoding="utf-8") == "killed"
    assert stale_digest.read_text(encoding="utf-8") == "killed"
    assert stale_archive.read_text(encoding="utf-8") == "killed"
    assert young.read_text(encoding="utf-8") == "live"
    assert foreign.read_text(encoding="utf-8") == "other-writer"
    assert linked.is_symlink()
    assert outside.read_text(encoding="utf-8") == "secret"
    assert dry["temps_would_remove"] == expected
    assert dry["temps_removed"] == []
    assert _names(snapshot) >= {"read_only_checkout_pre.json", "read_only_checkout_post.json"}

    applied = plan_retention(root, min_age_days=0, apply=True, now=NOW)

    assert not stale_record.exists()
    assert not stale_digest.exists()
    assert not stale_archive.exists()
    assert young.read_text(encoding="utf-8") == "live"
    assert foreign.read_text(encoding="utf-8") == "other-writer"
    assert linked.is_symlink()
    assert outside.read_text(encoding="utf-8") == "secret"
    assert applied["temps_removed"] == expected
    assert applied["temps_would_remove"] == []
    assert (snapshot / "digest.json").is_file()
    assert "read_only_checkout_pre.json" not in _names(snapshot)
    assert "read_only_checkout_post.json" not in _names(snapshot)


def _write_artifact_tree(tasks: Path, name: str) -> Path:
    directory = tasks / f"{name}.artifacts"
    nested = directory / "nested"
    nested.mkdir(parents=True)
    (directory / "note.txt").write_bytes(b"hello")
    (nested / "blob.bin").write_bytes(b"xyz" * 10)
    (directory / "empty").write_bytes(b"")
    return directory


def _artifact_members(archive: Path) -> dict[str, int]:
    with tarfile.open(archive, "r:xz") as packed:
        return {member.name: member.size for member in packed.getmembers() if member.isreg()}


def test_settled_artifact_dir_is_archived_and_recorded(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "old-done", status="done", age_days=10, run_nonce="same")
    record_path = tasks / "old-done.json"
    record_path.chmod(0o640)
    directory = _write_artifact_tree(tasks, "old-done")
    _write_full_sidecars(tasks, "old-done", entries=2)
    shared = tmp_path / "shared.bin"
    shared.write_bytes(b"shared-bytes")
    os.link(shared, directory / "shared.bin")
    keep = tasks / "keep.artifacts.tar.xz"
    keep.write_bytes(b"keep-this-archive")
    archived = tasks / "archive"
    archived.mkdir()
    archived_payload = archived / "old.artifacts"
    archived_payload.mkdir()
    (archived_payload / "keep.txt").write_bytes(b"archived-copy")
    (tasks / "notes.txt").write_bytes(b"leave-me")
    loose = {
        "note.txt": 5,
        "nested/blob.bin": 30,
        "empty": 0,
        "shared.bin": len(b"shared-bytes"),
    }

    report = plan_retention(root, min_age_days=1, apply=True, now=NOW)

    rows = [row for row in report["selected"] if row.get("artifacts_dir")]
    assert rows == [
        {
            "artifacts_dir": "tasks/old-done.artifacts",
            "task_id": "old-done",
            "status": "done",
            "reclaimable_bytes": sum(loose.values()),
            "action": "archived",
        }
    ]
    assert report["artifacts_reclaimable_bytes"] == sum(loose.values())
    archive = tasks / "old-done.artifacts.tar.xz"
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert _artifact_members(archive) == loose
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    record = json.loads((tasks / "old-done.json").read_text(encoding="utf-8"))
    assert record["artifacts_retention"] == "archived"
    assert record["artifacts_archive_sha256"] == digest
    assert record["status"] == "done"
    assert record["run_nonce"] == "same"
    assert record["read_only_snapshot_retention"] == "digest"
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o640
    assert _names(tasks / "old-done.snapshots") == {"digest.json"}
    assert not directory.exists()
    assert keep.read_bytes() == b"keep-this-archive"
    assert (archived_payload / "keep.txt").read_bytes() == b"archived-copy"
    assert (tasks / "notes.txt").read_bytes() == b"leave-me"
    assert shared.read_bytes() == b"shared-bytes"

    again = plan_retention(root, min_age_days=1, apply=True, now=NOW)
    assert [row for row in again["selected"] if row.get("artifacts_dir")] == []
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest

    _write_record(tasks, "resume", status="rate_limited", age_days=8)
    resume_dir = _write_artifact_tree(tasks, "resume")
    resume_payload = io.BytesIO()
    with tarfile.open(fileobj=resume_payload, mode="w:xz") as packed:
        for name, data in (("empty", b""), ("nested/blob.bin", b"xyz" * 10), ("note.txt", b"hello")):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            packed.addfile(member, io.BytesIO(data))
    resume_archive = tasks / "resume.artifacts.tar.xz"
    resume_archive.write_bytes(resume_payload.getvalue())
    resume_before = resume_archive.read_bytes()
    resumed = plan_retention(root, min_age_days=1, apply=True, now=NOW)
    assert [row for row in resumed["selected"] if row.get("artifacts_dir")] == [
        {
            "artifacts_dir": "tasks/resume.artifacts",
            "task_id": "resume",
            "status": "rate_limited",
            "reclaimable_bytes": 35,
            "action": "archived",
        }
    ]
    assert not resume_dir.exists()
    assert resume_archive.read_bytes() == resume_before
    resumed_record = json.loads((tasks / "resume.json").read_text(encoding="utf-8"))
    assert resumed_record["artifacts_retention"] == "archived"
    assert resumed_record["artifacts_archive_sha256"] == hashlib.sha256(resume_before).hexdigest()
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest


def test_live_or_nonterminal_artifact_dir_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "still-running", status="running", age_days=30)
    running = _write_artifact_tree(tasks, "still-running")
    _write_record(tasks, "live-done", status="done", age_days=10, pid=os.getpid())
    live = _write_artifact_tree(tasks, "live-done")
    _write_record(tasks, "unknown-pid", status="failed", age_days=10, pid=424242)
    unknown = _write_artifact_tree(tasks, "unknown-pid")
    _write_record(tasks, "locked", status="cancelled", age_days=10)
    locked = _write_artifact_tree(tasks, "locked")
    _write_record(tasks, "linked", status="no_deliverable", age_days=10)
    linked = _write_artifact_tree(tasks, "linked")
    outside = tmp_path / "secret.bin"
    outside.write_bytes(b"do-not-follow")
    (linked / "leak").symlink_to(outside)
    lock_fd = os.open(tasks / "locked.json.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    monkeypatch.setattr(retention, "worker_liveness", lambda record: True if record.get("task_id") == "live-done" else None)

    try:
        report = plan_retention(root, min_age_days=0, apply=True, now=NOW)
    finally:
        os.close(lock_fd)

    by_dir = {row["artifacts_dir"]: row for row in report["selected"] if row.get("artifacts_dir")}
    assert "tasks/still-running.artifacts" not in by_dir
    assert by_dir["tasks/live-done.artifacts"]["action"] == "skipped"
    assert by_dir["tasks/live-done.artifacts"]["skip_reason"] == "live process"
    assert by_dir["tasks/unknown-pid.artifacts"]["skip_reason"] == "process liveness unknown"
    assert by_dir["tasks/locked.artifacts"]["skip_reason"] == "lock held"
    assert by_dir["tasks/linked.artifacts"]["skip_reason"] == "unsupported entry"
    assert report["artifacts_reclaimable_bytes"] == 0
    for directory in (running, live, unknown, locked, linked):
        assert directory.is_dir()
        assert (directory / "note.txt").read_bytes() == b"hello"
    assert outside.read_bytes() == b"do-not-follow"
    assert (linked / "leak").is_symlink()
    assert not list(tasks.glob("*.artifacts.tar.xz"))
    for name in ("still-running", "live-done", "unknown-pid", "locked", "linked"):
        record = json.loads((tasks / f"{name}.json").read_text(encoding="utf-8"))
        assert "artifacts_retention" not in record


def test_young_artifact_dir_is_skipped(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "young-done", status="done", age_days=0.5)
    young = _write_artifact_tree(tasks, "young-done")
    mtime_record = tasks / "mtime-young.json"
    mtime_record.write_text(
        json.dumps({"task_id": "mtime-young", "status": "timeout", "finished_at": "bad"}),
        encoding="utf-8",
    )
    os.utime(mtime_record, (NOW.timestamp(), NOW.timestamp()))
    mtime_dir = _write_artifact_tree(tasks, "mtime-young")

    report = plan_retention(root, min_age_days=1, apply=True, now=NOW)

    assert report["artifacts_reclaimable_bytes"] == 0
    assert [row for row in report["selected"] if row.get("artifacts_dir")] == []
    assert young.is_dir() and mtime_dir.is_dir()
    assert not list(tasks.glob("*.tar.xz"))
    assert "artifacts_retention" not in json.loads((tasks / "young-done.json").read_text(encoding="utf-8"))


def test_archive_verification_failure_leaves_the_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "mismatch", status="done", age_days=10)
    mismatch = _write_artifact_tree(tasks, "mismatch")
    archive = tasks / "mismatch.artifacts.tar.xz"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:xz") as packed:
        member = tarfile.TarInfo("note.txt")
        member.size = 3
        packed.addfile(member, io.BytesIO(b"hel"))
    archive.write_bytes(payload.getvalue())
    before = archive.read_bytes()
    outside = tmp_path / "outside-archive"
    outside.write_bytes(b"not-the-archive")
    alias = tasks / "alias.artifacts.tar.xz"
    _write_record(tasks, "alias", status="done", age_days=10)
    alias_dir = _write_artifact_tree(tasks, "alias")
    alias.symlink_to(outside)

    refused = plan_retention(root, min_age_days=0, apply=True, now=NOW)

    by_dir = {row["artifacts_dir"]: row for row in refused["selected"]}
    assert by_dir["tasks/mismatch.artifacts"]["action"] == "error"
    assert by_dir["tasks/mismatch.artifacts"]["error"] == "archive verification failed"
    assert by_dir["tasks/alias.artifacts"]["error"] == "archive exists"
    assert (mismatch / "note.txt").read_bytes() == b"hello"
    assert (alias_dir / "note.txt").read_bytes() == b"hello"
    assert archive.read_bytes() == before
    assert alias.is_symlink()
    assert outside.read_bytes() == b"not-the-archive"
    assert "artifacts_retention" not in json.loads((tasks / "mismatch.json").read_text(encoding="utf-8"))
    assert "artifacts_retention" not in json.loads((tasks / "alias.json").read_text(encoding="utf-8"))

    _write_record(tasks, "fresh", status="crashed", age_days=4)
    fresh = _write_artifact_tree(tasks, "fresh")
    monkeypatch.setattr(retention, "_verify_archive_at", lambda *_args, **_kwargs: False)
    failed = plan_retention(root, min_age_days=0, apply=True, now=NOW)

    fresh_row = next(row for row in failed["selected"] if row.get("artifacts_dir") == "tasks/fresh.artifacts")
    assert fresh_row["action"] == "error"
    assert fresh_row["error"] == "archive verification failed"
    assert (fresh / "note.txt").read_bytes() == b"hello"
    assert not (tasks / "fresh.artifacts.tar.xz").exists()
    assert list(tasks.glob(".*.tmp")) == []
    assert "artifacts_retention" not in json.loads((tasks / "fresh.json").read_text(encoding="utf-8"))
    assert archive.read_bytes() == before
    assert alias.is_symlink()
    assert outside.read_bytes() == b"not-the-archive"


def test_artifact_dry_run_changes_nothing(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "old-done", status="done", age_days=3)
    directory = _write_artifact_tree(tasks, "old-done")
    record_path = tasks / "old-done.json"
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    mtimes = {path: path.stat().st_mtime_ns for path in (directory / "note.txt", record_path)}

    report = plan_retention(root, min_age_days=1, apply=False, now=NOW)

    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert report["mode"] == "dry-run"
    assert report["artifacts_reclaimable_bytes"] == 5 + 30 + 0
    assert report["totals"]["reclaimable_bytes"] == 0
    assert report["selected"] == [
        {
            "artifacts_dir": "tasks/old-done.artifacts",
            "task_id": "old-done",
            "status": "done",
            "reclaimable_bytes": 35,
            "action": "would_archive",
        }
    ]
    assert after == before
    assert {path: path.stat().st_mtime_ns for path in mtimes} == mtimes
    assert not (tasks / "old-done.artifacts.tar.xz").exists()
    assert directory.is_dir()


def test_rewrite_preserves_the_record_mode(tmp_path: Path) -> None:
    root = _batch(tmp_path)
    tasks = root / "tasks"
    _write_record(tasks, "old-clean", status="done", age_days=3, run_nonce="same")
    record = tasks / "old-clean.json"
    record.chmod(0o640)
    snapshot = _write_full_sidecars(tasks, "old-clean", entries=1)

    report = plan_retention(root, min_age_days=0, apply=True, now=NOW)

    assert report["selected"][0]["action"] == "digested"
    assert stat.S_IMODE(record.stat().st_mode) == 0o640
    assert stat.S_IMODE((snapshot / "digest.json").stat().st_mode) == 0o600
