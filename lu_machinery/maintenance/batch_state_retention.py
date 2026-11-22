#!/usr/bin/env python3
"""Shrink clean snapshot sidecars and settled task artifact directories.

Use this to turn old terminal ``tasks/*.snapshots/`` directories that recorded an
explicit clean checkout into a ``digest.json``, and to pack a settled
``tasks/<id>.artifacts`` directory into ``tasks/<id>.artifacts.tar.xz``. Do not
use it to delete manifests, atlas output, open-model data, an existing artifact
archive, or any other batch_state subtree: those are reported and left
untouched. Dry-run is the default; pass ``--apply`` to write.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tarfile
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The package directory, from the unresolved file location. A literal scripts/
# join is the machinery package name (design-v1 amendment 1). It and its parent go
# on sys.path only for file-path invocation; neither is a state or product root.
_PACKAGE_DIR = Path(os.path.abspath(__file__)).parents[1]
for _path in (_PACKAGE_DIR.parent, _PACKAGE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import state_root
from lu_machinery.orchestration import worktree_claims
from lu_machinery.orchestration.dead_worker_state import worker_liveness
from lu_machinery.orchestration.task_record_store import ARCHIVE_DIR_NAME
from lu_machinery.orchestration.worktree_claims import (
    RELEASED_TASK_STATUSES,
    WorktreeLockError,
    WorktreeLockReentry,
    WorktreeLockTimeout,
)


def _mp(relative: str) -> str:
    return f"{Path(os.path.abspath(__file__)).parents[1].name}/{relative}"


# A clean verdict already lives on the task record, so the full JSON has no
# forensic value after a day. Seven days reclaimed nothing: the dirs are younger.
DEFAULT_MIN_AGE_DAYS = 1.0
DEFAULT_ONE_OFF_MIN_BYTES = 100 * 1024 * 1024
DEFAULT_LOCK_TIMEOUT_S = 5.0
_READ_ONLY_CHECKOUT_SNAPSHOT_SUFFIX = ".snapshots"
_READ_ONLY_SNAPSHOT_DIGEST_NAME = "digest.json"
_READ_ONLY_SNAPSHOT_RETENTION_DIGEST = "digest"
_SNAPSHOT_SUFFIX = _READ_ONLY_CHECKOUT_SNAPSHOT_SUFFIX
# Only these directory globs may be rewritten. Everything else under batch_state
# is measured and left alone.
_ALLOWLISTED_SNAPSHOT_PARENTS = ("", ARCHIVE_DIR_NAME)
# Settled task outputs. The archive is the sibling ``<id>.artifacts.tar.xz``;
# this sweep never deletes that sibling, and it never rewrites ``tasks/archive``.
_ARTIFACTS_SUFFIX = ".artifacts"
_ARTIFACTS_ARCHIVE_TAIL = ".artifacts.tar.xz"
_ARTIFACTS_RETENTION_ARCHIVED = "archived"
_ARTIFACTS_SHA_FIELD = "artifacts_archive_sha256"
_PHASES = ("pre", "post")
# ``path_safety`` does not open by directory fd. Every name below the
# batch_state fd is a single path component; ``..`` and symlinks never become
# a path string passed to open/stat/replace/unlink.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
# ``_temp_name`` is ``.{name}.{pid}.{16 hex}.tmp``. Only that shape is ours.
_OWN_TEMP_RE = re.compile(r"^\..+\.[0-9]+\.[0-9a-f]{16}\.tmp\Z")
_OWN_TEMP_MAX_AGE_S = 60 * 60


def _canonical_snapshot_bytes(snapshot: dict[str, str] | None) -> bytes:
    payload = snapshot if isinstance(snapshot, dict) else {}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _snapshot_digest_phase(snapshot: dict[str, str] | None) -> dict[str, Any]:
    raw = _canonical_snapshot_bytes(snapshot)
    payload = snapshot if isinstance(snapshot, dict) else {}
    return {"sha256": hashlib.sha256(raw).hexdigest(), "entries": len(payload)}


def read_only_snapshot_digest(
    pre: dict[str, str] | None,
    post: dict[str, str] | None,
) -> dict[str, Any]:
    """Content digest of the two canonical checkout snapshots."""
    return {"pre": _snapshot_digest_phase(pre), "post": _snapshot_digest_phase(post)}


def worktree_lock(path: Path | str, timeout_s: float | None = None) -> contextlib.AbstractContextManager[None]:
    """Use the shared repository lock also held by dispatch and record movers."""
    return worktree_claims.worktree_lock(
        path, lock_dir=worktree_claims.repository_lock_dir(state_root()), timeout_s=timeout_s,
    )


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _record_age_days(record: dict[str, Any], mtime_ns: int, now: datetime) -> float:
    finished = _parse_ts(record.get("finished_at"))
    if finished is None:
        finished = datetime.fromtimestamp(mtime_ns / 1e9, UTC)
    return (now - finished).total_seconds() / 86400.0


def _record_lock_target(record: Mapping[str, Any], record_path: Path) -> str:
    """The checkout lock archive uses, so a relative recorded path is not read from the cwd.

    An unattributed relative path raises :class:`WorktreeLockError`. Callers
    report that record unknown and leave it in place.
    """
    from lu_machinery.orchestration.stale_task_records import _record_lock_target as anchored_lock_target

    return anchored_lock_target(record, record_path)


def _component(name: str) -> bool:
    return name not in {"", ".", ".."} and "/" not in name and "\x00" not in name


def _open_root(batch_state: Path) -> int:
    """Open the real ``batch_state`` directory. A symlink or any other name is refused."""
    if batch_state.name != "batch_state":
        raise ValueError(f"refusing to sweep {batch_state}: path must be a directory named batch_state")
    try:
        return os.open(batch_state, _DIR_FLAGS)
    except OSError as exc:
        raise ValueError(f"refusing to sweep {batch_state}: path must be a directory named batch_state") from exc


def _open_dir(dir_fd: int, name: str) -> int | None:
    if not _component(name):
        return None
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None


def _lstat_at(dir_fd: int, name: str) -> os.stat_result | None:
    if not _component(name):
        return None
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None


def _is_regular(dir_fd: int, name: str) -> bool:
    info = _lstat_at(dir_fd, name)
    return info is not None and stat.S_ISREG(info.st_mode)


def _read_regular_at(dir_fd: int, name: str) -> bytes | None:
    """Read one regular file in ``dir_fd``. A symlink or a swap of ``name`` is not followed."""
    if not _component(name):
        return None
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _temp_name(name: str) -> str:
    """A private name inside one directory fd. A leftover ``.{name}.tmp.{pid}`` does not match it."""
    return f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"


def _original_mode(dir_fd: int, name: str) -> int | None:
    """Permission bits of the regular file ``name``, from the inode ``open`` returned."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode):
        return None
    return stat.S_IMODE(info.st_mode)


