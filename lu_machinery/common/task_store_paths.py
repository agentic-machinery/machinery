"""Call-time task-store location and bounded Monitor task-record reads."""

from __future__ import annotations

import os
import stat
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from lu_machinery.common.repo_root import state_root

# Keep one version per path, including foreign-file classifications. The bound
# covers the historical inventory without retaining an unbounded number of runs.
TASK_RECORD_CACHE_MAX_ENTRIES = 65_536
TASK_RECORD_CACHE_MAX_BYTES = 64 * 1024 * 1024
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
