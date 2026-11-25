"""Call-time task-store location and bounded Monitor task-record reads."""

from __future__ import annotations

import os
import stat
import threading
import uuid
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Any

from lu_machinery.common.repo_root import MachineryConfigError, state_root
from lu_machinery.common.safe_open import UnsafeEntryError, safe_open_below

# Keep one version per path, including foreign-file classifications. Entry and
# content caps bound retention; an evicted version is an ordinary cache miss.
TASK_RECORD_CACHE_MAX_ENTRIES = 65_536
# Smallest power-of-two budget that retains the measured 163 MB working fixture.
# Candidate-file bytes also include foreign data; do not size to that whole inventory.
TASK_RECORD_CACHE_MAX_BYTES = 256 * 1024 * 1024
_RecordStamp = tuple[int, int, int, int, int]
_record_cache: OrderedDict[str, tuple[_RecordStamp, dict[str, Any] | None | ValueError, int]] = OrderedDict()
_record_cache_lock = threading.Lock()
_record_cache_bytes = 0


def _discard_cached_record(key: str) -> None:
    """Drop a version and its charged bytes; the caller holds the cache lock."""
    global _record_cache_bytes
    cached = _record_cache.pop(key, None)
    if cached is not None:
        _record_cache_bytes -= cached[2]


def _record_stamp(info: os.stat_result) -> _RecordStamp:
    return info.st_mtime_ns, info.st_size, info.st_ctime_ns, info.st_dev, info.st_ino


class TaskRecordReadRefusal(ValueError):
    """Typed regular-file or byte-limit refusal for bounded collectors."""


def read_cached_task_record(
    path: Path, *, tasks_dir: Path, display_unknown_schema: bool = False, max_bytes: int | None = None,
) -> dict[str, Any] | None:
    """Read a task once per file version across Monitor collectors.

    Stat and read failures propagate; invalid JSON is never served from an older
    version. ctime and inode also invalidate same-size rewrites and replacements
    whose mtime was restored. The lock coalesces concurrent misses. Returned
    records have a private top-level mapping; nested values are read-only to callers.
    Containment is checked here, beside the stat/open sinks, while ``path``
    retains the filename used by task-identity classification. Structural
    failures are memoized as ValueError messages; transient I/O stays retryable.
    The LRU bounds both entries and charged source bytes plus metadata; an
    oversized entry is served without retention. Optional per-read byte limits
    apply to cached versions too; non-regular files are
    refused, and nonblocking opens prevent a concurrent FIFO swap from hanging.
    """
    global _record_cache_bytes

    from lu_machinery.orchestration.task_record_store import parse_task_record

    path = Path(os.path.abspath(path))
    root = os.path.realpath(tasks_dir)
    fullpath = os.path.realpath(path)
    if not fullpath.startswith(root + os.sep):
        return None
    source = Path(fullpath)
    key = os.fspath(path)
    with _record_cache_lock:
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise TaskRecordReadRefusal("task_record_not_regular")
        if max_bytes is not None and info.st_size > max_bytes:
            raise TaskRecordReadRefusal("task_record_too_large")
        stamp = _record_stamp(info)
        cached = _record_cache.get(key)
        if cached is not None and cached[0] == stamp:
            record = cached[1]
            _record_cache.move_to_end(key)
        else:
            # Drop stale bytes before attempting a read, including failed reads.
            _discard_cached_record(key)
            fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise TaskRecordReadRefusal("task_record_not_regular")
                if max_bytes is not None and opened.st_size > max_bytes:
                    raise TaskRecordReadRefusal("task_record_too_large")
                opened_stamp = _record_stamp(opened)
                raw = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
                if max_bytes is not None and len(raw) > max_bytes:
                    raise TaskRecordReadRefusal("task_record_too_large")
                finished_stamp = _record_stamp(os.fstat(handle.fileno()))
            try:
                record = parse_task_record(path, raw, display_unknown_schema=True)
            except ValueError as exc:
                # No traceback or JSONDecodeError.doc is retained in the cache.
                record = ValueError(str(exc))
            # Do not memoize a concurrent rewrite under the earlier stat key.
            if stamp == opened_stamp == finished_stamp:
                # Bound serialized content as well as the metadata entry count;
                # parsed-object overhead is measured separately in the RSS proof.
                cost = len(raw) if isinstance(record, dict) else len(str(record).encode("utf-8"))
                if cost <= TASK_RECORD_CACHE_MAX_BYTES:
                    _record_cache[key] = (stamp, record, cost)
                    _record_cache_bytes += cost
                    while (len(_record_cache) > TASK_RECORD_CACHE_MAX_ENTRIES
                           or _record_cache_bytes > TASK_RECORD_CACHE_MAX_BYTES):
                        _, evicted = _record_cache.popitem(last=False)
                        _record_cache_bytes -= evicted[2]
        if isinstance(record, ValueError):
            raise ValueError(str(record))
        if record is not None and record.get("schema_version", 1) > 2 and not display_unknown_schema:
            raise ValueError(f"task record {path} has invalid schema_version")
        return None if record is None else dict(record)


def tasks_dir() -> Path:
    """Use a test override or the primary checkout's dispatch task store."""
    override = os.environ.get("LU_TASKS_DIR")
    if override:
        return Path(override)
    return state_root() / "batch_state" / "tasks"


def _is_flat_task_store_dir(directory: Path, forbidden_dirs: set[Path]) -> bool:
    if directory in forbidden_dirs:
        return True
    parts = directory.parts
    return parts[-2:] == ("batch_state", "tasks") or parts[-3:] == ("batch_state", "tasks", "archive")