def _write_bytes_at(dir_fd: int, name: str, raw: bytes) -> None:
    """Create or replace ``name`` inside ``dir_fd`` without a path below the root fd.

    The temp name is unique, so a stale ``.{name}.tmp.{pid}`` left by a killed
    run cannot make ``O_EXCL`` fail. On any failure after the temp is created,
    that temp is unlinked; a name this call did not create is left alone.
    Replacing a regular file keeps that file's mode. A new name is owner-only.
    """
    if not _component(name):
        raise OSError(errno.EINVAL, f"refusing to write {name}: symlink or path outside batch_state")
    preserved = _original_mode(dir_fd, name)
    tmp = _temp_name(name)
    if not _component(tmp):
        raise OSError(errno.EINVAL, f"refusing to write {tmp}")
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        preserved if preserved is not None else 0o600,
        dir_fd=dir_fd,
    )
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view) :]
        if preserved is not None:
            os.fchmod(fd, preserved)
    except OSError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    os.close(fd)
    try:
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _reap_own_temps(dir_fd: int, rel_dir: str, *, now: datetime, apply: bool) -> list[dict[str, str]]:
    """Report this sweep's temp files in ``dir_fd`` once they are older than an hour.

    Unlink them only when ``apply`` is true. Dry-run reports the same names and
    leaves every inode in place. The name must match :func:`_temp_name` exactly.
    The open uses ``O_NOFOLLOW`` on ``dir_fd``, so a symlink of that name is
    left in place. A younger file may belong to a live writer and is left alone.
    """
    cutoff = now.timestamp() - _OWN_TEMP_MAX_AGE_S
    matched: list[dict[str, str]] = []
    for name in _names(dir_fd):
        if _OWN_TEMP_RE.fullmatch(name) is None:
            continue
        try:
            fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
        except OSError:
            continue
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
            continue
        if apply:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                continue
        matched.append({"dir": rel_dir, "name": name})
    return matched


def _collect_own_temps(
    temps_removed: list[dict[str, str]],
    temps_would_remove: list[dict[str, str]],
    dir_fd: int,
    rel_dir: str,
    *,
    now: datetime,
    apply: bool,
) -> None:
    """Record stale own-temps. Only the apply pass unlinks them."""
    found = _reap_own_temps(dir_fd, rel_dir, now=now, apply=apply)
    if apply:
        temps_removed.extend(found)
    else:
        temps_would_remove.extend(found)


def _unlink_regular_at(dir_fd: int, name: str) -> None:
    if not _is_regular(dir_fd, name):
        return
    with contextlib.suppress(OSError):
        os.unlink(name, dir_fd=dir_fd)


def _file_bytes_at(dir_fd: int, name: str) -> int:
    info = _lstat_at(dir_fd, name)
    if info is None or not stat.S_ISREG(info.st_mode):
        return 0
    return info.st_size


def _tree_bytes_at(dir_fd: int) -> int:
    try:
        names = os.listdir(dir_fd)
    except OSError:
        return 0
    total = 0
    for name in names:
        info = _lstat_at(dir_fd, name)
        if info is None or stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            child = _open_dir(dir_fd, name)
            if child is None:
                continue
            try:
                total += _tree_bytes_at(child)
            finally:
                os.close(child)
    return total


def _names(dir_fd: int) -> list[str]:
    try:
        return sorted(os.listdir(dir_fd))
    except OSError:
        return []


def _one_off_files_at(dir_fd: int, *, now: datetime, prefix: str = "") -> list[dict[str, Any]]:
    """List large files for owner disposition, without following symlinks or deleting."""
    rows: list[dict[str, Any]] = []
    for name in _names(dir_fd):
        if not prefix and name == "tasks":
            # Managed task sidecars are retention inputs, not one-off outputs.
            continue
        info = _lstat_at(dir_fd, name)
        if info is None:
            continue
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISREG(info.st_mode) and info.st_size >= DEFAULT_ONE_OFF_MIN_BYTES:
            rows.append(
                {
                    "path": relative,
                    "bytes": info.st_size,
                    "age_days": max(0.0, (now.timestamp() - info.st_mtime) / 86400),
                    "action": "report_only",
                }
            )
        elif stat.S_ISDIR(info.st_mode):
            child = _open_dir(dir_fd, name)
            if child is not None:
                try:
                    rows.extend(_one_off_files_at(child, now=now, prefix=relative))
                finally:
                    os.close(child)
    return rows


@contextlib.contextmanager
def _task_record_lock(parent_fd: int, record_name: str) -> Iterator[None]:
    """Lock ``<record>.lock`` in ``parent_fd`` — the same file :func:`task_state_lock` uses.

    ``Path.with_suffix(suffix + ".lock")`` on ``foo.json`` is ``foo.json.lock``.
    Opening it through the directory fd keeps a parent-directory swap from
    redirecting the lock.
    """
    fd = os.open(
        record_name + ".lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _hot_pair(parent_fd: int, record_name: str, snapshot_name: str, snap_fd: int) -> bool:
    """True when the record and snapshot dir are still the hot names we locked.

    Archive renames both out of this directory. A leftover fd must not be
    treated as the hot path, and a replaced directory inode must not either.
    """
    record = _lstat_at(parent_fd, record_name)
    current = _lstat_at(parent_fd, snapshot_name)
    if record is None or current is None:
        return False
    if not stat.S_ISREG(record.st_mode) or not stat.S_ISDIR(current.st_mode):
        return False
    held = os.fstat(snap_fd)
    return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)


def _phase_snapshots(snap_fd: int) -> tuple[dict[str, str], dict[str, str]] | None:
    """Return parsed phase maps when both files are regular JSON objects in ``snap_fd``."""
    loaded: list[dict[str, str]] = []
    for phase in _PHASES:
        raw = _read_regular_at(snap_fd, f"read_only_checkout_{phase}.json")
        if raw is None:
            return None
        try:
            payload = json_depth.loads(raw, max_depth=512)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        loaded.append(payload)
    return loaded[0], loaded[1]


