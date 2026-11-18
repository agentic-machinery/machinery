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
# away, a transient error. Every other outcome (removed, absent, refused, kept) is final.
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
_OUTCOME_FIELDS = ("task_scratch_disposition", "task_scratch_disposition_reason", "task_scratch_bytes")


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


def settle(record: dict, provider_gone: Callable[[], bool], kept: str | None = None, *, final: bool = False) -> None:
    """Remove the recorded scratch once ``provider_gone()`` proves the provider's process tree gone.

    The scratch is the worker's temp area, never deliverable state, so a kept worktree does not keep it.
    ``provider_gone`` is asked only when there is a recorded scratch to remove, and anything but ``True`` keeps
    it. ``kept``, a reason the caller has for keeping it, keeps it too: as ``retained`` (retried), or as ``kept``
    (final) with ``final``. ``task_scratch_bytes`` is what the pass that recorded the disposition found: the
    allocated bytes of the directories and files whose every name lies in the scratch, measured before removal
    (None when the pass did not measure). A refusal or failure is recorded and never raised.
    """
    try:
        disposition, reason, found = _remove(record, provider_gone, kept, final)
    except Exception as exc:  # recorded, never raised: settlement must still publish the record
        disposition, reason, found = "failed", f"{type(exc).__name__}: {exc}"[:300], None
    record.update(task_scratch_disposition=disposition, task_scratch_disposition_reason=reason,
                  task_scratch_bytes=found)


# (disposition, reason, allocated bytes this pass found, or None when it did not measure)
_Outcome = tuple[str, str, int | None]


def _remove(record: dict, provider_gone: Callable[[], bool], kept: str | None, final: bool) -> _Outcome:
    task_id, created = record.get("task_id"), record.get("task_scratch")
    if not isinstance(task_id, str):
        return "refused", "task id missing", None
    try:
        path = environment.task_scratch_path(task_id)
    except ValueError:
        return "refused", "task id fails validation", None
    if created is None:
        if os.path.lexists(path):
            return "refused", "the scratch directory was not created by this run", None
        return "absent", "no scratch created by this run", None
    if (not isinstance(created, dict) or created.get("path") != str(path)
            or type(created.get("dev")) is not int or type(created.get("ino")) is not int):
        return "refused", "the recorded scratch identity is malformed", None
    if kept:
        return ("kept" if final else "retained"), kept, None
    try:
        gone = provider_gone() is True
    except Exception:  # unknown liveness is not proof the provider is gone
        gone = False
    if not gone:
        return "retained", "the provider is not proven gone", None
    base_fd = os.open(path.parent, _DIR_FLAGS)
    try:
        return _remove_held(base_fd, path.name, (created["dev"], created["ino"]))
    finally:
        os.close(base_fd)


def _census_outcome(exc: task_scratch.ContainmentError) -> str:
    """A census refusal is final, except unavailable mount information, which a later pass may read."""
    return "failed" if "mount information unavailable" in str(exc) else "refused"


def _remove_held(base_fd: int, name: str, identity: tuple[int, int]) -> _Outcome:
    try:
        info = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent", "no scratch directory", None
    if (info.st_dev, info.st_ino) != identity:
        return "refused", "the scratch directory is not the one this run created", None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return "refused", "the scratch path is not a directory this user owns", None
    device, mount_id, mounts = os.fstat(base_fd).st_dev, task_scratch.fd_mount_id(base_fd), task_scratch.mount_points()
    if info.st_dev != device:
        return "refused", "the scratch directory is on another filesystem", None
    if mount_id is None or mounts is None:
        return "failed", "mount information unavailable", None
    # /proc names real paths: compare against the held base's, whatever symlinks lead to it.
    base = Path(os.readlink(f"/proc/self/fd/{base_fd}"))
    if task_scratch._mounts_below(base / name, mounts):
        return "refused", "a mount point lies below the scratch directory", None
    try:
        found = _census(base_fd, name, info, device=device, mount_id=mount_id)
    except task_scratch.ContainmentError as exc:
        return _census_outcome(exc), str(exc)[:300], None
    if found.rendezvous:
        reason = f"provider gone; kept whole for {found.rendezvous} socket, FIFO or tmux/ssh entry"
        return "in_use", reason, found.allocated
    holders = _holders(found, str(base / name))
    if holders is None:
        return "failed", "the process table cannot be listed", found.allocated
    if holders:
        return "in_use", f"provider gone; kept whole while {holders} live process(es) hold part of it", found.allocated
    try:
        task_scratch._remove_invocation_dir(base_fd, name, namespace=base, expected_dev=info.st_dev,
                                            expected_ino=info.st_ino)
    except (task_scratch.ContainmentError, OSError) as exc:
        # Retried: the next pass's census decides afresh, and only a census refusal is final.
        return "failed", f"{type(exc).__name__}: {exc}"[:300], found.allocated
    return "removed", "provider gone", found.allocated


