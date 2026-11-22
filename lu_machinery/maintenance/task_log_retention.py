"""Expire stdout and stderr logs only when every run of their task ID is old and settled."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import stat
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from lu_machinery.common.task_store_paths import tasks_dir
from lu_machinery.dispatcher import store, worker
from lu_machinery.hygiene.batch_state_retention import RETENTION_LIST, load_holds
from lu_machinery.orchestration.dead_worker_state import worker_liveness
from lu_machinery.orchestration.task_record_store import (
    _V2_TASK_ID_RE,
    ARCHIVE_DIR_NAME,
    candidate_task_record_names,
    parse_task_record,
    task_archive_lock,
)

DEFAULT_MIN_AGE_DAYS = 7
_LOG_NAME = re.compile(r"(.+)\.(?:stdout|stderr)\.log")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# Bound descriptors and time spent holding the archive lock during large sweeps.
_LOCK_BATCH_SIZE = 64
_MAX_RECORD_BYTES = 32 * 1024 * 1024


def _open_child(parent_fd: int, name: str, stack: ExitStack) -> int | None:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    stack.callback(os.close, fd)
    return fd


def _lock_task(parent_fd: int, task_id: str, *, apply: bool, stack: ExitStack) -> str | None:
    """Use dispatch's stable task-state lock without waiting behind a settling task."""
    flags = (os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK) if apply else _READ_FLAGS
    try:
        fd = os.open(f"{task_id}.json.lock", flags, 0o600, dir_fd=parent_fd)
    except FileNotFoundError:
        return "task_lock_unknown" if apply else None  # Dry runs never create lock files.
    except OSError:
        return "task_lock_unknown"
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "task_lock_unknown"
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except BlockingIOError:
        return "task_lock_busy"
    except OSError:
        return "task_lock_unknown"
    else:
        stack.callback(os.close, fd)
        return None
    finally:
        # The batch owns only successfully locked descriptors.
        if not locked:
            os.close(fd)


def _record_names(names: list[str], task_id: str) -> tuple[str, ...]:
    # All candidate names start with <id>. and '/' immediately follows '.' in ASCII.
    return candidate_task_record_names(task_id, names[bisect_left(names, task_id + "."):bisect_left(names, task_id + "/")])


def _record_reason(record: dict, cutoff: float) -> str | None:
    if store.retention_hold_error(record):
        return "retention_hold_invalid"
    if record.get("worktree_retention_hold") is True:
        return "retention_hold"
    status = record.get("status")
    if not isinstance(status, str) or status not in store.TERMINAL:
        return "task_not_settled"
    finished = record.get("finished_at")
    if not isinstance(finished, str):
        return "task_finish_unknown"
    try:
        when = datetime.fromisoformat(finished)
        if when.tzinfo is None:
            return "task_finish_unknown"
        if when.timestamp() > cutoff:
            return "task_too_recent"
    except (ValueError, OverflowError, OSError):
        return "task_finish_unknown"
    try:
        launch_mode, pid = record.get("launch_mode"), record.get("pid")
        # Only a scope proves that descendants which left the process group have exited.
        no_scope = launch_mode != "scope"
        launched = launch_mode is not None or pid is not None or record.get("provider_pgid") is not None
        if (record.get("admission_hold") is not None or (no_scope and launched)
                or (pid is not None and worker_liveness(record) is not False)
                or worker._provider_alive(record) is not False):
            return "task_liveness_unproven"
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return "task_liveness_unproven"
    return None


def _history_reason(task_id: str, parents: list[tuple[int, list[str]]], cutoff: float) -> str | None:
    found = False
    for parent_fd, names in parents:
        for name in _record_names(names, task_id):
            try:
                fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
                        return "task_record_unknown"
                    raw = stream.read(_MAX_RECORD_BYTES + 1)
                    if len(raw) > _MAX_RECORD_BYTES:
                        return "task_record_unknown"
                    record = parse_task_record(Path(name), raw)
            except (OSError, ValueError):
                return "task_record_unknown"
            if record is None:
                return "task_record_unknown"
            record_id = record.get("task_id")
            if (not isinstance(record_id, str) or not _V2_TASK_ID_RE.fullmatch(record_id) or ".." in record_id
                    or not candidate_task_record_names(record_id, (name,))):
                return "task_record_unknown"
            if record_id == task_id:
                found = True
                if reason := _record_reason(record, cutoff):
                    return reason
    return None if found else "task_record_missing"


