"""Call-time task-store location and bounded Monitor task-record reads."""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from lu_machinery.common.repo_root import state_root

# Keep one version per path, including foreign-file classifications. The bound
# covers the historical inventory without retaining an unbounded number of runs.
TASK_RECORD_CACHE_MAX_ENTRIES = 65_536
_RecordStamp = tuple[int, int, int, int, int]
_record_cache: OrderedDict[str, tuple[_RecordStamp, dict[str, Any] | None]] = OrderedDict()
_record_cache_lock = threading.Lock()


def _record_stamp(info: os.stat_result) -> _RecordStamp:
    return info.st_mtime_ns, info.st_size, info.st_ctime_ns, info.st_dev, info.st_ino


def read_cached_task_record(path: Path, *, display_unknown_schema: bool = False) -> dict[str, Any] | None:
    """Read a task once per file version across Monitor collectors.

    Stat and read failures propagate; invalid JSON is never served from an older
    version. ctime and inode also invalidate same-size rewrites and replacements
    whose mtime was restored. The lock coalesces concurrent misses. Returned
    records have a private top-level mapping; nested values are read-only to callers.
    """
    from lu_machinery.orchestration.task_record_store import parse_task_record

    path = Path(os.path.abspath(path))
    key = os.fspath(path)
    with _record_cache_lock:
        stamp = _record_stamp(path.stat())
        cached = _record_cache.get(key)
        if cached is not None and cached[0] == stamp:
            record = cached[1]
            _record_cache.move_to_end(key)
        else:
            # Drop stale bytes before attempting a read, including failed reads.
            _record_cache.pop(key, None)
            with path.open("rb") as handle:
                opened_stamp = _record_stamp(os.fstat(handle.fileno()))
                raw = handle.read()
                finished_stamp = _record_stamp(os.fstat(handle.fileno()))
            record = parse_task_record(path, raw, display_unknown_schema=True)
            # Do not memoize a concurrent rewrite under the earlier stat key.
            if stamp == opened_stamp == finished_stamp:
                _record_cache[key] = (stamp, record)
                while len(_record_cache) > TASK_RECORD_CACHE_MAX_ENTRIES:
                    _record_cache.popitem(last=False)
        if record is not None and record.get("schema_version", 1) > 2 and not display_unknown_schema:
            raise ValueError(f"task record {path} has invalid schema_version")
        return None if record is None else dict(record)


def tasks_dir() -> Path:
    """Use a test override or the primary checkout's dispatch task store."""
    override = os.environ.get("LU_TASKS_DIR")
    if override:
        return Path(override)
    return state_root() / "batch_state" / "tasks"
