"""Prune settled tasks' bulky state, report batch_state staleness, declare scratch expiry (#9737, #11365).

Settled-task pruning (CLEANUP-WORKFILES-1118): ``settled_days`` after a task
reached a released terminal status, everything in its ``tasks/<task>.*``
directories goes except hand-off ``.md`` files, sockets, FIFOs, ``tmux-*`` and
``ssh-*`` directories and paths a live process uses. The task record, its
``.result`` and every other file beside it stay. A live, held, damaged or
recently written task is never touched, nor is the archive directory. The
dispatcher's settlement removes a task's scratch with the same pruner.

A top-level directory is stale when nothing below it was written for
``stale_days``. Live machinery (``tasks/``, ``preserved/``, ``reports/``,
``branch-archive/``) is excluded, and paths listed in
``batch_state_retention.json`` are reported as held evidence instead of stale
candidates. Unclassified top-level state is only reported: its deletion
belongs to the tmp_sweep quarantine and purge path, through explicit expiry.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from lu_machinery.common import json_depth, task_scratch
from lu_machinery.common.repo_root import state_root
from lu_machinery.common.task_scratch import ContainmentError, mount_points
from lu_machinery.orchestration.task_record_store import (
    ARCHIVE_DIR_NAME,
    archived_task_record_path,
    read_task_record,
    task_record_path,
    task_state_lock,
)
from lu_machinery.orchestration.worktree_claims import RELEASED_TASK_STATUSES

# Deepest JSON this module reads (json_depth): batch-state scratch expiry metadata.
_JSON_MAX_DEPTH = 128


def _machinery_top() -> str:
    # Name the package from the unresolved file location, never from __package__
    # (design-v1 amendment 1): a copy loaded without a package must name the same tree.
    return Path(os.path.abspath(__file__)).parents[1].name


def _mp(relative: str) -> str:
    return f"{_machinery_top()}/{relative}"


def _mod(*parts: str) -> str:
    return ".".join((_machinery_top(), *parts))


SCHEMA = "batch-state-retention.v1"
EXCLUDED = frozenset({"tasks", "preserved", "reports", "branch-archive"})
RETENTION_LIST = Path(__file__).with_name("batch_state_retention.json")
EXPIRY_FILE = ".scratch-expiry.json"
DEFAULT_SETTLED_DAYS = 2.0
# Hand-off notes survive inside a pruned task directory; everything else in it goes.
KEPT_SUFFIX = ".md"
# Agent and terminal socket directories (tmux-<uid>, ssh-XXXX): a server finds them by path, so they always stay.
_RENDEZVOUS_DIRS = ("tmux-", "ssh-")


class RetentionListError(ValueError):
    """The evidence-retention list is malformed; holds must never be ignored silently."""


def default_batch_root() -> Path:
    """The primary checkout's shared batch_state directory."""
    return state_root() / "batch_state"


def load_holds(path: Path) -> list[dict[str, Any]]:
    """Read and validate held paths; every entry names a repo-relative batch_state path."""
    try:
        data = json_depth.loads(path.read_text(encoding="utf-8"), max_depth=_JSON_MAX_DEPTH)
    except (OSError, ValueError) as error:
        raise RetentionListError(f"unreadable retention list: {error}") from error
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("holds"), list):
        raise RetentionListError(f"retention list must be a {SCHEMA} object with a holds list")
    holds = []
    for entry in data["holds"]:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in ("path", "reason")):
            raise RetentionListError("each hold needs string path and reason fields")
        parts = PurePosixPath(entry["path"]).parts
        if len(parts) < 2 or parts[0] != "batch_state" or any(part in {"..", "."} for part in parts):
            raise RetentionListError(f"hold path must lie below batch_state/: {entry['path']!r}")
        holds.append({**entry, "parts": parts[1:]})
    return holds