def _explicit_clean(record: dict[str, Any]) -> bool:
    """True only when the record itself says the checkout was clean.

    A missing key is not a clean verdict: older records predate the field and
    must keep their full sidecars.
    """
    if "read_only_mutation_paths" not in record or "read_only_checkout_snapshot_error" not in record:
        return False
    return record["read_only_mutation_paths"] == [] and record["read_only_checkout_snapshot_error"] is None


def _eligible(
    record: dict[str, Any] | None,
    info: os.stat_result | None,
    *,
    min_age_days: float,
    now: datetime,
) -> bool:
    if record is None or info is None or not _explicit_clean(record):
        return False
    status = record.get("status")
    try:
        released = status in RELEASED_TASK_STATUSES
    except TypeError:
        return False
    if not released:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    return _record_age_days(record, info.st_mtime_ns, now) >= min_age_days


def _read_record_at(parent_fd: int, record_name: str) -> dict[str, Any] | None:
    raw = _read_regular_at(parent_fd, record_name)
    if raw is None:
        return None
    try:
        loaded = json_depth.loads(raw, max_depth=512)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _record_name_for(snapshot_name: str) -> str | None:
    if not snapshot_name.endswith(_SNAPSHOT_SUFFIX):
        return None
    stem = snapshot_name[: -len(_SNAPSHOT_SUFFIX)]
    if not stem or not _component(stem):
        return None
    return f"{stem}.json"


def _digest_bytes(pre: dict[str, str], post: dict[str, str]) -> bytes:
    return json.dumps(read_only_snapshot_digest(pre, post), separators=(",", ":")).encode("utf-8")


def _apply_one(
    parent_fd: int,
    snapshot_name: str,
    record_name: str,
    logical_record: Path,
    observed: dict[str, Any],
    *,
    observed_status: Any,
    observed_nonce: Any,
    min_age_days: float,
    now: datetime,
    crash_after: str | None,
    on_after_digest: Callable[[], None] | None,
) -> dict[str, str] | None:
    """Digest one sidecar. Return the action row, or None to skip.

    Lock order is the checkout lock, then the per-task record lock
    (``<record>.json.lock``, the file :func:`task_state_lock` uses). Every
    mover of a task record:

    * this sweep: checkout lock, then the per-task lock, for the whole rewrite
    * dispatch (``_write_state_atomic``): the checkout lock, then the per-task lock
    * ``delegate._archive_task_artifacts`` (``--force-new``): the per-task lock
      around the rename, released before dispatch takes the checkout lock
    * ``stale_task_records._restore_group``: the per-task lock on the archived
      record around that record's move
    * ``stale_task_records.archive_terminal``: the checkout lock
      (:func:`_record_lock_target`) while it renames the record and its sidecar

    Taking the per-task lock and then waiting on the checkout lock deadlocks
    a holder that already has the checkout lock and is waiting for the per-task
    lock. ``_archive_task_artifacts`` does not do that: it drops the per-task
    lock before dispatch acquires the checkout lock. Under both locks the
    record and the snapshot directory must still be the hot names; if a mover
    has already moved them, this returns without writing a hot record back.
    One record's ``OSError`` is that record's error; it does not abort the sweep.
    """
    try:
        held_target = _record_lock_target(observed, logical_record)
    except WorktreeLockError:
        return {"action": "skipped", "skip_reason": "worktree path state unknown"}
    try:
        with (
            worktree_lock(held_target, timeout_s=DEFAULT_LOCK_TIMEOUT_S),
            _task_record_lock(parent_fd, record_name),
        ):
            if not _is_regular(parent_fd, record_name):
                return None
            snap_fd = _open_dir(parent_fd, snapshot_name)
            if snap_fd is None or not _hot_pair(parent_fd, record_name, snapshot_name, snap_fd):
                if snap_fd is not None:
                    os.close(snap_fd)
                return None
            try:
                current = _read_record_at(parent_fd, record_name)
                info = _lstat_at(parent_fd, record_name)
                if current is None:
                    return None
                try:
                    current_target = _record_lock_target(current, logical_record)
                except WorktreeLockError:
                    return {"action": "skipped", "skip_reason": "worktree path state unknown"}
                if current_target != held_target:
                    return {"action": "skipped", "skip_reason": "worktree path state unknown"}
                if current.get("status") != observed_status or current.get("run_nonce") != observed_nonce:
                    return None
                if not _eligible(current, info, min_age_days=min_age_days, now=now):
                    return None
                phases = _phase_snapshots(snap_fd)
                if phases is None:
                    return None
                pre, post = phases
                _write_bytes_at(snap_fd, _READ_ONLY_SNAPSHOT_DIGEST_NAME, _digest_bytes(pre, post))
                if crash_after == "digest":
                    raise RuntimeError("crash after digest")
                if on_after_digest is not None:
                    on_after_digest()
                if not _hot_pair(parent_fd, record_name, snapshot_name, snap_fd):
                    return None
                current["read_only_snapshot_retention"] = _READ_ONLY_SNAPSHOT_RETENTION_DIGEST
                _write_bytes_at(
                    parent_fd,
                    record_name,
                    json.dumps(current, indent=2, default=str).encode("utf-8"),
                )
                if crash_after == "record":
                    raise RuntimeError("crash after record")
                for phase in _PHASES:
                    _unlink_regular_at(snap_fd, f"read_only_checkout_{phase}.json")
                return {"action": "digested"}
            finally:
                os.close(snap_fd)
    except WorktreeLockError:
        return None
    except OSError as exc:
        return {"action": "error", "error": errno.errorcode.get(exc.errno or 0, "OSError")}