def _candidate_logs(logs_fd: int, cutoff: float, retained: Counter[str]) -> dict[str, list[str]]:
    candidates = defaultdict(list)
    for name in sorted(os.listdir(logs_fd)):
        match = _LOG_NAME.fullmatch(name)
        if match is None or not _V2_TASK_ID_RE.fullmatch(match[1]) or ".." in match[1]:
            retained["unrecognized_name"] += 1
            continue
        try:
            info = os.stat(name, dir_fd=logs_fd, follow_symlinks=False)
        except OSError:
            retained["log_unreadable"] += 1
            continue
        if not stat.S_ISREG(info.st_mode):
            retained["log_not_regular"] += 1
        elif info.st_mtime > cutoff:
            retained["log_too_recent"] += 1
        else:
            candidates[match[1]].append(name)
    return candidates


def sweep(task_root: Path, *, cutoff: float, apply: bool) -> tuple[list[str], Counter[str]]:
    """Return expired (or dry-run eligible) basenames and counts for retained entries."""
    if not task_root.is_absolute():
        raise ValueError("task root must be absolute")
    eligible: list[str] = []
    retained: Counter[str] = Counter()
    hold = any(entry["parts"][0] == "tasks" for entry in load_holds(RETENTION_LIST))
    with ExitStack() as roots:
        tasks_fd = os.open(task_root, _DIR_FLAGS)
        roots.callback(os.close, tasks_fd)
        logs_fd = _open_child(tasks_fd, "logs", roots)
        if logs_fd is None:
            return eligible, retained
        candidates = _candidate_logs(logs_fd, cutoff, retained)
        if hold:
            retained["batch_state_hold"] += sum(map(len, candidates.values()))
            return eligible, retained

        task_ids = tuple(candidates)
        for offset in range(0, len(task_ids), _LOCK_BATCH_SIZE):
            batch = task_ids[offset:offset + _LOCK_BATCH_SIZE]
            with ExitStack() as stack:
                # Take task locks before the archive lock, as restore does. The task lock
                # excludes admission, settlement and hold changes for this ID until unlink.
                locked = []
                for task_id in batch:
                    if reason := _lock_task(tasks_fd, task_id, apply=apply, stack=stack):
                        retained[reason] += len(candidates[task_id])
                    else:
                        locked.append(task_id)
                if not locked:
                    continue
                stack.enter_context(task_archive_lock(task_root))
                archive_fd = _open_child(tasks_fd, ARCHIVE_DIR_NAME, stack)
                parents = [(tasks_fd, sorted(os.listdir(tasks_fd)))]
                if archive_fd is not None:
                    parents.append((archive_fd, sorted(os.listdir(archive_fd))))
                for task_id in locked:
                    names = candidates[task_id]
                    if reason := _history_reason(task_id, parents, cutoff):
                        retained[reason] += len(names)
                        continue
                    for name in names:
                        try:
                            # A dispatch can have rewritten a log before we took the lock.
                            info = os.stat(name, dir_fd=logs_fd, follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                                retained["log_changed"] += 1
                                continue
                            if apply:
                                os.unlink(name, dir_fd=logs_fd)
                        except OSError:
                            retained["log_io_error"] += 1
                        else:
                            eligible.append(name)
    return eligible, retained


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Expire old dispatch stdout and stderr logs after all runs settle.\n"
        "Run without arguments to preview eligibility. Use --apply to unlink eligible logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  .venv/bin/python -m lu_machinery.maintenance.task_log_retention\n"
        "  .venv/bin/python -m lu_machinery.maintenance.task_log_retention --apply --summary\n"
        "Outputs: JSON counts and eligible basenames. Only --apply unlinks logs.\n"
        "Exit codes: 0 completed, 1 I/O or store failure, 2 invalid arguments.\n"
        "Related: docs/runbooks/tmp-retention.md, issue #1206.",
    )
    parser.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE_DAYS,
                        help="Minimum age of the log and every run's completion, in days (default 7, example 14).")
    parser.add_argument("--apply", action="store_true", help="Unlink eligible logs (default dry run).")
    parser.add_argument("--summary", action="store_true", help="Omit eligible basenames (default show them).")
    args = parser.parse_args(argv)
    age = args.min_age_days * 86400
    if not math.isfinite(age) or age <= 0:
        parser.error("--min-age-days must be finite and positive")
    try:
        eligible, retained = sweep(tasks_dir(), cutoff=time.time() - age, apply=args.apply)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": "task_log_inventory_unavailable", "kind": type(error).__name__}))
        return 1
    report = {"mode": "apply" if args.apply else "dry-run", "selected": len(eligible), "retained": dict(retained)}
    if not args.summary:
        report["logs"] = eligible
    print(json.dumps(report, sort_keys=True))
    return int(bool(retained["log_io_error"] or retained["log_unreadable"]))


if __name__ == "__main__":
    raise SystemExit(main())