def scratch_expiry(
    path: Path, *, origin: Path, now: float, expected: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Prove explicit scratch expiry, current holds and Git protection, including in quarantine.

    An absent marker is unknown state, never seven-day deletion permission.
    ``origin`` remains the original batch_state child after quarantine moves it.
    """
    if origin.name in EXCLUDED or origin.parent.name != "batch_state":
        return None, "expiry_excluded"
    try:
        fd = os.open(path / EXPIRY_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > 65536:
                return None, "expiry_metadata_unknown"
            raw = handle.read(65537)
        data = json_depth.loads(raw, max_depth=_JSON_MAX_DEPTH)
        if (len(raw) > 65536 or not isinstance(data, dict) or data.get("schema") != "batch-state-scratch.v1"
                or not all(isinstance(data.get(key), str) and data[key].strip() for key in ("owner", "purpose", "delete_after"))):
            return None, "expiry_metadata_unknown"
        expiry = datetime.fromisoformat(data["delete_after"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry.utcoffset().total_seconds() != 0:
            return None, "expiry_metadata_unknown"
        metadata = {key: data[key] for key in ("owner", "purpose", "delete_after")}
        if expected is not None and metadata != expected:
            return None, "expiry_metadata_changed"
        if not math.isfinite(now) or expiry.timestamp() >= now:
            return None, "expiry_not_reached"
        holds = load_holds(RETENTION_LIST)
        if any(hold["parts"][0] == origin.name for hold in holds):
            return None, "expiry_held"
        tracked = subprocess.run(
            ["git", "-C", str(origin.parent.parent), "ls-files", "-z", "--",
             f":(literal)batch_state/{origin.name}"], capture_output=True, check=True, timeout=15,
        )
        if tracked.stdout:
            return None, "expiry_tracked"
        return metadata, None
    except (OSError, ValueError, OverflowError, RecursionError, subprocess.SubprocessError):
        return None, "expiry_proof_unknown"


def write_scratch_expiry(
    path: Path, *, owner: str, purpose: str, delete_after: datetime | None = None,
) -> None:
    """Opt a producer-owned scratch directory in, exclusively; default expiry is seven days in UTC.

    Producers must call this before writing disposable payloads. This never
    retroactively marks existing unclassified state or overwrites a marker.
    """
    expiry = delete_after if delete_after is not None else datetime.now(UTC) + timedelta(days=7)
    if (not owner.strip() or not purpose.strip() or expiry.tzinfo is None
            or expiry.utcoffset().total_seconds() != 0 or path.parent.name != "batch_state"
            or path.name in EXCLUDED or path.name.startswith(".")):
        raise ValueError("scratch needs an owner, purpose, UTC expiry and a top-level batch_state directory")
    data = {"schema": "batch-state-scratch.v1", "owner": owner, "purpose": purpose,
            "delete_after": expiry.isoformat()}
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(EXPIRY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def tree_activity(path: Path, cutoff: float) -> tuple[int, float, str | None]:
    """Allocated bytes and newest write below ``path``, never following links or mounts.

    The walk stops early once a write newer than ``cutoff`` proves the tree
    fresh, so the byte count is complete only for stale trees.
    """
    mounts = mount_points()
    if mounts is None:
        return 0, 0.0, "mount_probe_unknown"
    allocated = 0
    newest = 0.0
    reason = None
    seen = set()
    try:
        device = path.lstat().st_dev
        pending = [path]
        while pending:
            node = pending.pop()
            info = node.lstat()
            newest = max(newest, info.st_mtime)
            if newest >= cutoff:
                return allocated, newest, None
            if (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                allocated += info.st_blocks * 512
            if info.st_dev != device or str(node) in mounts:
                reason = reason or "mount_point"
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.extend(node.iterdir())
    except OSError:
        return allocated, newest, "tree_unknown"
    return allocated, newest, reason


def report(
    batch_root: Path,
    *,
    holds: list[dict[str, Any]],
    stale_days: float = 7,
    now: float | None = None,
) -> dict[str, Any]:
    """Classify each top-level directory as excluded, held, fresh or stale."""
    if not math.isfinite(stale_days) or stale_days <= 0:
        raise ValueError("stale days must be finite and positive")
    if batch_root.is_symlink() or not batch_root.is_dir():
        raise ValueError("batch_state root must be a real directory")
    now = time.time() if now is None else now
    cutoff = now - stale_days * 86400
    counts = {"excluded": 0, "fresh": 0, "held": 0, "stale": 0, "unknown": 0}
    rows = []
    for path in sorted(batch_root.iterdir()):
        if path.name.startswith(".") or path.is_symlink() or not path.is_dir():
            continue
        if path.name in EXCLUDED:
            counts["excluded"] += 1
            continue
        row: dict[str, Any] = {"path": f"batch_state/{path.name}/"}
        held = [hold for hold in holds if hold["parts"][0] == path.name]
        allocated, newest, reason = tree_activity(path, cutoff)
        if held:
            row.update(status="held", holds=[{k: v for k, v in h.items() if k != "parts"} for h in held])
        elif reason:
            row.update(status="unknown", reason=reason)
        elif newest >= cutoff:
            counts["fresh"] += 1
            continue
        else:
            row.update(status="stale")
        if row["status"] != "held" or newest < cutoff:
            row["bytes"] = allocated
        row["newest_write_utc"] = datetime.fromtimestamp(newest, UTC).strftime("%Y-%m-%d") if newest else None
        counts[row["status"]] += 1
        rows.append(row)
    return {
        "schema": "batch-state-staleness.v1",
        "stale_days": stale_days,
        "excluded_names": sorted(EXCLUDED),
        "counts": counts,
        "stale_bytes": sum(row["bytes"] for row in rows if row["status"] == "stale"),
        "rows": rows,
    }


def _task_owner(tasks_root: Path, name: str) -> tuple[str, dict[str, Any]] | None:
    """The task whose record names ``name`` as its ``<task>.<suffix>`` directory: the longest such id.

    The hot record wins over an archived one (a re-dispatch). Raises for a damaged record.
    """
    parts = name.split(".")
    for end in range(len(parts) - 1, 0, -1):
        task_id = ".".join(parts[:end])
        for path in (task_record_path(tasks_root, task_id), archived_task_record_path(tasks_root, task_id)):
            if path.is_file() and not path.is_symlink():
                record = read_task_record(path)
                if record is not None and record.get("task_id") == task_id:
                    return task_id, record
    return None


def _settled_at(record: dict[str, Any]) -> float | None:
    """When a released task finished, or None: unknown time never releases its state."""
    value = record.get("finished_at")
    try:
        finished = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        return None
    return finished.timestamp() if finished is not None and finished.tzinfo is not None else None


def _held_task(holds: list[dict[str, Any]], record: dict[str, Any], owner: str, name: str) -> bool:
    """A retention-list path naming the task store, this directory or any file of the task, or a record hold."""
    if record.get("worktree_retention_hold") is not False and "worktree_retention_hold" in record:
        return True  # true, or malformed: never read as permission
    return any(
        hold["parts"][0] == "tasks"
        and (len(hold["parts"]) == 1 or hold["parts"][1] == name or hold["parts"][1].startswith(f"{owner}."))
        for hold in holds
    )


def paths_in_use() -> frozenset[str]:
    """Every path a live process has as its cwd, an open descriptor or a mapping, as far as ``/proc`` shows.

    Best effort: a process the kernel will not let us inspect is skipped (tmp_sweep.process_snapshot reports
    it as incomplete). Unlinking a file such a process holds open does not disturb it. What a process finds by
    path is a socket or a FIFO, and :func:`prune_tree` keeps those by type, whatever this probe saw.
    """
    from lu_machinery.hygiene.tmp_sweep import process_snapshot  # tmp_sweep imports this module

    references, _complete = process_snapshot()
    return frozenset(str(path) for _pid, path in references)


def prune_tree(
    parent_fd: int, name: str, expected: os.stat_result, *, path: Path, device: int, mount_id: int,
    in_use: frozenset[str], keep_handoffs: bool,
) -> tuple[int, int]:
    """Unlink what is below ``name`` and remove what is left empty, keeping anything something may still use.

    Never removed: a socket or FIFO, a ``tmux-*`` or ``ssh-*`` directory, any path in ``in_use`` (a directory
    there keeps its whole tree) and, with ``keep_handoffs``, hand-off ``.md`` files. ``path`` is the real path
    of ``name``. Returns (allocated bytes freed, entries kept). Never follows a symlink or leaves the device
    and mount: each descent re-proves identity on its descriptor and each ``rmdir`` re-identifies the
    emptied directory (the task-scratch remover's checks). A directory without owner rwx gets it first.
    """
    if str(path) in in_use:
        return 0, 1
    try:
        fd = task_scratch._open_dir_at(parent_fd, name)
    except PermissionError:
        task_scratch.grant_owner_rwx(parent_fd, name, expected, device=device, mount_id=mount_id)
        fd = task_scratch._open_dir_at(parent_fd, name)
    try:
        held = task_scratch._verify_held_dir(fd, expected_dev=expected.st_dev, expected_ino=expected.st_ino,
                                             device=device, mount_id=mount_id, what=f"directory {name!r}")
        if stat.S_IMODE(held.st_mode) & stat.S_IRWXU != stat.S_IRWXU:
            os.fchmod(fd, stat.S_IMODE(held.st_mode) | stat.S_IRWXU)
        with os.scandir(fd) as entries:
            children = [(entry.name, entry.stat(follow_symlinks=False)) for entry in entries]
        freed = kept = 0
        for child, info in children:
            if (stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode) or str(path / child) in in_use
                    or (stat.S_ISDIR(info.st_mode) and child.startswith(_RENDEZVOUS_DIRS))
                    or (keep_handoffs and stat.S_ISREG(info.st_mode) and child.endswith(KEPT_SUFFIX))):
                kept += 1
            elif stat.S_ISDIR(info.st_mode):
                if info.st_dev != device:
                    raise ContainmentError(f"refusing to cross a mount boundary at {child!r}")
                child_freed, child_kept = prune_tree(fd, child, info, path=path / child, device=device,
                                                     mount_id=mount_id, in_use=in_use, keep_handoffs=keep_handoffs)
                freed, kept = freed + child_freed, kept + child_kept
            else:
                os.unlink(child, dir_fd=fd)
                freed += info.st_blocks * 512 if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else 0
        if not kept:
            task_scratch._rmdir_held(parent_fd, name, held_fd=fd, held_st=held)
        return freed, kept
    finally:
        os.close(fd)


def prune_settled_tasks(
    tasks_root: Path,
    *,
    holds: list[dict[str, Any]],
    settled_days: float = DEFAULT_SETTLED_DAYS,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove settled tasks' ``<task>.*`` directories, keeping hand-off ``.md`` files; ``dry_run`` only lists them.

    Sockets, FIFOs, agent socket directories and paths a live process uses stay too (:func:`prune_tree`).

    A directory is pruned only when its owner record (hot, else archived) has a released terminal status
    and a completion time older than ``settled_days``, no hold names it, and nothing below it was written
    since that cutoff. The record is read again under its task-state lock before removal, which keeps that
    lock: a re-dispatch of the same id cannot start in between. A directory with no owner record, a damaged
    record or an unknown completion time is left alone and counted, never guessed.
    """
    if not math.isfinite(settled_days) or settled_days <= 0:
        raise ValueError("settled days must be finite and positive")
    if tasks_root.is_symlink() or (os.path.lexists(tasks_root) and not tasks_root.is_dir()):
        raise ValueError("task store must be a real directory")
    now = time.time() if now is None else now
    cutoff = now - settled_days * 86400
    counts = dict.fromkeys(("removed", "would_remove", "live", "held", "settling", "fresh", "undated",
                            "unowned", "owner_unreadable", "unknown", "failed"), 0)
    rows: list[dict[str, Any]] = []
    result = {"schema": "batch-state-task-prune.v1", "dry_run": dry_run, "settled_days": settled_days,
              "counts": counts, "rows": rows}
    if not tasks_root.exists():
        return {**result, "bytes": 0}  # a machine that never dispatched has nothing to prune
    tasks_root = tasks_root.resolve(strict=True)
    mounts = mount_points()
    in_use = None if dry_run else paths_in_use()
    root_fd = os.open(tasks_root, task_scratch._DIR_OPEN_FLAGS | os.O_CLOEXEC)
    try:
        device, mount_id = os.fstat(root_fd).st_dev, task_scratch.fd_mount_id(root_fd)
        with os.scandir(root_fd) as entries:
            names = sorted(entry.name for entry in entries
                           if not entry.name.startswith(".") and entry.name != ARCHIVE_DIR_NAME
                           and entry.is_dir(follow_symlinks=False))
        for name in names:
            row: dict[str, Any] = {"path": f"batch_state/tasks/{name}/"}
            try:
                owned = _task_owner(tasks_root, name)
            except (OSError, ValueError, RecursionError) as error:
                counts["owner_unreadable"] += 1
                rows.append({**row, "status": "owner_unreadable", "reason": type(error).__name__})
                continue
            if owned is None:
                counts["unowned"] += 1
                continue
            owner, record = owned
            settled = _settled_at(record)
            if record.get("status") not in RELEASED_TASK_STATUSES:
                status = "live"
            elif _held_task(holds, record, owner, name):
                status = "held"
            elif settled is None:
                status = "undated"
            elif settled >= cutoff:
                status = "settling"
            else:
                status = None
            if status is not None:
                counts[status] += 1
                continue
            allocated, newest, reason = tree_activity(tasks_root / name, cutoff)
            if mounts is None or mount_id is None:
                reason = "mount_probe_unknown"
            elif task_scratch._mounts_below(tasks_root / name, mounts):
                reason = "mount_point"
            if reason:
                counts["unknown"] += 1
                rows.append({**row, "status": "unknown", "reason": reason})
                continue
            if newest >= cutoff:
                counts["fresh"] += 1
                continue
            if dry_run:
                counts["would_remove"] += 1
                rows.append({**row, "status": "would_remove", "task_id": owner, "bytes": allocated})
                continue
            try:
                with task_state_lock(task_record_path(tasks_root, owner)):
                    again = _task_owner(tasks_root, name)
                    if (again is None or again[0] != owner or again[1].get("status") not in RELEASED_TASK_STATUSES
                            or _settled_at(again[1]) != settled or _held_task(holds, again[1], owner, name)):
                        raise ContainmentError("the owner record changed before removal")
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                        raise ContainmentError("not a directory this user owns")
                    if mount_id is None or in_use is None:  # classified unknown above; never delete without it
                        raise ContainmentError("mount information unavailable")
                    freed, kept = prune_tree(root_fd, name, info, path=tasks_root / name, device=device,
                                             mount_id=mount_id, in_use=in_use, keep_handoffs=True)
            except (OSError, ValueError, RecursionError, task_scratch.TaskScratchError) as error:
                counts["failed"] += 1
                rows.append({**row, "status": "failed", "task_id": owner, "reason": f"{type(error).__name__}: {error}"[:300]})
                continue
            counts["removed"] += 1
            rows.append({**row, "status": "removed", "task_id": owner, "bytes": freed, "kept": kept})
    finally:
        os.close(root_fd)
    return {**result, "bytes": sum(row["bytes"] for row in rows if row["status"] in {"removed", "would_remove"})}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prune settled tasks' bulky state, then report top-level batch_state directories with no write for N days.\n"
            "Removes, after --settled-days, everything in a settled task's tasks/<task>.* directories except hand-off\n"
            ".md files; records, .result files, live or held tasks and tasks/archive stay. --dry-run lists instead.\n"
            "Top-level stale directories are only reported; hold evidence in the retention list."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  .venv/bin/python -m {_mod('hygiene', 'batch_state_retention')} --dry-run\n"
            f"  .venv/bin/python -m {_mod('hygiene', 'batch_state_retention')} --json --settled-days 3\n"
            "Outputs: stdout table (or --json) with the prune rows and the staleness rows; no files written.\n"
            "Exit codes: 0 complete; 1 some directories could not be scanned or removed; "
            "2 invalid arguments or malformed retention list.\n"
            f"Related: #9737; CLEANUP-WORKFILES-1118; {_mp('hygiene/batch_state_retention.json')}; "
            "docs/runbooks/tmp-retention.md."
        ),
    )
    parser.add_argument(
        "--batch-root",
        type=Path,
        help="batch_state directory to scan (default: the primary checkout's batch_state; example fixture/batch_state).",
    )
    parser.add_argument(
        "--retention-list",
        type=Path,
        default=RETENTION_LIST,
        help=f"Evidence-retention list (default {_mp('hygiene/batch_state_retention.json')}).",
    )
    parser.add_argument(
        "--settled-days",
        type=float,
        default=DEFAULT_SETTLED_DAYS,
        help=f"Days after a task settled before its directories are pruned (default {DEFAULT_SETTLED_DAYS:g}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List what the prune would remove; delete nothing (default: remove)."
    )
    parser.add_argument(
        "--stale-days", type=float, default=7, help="Days without any write before a directory is stale (default 7)."
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON (default Markdown table).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    batch_root = args.batch_root or default_batch_root()
    try:
        holds = load_holds(args.retention_list)
        pruned = prune_settled_tasks(batch_root / "tasks", holds=holds, settled_days=args.settled_days,
                                     dry_run=args.dry_run)
        result = report(batch_root, holds=holds, stale_days=args.stale_days)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps({**result, "task_prune": pruned}, indent=2, sort_keys=True))
    else:
        print("| Task directory | Status | Bytes | Note |")
        print("|---|---|---:|---|")
        for row in pruned["rows"]:
            print(f"| {row['path']} | {row['status']} | {row.get('bytes', '')} | {row.get('reason', '')} |")
        counts = " ".join(f"{key}={value}" for key, value in pruned["counts"].items())
        print(f"settled_days={pruned['settled_days']} dry_run={pruned['dry_run']} {counts} bytes={pruned['bytes']}")
        print("| Directory | Status | Allocated bytes | Newest write (UTC) | Note |")
        print("|---|---|---:|---|---|")
        for row in result["rows"]:
            note = "; ".join(f"{h['reason']} until {h.get('until', 'released')}" for h in row.get("holds", []))
            print(
                f"| {row['path']} | {row['status']} | {row.get('bytes', '')} | {row['newest_write_utc']} "
                f"| {note or row.get('reason', '')} |"
            )
        counts = " ".join(f"{key}={value}" for key, value in result["counts"].items())
        print(f"stale_days={result['stale_days']} {counts} stale_bytes={result['stale_bytes']}")
    return 1 if result["counts"]["unknown"] or pruned["counts"]["unknown"] or pruned["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