def _measure_candidate(
    parent_fd: int,
    rel_dir: str,
    snapshot_name: str,
    *,
    root: Path,
    min_age_days: float,
    now: datetime,
    apply: bool,
    on_before_lock: Callable[[], None] | None,
    on_after_digest: Callable[[], None] | None,
    crash_after: str | None,
) -> tuple[dict[str, Any], int] | None:
    record_name = _record_name_for(snapshot_name)
    if record_name is None:
        return None
    info = _lstat_at(parent_fd, record_name)
    record = _read_record_at(parent_fd, record_name)
    if not _eligible(record, info, min_age_days=min_age_days, now=now) or record is None:
        return None
    logical_record = Path(os.path.abspath(os.path.join(os.fspath(root), rel_dir, record_name)))
    try:
        _record_lock_target(record, logical_record)
    except WorktreeLockError:
        return {
            "snapshot_dir": f"{rel_dir}/{snapshot_name}",
            "task_id": record.get("task_id") if record else record_name[: -len(".json")],
            "status": record.get("status") if record else None,
            "reclaimable_bytes": 0,
            "action": "skipped",
            "skip_reason": "worktree path state unknown",
        }, 0
    snap_fd = _open_dir(parent_fd, snapshot_name)
    if snap_fd is None:
        return None
    try:
        phases = _phase_snapshots(snap_fd)
        if phases is None:
            return None
        pre, post = phases
        digest_raw = _digest_bytes(pre, post)
        before = sum(_file_bytes_at(snap_fd, f"read_only_checkout_{phase}.json") for phase in _PHASES)
    finally:
        os.close(snap_fd)
    reclaimed = before - len(digest_raw)
    row = {
        "snapshot_dir": f"{rel_dir}/{snapshot_name}",
        "task_id": record.get("task_id") if record else record_name[: -len(".json")],
        "status": record.get("status") if record else None,
        "reclaimable_bytes": reclaimed,
        "action": "would_digest",
    }
    if apply:
        if on_before_lock is not None:
            on_before_lock()
        outcome = _apply_one(
            parent_fd,
            snapshot_name,
            record_name,
            Path(os.path.abspath(os.path.join(os.fspath(root), rel_dir, record_name))),
            record,
            observed_status=record.get("status"),
            observed_nonce=record.get("run_nonce"),
            min_age_days=min_age_days,
            now=now,
            crash_after=crash_after,
            on_after_digest=on_after_digest,
        )
        if outcome is None:
            return None
        row["action"] = outcome["action"]
        if outcome.get("skip_reason"):
            row["skip_reason"] = outcome["skip_reason"]
            row["reclaimable_bytes"] = 0
            return row, 0
        if outcome["action"] == "error":
            row["error"] = outcome["error"]
            return row, 0
    return row, reclaimed


def _artifact_stem(directory_name: str) -> str | None:
    """Task id for ``<id>.artifacts``, or None when the name is not one component."""
    if not directory_name.endswith(_ARTIFACTS_SUFFIX):
        return None
    stem = directory_name[: -len(_ARTIFACTS_SUFFIX)]
    if not stem or not _component(stem) or any(ord(char) < 32 or ord(char) == 127 for char in stem):
        return None
    return stem


def _recorded_pid(record: Mapping[str, Any]) -> bool:
    raw = record.get("pid")
    if isinstance(raw, bool) or raw is None:
        return False
    if isinstance(raw, int):
        return raw > 0
    if isinstance(raw, str):
        try:
            return int(raw) > 0
        except ValueError:
            return False
    return False


def _liveness_skip(record: Mapping[str, Any]) -> str | None:
    """Skip a live process, and a recorded pid whose liveness cannot be shown.

    A record with no pid has no process to skip. Unknown liveness with a pid
    is not permission to remove the directory.
    """
    try:
        live = worker_liveness(dict(record))
    except (OSError, ValueError, TypeError):
        return "process liveness unknown"
    if live is True:
        return "live process"
    if live is None and _recorded_pid(record):
        return "process liveness unknown"
    return None


def _artifact_eligible(
    record: dict[str, Any] | None,
    info: os.stat_result | None,
    *,
    min_age_days: float,
    now: datetime,
) -> bool:
    """True when the record exists, is terminal, and is old enough.

    Artifact directories do not need the snapshot clean-checkout verdict.
    ``finished_at`` sets the age; a missing or unparseable value uses the
    record file's mtime.
    """
    if record is None or info is None or not stat.S_ISREG(info.st_mode):
        return False
    try:
        released = record.get("status") in RELEASED_TASK_STATUSES
    except TypeError:
        return False
    if not released:
        return False
    return _record_age_days(record, info.st_mtime_ns, now) >= min_age_days


def _inventory_artifacts(dir_fd: int, *, root_dev: int, prefix: str = "") -> dict[str, int] | None:
    """Map regular-file paths to sizes. None when the tree is not safe to pack.

    Symlinks, special files, another filesystem, and names that are not one
    path component make the whole directory ineligible. Nothing is followed.
    """
    found: dict[str, int] = {}
    for name in _names(dir_fd):
        if not _component(name) or any(ord(char) < 32 or ord(char) == 127 for char in name):
            return None
        info = _lstat_at(dir_fd, name)
        if info is None or info.st_dev != root_dev:
            return None
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISREG(info.st_mode):
            found[relative] = info.st_size
        elif stat.S_ISDIR(info.st_mode):
            child = _open_dir(dir_fd, name)
            if child is None:
                return None
            try:
                nested = _inventory_artifacts(child, root_dev=root_dev, prefix=relative)
            finally:
                os.close(child)
            if nested is None:
                return None
            found.update(nested)
        else:
            return None
    return found


def _open_relative(root_fd: int, relative: str, flags: int) -> int | None:
    """Open ``relative`` under ``root_fd`` without following a symlink."""
    parts = relative.split("/")
    if not parts or any(not _component(part) for part in parts):
        return None
    current = root_fd
    opened: list[int] = []
    try:
        for index, part in enumerate(parts):
            step = flags if index == len(parts) - 1 else _DIR_FLAGS
            current = os.open(part, step, dir_fd=current)
            opened.append(current)
        return opened.pop()
    except OSError:
        return None
    finally:
        for descriptor in opened:
            os.close(descriptor)


def _members_match(expected: Mapping[str, int], archive: tarfile.TarFile) -> bool:
    """True when the archive's regular files are exactly ``expected`` paths and sizes."""
    found: dict[str, int] = {}
    for member in archive.getmembers():
        if member.isdir():
            continue
        name = member.name
        parts = name.split("/")
        if (
            not member.isreg()
            or member.issym()
            or member.islnk()
            or member.isdev()
            or not parts
            or any(not _component(part) for part in parts)
            or name in found
            or member.size < 0
        ):
            return False
        found[name] = member.size
    return found == dict(expected)


