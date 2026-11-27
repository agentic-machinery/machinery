"""Call-time task-store location and bounded Monitor task-record reads."""

from __future__ import annotations

import argparse
import os
import stat
import threading
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

from lu_machinery import state_paths
from lu_machinery.common.repo_root import MachineryConfigError, configured_host_state_root, state_root

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
    """Resolve the dispatch task store at call time."""
    override = os.environ.get("LU_TASKS_DIR")
    if override:
        host_state = configured_host_state_root()
        if host_state is not None:
            return state_paths.tasks_dir(host_state)
        return Path(override)
    return state_paths.tasks_dir(state_root())


_MAX_SYMLINK_HOPS = 40


def _is_flat_task_store_dir(directory: Path, forbidden_dirs: set[Path]) -> bool:
    if directory in forbidden_dirs:
        return True
    parts = directory.parts
    return parts[-2:] == ("batch_state", "tasks") or parts[-3:] == ("batch_state", "tasks", "archive")


def _path_resolution_candidates(path: Path) -> tuple[list[Path], set[Path]] | None:
    """Return lexical/intermediate POSIX resolution states and walked conventional store roots.

    Walking components left to right preserves conventional ``batch_state/tasks``
    roots before following symlinks or collapsing pending ``..`` traversals, even
    when reached through parent-directory symlinks and backed by differently named
    directories without a configured state root. Returns ``None`` on malformed
    paths, non-directory prefix traversal, or symlink loops so callers fail closed.
    """
    raw = os.fspath(path)
    if not raw or "\x00" in raw:
        return None
    if not os.path.isabs(raw):
        try:
            raw = os.path.join(os.getcwd(), raw)
        except OSError:
            return None
    candidates: list[Path] = [Path(os.path.normpath(raw))]
    conventional_roots: set[Path] = set()
    anchor = Path(raw).anchor or os.sep
    resolved = Path(anchor)
    remainder = raw[len(anchor) :]
    queue: deque[str] = deque(part for part in remainder.split(os.sep) if part and part != ".")
    hops = 0
    while queue:
        part = queue.popleft()
        if part == "..":
            resolved = resolved.parent
            if resolved.parts[-2:] == ("batch_state", "tasks"):
                conventional_roots.add(resolved)
            candidates.append(Path(os.path.normpath(os.path.join(resolved, *queue))))
            continue
        next_path = resolved / part
        if next_path.parts[-2:] == ("batch_state", "tasks"):
            conventional_roots.add(next_path)
        try:
            info = os.lstat(next_path)
        except FileNotFoundError:
            resolved = next_path
            continue
        except (OSError, ValueError):
            return None
        if stat.S_ISLNK(info.st_mode):
            candidates.append(Path(os.path.normpath(os.path.join(next_path, *queue))))
            hops += 1
            if hops > _MAX_SYMLINK_HOPS:
                return None
            try:
                raw_target = os.readlink(next_path)
            except OSError:
                return None
            if not raw_target or "\x00" in raw_target:
                return None
            if os.path.isabs(raw_target):
                target_anchor = Path(raw_target).anchor or os.sep
                resolved = Path(target_anchor)
                target_remainder = raw_target[len(target_anchor) :]
            else:
                target_remainder = raw_target
            target_parts = [p for p in target_remainder.split(os.sep) if p and p != "."]
            queue.extendleft(reversed(target_parts))
            candidates.append(Path(os.path.normpath(os.path.join(resolved, *queue))))
        elif queue and not stat.S_ISDIR(info.st_mode):
            return None
        else:
            resolved = next_path
    candidates.append(resolved)
    return candidates, conventional_roots


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


def _forbidden_task_store_dirs(
    candidates: list[Path],
    conventional_roots: set[Path],
    *,
    store_dir: Path | None = None,
) -> set[Path]:
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
    for root in conventional_roots:
        _add_store_root(forbidden_dirs, root)
    for candidate in candidates:
        for ancestor in (candidate, *candidate.parents):
            if ancestor.parts[-2:] == ("batch_state", "tasks"):
                _add_store_root(forbidden_dirs, ancestor)
    return forbidden_dirs


