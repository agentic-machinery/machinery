"""A dispatched write worker's task scratch: its ``TMPDIR``, created before launch and removed at settlement.

The scratch is ``environment.task_scratch_path(task_id)``, exported as ``LU_TASK_SCRATCH`` and, for a
workspace-write worker, as ``TMPDIR`` (``environment.child_env``). :func:`create` makes it before the provider
starts and returns the identity the task record keeps as ``task_scratch``. :func:`settle` removes exactly that
directory, whatever the task's terminal status, once the caller has proven the provider's whole process tree gone,
and records ``task_scratch_disposition``, ``task_scratch_disposition_reason`` and ``task_scratch_bytes``.
:func:`pending` tells the scheduled sweep which finished tasks to retry.

Removal takes the whole tree or nothing. A tree that holds a socket, a FIFO or a ``tmux-*``/``ssh-*`` entry,
or any part of which a live process holds, is kept whole and retried.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lu_machinery.common import task_scratch
from lu_machinery.dispatcher import environment, store

# Outcomes the scheduled sweep retries: a provider not yet proven gone, a holder or rendezvous entry that may go
# away, a transient error. Every other outcome is final.
RETRY = frozenset({"retained", "in_use", "failed"})
# Names tmux and ssh-agent give the directories that hold their sockets.
_AGENT_PREFIXES = ("tmux-", "ssh-")
_DIR_FLAGS = task_scratch._DIR_OPEN_FLAGS | os.O_CLOEXEC


def create(task_id: str) -> dict:
    """Create the task's scratch, private and exclusively; return the identity :func:`settle` removes.

    An entry already at the name is never adopted (``FileExistsError``), since a task id can name anything in the
    base. Linux cannot create and open a directory in one step, so the directory held afterwards is accepted only
    when it is empty, private to this user (mode 0700) and on the base's own mount: such a directory is the
    run's fresh one or indistinguishable from it, and everything it later holds arrived as the task's temp
    output. Anything else raises ``ValueError`` and the launch fails closed, and so does a path longer than
    ``environment.TASK_SCRATCH_PATH_MAX``: a Unix socket below ``TMPDIR`` (Python's multiprocessing makes one)
    must fit the kernel's 107-byte socket path.
    """
    path = environment.task_scratch_path(task_id)
    if (size := len(os.fsencode(str(path)))) > environment.TASK_SCRATCH_PATH_MAX:
        raise ValueError(f"{path} is {size} bytes, over the {environment.TASK_SCRATCH_PATH_MAX}-byte budget that "
                         "leaves room for Unix sockets below TMPDIR")
    base_fd = os.open(path.parent, _DIR_FLAGS)
    try:
        os.mkdir(path.name, 0o700, dir_fd=base_fd)
        fd = os.open(path.name, _DIR_FLAGS, dir_fd=base_fd)
        try:
            held, base = os.fstat(fd), os.fstat(base_fd)
            with os.scandir(fd) as entries:
                empty = next(entries, None) is None
            mount, base_mount = task_scratch.fd_mount_id(fd), task_scratch.fd_mount_id(base_fd)
        finally:
            os.close(fd)
    finally:
        os.close(base_fd)
    if not (empty and mount is not None and mount == base_mount and held.st_dev == base.st_dev
            and held.st_uid == os.geteuid() and stat.S_IMODE(held.st_mode) == 0o700):
        raise ValueError(f"{path} is not a fresh private directory of uid {os.geteuid()} on its base's mount")
    return {"path": str(path), "dev": held.st_dev, "ino": held.st_ino}


# What settlement records about a scratch. A new scratch starts with none of them.
_OUTCOME_FIELDS = ("task_scratch_disposition", "task_scratch_disposition_reason", "task_scratch_bytes",
                   "task_scratch_unmeasured_bytes")


def start(record: dict, task_id: str) -> None:
    """Create the task's scratch (:func:`create`) and record it, dropping any earlier scratch outcome."""
    identity = create(task_id)
    for key in _OUTCOME_FIELDS:
        record.pop(key, None)
    record["task_scratch"] = identity


def settleable(record: dict) -> bool:
    """Whether settlement may (re)settle this scratch: never settled, or a retried outcome.

    A malformed disposition reads as final, so a damaged record is left as it is.
    """
    if "task_scratch_disposition" not in record:
        return True
    disposition = record["task_scratch_disposition"]
    return isinstance(disposition, str) and disposition in RETRY


def pending(record: dict) -> bool:
    """Whether a terminal task's recorded scratch still waits: never settled (a dead supervisor) or retried.

    Field types are checked before any membership test. Only a known terminal status counts; a malformed or
    unknown one reads as live.
    """
    status = record.get("status")
    return (isinstance(status, str) and status in store.TERMINAL and isinstance(record.get("task_scratch"), dict)
            and settleable(record))


def settle(record: dict, provider_gone: Callable[[], bool], kept: str | None = None) -> None:
    """Remove the recorded scratch once ``provider_gone()`` proves the provider's process tree gone.

    The scratch is the worker's temp area, never deliverable state, so a kept worktree does not keep it.
    ``provider_gone`` is asked only when there is a recorded scratch to remove, and anything but ``True`` keeps
    it. ``kept``, a reason the caller has for keeping it (state that lives in it), keeps it too.
    ``task_scratch_bytes`` counts the allocated bytes every pass so far has freed. A removal that fails partway
    counts what it found less what is left; when what is left cannot be measured, it records what it
    found as ``task_scratch_unmeasured_bytes`` and the next pass that measures the tree counts the difference.
    A refusal or failure is recorded and never raised.
    """
    earlier, carried = (_count(record.get(key)) for key in ("task_scratch_bytes", "task_scratch_unmeasured_bytes"))
    try:
        disposition, reason, freed, carry = _remove(record, provider_gone, carried, kept)
    except Exception as exc:  # recorded, never raised: settlement must still publish the record
        disposition, reason, freed, carry = "failed", f"{type(exc).__name__}: {exc}"[:300], 0, carried
    record.update(task_scratch_disposition=disposition, task_scratch_disposition_reason=reason,
                  task_scratch_bytes=(earlier or 0) + freed)
    if carry is None:
        record.pop("task_scratch_unmeasured_bytes", None)
    else:
        record["task_scratch_unmeasured_bytes"] = carry


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


# (disposition, reason, bytes this pass freed, bytes a failed removal left unmeasured)
_Outcome = tuple[str, str, int, int | None]


def _remove(record: dict, provider_gone: Callable[[], bool], carried: int | None, kept: str | None) -> _Outcome:
    task_id, created = record.get("task_id"), record.get("task_scratch")
    if not isinstance(task_id, str):
        return "refused", "task id missing", 0, carried
    try:
        path = environment.task_scratch_path(task_id)
    except ValueError:
        return "refused", "task id fails validation", 0, carried
    if created is None:
        if os.path.lexists(path):
            return "refused", "the scratch directory was not created by this run", 0, carried
        return "absent", "no scratch created by this run", 0, carried
    if (not isinstance(created, dict) or created.get("path") != str(path)
            or type(created.get("dev")) is not int or type(created.get("ino")) is not int):
        return "refused", "the recorded scratch identity is malformed", 0, carried
    if kept:
        return "retained", kept, 0, carried
    try:
        gone = provider_gone() is True
    except Exception:  # unknown liveness is not proof the provider is gone
        gone = False
    if not gone:
        return "retained", "the provider is not proven gone", 0, carried
    base_fd = os.open(path.parent, _DIR_FLAGS)
    try:
        return _remove_held(base_fd, path.name, (created["dev"], created["ino"]), carried)
    finally:
        os.close(base_fd)


def _containment(exc: task_scratch.ContainmentError) -> str:
    """A refusal is final, except unavailable mount information, which a later pass may read."""
    return "failed" if "mount information unavailable" in str(exc) else "refused"


def _remove_held(base_fd: int, name: str, identity: tuple[int, int], carried: int | None) -> _Outcome:
    try:
        info = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
    except FileNotFoundError:
        # Gone since a removal that failed unmeasured: that pass, or what followed it, freed what it found.
        return "absent", "no scratch directory", carried or 0, None
    if (info.st_dev, info.st_ino) != identity:
        return "refused", "the scratch directory is not the one this run created", 0, carried
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return "refused", "the scratch path is not a directory this user owns", 0, carried
    device, mount_id, mounts = os.fstat(base_fd).st_dev, task_scratch.fd_mount_id(base_fd), task_scratch.mount_points()
    if info.st_dev != device:
        return "refused", "the scratch directory is on another filesystem", 0, carried
    if mount_id is None or mounts is None:
        return "failed", "mount information unavailable", 0, carried
    # /proc names real paths: compare against the held base's, whatever symlinks lead to it.
    base = Path(os.readlink(f"/proc/self/fd/{base_fd}"))
    if task_scratch._mounts_below(base / name, mounts):
        return "refused", "a mount point lies below the scratch directory", 0, carried
    try:
        found = _census(base_fd, name, info, device=device, mount_id=mount_id)
    except task_scratch.ContainmentError as exc:
        return _containment(exc), str(exc)[:300], 0, carried
    # What a removal that failed unmeasured had already freed is what it found less what is here now.
    reconciled = max(carried - found.allocated, 0) if carried is not None else 0
    if found.rendezvous:
        return ("in_use", f"provider gone; kept whole for {found.rendezvous} socket, FIFO or tmux/ssh entry",
                reconciled, None)
    holders = _holders(found.owned_inodes)
    if holders is None:
        return "failed", "the process table cannot be listed", reconciled, None
    if holders:
        return "in_use", f"provider gone; kept whole while {holders} live process(es) hold part of it", reconciled, None
    try:
        task_scratch._remove_invocation_dir(base_fd, name, namespace=base, expected_dev=info.st_dev,
                                            expected_ino=info.st_ino)
    except task_scratch.ContainmentError as exc:
        disposition, detail = _containment(exc), str(exc)
    except OSError as exc:
        disposition, detail = "failed", f"{type(exc).__name__}: {exc.strerror or exc}"
    else:
        return "removed", "provider gone", reconciled + found.allocated, None
    # A removal that stopped partway has still freed what it unlinked: count it, or carry what it found.
    freed = _freed_after_failure(base_fd, name, info, found, device=device, mount_id=mount_id)
    if freed is None:
        return disposition, f"{detail}; bytes freed by this pass not yet measured"[:300], reconciled, found.allocated
    return disposition, detail[:300], reconciled + freed, None


def _freed_after_failure(base_fd: int, name: str, info: os.stat_result, before: _Census, *, device: int,
                         mount_id: int) -> int | None:
    """Bytes a removal freed before it stopped: what it found less what is left; None when that is unknown."""
    try:
        now = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
    except FileNotFoundError:
        return before.allocated
    except OSError:
        return None
    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
        return None
    try:
        left = _census(base_fd, name, now, device=device, mount_id=mount_id)
    except (task_scratch.ContainmentError, OSError, RecursionError):
        return None
    return max(before.allocated - left.allocated, 0)


@dataclass
class _Census:
    """One walk of the scratch: its inodes and link counts, its rendezvous entries and the bytes removal frees."""

    rendezvous: int = 0
    # (device, inode) -> [allocated bytes, link count, names seen in the tree, is a directory]
    _links: dict[tuple[int, int], list[int]] = field(default_factory=dict)

    def add(self, name: str | None, info: os.stat_result) -> None:
        """Count one entry; ``name`` None for the scratch itself, whose own name is the task id's."""
        key = (info.st_dev, info.st_ino)
        if (stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode)
                or (name is not None and name.startswith(_AGENT_PREFIXES))):
            self.rendezvous += 1
        entry = self._links.setdefault(key, [info.st_blocks * 512, info.st_nlink, 0, stat.S_ISDIR(info.st_mode)])
        entry[2] += 1

    @property
    def owned_inodes(self) -> set[tuple[int, int]]:
        """The inodes removal can take from a holder: directories and files with every name in the tree.

        A file hard-linked from outside (a package cache's link into a virtual environment, say) keeps its
        outside name and its data, so a process mapping that cache file holds nothing removal takes.
        """
        return {key for key, (_size, links, seen, is_dir) in self._links.items() if is_dir or seen >= links}

    @property
    def allocated(self) -> int:
        """Allocated bytes of the directories and of every inode whose names all lie in the tree.

        A file hard-linked from outside (a package cache's link, say) keeps its blocks, so it is not counted.
        """
        return sum(size for size, links, seen, is_dir in self._links.values() if is_dir or seen >= links)