def _verify_archive_at(dir_fd: int, name: str, expected: Mapping[str, int]) -> bool:
    """True when ``name`` is a regular xz archive of exactly ``expected``."""
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as raw:
            try:
                with tarfile.open(fileobj=raw, mode="r:xz") as archive:
                    return _members_match(expected, archive)
            except (tarfile.TarError, OSError, EOFError):
                return False
    finally:
        os.close(descriptor)


def _sha256_at(dir_fd: int, name: str) -> str | None:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _add_archived_file(archive: tarfile.TarFile, root_fd: int, relative: str, size: int) -> None:
    descriptor = _open_relative(root_fd, relative, _READ_FLAGS)
    if descriptor is None:
        raise OSError(errno.EINVAL, "refusing to archive an entry that is not a regular file")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise OSError(errno.EINVAL, "artifact entry changed while packing")
        member = tarfile.TarInfo(name=relative)
        member.size = info.st_size
        member.mode = 0o600
        member.mtime = int(info.st_mtime)
        member.uid = 0
        member.gid = 0
        member.uname = ""
        member.gname = ""
        member.type = tarfile.REGTYPE
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
            archive.addfile(member, handle)
    finally:
        os.close(descriptor)


def _write_xz_archive(parent_fd: int, tmp_name: str, root_fd: int, files: Mapping[str, int]) -> None:
    """Create ``tmp_name`` mode 0600. On failure the temp name is removed; a finished archive is not."""
    descriptor = os.open(
        tmp_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+b", closefd=False) as raw:
            with tarfile.open(fileobj=raw, mode="w:xz", format=tarfile.PAX_FORMAT) as archive:
                for relative in sorted(files):
                    _add_archived_file(archive, root_fd, relative, files[relative])
            raw.flush()
            os.fsync(raw.fileno())
    except (OSError, tarfile.TarError):
        os.close(descriptor)
        _unlink_regular_at(parent_fd, tmp_name)
        raise
    os.close(descriptor)


def _unlink_relative_file(root_fd: int, relative: str, size: int) -> bool:
    parts = relative.split("/")
    if not parts or any(not _component(part) for part in parts):
        return False
    parent_relative = "/".join(parts[:-1])
    name = parts[-1]
    parent = root_fd
    opened: list[int] = []
    try:
        if parent_relative:
            parent_fd = _open_relative(root_fd, parent_relative, _DIR_FLAGS)
            if parent_fd is None:
                return False
            opened.append(parent_fd)
            parent = parent_fd
        info = _lstat_at(parent, name)
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return False
        os.unlink(name, dir_fd=parent)
        return True
    except OSError:
        return False
    finally:
        for descriptor in opened:
            os.close(descriptor)


def _rmdir_relative(root_fd: int, relative: str) -> bool:
    parts = relative.split("/")
    if not parts or any(not _component(part) for part in parts):
        return False
    parent_relative = "/".join(parts[:-1])
    name = parts[-1]
    parent = root_fd
    opened: list[int] = []
    child: int | None = None
    try:
        if parent_relative:
            parent_fd = _open_relative(root_fd, parent_relative, _DIR_FLAGS)
            if parent_fd is None:
                return False
            opened.append(parent_fd)
            parent = parent_fd
        child = _open_dir(parent, name)
        if child is None or _names(child):
            return False
        os.rmdir(name, dir_fd=parent)
        return True
    except OSError:
        return False
    finally:
        if child is not None:
            os.close(child)
        for descriptor in opened:
            os.close(descriptor)


def _remove_archived_directory(parent_fd: int, name: str, expected: Mapping[str, int]) -> bool:
    """Remove ``name`` only while a fresh inventory still equals ``expected``.

    Unlink inventoried regular files, then their empty parents. A new name, a
    symlink, or a size change leaves the directory in place. This never unlinks
    the sibling archive.
    """
    dir_fd = _open_dir(parent_fd, name)
    if dir_fd is None:
        return False
    try:
        info = os.fstat(dir_fd)
        current = _inventory_artifacts(dir_fd, root_dev=info.st_dev)
        if current != dict(expected):
            return False
        for relative, size in sorted(expected.items(), key=lambda item: (item[0].count("/"), item[0]), reverse=True):
            if not _unlink_relative_file(dir_fd, relative, size):
                return False
        prefixes: set[str] = set()
        for relative in expected:
            parts = relative.split("/")
            for index in range(1, len(parts)):
                prefixes.add("/".join(parts[:index]))
        for prefix in sorted(prefixes, key=lambda item: (item.count("/"), item), reverse=True):
            if not _rmdir_relative(dir_fd, prefix):
                return False
        if _names(dir_fd):
            return False
    finally:
        os.close(dir_fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        return False
    return True


def _dry_run_lock_skip(
    parent_fd: int,
    record_name: str,
    record: Mapping[str, Any],
    logical_record: Path,
) -> str | None:
    """Report a held lock without creating one. None means no held lock was observed."""
    try:
        target = _record_lock_target(record, logical_record)
    except WorktreeLockError:
        return "worktree path state unknown"
    try:
        refusal = worktree_claims.existing_worktree_lock_refusal(
            Path(target),
            lock_dir=worktree_claims.repository_lock_dir(state_root()),
        )
    except WorktreeLockError:
        return "worktree path state unknown"
    if refusal is not None:
        return "lock held"
    try:
        descriptor = os.open(record_name + ".lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return "lock held"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return "lock held"
    finally:
        os.close(descriptor)
    return None


@contextlib.contextmanager
def _artifact_mutation_locks(parent_fd: int, record_name: str, held_target: str) -> Iterator[str | None]:
    """Yield None while both locks are held, otherwise a skip reason.

    The record lock is non-blocking. Creating it is an apply-time side effect,
    the same permanent ``<record>.json.lock`` the snapshot rewrite uses.
    """
    try:
        locker = worktree_lock(held_target, timeout_s=0)
        locker.__enter__()
    except (WorktreeLockTimeout, WorktreeLockReentry):
        yield "lock held"
        return
    except WorktreeLockError:
        yield "worktree path state unknown"
        return
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                record_name + ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent_fd,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield "lock held"
            return
        except OSError as exc:
            yield errno.errorcode.get(exc.errno or 0, "OSError")
            return
        yield None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        locker.__exit__(None, None, None)


def _artifact_row(
    rel_dir: str,
    directory_name: str,
    record: Mapping[str, Any],
    record_name: str,
    *,
    action: str,
    reclaimable: int,
    **extra: Any,
) -> dict[str, Any]:
    task_id = record.get("task_id")
    row = {
        "artifacts_dir": f"{rel_dir}/{directory_name}",
        "task_id": task_id if isinstance(task_id, str) and task_id else record_name[: -len(".json")],
        "status": record.get("status"),
        "reclaimable_bytes": reclaimable,
        "action": action,
    }
    row.update(extra)
    return row


def _record_archive(parent_fd: int, record_name: str, record: dict[str, Any], digest: str) -> None:
    """Set the retention field and archive sha, preserving the record's mode."""
    if record.get("artifacts_retention") == _ARTIFACTS_RETENTION_ARCHIVED and record.get(_ARTIFACTS_SHA_FIELD) == digest:
        return
    record["artifacts_retention"] = _ARTIFACTS_RETENTION_ARCHIVED
    record[_ARTIFACTS_SHA_FIELD] = digest
    _write_bytes_at(parent_fd, record_name, json.dumps(record, indent=2, default=str).encode("utf-8"))


def _commit_verified_archive(
    parent_fd: int,
    directory_name: str,
    record_name: str,
    record: dict[str, Any],
    files: Mapping[str, int],
    *,
    archive_name: str,
) -> dict[str, Any]:
    """Record a verified archive, then remove the loose directory. Never unlinks the archive."""
    digest = _sha256_at(parent_fd, archive_name)
    if digest is None or not _verify_archive_at(parent_fd, archive_name, files):
        return {"action": "error", "error": "archive verification failed"}
    dir_fd = _open_dir(parent_fd, directory_name)
    if dir_fd is None:
        return {"action": "error", "error": "archive verification failed"}
    try:
        info = os.fstat(dir_fd)
        current = _inventory_artifacts(dir_fd, root_dev=info.st_dev)
    finally:
        os.close(dir_fd)
    if current != dict(files):
        return {"action": "error", "error": "archive verification failed"}
    _record_archive(parent_fd, record_name, record, digest)
    if not _remove_archived_directory(parent_fd, directory_name, files):
        return {"action": "error", "error": "directory changed", "reclaimable_bytes": 0}
    return {"action": "archived", "reclaimable_bytes": sum(files.values())}


def _publish_new_archive(
    parent_fd: int,
    root_fd: int,
    files: Mapping[str, int],
    *,
    archive_name: str,
) -> str | None:
    """Link a verified temp archive to ``archive_name``. Return an error, or None on success.

    Only the temp name is removed. ``archive_name`` is never unlinked, including
    when it already exists or the new archive fails verification.
    """
    tmp_name = _temp_name(archive_name)
    try:
        _write_xz_archive(parent_fd, tmp_name, root_fd, files)
        dir_info = os.fstat(root_fd)
        current = _inventory_artifacts(root_fd, root_dev=dir_info.st_dev)
        if current != dict(files) or not _verify_archive_at(parent_fd, tmp_name, files):
            return "archive verification failed"
        try:
            os.link(tmp_name, archive_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        except FileExistsError:
            return "archive verification failed"
        os.fsync(parent_fd)
    except (OSError, tarfile.TarError) as exc:
        return errno.errorcode.get(getattr(exc, "errno", None) or 0, "OSError")
    finally:
        if _is_regular(parent_fd, tmp_name):
            _unlink_regular_at(parent_fd, tmp_name)
    return None


def _apply_one_artifact(
    parent_fd: int,
    directory_name: str,
    record_name: str,
    logical_record: Path,
    observed: dict[str, Any],
    *,
    observed_status: Any,
    observed_nonce: Any,
    min_age_days: float,
    now: datetime,
    identity: tuple[int, int],
) -> dict[str, Any] | None:
    """Pack one artifact directory. None means it disappeared or was redispatched."""
    try:
        held_target = _record_lock_target(observed, logical_record)
    except WorktreeLockError:
        return {"action": "skipped", "skip_reason": "worktree path state unknown"}
    try:
        with _artifact_mutation_locks(parent_fd, record_name, held_target) as skip:
            if skip is not None:
                reason = skip if skip in {"lock held", "worktree path state unknown"} else None
                if reason is None:
                    return {"action": "error", "error": skip}
                return {"action": "skipped", "skip_reason": reason}
            if not _is_regular(parent_fd, record_name):
                return None
            dir_fd = _open_dir(parent_fd, directory_name)
            if dir_fd is None:
                return None
            try:
                current_info = _lstat_at(parent_fd, directory_name)
                if current_info is None or (current_info.st_dev, current_info.st_ino) != identity:
                    return None
                if os.fstat(dir_fd).st_ino != identity[1] or os.fstat(dir_fd).st_dev != identity[0]:
                    return None
                current = _read_record_at(parent_fd, record_name)
                record_info = _lstat_at(parent_fd, record_name)
                if current is None or not _artifact_eligible(current, record_info, min_age_days=min_age_days, now=now):
                    return None
                try:
                    current_target = _record_lock_target(current, logical_record)
                except WorktreeLockError:
                    return {"action": "skipped", "skip_reason": "worktree path state unknown"}
                if current_target != held_target:
                    return {"action": "skipped", "skip_reason": "worktree path state unknown"}
                if current.get("status") != observed_status or current.get("run_nonce") != observed_nonce:
                    return None
                liveness = _liveness_skip(current)
                if liveness is not None:
                    return {"action": "skipped", "skip_reason": liveness}
                root_info = os.fstat(dir_fd)
                files = _inventory_artifacts(dir_fd, root_dev=root_info.st_dev)
                if files is None:
                    return {"action": "skipped", "skip_reason": "unsupported entry"}
                archive_name = f"{directory_name}.tar.xz"
                existing = _lstat_at(parent_fd, archive_name)
                if existing is not None and not stat.S_ISREG(existing.st_mode):
                    return {"action": "error", "error": "archive exists"}
                if existing is not None:
                    if not _verify_archive_at(parent_fd, archive_name, files):
                        return {"action": "error", "error": "archive verification failed"}
                else:
                    problem = _publish_new_archive(parent_fd, dir_fd, files, archive_name=archive_name)
                    if problem is not None:
                        return {"action": "error", "error": problem}
                return _commit_verified_archive(
                    parent_fd,
                    directory_name,
                    record_name,
                    current,
                    files,
                    archive_name=archive_name,
                )
            finally:
                os.close(dir_fd)
    except WorktreeLockError:
        return {"action": "skipped", "skip_reason": "lock held"}
    except OSError as exc:
        return {"action": "error", "error": errno.errorcode.get(exc.errno or 0, "OSError")}


def _measure_artifact_dirs(
    parent_fd: int,
    rel_dir: str,
    *,
    root: Path,
    min_age_days: float,
    now: datetime,
    apply: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Plan or pack ``tasks/*.artifacts``. Returns rows and reclaimable loose bytes."""
    rows: list[dict[str, Any]] = []
    reclaimable = 0
    for directory_name in _names(parent_fd):
        stem = _artifact_stem(directory_name)
        info = _lstat_at(parent_fd, directory_name)
        if stem is None or info is None or not stat.S_ISDIR(info.st_mode):
            continue
        record_name = f"{stem}.json"
        record = _read_record_at(parent_fd, record_name)
        record_info = _lstat_at(parent_fd, record_name)
        if not _artifact_eligible(record, record_info, min_age_days=min_age_days, now=now) or record is None:
            continue
        logical = Path(os.path.abspath(os.path.join(os.fspath(root), rel_dir, record_name)))
        liveness = _liveness_skip(record)
        if liveness is not None:
            rows.append(
                _artifact_row(
                    rel_dir, directory_name, record, record_name,
                    action="skipped", reclaimable=0, skip_reason=liveness,
                )
            )
            continue
        if not apply:
            lock_skip = _dry_run_lock_skip(parent_fd, record_name, record, logical)
            if lock_skip is not None:
                rows.append(
                    _artifact_row(
                        rel_dir, directory_name, record, record_name,
                        action="skipped", reclaimable=0, skip_reason=lock_skip,
                    )
                )
                continue
            dir_fd = _open_dir(parent_fd, directory_name)
            if dir_fd is None:
                continue
            try:
                files = _inventory_artifacts(dir_fd, root_dev=os.fstat(dir_fd).st_dev)
            finally:
                os.close(dir_fd)
            if files is None:
                rows.append(
                    _artifact_row(
                        rel_dir, directory_name, record, record_name,
                        action="skipped", reclaimable=0, skip_reason="unsupported entry",
                    )
                )
                continue
            archive_name = f"{directory_name}.tar.xz"
            existing = _lstat_at(parent_fd, archive_name)
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode) or not _verify_archive_at(parent_fd, archive_name, files)
            ):
                rows.append(
                    _artifact_row(
                        rel_dir, directory_name, record, record_name,
                        action="error", reclaimable=0, error="archive verification failed",
                    )
                )
                continue
            nbytes = sum(files.values())
            rows.append(
                _artifact_row(
                    rel_dir, directory_name, record, record_name, action="would_archive", reclaimable=nbytes,
                )
            )
            reclaimable += nbytes
            continue
        outcome = _apply_one_artifact(
            parent_fd,
            directory_name,
            record_name,
            logical,
            record,
            observed_status=record.get("status"),
            observed_nonce=record.get("run_nonce"),
            min_age_days=min_age_days,
            now=now,
            identity=(info.st_dev, info.st_ino),
        )
        if outcome is None:
            continue
        nbytes = int(outcome.get("reclaimable_bytes") or 0)
        row = _artifact_row(
            rel_dir, directory_name, record, record_name, action=str(outcome["action"]), reclaimable=nbytes,
        )
        if outcome.get("skip_reason"):
            row["skip_reason"] = outcome["skip_reason"]
        if outcome.get("error"):
            row["error"] = outcome["error"]
        rows.append(row)
        if outcome["action"] == "archived":
            reclaimable += nbytes
    return rows, reclaimable


def plan_retention(
    batch_state: Path,
    *,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
    apply: bool = False,
    now: datetime | None = None,
    on_before_lock: Callable[[], None] | None = None,
    on_after_digest: Callable[[], None] | None = None,
    crash_after: str | None = None,
) -> dict[str, Any]:
    """Measure every batch_state subtree. Rewrite only eligible snapshot and artifact dirs.

    ``batch_state`` must be a directory named ``batch_state``. A symlink at any
    level is ignored: every open, stat, write, and unlink below that directory
    uses a descriptor opened with ``O_NOFOLLOW``. Non-terminal tasks and
    snapshot dirs without an explicit clean verdict are left as they are.
    Artifact directories are packed only for a terminal record old enough to
    qualify, with no live process and no held lock.
    """
    root_fd = _open_root(batch_state)
    try:
        return _plan_open(
            batch_state,
            root_fd,
            min_age_days=min_age_days,
            apply=apply,
            now=now or datetime.now(UTC),
            on_before_lock=on_before_lock,
            on_after_digest=on_after_digest,
            crash_after=crash_after,
        )
    finally:
        os.close(root_fd)


def _plan_open(
    batch_state: Path,
    root_fd: int,
    *,
    min_age_days: float,
    apply: bool,
    now: datetime,
    on_before_lock: Callable[[], None] | None,
    on_after_digest: Callable[[], None] | None,
    crash_after: str | None,
) -> dict[str, Any]:
    reclaimable_by_top: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    temps_removed: list[dict[str, str]] = []
    temps_would_remove: list[dict[str, str]] = []
    artifacts_reclaimable = 0
    tasks_fd = _open_dir(root_fd, "tasks")
    if tasks_fd is not None:
        try:
            parents = [("tasks", tasks_fd)]
            archive_fd = _open_dir(tasks_fd, ARCHIVE_DIR_NAME)
            if archive_fd is not None:
                parents.append((f"tasks/{ARCHIVE_DIR_NAME}", archive_fd))
            try:
                for rel_dir, parent_fd in parents:
                    _collect_own_temps(temps_removed, temps_would_remove, parent_fd, rel_dir, now=now, apply=apply)
                    for snapshot_name in _names(parent_fd):
                        info = _lstat_at(parent_fd, snapshot_name)
                        if (
                            info is None
                            or not snapshot_name.endswith(_SNAPSHOT_SUFFIX)
                            or not stat.S_ISDIR(info.st_mode)
                        ):
                            continue
                        snap_fd = _open_dir(parent_fd, snapshot_name)
                        if snap_fd is not None:
                            try:
                                _collect_own_temps(
                                    temps_removed,
                                    temps_would_remove,
                                    snap_fd,
                                    f"{rel_dir}/{snapshot_name}",
                                    now=now,
                                    apply=apply,
                                )
                            finally:
                                os.close(snap_fd)
                        measured = _measure_candidate(
                            parent_fd,
                            rel_dir,
                            snapshot_name,
                            root=batch_state,
                            min_age_days=min_age_days,
                            now=now,
                            apply=apply,
                            on_before_lock=on_before_lock,
                            on_after_digest=on_after_digest,
                            crash_after=crash_after,
                        )
                        if measured is None:
                            continue
                        row, reclaimed = measured
                        selected.append(row)
                        reclaimable_by_top["tasks"] = reclaimable_by_top.get("tasks", 0) + reclaimed
                    if rel_dir == "tasks":
                        artifact_rows, artifact_bytes = _measure_artifact_dirs(
                            parent_fd,
                            rel_dir,
                            root=batch_state,
                            min_age_days=min_age_days,
                            now=now,
                            apply=apply,
                        )
                        selected.extend(artifact_rows)
                        artifacts_reclaimable += artifact_bytes
            finally:
                if archive_fd is not None:
                    os.close(archive_fd)
        finally:
            os.close(tasks_fd)

    subtrees: list[dict[str, Any]] = []
    for name in _names(root_fd):
        info = _lstat_at(root_fd, name)
        if info is None or stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISREG(info.st_mode):
            nbytes = info.st_size
        elif stat.S_ISDIR(info.st_mode):
            child = _open_dir(root_fd, name)
            if child is None:
                continue
            try:
                nbytes = _tree_bytes_at(child)
            finally:
                os.close(child)
        else:
            continue
        subtrees.append(
            {
                "name": name,
                "bytes": nbytes,
                "reclaimable_bytes": reclaimable_by_top.get(name, 0),
            }
        )
    return {
        "mode": "apply" if apply else "dry-run",
        "batch_state": str(batch_state),
        "min_age_days": min_age_days,
        "allowlist": [
            "tasks/*.snapshots",
            "tasks/archive/*.snapshots",
            "tasks/*.artifacts",
        ],
        "subtrees": subtrees,
        "one_off_artifacts": _one_off_files_at(root_fd, now=now),
        "one_off_min_bytes": DEFAULT_ONE_OFF_MIN_BYTES,
        "totals": {
            "bytes": sum(row["bytes"] for row in subtrees),
            "reclaimable_bytes": sum(row["reclaimable_bytes"] for row in subtrees),
        },
        "artifacts_reclaimable_bytes": artifacts_reclaimable,
        "selected": selected,
        "temps_removed": temps_removed,
        "temps_would_remove": temps_would_remove,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report batch_state size and replace explicitly clean terminal snapshot sidecars with a digest.\n"
            "Use it from the scheduled hygiene run; do not use it to delete any other batch_state subtree. "
            "It also packs eligible tasks/*.artifacts directories into one xz archive."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  .venv/bin/python {_mp('maintenance/batch_state_retention.py')}\n"
            "      # dry-run. artifacts_reclaimable_bytes sums eligible tasks/*.artifacts files.\n"
            f"  .venv/bin/python {_mp('maintenance/batch_state_retention.py')} \\\n"
            "      --min-age-days 1 --apply\n"
            "      # pack each eligible tasks/<id>.artifacts directory and remove the loose copy.\n"
            "\n"
            "Outputs:\n"
            "  JSON on stdout: per-subtree bytes, reclaimable bytes, and selected snapshot dirs.\n"
            "  artifacts_reclaimable_bytes is its own field: the sum of regular-file sizes in\n"
            "  eligible tasks/*.artifacts directories. It is not included in totals.reclaimable_bytes,\n"
            "  which counts only snapshot digests. The xz archive is kept, so bytes freed on apply\n"
            "  are this loose size minus the archive size. Dry-run changes nothing.\n"
            "  one_off_artifacts lists regular files >=100 MiB with relative path, bytes and age_days;\n"
            "  these owner-disposition candidates are report-only and never deleted by that report.\n"
            "  With --apply, eligible tasks/*.snapshots and tasks/archive/*.snapshots dirs lose\n"
            "  read_only_checkout_{pre,post}.json and gain digest.json. The task record's\n"
            '  read_only_snapshot_retention field becomes "digest".\n'
            "  With --apply, an eligible tasks/<id>.artifacts directory is packed to\n"
            "  tasks/<id>.artifacts.tar.xz (mode 0600) only after the archive lists every file\n"
            "  with the same size. The task record gains artifacts_retention \"archived\" and\n"
            "  artifacts_archive_sha256. The loose directory is then removed. An existing archive\n"
            "  is never deleted or replaced. A live process, a held lock, a non-terminal status,\n"
            "  a young record, or a failed check leaves the directory in place.\n"
            "\n"
            "Exit codes:\n"
            "  0  report written (dry-run or apply)\n"
            "  2  the path is not a directory named batch_state, or no state root is configured\n"
            "\n"
            "Related:\n"
            "  Legacy read-only snapshot sidecars (#7203, #8783)\n"
            f"  {_mp('orchestration/stale_task_records.py')} archive (moves sidecars, does not shrink them)\n"
        ),
    )
    parser.add_argument(
        "--batch-state",
        type=Path,
        help=(
            "Directory named batch_state to measure. Default: <state root>/batch_state, "
            "the primary named by LEARN_UKRAINIAN_PRIMARY_REPO_ROOT. "
            "The sweep refuses any other directory name."
        ),
    )
    parser.add_argument(
        "--min-age-days",
        type=float,
        default=DEFAULT_MIN_AGE_DAYS,
        help=(
            "Only digest snapshot dirs and pack artifact dirs whose task record is at least this many days old "
            f"(finished_at, else the record file mtime). Default: {DEFAULT_MIN_AGE_DAYS:g}. "
            "A clean verdict is already on the task record, so the full JSON has no "
            "forensic value after a day; 7 reclaimed nothing because these dirs are "
            "younger than a week. Example: 0"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Rewrite eligible snapshot sidecars and pack eligible artifact directories. "
            "Default is dry-run: report reclaimable bytes and change nothing. Example: --apply"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = plan_retention(
            args.batch_state or state_root() / "batch_state",
            min_age_days=args.min_age_days,
            apply=args.apply,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