def _lexical_candidates(path: Path) -> list[Path]:
    """Return normalized lexical forms of ``path`` plus any unresolved symlink hops.

    Preserving lexical directory spellings keeps the ``batch_state/tasks`` fallback
    effective even when a conventional store directory is itself a symlink to a
    differently named backing directory and no state root is configured.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()
    current = Path(os.path.normpath(os.path.abspath(path)))
    for _ in range(40):
        if current in seen:
            break
        seen.add(current)
        candidates.append(current)
        if not os.path.islink(current):
            break
        try:
            raw_target = os.readlink(current)
        except OSError:
            break
        target = Path(raw_target)
        if not target.is_absolute():
            target = current.parent / target
        current = Path(os.path.normpath(os.path.abspath(target)))
    return candidates


def _add_store_root(forbidden_dirs: set[Path], root: Path) -> None:
    lexical = Path(os.path.normpath(os.path.abspath(root)))
    real = Path(os.path.realpath(root))
    forbidden_dirs.update(
        (
            lexical,
            lexical / "archive",
            real,
            real / "archive",
            Path(os.path.realpath(root / "archive")),
        )
    )


def _forbidden_task_store_dirs(path: Path, *, store_dir: Path | None = None) -> tuple[set[Path], list[Path]]:
    configured: Path | None
    if store_dir is not None:
        configured = store_dir
    else:
        try:
            configured = tasks_dir()
        except (MachineryConfigError, OSError):
            configured = None
    forbidden_dirs: set[Path] = set()
    if configured is not None:
        _add_store_root(forbidden_dirs, configured)
    lexical_paths = _lexical_candidates(path)
    for candidate in lexical_paths:
        for ancestor in (candidate, *candidate.parents):
            if ancestor.parts[-2:] == ("batch_state", "tasks"):
                _add_store_root(forbidden_dirs, ancestor)
    return forbidden_dirs, lexical_paths


def is_direct_task_store_path(path: Path, *, store_dir: Path | None = None) -> bool:
    """Whether ``path`` targets the flat task-record store or its archive directly.

    Every task-store reader scans ``tasks_dir/*.json`` and ``tasks_dir/archive/*.json``
    as dispatch task records. Non-record tool files belong in a subdirectory such as
    ``<task-id>.artifacts/`` rather than directly inside those two directories.

    Checks lexical normalized spellings (including unresolved symlink hops), the
    resolved parent directory, and the resolved target path so symlinked store
    directories, symlink files pointing into or out of the store, and ``..``
    traversals are all caught both with and without a configured state root.
    """
    forbidden_dirs, lexical_paths = _forbidden_task_store_dirs(path, store_dir=store_dir)
    resolved_parent = Path(os.path.realpath(path.parent))
    resolved_target = Path(os.path.realpath(path))
    directories = [resolved_parent, resolved_target.parent, resolved_target]
    for candidate in lexical_paths:
        directories.append(candidate.parent)
        directories.append(candidate)
    return any(_is_flat_task_store_dir(directory, forbidden_dirs) for directory in directories)


def require_not_direct_task_store_path(
    path: Path, *, label: str = "non-record file", store_dir: Path | None = None,
) -> None:
    """Refuse writing a non-record file directly inside the task store or its archive."""
    if is_direct_task_store_path(path, store_dir=store_dir):
        raise ValueError(
            f"task_store_direct_write_refused: refusing to write {label} directly in the task store "
            f"({path}); write it under <task>.artifacts/ instead"
        )


def _dir_fd_path(dir_fd: int, fallback: Path) -> Path:
    """Canonical path of an open directory descriptor."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
    except OSError:
        return Path(os.path.realpath(fallback))


def open_verified_non_task_store_dir(
    path: Path, *, label: str = "non-record file", store_dir: Path | None = None, create: bool = True,
) -> tuple[int, Path, str]:
    """Validate ``path``, open its parent directory fd, re-verify the opened fd, and return ``(dir_fd, dir_path, name)``."""
    require_not_direct_task_store_path(path, label=label, store_dir=store_dir)
    name = path.name
    if not name or name in {".", ".."}:
        raise ValueError(
            f"task_store_direct_write_refused: invalid {label} filename ({path}); "
            "write it under <task>.artifacts/ instead"
        )
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        verified_dir = _dir_fd_path(dir_fd, path.parent)
        forbidden_dirs, _ = _forbidden_task_store_dirs(path, store_dir=store_dir)
        dir_stat = os.fstat(dir_fd)
        is_forbidden = _is_flat_task_store_dir(verified_dir, forbidden_dirs)
        if not is_forbidden:
            for forbidden in forbidden_dirs:
                try:
                    if os.path.samestat(dir_stat, os.stat(forbidden)):
                        is_forbidden = True
                        break
                except OSError:
                    continue
        if is_forbidden:
            raise ValueError(
                f"task_store_direct_write_refused: refusing to write {label} directly in the task store "
                f"({verified_dir / name}); write it under <task>.artifacts/ instead"
            )
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd, verified_dir, name


def write_non_task_store_bytes(
    path: Path,
    payload: bytes,
    *,
    label: str = "non-record file",
    mode: int = 0o600,
    fsync: bool = False,
    store_dir: Path | None = None,
) -> Path:
    """Atomically write ``payload`` into a verified non-task-store directory without following destination symlinks.

    Returns the canonical path bound to the opened parent directory descriptor.
    """
    dir_fd, verified_dir, name = open_verified_non_task_store_dir(
        path, label=label, store_dir=store_dir, create=True,
    )
    tmp_name = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    created_tmp = False
    try:
        try:
            existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid():
                raise UnsafeEntryError
        fd = safe_open_below(dir_fd, tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        created_tmp = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        created_tmp = False
        return verified_dir / name
    finally:
        if created_tmp:
            with suppress(FileNotFoundError):
                os.unlink(tmp_name, dir_fd=dir_fd)
        os.close(dir_fd)