def _census(base_fd: int, name: str, info: os.stat_result, *, device: int, mount_id: int) -> _Census:
    """Walk the scratch without leaving its device or mount, giving an owner-locked directory owner rwx.

    Every descent re-proves the directory it opened (identity, device, mount id), as the remover does. Owner rwx
    on a directory of ours is what removal would grant anyway; without it a locked directory would hide its
    contents from the rendezvous and holder checks.
    """
    found = _Census()
    found.add(None, info)
    fd = _open_proven(base_fd, name, info, device=device, mount_id=mount_id)
    try:
        _walk(fd, found, device=device, mount_id=mount_id)
    finally:
        os.close(fd)
    return found


def _open_proven(dir_fd: int, name: str, info: os.stat_result, *, device: int, mount_id: int) -> int:
    if info.st_dev != device:
        raise task_scratch.ContainmentError(f"refusing to cross a mount boundary at {name!r}")
    try:
        fd = task_scratch._open_dir_at(dir_fd, name)
    except PermissionError:
        if info.st_uid != os.geteuid():
            raise task_scratch.ContainmentError(f"directory {name!r} belongs to another user") from None
        task_scratch.grant_owner_rwx(dir_fd, name, info, device=device, mount_id=mount_id)
        fd = task_scratch._open_dir_at(dir_fd, name)
    try:
        held = task_scratch._verify_held_dir(fd, expected_dev=info.st_dev, expected_ino=info.st_ino, device=device,
                                             mount_id=mount_id, what=f"directory {name!r}")
        if stat.S_IMODE(held.st_mode) & stat.S_IRWXU != stat.S_IRWXU:
            # Readable without search (0400, 0600) still hides its entries' details: repair through the proven fd.
            os.fchmod(fd, stat.S_IMODE(held.st_mode) | stat.S_IRWXU)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk(dir_fd: int, found: _Census, *, device: int, mount_id: int) -> None:
    with os.scandir(dir_fd) as it:
        entries = [(entry.name, entry.stat(follow_symlinks=False)) for entry in it]
    for name, info in entries:
        found.add(name, info)
        if stat.S_ISDIR(info.st_mode):
            child = _open_proven(dir_fd, name, info, device=device, mount_id=mount_id)
            try:
                _walk(child, found, device=device, mount_id=mount_id)
            finally:
                os.close(child)