def is_direct_task_store_path(path: Path, *, store_dir: Path | None = None) -> bool:
    """Whether ``path`` targets the flat task-record store or its archive directly.

    Every task-store reader scans ``tasks_dir/*.json`` and ``tasks_dir/archive/*.json``
    as dispatch task records. Non-record tool files belong in a subdirectory such as
    ``<task-id>.artifacts/`` rather than directly inside those two directories.

    Checks lexical spellings, intermediate POSIX symlink-resolution states across
    all components, and realpath forms against the configured ``tasks_dir`` (and
    ``archive/``) as well as conventional ``batch_state/tasks`` layouts. Fails
    closed on malformed paths, non-directory prefix traversal, or symlink loops.
    """
    walked = _path_resolution_candidates(path)
    if walked is None:
        return True
    candidates, conventional_roots = walked
    try:
        forbidden_dirs = _forbidden_task_store_dirs(
            candidates, conventional_roots, store_dir=store_dir,
        )
        resolved_parent = Path(os.path.realpath(path.parent))
        resolved_target = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return True
    directories = [resolved_parent, resolved_target.parent, resolved_target]
    for candidate in candidates:
        directories.append(candidate.parent)
        directories.append(candidate)
    return any(_is_flat_task_store_dir(directory, forbidden_dirs) for directory in directories)


def write_path_spelling_error(raw: str) -> str | None:
    """Why the string ``raw`` cannot name a file to write, else ``None``.

    Checked on the string as given, because ``Path`` drops a trailing ``/``, ``/.`` or ``/..``
    (``out.json/`` would become ``out.json``). An unexpanded leading ``~`` (an unknown user or a
    quoted tilde) is refused rather than becoming a directory named ``~`` in the working directory.
    A spelling at or over the kernel's path limit is refused too: no lookup through it succeeds,
    while ``Path`` would collapse its repeated separators into a shorter path that does.
    """
    if not raw or raw.rsplit(os.sep, 1)[-1] in {"", ".", ".."}:
        return "empty file name, or a trailing '/', '.' or '..'"
    if raw.startswith("~"):
        return "unexpanded '~' home directory"
    if "\x00" in raw:
        return "NUL byte"
    try:
        path_max = os.pathconf(os.sep, "PC_PATH_MAX")
    except (OSError, ValueError):
        return "the kernel's path length limit is unavailable"
    if len(os.fsencode(raw)) >= path_max:
        return f"{len(os.fsencode(raw))} bytes, at or over the kernel's {path_max}-byte path limit"
    return None


def write_path_argument(value: str) -> Path:
    """``argparse`` type for a file a CLI writes; refuses what :func:`write_path_spelling_error` names."""
    reason = write_path_spelling_error(value)
    if reason is not None:
        raise argparse.ArgumentTypeError(f"not a writable file path ({reason}): {value!r}")
    return Path(value)


def kernel_follows_write_path(path: Path | str) -> bool:
    """Whether the kernel can follow ``path``'s nearest existing ancestor (``path`` itself if it exists).

    An existing ``path`` must not be a directory (or a symlink to one), and a dangling symlink is
    refused. Any other existing file (a FIFO, ``/dev/null``, ``/dev/stdout``) is written as the
    kernel opens it, as any Unix tool would.

    ``os.path.realpath`` collapses ``missing/..`` and drops a trailing ``/`` in a symlink target, so
    a spelling the kernel cannot follow (a symlink to ``file.json/`` or to ``missing/../dir``) can
    still resolve to an existing file. A writer that resolves before writing refuses ``False``, so
    it never writes where a lookup through the caller's spelling fails. The components below that
    ancestor do not exist yet, so they hold no symlink; the ancestor must then be a directory, and a
    ``..`` among them is refused. Only a missing entry is climbed past: any other lookup error on the
    spelling (for example a name too long for the kernel) refuses it.
    """
    raw = os.fspath(path)
    try:
        probe = raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)
    except (OSError, ValueError):
        return False
    leaf = True
    while True:
        try:
            os.lstat(probe)
        except FileNotFoundError:
            parent, name = os.path.split(probe)
            if name == ".." or parent == probe:
                return False
            probe, leaf = parent, False
            continue
        except (OSError, ValueError):
            # ENAMETOOLONG, ENOTDIR, EACCES or ELOOP on the spelling itself: a later lookup through it fails too.
            return False
        if not leaf:
            return os.path.isdir(probe)
        try:
            return not stat.S_ISDIR(os.stat(probe).st_mode)
        except (OSError, ValueError):
            return False


def require_not_direct_task_store_path(
    path: Path, *, label: str = "non-record file", store_dir: Path | None = None,
) -> None:
    """Refuse writing a non-record file directly inside the task store or its archive."""
    if is_direct_task_store_path(path, store_dir=store_dir):
        raise ValueError(
            f"task_store_direct_write_refused: refusing to write {label} directly in the task store, or at a "
            f"path whose place relative to it cannot be checked ({path}); write it under <task>.artifacts/ instead"
        )