@dataclass
class _Census:
    """One walk of the scratch: its inodes and link counts, its rendezvous entries and the bytes it holds."""

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
    def owned(self) -> set[tuple[int, int]]:
        """Directories and the files whose every name lies in the tree."""
        return {key for key, (_size, links, seen, is_dir) in self._links.items() if is_dir or seen >= links}

    @property
    def shared(self) -> set[tuple[int, int]]:
        """Files also linked from outside the tree, such as a package cache's link into a virtual environment."""
        return set(self._links) - self.owned

    @property
    def allocated(self) -> int:
        """Allocated bytes of the owned inodes: a shared file keeps its outside name and its blocks."""
        return sum(self._links[key][0] for key in self.owned)


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


def _grant_owner_rwx(dir_fd: int, name: str, info: os.stat_result, *, device: int, mount_id: int) -> None:
    """Give owner rwx to the directory ``name`` below ``dir_fd`` through a descriptor proven to be it.

    As ``task_scratch.grant_owner_rwx``, except that unavailable mount information is reported as such (a later
    pass may read it), apart from a proven mismatch (final).
    """
    fd = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        held = os.fstat(fd)
        if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino) or held.st_uid != os.geteuid():
            raise task_scratch.ContainmentError(f"directory {name!r} changed before its permission repair")
        held_mount = task_scratch.fd_mount_id(fd)
        if held_mount is None:
            raise task_scratch.ContainmentError(f"mount information unavailable for {name!r}; refusing to repair")
        if held.st_dev != device or held_mount != mount_id:
            raise task_scratch.ContainmentError(f"refusing to repair permissions across a mount at {name!r}")
        os.chmod(f"/proc/self/fd/{fd}", stat.S_IMODE(held.st_mode) | stat.S_IRWXU)
    finally:
        os.close(fd)


def _open_proven(dir_fd: int, name: str, info: os.stat_result, *, device: int, mount_id: int) -> int:
    if info.st_dev != device:
        raise task_scratch.ContainmentError(f"refusing to cross a mount boundary at {name!r}")
    try:
        fd = task_scratch._open_dir_at(dir_fd, name)
    except PermissionError:
        if info.st_uid != os.geteuid():
            raise task_scratch.ContainmentError(f"directory {name!r} belongs to another user") from None
        _grant_owner_rwx(dir_fd, name, info, device=device, mount_id=mount_id)
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


def _holders(found: _Census, root: str) -> int | None:
    """Count the other processes holding part of the scratch at ``root``; None when ``/proc`` cannot be listed.

    A holder is a working directory, root, executable, open descriptor or file mapping, matched by device and
    inode, never by name, so a renamed, deleted or oddly named file is still seen, through the process's view
    and each of its threads' (a leader that exited before its threads). A file also linked from outside counts
    unless the reference is proven to come through an outside name. A process the kernel will not let this user
    inspect is skipped: the caller's provider-exit proof, not this scan, covers the task's own processes.
    """
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    me, owned, shared = str(os.getpid()), found.owned, found.shared
    return sum(1 for pid in pids if pid != me and _holds(Path("/proc", pid), owned, shared, root))


def _holds(proc: Path, owned: set[tuple[int, int]], shared: set[tuple[int, int]], root: str) -> bool:
    views = [proc]
    with contextlib.suppress(OSError):
        views += [proc / "task" / tid for tid in os.listdir(proc / "task")]
    return any(_view_holds(view, owned, shared, root) for view in views)


def _inside(reference: str | None, root: str) -> bool:
    """Whether a reference may come through the scratch: unknown, relative, or below ``root`` as written."""
    if reference is None or not reference.startswith("/"):
        return True
    spellings = (reference, reference.removesuffix(" (deleted)"))
    return any(path == root or path.startswith(root + "/") for path in spellings)


def _view_holds(proc: Path, owned: set[tuple[int, int]], shared: set[tuple[int, int]], root: str) -> bool:
    links = [proc / "cwd", proc / "root", proc / "exe"]
    with contextlib.suppress(OSError):
        links += [proc / "fd" / fd for fd in os.listdir(proc / "fd")]
    for link in links:
        with contextlib.suppress(OSError):
            held = os.stat(link)
            key = (held.st_dev, held.st_ino)
            if key in owned:
                return True
            if key in shared:
                try:
                    reference: str | None = os.readlink(link)
                except OSError:
                    reference = None
                if _inside(reference, root):
                    return True
    try:
        lines = (proc / "maps").read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return False
    for line in lines:
        fields = line.split(maxsplit=5)
        with contextlib.suppress(IndexError, ValueError):
            major, minor = fields[3].split(":")
            key = (os.makedev(int(major, 16), int(minor, 16)), int(fields[4]))
            if key in owned or (key in shared and _inside(fields[5] if len(fields) == 6 else None, root)):
                return True
    return False