def _holders(inodes: set[tuple[int, int]]) -> int | None:
    """Count the other processes holding one of ``inodes``; None when ``/proc`` cannot be listed.

    A holder is a working directory, root, executable, open descriptor or file mapping. Each is compared by
    device and inode, never by name, so a renamed, deleted or oddly named file is still seen. A process the
    kernel will not let this user inspect is skipped: the caller's provider-exit proof, not this scan, covers
    the task's own processes.
    """
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    me = str(os.getpid())
    return sum(1 for pid in pids if pid != me and _holds(Path("/proc", pid), inodes))


def _holds(proc: Path, inodes: set[tuple[int, int]]) -> bool:
    """Whether the process holds one of ``inodes``, seen through it and through each of its threads.

    A thread's view stays readable when the process view is not (a leader that exited before its threads).
    """
    views = [proc]
    with contextlib.suppress(OSError):
        views += [proc / "task" / tid for tid in os.listdir(proc / "task")]
    return any(_view_holds(view, inodes) for view in views)


def _view_holds(proc: Path, inodes: set[tuple[int, int]]) -> bool:
    links = [proc / "cwd", proc / "root", proc / "exe"]
    with contextlib.suppress(OSError):
        links += [proc / "fd" / fd for fd in os.listdir(proc / "fd")]
    for link in links:
        with contextlib.suppress(OSError):
            held = os.stat(link)
            if (held.st_dev, held.st_ino) in inodes:
                return True
    try:
        lines = (proc / "maps").read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        fields = line.split(maxsplit=5)
        with contextlib.suppress(IndexError, ValueError):
            major, minor = fields[3].split(":")
            if (os.makedev(int(major, 16), int(minor, 16)), int(fields[4])) in inodes:
                return True
    return False
