"""Detached supervision, client waits and process-group cancellation."""

import contextlib
import ctypes
import hashlib
import json
import logging
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psutil

from lu_machinery.agent_runtime import bounded_advisory, model_pause
from lu_machinery.agent_runtime.codex_accounts import CodexAccountRefused, resolve_codex_account_label
from lu_machinery.agent_runtime.read_only_tmp import create_dispatch_lease, discard_dispatch_lease, dispatch_lease
from lu_machinery.common import json_depth, task_scratch
from lu_machinery.common.repo_root import in_tree_source, machinery_path, primary_root, state_root
from lu_machinery.common.scratch import ensure_scratch_root
from lu_machinery.common.task_scratch import TaskScratchError
from lu_machinery.dispatcher import (
    admission,
    agy_sandbox,
    git,
    headroom,
    ownership,
    providers,
    runtime_context,
    scratch,
    store,
    write_boundary,
)
from lu_machinery.dispatcher.environment import child_env
from lu_machinery.dispatcher.types import ParsedResult, TaskSpec
from lu_machinery.dispatcher.watchdog import (
    TERMINAL_EXIT_GRACE_S,
    DeadlineExpired,
    communicate,
    deadlines,
    liveness_paths,
)
from lu_machinery.fleet import claude_account_lane
from lu_machinery.review.model_catalog import advisory_role_policy
from lu_machinery.review.product_hooks import attempt_hooks


def _seal_advisory_result(record: dict, result: Path) -> None:
    """Seal an advisory decision's result as written (``bounded_advisory.seal_advisor_result``).

    A seal that cannot be computed is left out, so every consumer refuses the
    result as unsealed; it never stops the record from settling.
    """
    try:
        fence = advisory_role_policy(record["advisory_role"]).fence
        response = result.read_bytes().decode("utf-8")
    except (KeyError, OSError, UnicodeDecodeError, ValueError) as exc:
        record["advisory_seal_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return
    record[bounded_advisory.SEAL_FIELD] = bounded_advisory.seal_advisor_result(
        response, model=record.get("model"), run_nonce=record.get("run_nonce"), fence=fence
    )


# Re-creatable caches a worker leaves in its own task output directories. Settlement removes these directories
# and keeps every log, result, receipt and hand-off beside them (C-4946(a)).
TASK_CACHE_DIRS = frozenset({"venv", ".venv", "buildvenv", "npm-cache", ".npm", "node_modules", "pip-cache", ".cache",
                             "__pycache__"})


def _readonly_parent_empty_cache(dir_fd: int, name: str) -> bool:
    """True when ``name`` is an empty directory we own and its parent lacks owner write.

    Unlinking that directory needs write permission on the parent. The parent is not a cache,
    so settlement must not chmod it. An empty leftover is not residue: the owned contents are
    already gone, and a later hourly pass has nothing further it may remove.
    """
    if stat.S_IMODE(os.fstat(dir_fd).st_mode) & stat.S_IWUSR:
        return False
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return False
    try:
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        with os.scandir(child) as entries:
            return next(entries, None) is None
    finally:
        os.close(child)


def _remove_cache_entry(dir_fd: int, name: str) -> None:
    """Unlink a cache-named link, or remove one cache directory, relative to ``dir_fd``.

    A link is unlinked and never followed. A directory is removed by the same remover
    task scratch settlement uses (``scratch.settle``): this directory gets owner rwx when it lacks it, children
    are repaired on descent, and another device or a mount point is refused before anything
    is unlinked. The chmod and the deletion stay inside ``name``.

    A parent that lacks owner write is not chmod'd. When that parent is not itself a cache,
    the cache contents are still removed and the empty directory is left in place. That
    leftover is not a failure, so it is not recorded as residue.
    """
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    parent = os.fstat(dir_fd)
    mount_id = task_scratch.fd_mount_id(dir_fd)
    if info.st_dev != parent.st_dev:
        raise task_scratch.ContainmentError(f"cache {name!r} is on another filesystem")
    if mount_id is None:
        raise task_scratch.ContainmentError("mount information unavailable")
    if stat.S_IMODE(info.st_mode) & stat.S_IRWXU != stat.S_IRWXU:
        # A read-only top directory would refuse to give up its entries; children are repaired on descent.
        task_scratch.grant_owner_rwx(dir_fd, name, info, device=parent.st_dev, mount_id=mount_id)
    # The held descriptor's path is what mountinfo names. The scratch remover refuses a mount
    # below this directory before it unlinks anything, and it never chmods outside the directory.
    try:
        task_scratch._remove_invocation_dir(
            dir_fd,
            name,
            namespace=Path(os.readlink(f"/proc/self/fd/{dir_fd}")),
            expected_dev=info.st_dev,
            expected_ino=info.st_ino,
        )
    except PermissionError:
        if not _readonly_parent_empty_cache(dir_fd, name):
            raise


def _task_caches(task_id: str, *, remove: bool) -> tuple[list[str], list[str]]:
    """The cache directories in ``<task_id>.artifacts`` and ``<task_id>.evidence``, and what could not be walked or removed.

    os.fwalk holds each directory open and checks it with lstat, open and fstat before it descends, so neither a
    link nor an ancestor swapped during the walk takes it outside the task's outputs. A non-cache directory that
    mountinfo names as a mount point is not entered (RV298-F2): refusal at deletion does not stop that descent.
    Removal acts relative to the held parent. ``DirEntry.is_dir`` follows links, so a dangling cache link or a link to a file is listed in
    ``files``; those names are unlinked too. A real directory goes through the scratch remover.
    """
    tasks = store.record_path(task_id).parent  # refuses an invalid task id before any path is built from it
    found: list[str] = []
    failed: list[str] = []

    def refused(error: OSError) -> None:
        failed.append(f"{error.filename}: {type(error).__name__}")

    def drop(directory: str, dir_fd: int, name: str) -> None:
        found.append(f"{directory}/{name}")
        if not remove:
            return
        try:
            _remove_cache_entry(dir_fd, name)
        except (OSError, task_scratch.ContainmentError) as exc:
            failed.append(f"{directory}/{name}: {type(exc).__name__}")

    tasks_fd = os.open(tasks, os.O_RDONLY | os.O_DIRECTORY)
    try:
        mounts = task_scratch.mount_points()
        for root in (f"{task_id}.artifacts", f"{task_id}.evidence"):
            try:
                os.stat(root, dir_fd=tasks_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            try:
                for directory, names, files, dir_fd in os.fwalk(root, onerror=refused, dir_fd=tasks_fd):
                    if mounts:
                        try:
                            held = os.readlink(f"/proc/self/fd/{dir_fd}")
                        except OSError:
                            held = ""
                        if held:
                            for name in [
                                name for name in names
                                if name not in TASK_CACHE_DIRS and os.path.join(held, name) in mounts
                            ]:
                                names.remove(name)  # do not descend into a non-cache mount
                    for name in [name for name in names if name in TASK_CACHE_DIRS]:
                        names.remove(name)  # do not descend into a directory this pass is removing
                        # An empty cache under a non-cache parent we must not chmod is not residue.
                        if not remove and _readonly_parent_empty_cache(dir_fd, name):
                            continue
                        drop(directory, dir_fd, name)
                    for name in files:
                        if name not in TASK_CACHE_DIRS:
                            continue
                        try:
                            linked = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
                        except OSError as exc:
                            failed.append(f"{directory}/{name}: {type(exc).__name__}")
                            continue
                        if linked:
                            drop(directory, dir_fd, name)
            except OSError as exc:  # the root, or a listing os.fwalk does not pass to onerror
                failed.append(f"{root}: {type(exc).__name__}")
    finally:
        os.close(tasks_fd)
    return found, failed


def _clear_task_caches(record: dict) -> bool:
    """Remove a finished task's re-creatable caches from its own output directories; True when the record changed.

    The caller holds the task state lock. ``task_cache_residue`` records the result: whatever could not
    be walked or removed, which the scheduled settlement sweep retries, or an empty list once nothing
    is left, which ends the sweep's walks of this task's output directories (PR298-F3). The empty list
    is recorded only when the provider was proven gone before the walk; while it may still run and
    write a cache, no result is recorded and the sweep walks again.
    """
    if not _finished(record):
        return False
    try:
        gone = _provider_alive(record) is False
    except Exception:  # unknown liveness is not proof the provider is gone
        gone = False
    try:
        residue = _task_caches(record["task_id"], remove=True)[1]
    except Exception as exc:  # cache removal never stops the worktree settlement that follows it
        residue = [f"scan: {type(exc).__name__}"]
    if not residue and not gone:
        return record.pop("task_cache_residue", None) is not None
    if residue == record.get("task_cache_residue"):
        return False
    record["task_cache_residue"] = residue
    return True


def _identity(pid: int | None) -> str | None:
    if not pid:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return None if fields[0] == "Z" else f"{boot}:{fields[19]}"
    except (OSError, IndexError):
        return None


def _observe(record: dict) -> dict:
    if _spawn_expired(record):
        path = store.record_path(record["task_id"])
        with store.task_state_lock(path):
            current = store.read(record["task_id"], record["run_nonce"])
            if current.get("started_at") == record.get("started_at") and _spawn_expired(current):
                current.update(status="crashed", finished_at=store.now(),
                               returncode_reason="spawn_process_missing", error="spawn never produced a supervisor")
                store.replace(current, path)
            record = current
    if (record.get("schema_version") == 2 and record.get("status") in store.ACTIVE
            and record.get("pid") is None and record.get("admission_hold")):
        from lu_machinery.dispatcher.host_admission import heal_dead

        heal_dead(store.record_path(record["task_id"]), record)
        return store.read(record["task_id"], record["run_nonce"])
    if (
        record.get("schema_version") == 2
        and record["status"] in store.ACTIVE
        and record.get("pid")
        and _identity(record["pid"]) != record.get("process_start")
    ):
        current = store.read(record["task_id"], record["run_nonce"])
        if current["status"] not in store.ACTIVE:
            return current
        record.update(status="crashed", finished_at=store.now(), error="supervisor exited without a result")
        _discard_read_only_scratch(record)  # a killed supervisor never reached its own cleanup
        record["duration_s"] = (
            datetime.fromisoformat(record["finished_at"]) - datetime.fromisoformat(record["started_at"])
        ).total_seconds()
        record.update(git.snapshot_delivery(_spec(record, {}), record))
        store.replace(record)
    return record


def _dispatcher_code() -> dict:
    """The checkout whose dispatcher supervises this task: its HEAD, whether its code is unmodified, and sealed.

    Recorded when the supervisor starts and again when it settles. A review-contract canary
    needs both to be the same clean, read-only checkout at the head it reviewed (#12399). An
    unreadable checkout records ``head`` None, which no canary accepts.

    Only a Git checkout attests, and only when this file is loaded through no symbolic link
    from ``<top level>/<package>/dispatcher/worker.py``, where the top level is the parent
    of the package directory: the in-tree copy's product checkout, or a standalone or linked
    machinery worktree. An installed distribution (site-packages, even inside a product
    checkout's venv) is no such top level, so product Git never attests it.

    ``clean`` compares bytes, never the index or ``git status``: every regular file in HEAD's
    tree must hash to its blob, and every file on disk outside root Git metadata must be
    in that tree. Ignore rules, assume-unchanged and skip-worktree flags, filters
    and replace refs cannot hide a change. Python may run cached bytecode in place of a source,
    so a ``__pycache__`` file is untracked like any other: an attesting dispatcher runs on a
    fresh checkout without caches. Any probe error, link, file that is not regular, or other
    tree entry is unclean; an untracked file is never opened.

    ``read_only`` is true only when creating a file in every checkout directory fails with
    ``EROFS`` and every file outside Git metadata sits on a read-only mount (``statvfs``), so a writable bind
    mount over one file is caught: the checkout is a read-only mount for this process (the
    canary's read-only image), and no participating process, nested sandboxes included, can
    have written code or a cache there. Any other outcome, including a writable directory or
    file, or another error, is false.
    """
    import errno
    import tomllib

    from lu_machinery.common.git_context import sanitized_git_env

    loaded = os.path.abspath(__file__)
    root = Path(loaded).parents[2]  # <top level>/<package>/dispatcher/worker.py
    package = Path(loaded).parents[1].name  # the machinery package directory
    # Inline config, repository redirects and replace refs cannot steer the probes.
    env = {**sanitized_git_env(strip_config_injection=True), "GIT_NO_REPLACE_OBJECTS": "1"}

    def git_out(*args: str) -> str | None:
        try:
            return subprocess.run(["git", "--no-replace-objects", *args], cwd=root, env=env, capture_output=True,
                                  check=True, timeout=30).stdout.decode()
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            return None

    def read(path: str) -> bytes:
        # A FIFO or device swapped in after the walk's check must not block the open.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError(f"{path} is not a regular file")
            return handle.read()

    def unreadable(exc: OSError) -> None:
        raise exc

    def unmodified(top: str, listing: str, algorithm: str) -> bool:
        blobs: dict[str, str] = {}
        for entry in filter(None, listing.split("\0")):
            meta, _, path = entry.partition("\t")
            mode, kind, oid = meta.split(" ")
            if kind != "blob" or mode not in {"100644", "100755"}:
                return False  # a link or submodule is never attested code
            blobs[path] = oid
        if f"{package}/dispatcher/worker.py" not in blobs:
            return False
        directories = {str(parent).replace(os.sep, "/") for path in blobs for parent in Path(path).parents}
        seen: set[str] = set()
        # Any tracked top-level tree can shadow an import, not only the package.
        for current, dirs, files in os.walk(top, onerror=unreadable):
            if current == top:
                dirs[:] = [name for name in dirs if name != ".git"]
                files = [name for name in files if name != ".git"]
            for name in (*dirs, *files):
                path = os.path.join(current, name)
                if stat.S_ISLNK(os.lstat(path).st_mode):
                    return False
                relative = os.path.relpath(path, top).replace(os.sep, "/")
                if name in dirs:
                    if relative not in directories:
                        return False
                    continue
                if relative not in blobs or not stat.S_ISREG(os.lstat(path).st_mode):
                    return False
                data = read(path)
                if hashlib.new(algorithm, b"blob %d\0" % len(data) + data).hexdigest() != blobs[relative]:
                    return False
                seen.add(relative)
        return seen == set(blobs)

    def sealed(top: str) -> bool:
        for current, dirs, files in os.walk(top, onerror=unreadable):
            if current == top:
                dirs[:] = [name for name in dirs if name != ".git"]
                files = [name for name in files if name != ".git"]
            try:
                # An unnamed file leaves nothing behind if the directory is writable.
                os.close(os.open(current, os.O_WRONLY | os.O_TMPFILE, 0o600))
                return False
            except OSError as exc:
                if exc.errno != errno.EROFS:
                    return False
            # A file can be its own mount, even below an otherwise sealed tree.
            if not all(os.statvfs(os.path.join(current, name)).f_flag & os.ST_RDONLY for name in files):
                return False
        return True

    def attest() -> dict | None:
        # Never ask Git to search ancestors of an installed package. A standalone
        # source checkout identifies its project before any Git subprocess runs.
        if not (root / ".git").exists():
            return None
        if not in_tree_source() and (
            tomllib.loads(read(str(root / "pyproject.toml")).decode()).get("project", {}).get("name") != "lu-machinery"
        ):
            return None
        top = (git_out("rev-parse", "--show-toplevel") or "").rstrip("\n")
        if (
            not top
            or loaded != os.path.realpath(loaded)  # no link anywhere on the loaded path
            or loaded != os.path.join(top, package, "dispatcher", "worker.py")
            or str(root) != top
        ):
            return None
        head = (git_out("rev-parse", "--verify", "--quiet", "HEAD^{commit}") or "").strip() or None
        listing = git_out("ls-tree", "-r", "-z", "--full-tree", f"{head}^{{tree}}") if head else None
        algorithm = (git_out("rev-parse", "--show-object-format") or "").strip()
        try:
            clean = listing is not None and algorithm in {"sha1", "sha256"} and unmodified(top, listing, algorithm)
        except Exception:  # an unreadable or unexpected file is unclean, never a supervisor failure
            clean = False
        try:
            read_only = sealed(top)
        except Exception:  # no O_TMPFILE, or an unreadable directory: not proven read-only
            read_only = False
        return {"root": str(root), "head": head, "clean": clean, "read_only": read_only}

    try:
        attested = attest()
    except Exception:  # an unresolvable location attests nothing, never a supervisor failure
        attested = None
    # Product Git metadata cannot attest the installed distribution.
    return attested or {
        "root": str(root if in_tree_source() else primary_root()), "head": None, "clean": False, "read_only": False,
    }


def _discard_read_only_scratch(record: dict) -> None:
    """Remove a dispatched Codex read-only task's scratch; residue is recorded, never raised."""
    if record["agent"] == "codex" and record["mode"] == "read-only" and (
        residue := discard_dispatch_lease(record["task_id"], record.get("read_only_scratch"))
    ):
        record["read_only_scratch_cleanup"] = residue


def _spawn_expired(record: dict) -> bool:
    """Recover old pid-less launches, preserving a live or unprovable setup owner."""
    from lu_machinery.orchestration import dispatch_admission

    if (record.get("schema_version") != 2 or record.get("status") != "spawning"
            or record.get("pid") is not None or record.get("admission_hold") or record.get("worktree_prep")
            or any(name.startswith("launch_") for name in record)):
        return False
    try:
        started = datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
        age = (datetime.now(UTC) - started.replace(tzinfo=started.tzinfo or UTC)).total_seconds()
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return age > dispatch_admission.PIDLESS_SPAWNING_GRACE_S


def _stream_attestation(source: str, model: str | None) -> dict:
    """A record's model attestation from that provider's own stream-json, pending until the stream names one."""
    return dict(resolved_model=model if model is not None else "unknown", resolved_model_known=model is not None,
                resolved_model_source=source if model is not None else "pending")


def _submitted_prompt_already_published(record: dict, prompt: bytes, failed: str | None) -> bool:
    """True when exclusive prompt publication collided with the submitted bytes.

    ``store.create`` writes ``record["prompt_file"]`` with ``"xb"`` (``store.py``). A review
    brief is often already that path: ``--prompt-file batch_state/tasks/<task_id>.prompt``.
    The exclusive create then raises ``FileExistsError`` before any supervisor exists, and
    ``start`` records ``spawn_setup_failed``. The same bytes are the prompt this launch
    would publish, so the collision is the source file, not a foreign occupant. A different
    file, a symlink, or a ``FileExistsError`` from another path still fails closed.
    """
    expected = record.get("prompt_file")
    if not isinstance(failed, str) or not isinstance(expected, str) or not expected:
        return False
    if os.path.normpath(failed) != os.path.normpath(expected):
        return False
    path = Path(expected)
    try:
        info = path.lstat()
        held = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return False
    return held == prompt


def _launch_cleanup_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def _record_failed_launch(path: Path, spec: TaskSpec, record: dict, exc: BaseException,
                          cleanup_error: BaseException | None = None) -> None:
    """Record the launch error. A cleanup failure is a separate field, never that error."""
    with store.task_state_lock(path):
        current = store.read(spec.task_id, missing_ok=True)
        if (current is None or current.get("run_nonce") != record["run_nonce"]
                or current.get("status") not in store.ACTIVE):
            return
        fields = (exc.fields if isinstance(exc, admission.LaunchRefused) else
                  dict(status="failed" if current.get("pid") is None else "crashed",
                       error="supervisor launch failed"))
        reason = (fields["error"].split(":", 1)[0] if isinstance(exc, admission.LaunchRefused) else
                  "spawn_setup_failed" if current.get("pid") is None else "supervisor_launch_failed")
        if not isinstance(exc, admission.LaunchRefused):
            fields["error"] = f"{reason}: {type(exc).__name__}"
        if cleanup_error is not None:
            fields["launch_cleanup_error"] = _launch_cleanup_text(cleanup_error)
        current.update(fields, finished_at=store.now(), returncode_reason=reason)
        store.replace(current, path)


def _guard_launch_cleanup(path: Path, spec: TaskSpec, record: dict, exc: BaseException) -> None:
    """Record the failed launch. A cleanup failure is logged and kept on the record; the launch error still propagates."""
    try:
        _record_failed_launch(path, spec, record, exc)
    except BaseException as cleanup_exc:
        logging.getLogger(__name__).warning(
            "launch cleanup failed for %s: %s", spec.task_id, _launch_cleanup_text(cleanup_exc))
        try:
            _record_failed_launch(path, spec, record, exc, cleanup_exc)
        except BaseException as again:
            logging.getLogger(__name__).warning(
                "launch cleanup failed for %s: %s", spec.task_id, _launch_cleanup_text(again))


def start(spec: TaskSpec, record: dict, prompt: bytes, hard_timeout: float) -> None:
    from lu_machinery.orchestration.dispatch_admission import new_admission_hold

    source = getattr(providers.load(spec.agent), "MODEL_SOURCE", None)
    if source:
        record.update(_stream_attestation(source, None))
    if not record.get("admission_hold"):
        record["admission_hold"] = new_admission_hold(record["run_nonce"])
    path = store.record_path(spec.task_id)
    logs = path.parent / "logs"
    try:
        try:
            store.create(record, prompt)
        except FileExistsError as exc:
            if not _submitted_prompt_already_published(record, prompt, exc.filename):
                raise
        logs.mkdir(exist_ok=True, mode=0o700)
        with open(logs / f"{spec.task_id}.stderr.log", "ab", opener=lambda p, f: os.open(p, f, 0o600)) as err:
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
            try:
                process, launch = admission.spawn_supervisor(
                    [sys.executable, "-I", "-m", "lu_machinery.dispatcher.worker", str(path)],
                    task_id=spec.task_id, run_nonce=record["run_nonce"], cwd=state_root(),
                    stderr=err, stderr_log=logs / f"{spec.task_id}.stderr.log",
                    env={**os.environ,
                         "LEARN_UKRAINIAN_DISPATCH_TASK_ID": spec.task_id,
                         "LEARN_UKRAINIAN_DISPATCH_AGENT": spec.agent},
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        record.update(pid=process.pid, process_start=_identity(process.pid), **launch)
        if record.get("launch_mode") == "scope":
            # Crash recovery reads the scope's members to prove the whole task tree has exited.
            record["launch_cgroup"] = _cgroup(process.pid)
        store.replace(record, path)
        payload = json.dumps({"options": spec.options, "hard_timeout": hard_timeout}).encode()
        process.stdin.write(payload)
        process.stdin.close()
    except BaseException as exc:
        # create() can publish the record before prompt/log setup fails. Never
        # overwrite another run, or a supervisor that has already settled.
        # Recording that failure is cleanup: its own error must not replace the launch error.
        _guard_launch_cleanup(path, spec, record, exc)
        raise


class _Cancelled(Exception):
    pass


def _spec(record: dict, options: dict) -> TaskSpec:
    return TaskSpec(
        record["task_id"],
        record["agent"],
        record["model"],
        record["effort"],
        record["mode"],
        Path(record["worktree_path"]),
        Path(record["prompt_file"]),
        Path(record["result_file"]),
        record["claude_account"],
        record["provider"],
        {**record.get("options", {}), **options},
        codex_account=record.get("codex_account"),
    )


def _outside_cause(paths, owned) -> str | None:
    from lu_machinery.guardrails.delegate_ownership import sanitize_for_display
    from lu_machinery.secret_redactor import redact_text

    outside = ownership.outside_owned(paths, owned)
    if not outside:
        return None
    cause = "outside_owned_paths: " + ", ".join(sanitize_for_display(redact_text(path)) for path in outside)
    return cause if len(cause) <= 1024 else cause[:1009] + "... (truncated)"


def _verification_cause(exc: Exception) -> str:
    """Name what stopped the ownership check: the failing Git step, or the check's own refusal."""
    detail = str(exc) if isinstance(exc, git.VerificationFetchError) else ownership.failure_detail(exc)
    return f"owned_paths_verification_failed: {detail}"[:200]


def _enforce_delivery(record: dict) -> None:
    if record["mode"] == "read-only":
        return
    provider_failed = record["status"] == "failed" and record.get("provider_terminal_failure") is True
    if record["status"] != "done" and not provider_failed:
        return
    if provider_failed:
        record.update(provider_terminal_status="failed", provider_terminal_error=record.get("error"))
    cause = None
    paths = []
    try:
        owned = ownership.effective_scope(record)
    except (ValueError, TypeError) as exc:
        if provider_failed:
            return
        record.update(status="no_deliverable", error=_verification_cause(exc),
                      no_deliverable_reason=_verification_cause(exc))
        return
    # With no commits ahead there is nothing to verify, and the remote branch may never have existed.
    if owned and (record.get("commits_ahead") or 0) > 0:
        try:
            with git.ownership_git(record) as run:
                base_sha = run("rev-parse", "--verify", "refs/heads/base^{commit}")
                start_sha = record.get("worktree_start_sha", record["worktree_base_sha"])
                paths = ownership.delivered_paths(run, record["head_sha"], base_sha, start_sha,
                                                  commits_expected=True)
            cause = _outside_cause(paths, owned)
        except (OSError, ValueError, TypeError, IndexError, KeyError, TaskScratchError, subprocess.SubprocessError) as exc:
            cause = _verification_cause(exc)
    if cause is None:
        if record.get("worktree_dirty_on_exit") is not False:
            cause = "worktree_dirty_or_unknown"
        elif not record.get("commits_ahead"):
            cause = "no_commits_or_unknown"
        elif record.get("push_state") != "pushed":
            cause = "work_not_pushed_or_unknown"
    # Merged work leaves nothing on the task branch to inspect (it may be reset or deleted), so the
    # merged pull request is the evidence; it refuses an outside-owned path the same way.
    rescuable = cause is None or cause.split(":", 1)[0] in MERGE_RESCUABLE
    if (provider_failed or cause) and rescuable and (
            record.get("worktree_dirty_on_exit") is False or record.get("worktree_untracked_only_on_exit") is True) and (
            merged := git.merged_delivery(record)):
        paths = merged["paths"]
        cause = _outside_cause(paths, owned) if owned else None
        record["delivery_pr"] = f"{merged['repo']}#{merged['number']}"
        record["delivery_merge_sha"] = merged["merge_sha"]
        record["delivery_prs"] = [f"{pr['repo']}#{pr['number']}" for pr in merged.get("pull_requests", [merged])]
        record.update(status="done", error=None)
        record.pop("no_deliverable_reason", None)
    elif provider_failed and rescuable:
        return  # absent merged proof, a provider failure keeps its original status and error
    record["owned_path_grants_used"] = ownership.grants_used(record, paths)
    if cause:
        record.update(status="no_deliverable", error=cause, no_deliverable_reason=cause)


# Refusals that a merged pull request answers: the local branch could not show the delivery, or the tree
# held only untracked files (worktree_untracked_only_on_exit), which settlement's removal preserves.
MERGE_RESCUABLE = frozenset({"owned_paths_verification_failed", "no_commits_or_unknown", "work_not_pushed_or_unknown",
                             "worktree_dirty_or_unknown"})


def _cgroup(pid: int) -> str | None:
    with contextlib.suppress(OSError):
        return next((line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                     if line.startswith("0::")), None)
    return None


def _scope_cgroups(unit: str) -> list[str] | None:
    """Every cgroup the tree holds for ``unit``'s scope; None when the traversal was not complete."""
    root, failed, found = "/sys/fs/cgroup", [], []

    def refused(error: OSError) -> None:
        failed.append(error)

    for directory, _names, _files in os.walk(root, onerror=refused):
        if os.path.basename(directory) == f"{unit}.scope":
            found.append(directory.removeprefix(root))
    return None if failed else found


def _run_scope_cgroups(unit: str, task_id: str, run_nonce: str) -> list[str] | None:
    """Every scope in ``unit``'s dispatch slice named for this task and run nonce, whatever its launch token.

    None when the slice cannot be listed; a slice that does not exist holds no scope.
    """
    from lu_machinery.orchestration import dispatch_isolation

    parent = dispatch_isolation.scope_cgroup(unit, uid=os.getuid()).rpartition("/")[0]
    try:
        names = os.listdir("/sys/fs/cgroup" + parent)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    return [f"{parent}/{name}" for name in names if name.endswith(".scope") and dispatch_isolation.scope_unit_matches(
        name.removesuffix(".scope"), task_id=task_id, run_nonce=run_nonce)]


def _provider_alive(record: dict) -> bool | None:
    """Whether the provider's process tree still runs, from the identity recorded for it; None when unknown."""
    if record.get("launch_mode") == "scope":
        # The scope holds every descendant, including ones that left the provider's process group. Only this
        # launch's own scope proves anything, so the unit must be the one named for this task and run nonce
        # before either lookup; any other unit is unknown.
        from lu_machinery.orchestration import dispatch_isolation

        unit, task_id, nonce = record.get("launch_unit"), record.get("task_id"), record.get("run_nonce")
        if not (isinstance(unit, str) and re.fullmatch(r"lu-worker-[A-Za-z0-9._-]+", unit)
                and isinstance(task_id, str) and isinstance(nonce, str)
                and dispatch_isolation.scope_unit_matches(unit, task_id=task_id, run_nonce=nonce)):
            return None
        cgroup = record.get("launch_cgroup")
        if cgroup is not None:
            # A recorded cgroup counts only as that unit's canonical path. Anything else, [] or "" included, is
            # unknown: a damaged path's missing cgroup file proves nothing.
            if cgroup != dispatch_isolation.scope_cgroup(unit, uid=os.getuid()):
                return None
            cgroups = [cgroup]
        else:
            # A supervisor that ended before it read the provider's cgroup still named the scope's unit,
            # which is found wherever it was placed.
            cgroups = _scope_cgroups(unit)
            if cgroups is None:
                return None
        # A re-dispatch that reuses the run nonce launches under a fresh token, so the recorded scope's absence proves
        # nothing while another scope named for this task and run nonce still holds a process.
        siblings = _run_scope_cgroups(unit, task_id, nonce)
        if siblings is None:
            return None
        cgroups = [*cgroups, *(sibling for sibling in siblings if sibling not in cgroups)]
        controllers = Path("/sys/fs/cgroup/cgroup.controllers").exists()
        alive = False
        for cgroup in cgroups:
            try:
                procs = Path("/sys/fs/cgroup" + cgroup, "cgroup.procs").read_text().split()
            except FileNotFoundError:
                # The kernel refuses to remove a cgroup that still holds processes, so a gone scope is empty.
                if not controllers:
                    return None
                continue
            except OSError:
                return None
            alive = alive or any(int(pid) != os.getpid() for pid in procs)
        # No scope under the unit's name is a gone scope, which held nothing, on a cgroup v2 host.
        return alive if cgroups or controllers else None
    if "provider_pgid" not in record:
        # The supervisor ended before recording whether it started a provider. A launch that the
        # dispatcher itself recorded as never having produced a supervisor started none.
        # spawn_setup_failed is that record: start() settled with no pid before a provider existed.
        never_ran = record.get("returncode_reason") in {
            "spawn_process_missing", "dispatch_died_after_admission", "spawn_setup_failed",
        }
        return False if never_ran and record.get("pid") is None and record.get("process_start") is None else None
    pgid = record["provider_pgid"]
    if pgid is None:
        return False
    if type(pgid) is not int or not 0 < pgid < 2**31:
        return None  # a damaged group id is unknown, never an error that ends the sweep for later records
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        # A descendant that left the group is named by the stop that could not end it (#12520).
        return any(_identity(int(pid)) == identity for pid, identity in record.get("fallback_survivors", {}).items())
    except PermissionError:
        pass
    return True


def _same_tree(left: str | None, right: str | None) -> bool:
    """Whether two recorded paths name one directory: by inode while both exist, else by resolved path."""
    if not (left and right and isinstance(left, str) and isinstance(right, str)):
        return False
    with contextlib.suppress(OSError):
        return os.path.samefile(left, right)
    return os.path.realpath(left) == os.path.realpath(right)


def _borrowed(record: dict) -> bool:
    """Whether another task may still work in this task's worktree (dispatched there with --cwd).

    Liveness outranks status: a terminal borrower (a crashed supervisor) holds the tree until its
    provider is proven gone. A borrower whose supervisor never started (no pid) never ran a provider.
    """
    return any(
        other.get("task_id") != record["task_id"] and _same_tree(other.get("worktree_path"), record.get("worktree_path"))
        and (not _finished(other) or (other.get("pid") is not None and _provider_alive(other) is not False))
        for other in store.list_tasks())


def _running_children(record: dict) -> list[str]:
    """Tasks this run dispatched that are still running as it settles: no one is left to read their results.

    A child whose status is not a string is not running: no record may stop this settlement.
    """
    with contextlib.suppress(OSError, ValueError):
        return sorted(other["task_id"] for other in store.list_tasks()
                      if other.get("parent_task_id") == record["task_id"]
                      and isinstance(other.get("status"), str) and other["status"] in store.ACTIVE)
    return []


def _children_at(record: dict, deadline: float, checkpoint) -> list[str]:
    """Child tasks still running at ``deadline``.

    Polls until they leave the active states or the deadline passes. A hard
    timeout of zero does not wait: the caller is already out of time. Cancel
    is checked on each poll, the same way the provider wait checks it.
    """
    children = _running_children(record)
    while children and time.monotonic() < deadline:
        checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
        children = _running_children(record)
    return children


def _agy_follow_up(spec: TaskSpec, record: dict, parsed: ParsedResult, started: float,
                   hard_timeout: float, resumes: int, checkpoint) -> tuple[str | None, ParsedResult]:
    """Resume an AGY workspace-write turn that closed while its work was still open.

    Returns ``(conversation id, parsed)`` to launch again, or ``(None, parsed)``
    to settle ``parsed``. A child still active at the task deadline becomes a
    typed failure naming that child. The resume flag is the adapter's
    ``--conversation``.
    """
    from lu_machinery.dispatcher.providers.agy import CONTINUATION_LIMIT, conversation_id

    if spec.agent != "agy" or spec.mode != "workspace-write":
        return None, parsed
    incomplete = parsed.error == "agy: incomplete run"
    children = _running_children(record)
    if not incomplete and not children:
        return None, parsed
    deadline = started + hard_timeout if hard_timeout > 0 else time.monotonic()
    saw_children = bool(children)
    if children:
        children = _children_at(record, deadline, checkpoint)
    if children:
        named = ", ".join(children)
        return None, replace(parsed, error=f"agy_child_still_running: {named}", incomplete=True)
    if resumes >= CONTINUATION_LIMIT or time.monotonic() >= deadline:
        return None, _agy_unfinished(parsed)
    if not incomplete and not saw_children:
        return None, parsed
    log = spec.final_file.with_suffix(".log")
    conversation = conversation_id(log) if log.is_file() else None
    if not conversation:
        return None, _agy_unfinished(parsed)
    return conversation, parsed


def _agy_unfinished(parsed: ParsedResult) -> ParsedResult:
    """An interim success is not final when no continuation produced a later result."""
    return replace(parsed, error=parsed.error or "agy: incomplete run", incomplete=True)


# Dispositions that can change once other processes end, the tree is committed or reverted, the work is
# published (pushed or merged after the task ended), or a transient Git, lock, claim or removal failure
# clears; the scheduled cleanup retries these under the same removal proofs.
# Admission and worker exit never retry unrelated tasks. A public-push refusal
# (opsec_refused) and an owner retention hold (retention_hold) are absent on purpose:
# automation must not retry either.
# The worktree reasons settlement records when it cannot prove the provider's process tree gone.
LIVENESS_REASONS = frozenset({"provider_processes_alive", "provider_liveness_unknown"})
RETRY_REASONS = frozenset({"dirty", *LIVENESS_REASONS, "borrowed_by_live_task",
                           "unpublished", "remote_mismatch", "unknown", "removal_refused",
                           "worktree_remove_failed", "settlement_failed",
                           "salvage_failed", "salvage_unverified", "large_ignored_output",
                           "nested_repository_unpushed", "github_deferred", git.CODE_UNAVAILABLE})


def _load_bound_review_code() -> None:
    """Import what a bound formal review attempt (``review_attempt.run``) imports on demand.

    Only that kind of run needs the build engine's manifest, the evidence sources, the review prompt checks and the agent runner with
    its ``ai_llm`` package. The manifest import reaches ``scripts.wiki.dense_rerank``, which imports numpy, and numpy starts an
    OpenBLAS thread pool in this process. The pool shares the file table. A supervisor that then holds review slots keeps those
    flocks in the same table; SIGKILL makes the leader a zombie, which ``_identity`` treats as gone, while the pool still holds
    the flocks, so the hold stays live. A bound attempt does not take review slots (``hold_review_test_slots`` returns immediately
    when ``review_attempt`` is set), so only that run loads this code. The runner imports ``ai_llm`` from ``scripts/``, which
    ``review_attempt.run`` puts on ``sys.path`` just before it imports the runner; do the same, and leave ``sys.path`` as it was.
    """
    scripts_dir = str(machinery_path("agent_runtime").parent)
    added = scripts_dir not in sys.path
    if added:
        sys.path.insert(0, scripts_dir)
    try:
        with contextlib.suppress(Exception):
            attempt_hooks().bind_bound_review_modules()
        with contextlib.suppress(Exception):
            from lu_machinery.agent_runtime import runner  # noqa: F401
    finally:
        if added:
            sys.path.remove(scripts_dir)


def _load_provider_modules() -> None:
    """Import the provider modules in their own scope.

    ``load_post_start_code`` also imports ``scripts.agent_runtime.adapters`` under the names ``agy``, ``claude``, ``codex``,
    ``cursor`` and ``muse``. Binding those names twice in one function is a mypy ``no-redef`` error. The imports stay
    literal, and each module still lands in ``sys.modules``.
    """
    with contextlib.suppress(Exception):
        from lu_machinery.dispatcher.providers import (  # noqa: F401
            agy,
            claude,
            codex,
            cursor,
            muse,
            opencode,
            stream_json,
        )


def load_post_start_code(bound_review: bool = False) -> None:
    """Import, as the worker starts, the modules its later steps import inside functions.

    Settlement and the other steps after the provider runs import part of their code on demand, from the
    primary checkout. A dispatcher change that merged while the worker ran gave them new modules next to
    the old ones loaded at start, and removal failed on the mix (#12634). Loading them now makes the whole
    run use the version it started with. The list holds the deferred imports of the modules a worker runs
    after start (this one, ``git``, the provider registry, the settlement chain, review attempts and
    their helpers) and the modules the dispatcher tests loaded after start. A new deferred import in
    those steps belongs here; ``bound_review`` adds what only a bound review attempt imports. Each group is skipped whole when any module in it fails to import, whatever
    the error, so the worker still starts; a step that then cannot load its code records
    ``git.CODE_UNAVAILABLE`` and the scheduled cleanup completes it from a fresh process.
    """
    with contextlib.suppress(Exception):
        from lu_machinery.agent_runtime import (  # noqa: F401
            agy_hook_config,
            attempt_boundary,
            attempt_network,
            binary_resolve,
            codex_accounts,
            env_sanitize,
            failure_codes,
            jsonl,
            review_mcp,
            sources_read_only,
            telemetry,
            tool_calls,
        )
        from lu_machinery.agent_runtime.adapters import (  # noqa: F401
            _output_schema,
            agy,
            base,
            claude,
            codex,
            codex_events,
            cursor,
            muse,
        )
    if bound_review:
        _load_bound_review_code()
    with contextlib.suppress(Exception):
        from lu_machinery.common import fallback_substitutions, nofollow_walk, release_layout, safe_open  # noqa: F401

        attempt_hooks().bind_evidence_modules()
        from lu_machinery.guardrails import delegate_ownership  # noqa: F401
        from lu_machinery.lib import git_identity  # noqa: F401
        from lu_machinery.opsec import git_push  # noqa: F401
        from lu_machinery.session_supervisor import remote  # noqa: F401
        from lu_machinery.utils import claude_version  # noqa: F401
    with contextlib.suppress(Exception):
        from lu_machinery.review import (  # noqa: F401
            capacity,
            isolation,
            record_cf_verdict,
            render_contract,
            repair_round,
            review_contract,
            reviewer_resolver,
            role_resolution,
            subject_seat,
            verdict_parser,
        )

        attempt_hooks().bind_receipt_modules()
    with contextlib.suppress(Exception):
        from lu_machinery.dispatcher import checks, host_admission, output_schema, review_attempt, scope  # noqa: F401
    _load_provider_modules()
    with contextlib.suppress(Exception):
        from lu_machinery.fleet import (  # noqa: F401
            credit_lane,
            ignored_task_output,
            pr_owner_check,
            regenerable_output,
            reset_reserve,
            terminal_abandon,
        )
    with contextlib.suppress(Exception):
        from lu_machinery.orchestration import (  # noqa: F401
            admission_shadow,
            dead_worker_state,
            dispatch_admission,
            pool_headroom,
            pr_ownership,
            preparation_evidence,
            reaper_lifecycle,
            slot_routing,
            task_record_causes,
            worker_leftovers,
            worktree_artifacts,
            worktree_claims,
            worktree_git,
            worktree_prep,
        )


# Seconds settlement waits for a provider's lingering processes to exit before it retains the tree.
SETTLE_PATIENCE = 10
# Extra fallback scans, 0.2 s apart, before an unreadable one keeps the worktree.
FALLBACK_SCAN_RETRIES = 10
# Ancestry walks of one candidate, restarted when an ancestor exits during the walk.
ANCESTRY_ATTEMPTS = 5
# Parents followed from one candidate; a deeper chain is unknown, never a stranger.
ANCESTRY_DEPTH = 64


def _cgroup_holds_other_active_task(record: dict, cgroup: str) -> bool:
    """Whether another still-active task has its supervisor or provider in ``cgroup``.

    A fallback child started from inside this scope stays in it. Stopping the
    scope would cancel that task. Membership that cannot be read counts too,
    and so does a task record or task directory that cannot be read, so the
    stop is skipped and the liveness check keeps the tree. A child that has
    its own scope is not a member here and does not block the stop.
    """
    from lu_machinery.orchestration import worker_leftovers

    try:
        members = worker_leftovers.ProcFsReader().cgroup_procs(cgroup)
    except worker_leftovers.ScanUnknown:
        return True
    if not members:
        return False
    task_id = record.get("task_id")
    try:
        root = store.tasks_dir()
        if not root.is_dir():
            return True
        next(root.iterdir(), None)
        others = store.list_tasks(strict=True)
    except (OSError, ValueError):
        return True
    protected: set[int] = set()
    for other in others:
        if other.get("task_id") == task_id or _finished(other):  # a damaged status counts as active
            continue
        for key in ("pid", "provider_pgid"):
            value = other.get(key)
            if type(value) is int and value > 0:
                protected.add(value)
    return any(pid in protected for pid in members)


def _own_tree() -> set[int]:
    """Every process the supervisor started, whatever group or session it moved to (it adopts orphans)."""
    return {child.pid for child in psutil.Process().children(recursive=True)}


def _is_own_descendant(pid: int) -> bool:
    """Whether ``pid`` is in the supervisor's process tree now, which the marker alone does not prove.

    A client that inherited the task's marker, or a task this one dispatched, is not the task's descendant.
    ``stop_leftovers`` asks this of each candidate as it is listed and again after its pidfd is open, so a
    process that appears mid-stop is judged at that moment. An ancestor that exits during the walk has just
    reparented the candidate to the supervisor, so the walk starts again; ancestry that stays unresolved or
    unreadable raises ``ScanUnknown`` (the stop is then not ok) and is never read as "not a descendant".
    """
    from lu_machinery.orchestration import worker_leftovers

    me = os.getpid()
    for _ in range(ANCESTRY_ATTEMPTS):
        current, settled = pid, True
        for _ in range(ANCESTRY_DEPTH):
            try:
                parent = psutil.Process(current).ppid()
            except psutil.NoSuchProcess:
                if current == pid:
                    return False  # the candidate itself has exited
                settled = False
                break
            except psutil.Error as exc:
                raise worker_leftovers.ScanUnknown(f"ancestry of {pid} unreadable: {exc}") from None
            if parent == me:
                return True
            if parent <= 1:
                return False
            current = parent
        if settled:
            raise worker_leftovers.ScanUnknown(f"ancestry of {pid} is deeper than {ANCESTRY_DEPTH} processes")
    raise worker_leftovers.ScanUnknown(f"ancestry of {pid} kept changing")


def _fallback_tree_reader():
    """A host reader for the fallback stop that leaves out unreadable processes outside the supervisor's tree.

    A fallback launch has no cgroup of its own, so the scan reads the dispatch marker of every process on the
    host started after the supervisor. A process of this user that is not dumpable, or that runs as another
    user in the same cgroup, hides that marker, and the scan reported the whole stop as unknown, which kept a
    finished task's worktree as ``provider_liveness_unknown``. The stop only acts on processes in the
    supervisor's own tree (``_is_own_descendant``). So a hidden marker on any other process cannot change
    its result: that process reads as unmarked. A hidden marker on a descendant still fails closed.
    """
    from lu_machinery.orchestration import worker_leftovers

    class Reader(worker_leftovers.ProcFsReader):
        def dispatch_task_id(self, pid: int) -> str | None:
            try:
                return super().dispatch_task_id(pid)
            except worker_leftovers.ScanUnknown:
                if _is_own_descendant(pid):
                    raise
                return None

    return Reader()


def _stop_fallback_tree(task_id: str, nonce: str):
    """Stop the processes a Popen-fallback supervisor started, once; a scan that cannot be read is not ok."""
    from lu_machinery.orchestration import worker_leftovers

    reader = _fallback_tree_reader()
    try:
        scope = worker_leftovers.worker_scope_at_exit(
            task_id=task_id, launch_mode="popen-fallback", launch_unit=None, run_nonce=nonce, reader=reader)
        stopped = worker_leftovers.stop_leftovers(scope, reader=reader, owned=_is_own_descendant)
        for pid in _own_tree():
            # An adopted descendant stays a zombie in its old process group until reaped, which the group
            # liveness check would read as a live provider.
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, os.WNOHANG)
        return stopped
    except (OSError, ValueError, psutil.Error, worker_leftovers.ScanUnknown):
        return worker_leftovers.StopResult(ok=False, error="fallback boundary unreadable")


def _end_own_scope(record: dict):
    """End processes left in this finished task's own boundary; return the stop result, None when none ran.

    A scope stays alive while any member remains. Settlement used to read those
    members as a live provider and keep the worktree. A scope launch stops only
    the unit ``dispatch_isolation`` derives for this task and nonce: a recorded
    cgroup that is not that unit's scope is left alone, another active task's
    supervisor or provider in this cgroup is left alone, and the caller and its
    ancestors are not signalled. A Popen-fallback launch has no cgroup to stop, and
    ``killpg`` misses a descendant in another process group (GNU ``timeout``
    puts its child in one) or session, so the marked processes of the supervisor's
    own tree are stopped instead (#12520). A stop that cannot be proven
    leaves the processes for the liveness check, which keeps a scope's tree; the
    caller keeps a fallback tree from a failed stop. Only the supervisor sweeps a
    fallback launch (None otherwise): it alone knows the cgroup its jobs keep and the tree it adopted.
    """
    launch_mode = record.get("launch_mode")
    task_id, unit, nonce = record.get("task_id"), record.get("launch_unit"), record.get("run_nonce")
    if launch_mode == "popen-fallback":
        if not (isinstance(task_id, str) and task_id and isinstance(nonce, str) and nonce):
            return None
        if record.get("pid") != os.getpid():
            # Only the supervisor knows the cgroup its jobs keep, which a closed environment is judged by (#9514);
            # a later sweep from another process cannot bound the scan, so it leaves the task's processes alone.
            return None
        # A process that is mid-exec on a busy host reads as closed for an instant; only a scan that stays
        # unreadable keeps the tree.
        stopped = _stop_fallback_tree(task_id, nonce)
        for _ in range(FALLBACK_SCAN_RETRIES):
            if stopped.ok or stopped.survivors:
                break
            time.sleep(0.2)
            stopped = _stop_fallback_tree(task_id, nonce)
        return stopped
    if launch_mode != "scope":
        return None
    if not all(isinstance(value, str) and value for value in (task_id, unit, nonce)):
        return None
    from lu_machinery.orchestration import dispatch_isolation, worker_leftovers

    if not dispatch_isolation.scope_unit_matches(unit, task_id=task_id, run_nonce=nonce):
        return None
    cgroup = dispatch_isolation.scope_cgroup(unit, uid=os.getuid())
    recorded = record.get("launch_cgroup")
    # A recorded boundary that is not this unit's scope is not ours to stop.
    if isinstance(recorded, str) and recorded and recorded != cgroup:
        return None
    if _cgroup_holds_other_active_task(record, cgroup):
        return None
    scope = worker_leftovers.WorkerScope(
        task_id=task_id, launch_mode=dispatch_isolation.LAUNCH_SCOPE, unit=unit, cgroup=cgroup, run_nonce=nonce)
    try:
        return worker_leftovers.stop_leftovers(scope, reader=worker_leftovers.ProcFsReader())
    except (OSError, ValueError, worker_leftovers.ScanUnknown):
        return None


def _stop_task_services(record: dict) -> None:
    """Stop transient services this task started. A failure is recorded, never raised."""
    from lu_machinery.orchestration import dispatch_isolation

    try:
        dispatch_isolation.stop_task_services(record)
    except Exception as exc:
        stops = list(record.get("task_service_stops") or [])
        stops.append({
            "unit": "", "result": "unverified", "postcondition": "unknown",
            "error": type(exc).__name__,
        })
        record["task_service_stops"] = stops


def _run_merge_closeout(request: dict) -> dict:
    """``merge_closeout --apply`` for a merged delivery, after the supervisor removes its worktree.

    The target comes from the verified merged delivery, not a boundary broker.
    """
    pr, repo, root = request.get("pr"), request.get("repo"), request.get("repo_root")
    if not (type(pr) is int and pr > 0 and isinstance(repo, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}", repo)
            and isinstance(root, str) and Path(root).is_absolute() and Path(root).is_dir()):
        return {"pr": pr, "returncode": None, "reason": "the closeout request is malformed"}
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from lu_machinery.orchestration.merge_closeout "
            "import main; raise SystemExit(main(sys.argv[2:]))")
    argv = [sys.executable, "-I", "-c", code, str(machinery_path("orchestration/merge_closeout.py").parents[2]),
            str(pr), "--repo", repo, "--repo-root", root, "--apply", "--json"]
    try:
        # A fixed argv: this interpreter, the package's own closeout module and checked request fields.
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                                timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"pr": pr, "returncode": None, "reason": f"{type(exc).__name__}"}
    return {"pr": pr, "returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}


def _reap_settled(record: dict, patience: float = 0, salvage: bool = False) -> None:
    """Settle services and the worktree, close out merged delivery, then remove scratch.

    A failed closeout is visible to the waiting dispatching seat as a failed task, with the receipt retained.
    """
    _stop_task_services(record)
    # Attribution needs the checkout's origin, so take it before removal. Closeout itself runs afterward.
    request = merged = None
    if record.get("mode") == "workspace-write" and "closeout" not in record:
        merged = git.merged_delivery(record)
    _reap_worktree(record, patience=patience, salvage=salvage)
    if record.get("worktree_disposition") == "removed":
        if merged:
            record["delivery_pr"] = f"{merged['repo']}#{merged['number']}"
            record["delivery_merge_sha"] = merged["merge_sha"]
            deliveries = merged.get("pull_requests", [merged])
            record["delivery_prs"] = [f"{pr['repo']}#{pr['number']}" for pr in deliveries]
            try:
                root, _repository = git.resolve_repo(record.get("repo"))
                request = [{"pr": pr["number"], "repo": pr["repo"], "repo_root": str(root)} for pr in deliveries]
            except (OSError, ValueError) as exc:
                record["closeout"] = {"pr": merged["number"], "returncode": None, "reason": type(exc).__name__}
        if request and "closeout" not in record:
            receipts = [_run_merge_closeout(pr) for pr in request]
            record["closeout"] = next((r for r in receipts if r.get("returncode") != 0), receipts[0])
            if len(receipts) > 1:
                record["closeouts"] = receipts
        closeout = record.get("closeout")
        if "closeout" in record and (not isinstance(closeout, dict) or closeout.get("returncode") != 0):
            closeout = closeout if isinstance(closeout, dict) else {}
            record.update(status="failed", error=f"closeout_failed: PR {closeout.get('pr')} exit "
                                                 f"{closeout.get('returncode')}")
    if scratch.settleable(record):
        liveness_kept = _text(record.get("worktree_disposition_reason")) in LIVENESS_REASONS
        _settle_task_scratch(record, liveness_kept=liveness_kept)


def _settle_task_scratch(record: dict, *, liveness_kept: bool = False) -> None:
    """Remove the task's scratch, its write worker's TMPDIR, once the provider is proven gone (scratch.settle).

    The proof is taken fresh from the run's own identity on every pass, whatever the worktree or task status
    says. ``liveness_kept`` is this pass's own verdict that the provider may still run, which keeps it too.
    A service this task started that is not proven stopped once the provider is gone keeps it too: the scratch
    holds the list of them. Only a scope launch's probe sees every descendant from any process, so only a scope
    launch's scratch is released here. A fallback launch's scratch is kept for good (``kept``) and left to
    tmp_sweep's settled-task rule.
    """

    def provider_gone() -> bool:
        return not liveness_kept and _provider_alive(record) is False

    def services_running() -> str | None:
        # Asked only once the provider is proven gone, when no task process is left to register another service.
        # The scratch's task-services.txt names them (dispatch_isolation), so every one is stopped and checked first.
        _stop_task_services(record)
        stops = record.get("task_service_stops")
        unstopped = stops is not None and (not isinstance(stops, list) or any(
            not isinstance(row, dict) or row.get("postcondition") != "true" for row in stops))
        return "task services not proven stopped" if unstopped else None

    if record.get("launch_mode") != "scope":
        kept = "a fallback launch's process tree is never proven gone; kept for tmp_sweep's settled-task rule"
        scratch.settle(record, provider_gone, kept, final=True)
        return
    scratch.settle(record, provider_gone, hold=services_running)


def _reap_worktree(record: dict, patience: float = 0, salvage: bool = False) -> None:
    """Record the task worktree's disposition and remove the task's caches.

    The worktree goes only once the provider's whole process tree is proven gone; liveness that
    cannot be proven either way keeps it. A finished scope launch first ends whatever that scope
    still holds, so a stray descendant cannot keep a published tree. With salvage
    (git.settle_worktree), that proof is taken again with the borrower check, before the salvage
    push and under the removal lock.
    """
    deadline = time.monotonic() + patience
    record.pop("worktree_disposition_detail", None)  # a retry replaces an earlier attempt's detail
    stopped = _end_own_scope(record)
    try:
        if record.get("launch_mode") == "popen-fallback" and stopped is not None and not stopped.ok:
            # Only the process group's id is recorded for a fallback launch, so a descendant that left it is
            # invisible to the liveness check below. Name each survivor so a later retry still sees it.
            survivors = {str(proc.pid): identity for proc in stopped.survivors if (identity := _identity(proc.pid))}
            record["fallback_survivors"] = survivors
            record.update(worktree_disposition="retained",
                          worktree_disposition_reason="provider_processes_alive" if survivors else "provider_liveness_unknown")
            return
        while (alive := _provider_alive(record)) and time.monotonic() < deadline:
            time.sleep(0.05)
        if alive is not False:
            reason = "provider_processes_alive" if alive else "provider_liveness_unknown"
            record.update(worktree_disposition="retained", worktree_disposition_reason=reason)
            return

        def borrowed() -> bool:
            return (salvage and _provider_alive(record) is not False) or _borrowed(record)

        try:
            record.update(git.settle_worktree(record, borrowed=borrowed, salvage=salvage))
        except ImportError as exc:
            # Settlement's code could not be loaded: keep the tree and let the scheduled cleanup, a fresh process, finish.
            record.update(worktree_disposition="retained", worktree_disposition_reason=git.CODE_UNAVAILABLE,
                          worktree_disposition_detail=f"{type(exc).__name__}: {exc}")
        except Exception:
            record.update(worktree_disposition="error", worktree_disposition_reason="settlement_failed")
    finally:
        # After the liveness decision, so the recorded result is not taken while the provider can still
        # write a cache; a retained tree is retried by the sweep, and its caches with it (PR298-F3).
        _clear_task_caches(record)


def _holds_task_caches(record: dict) -> bool:
    """Whether a finished task may still have caches to remove, or a walk or removal failure to retry.

    Decided from the record, without a walk: an empty ``task_cache_residue`` is settlement's empty-cache
    result, anything else recorded is residue to retry, and a record without one (settled before the
    result was recorded, never reaped, or with a provider not yet proven gone) is walked by the locked
    removal, which records its result.
    """
    if not _finished(record):
        return False
    return bool(record.get("task_cache_residue") != [])


def _text(value: object) -> str | None:
    """A record field read for a membership test: a string, else None, so a malformed value never raises."""
    return value if isinstance(value, str) else None


def _finished(record: dict) -> bool:
    """A task with a known terminal status; a malformed or unknown status reads as live."""
    return _text(record.get("status")) in store.TERMINAL


def _unsettled(record: dict) -> bool:
    return _finished(record) and (
        "worktree_disposition" not in record or _text(record.get("worktree_disposition_reason")) in RETRY_REASONS)


def settle_left_worktrees(salvage: bool = False) -> None:
    """Retry finished tasks' worktrees and remove their caches during scheduled cleanup, including crash recovery.

    Admission and worker exit do not call this store-wide sweep. Each worker settles only
    its own task. The hourly hygiene run passes salvage for the trees settlement keeps.
    """
    records = []
    for record in store.list_tasks():
        if record.get("schema_version") == 2:
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                record = _observe(record)
            records.append(record)
    for record in records:
        # A damaged record is skipped, never allowed to end the sweep for the records after it.
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
            _settle_left(record, salvage)


def _settle_left(record: dict, salvage: bool) -> None:
    """One finished task's retry: its worktree, else its caches and scratch, under its task-state lock."""
    worktree = record.get("worktree_path")
    reap = _unsettled(record) and isinstance(worktree, str) and Path(worktree).is_dir()
    if not reap and not _holds_task_caches(record) and not scratch.pending(record):
        return
    path = store.record_path(record["task_id"])
    # wait() takes this lock before it reports a terminal task, so it never returns mid-removal.
    with store.task_state_lock(path):
        current = json_depth.loads(path.read_text(), max_depth=512)
        if not isinstance(current, dict):
            return
        if not reap or not _unsettled(current):
            # A task settled without a worktree, or by an earlier pass, still sheds its caches and scratch.
            changed = _holds_task_caches(current) and _clear_task_caches(current)
            if scratch.pending(current):
                _settle_task_scratch(current)
                changed = True
            if changed:
                store.replace(current, path)
        else:
            _reap_settled(current, salvage=salvage)
            store.replace(current, path)


def _end_lingering(child: subprocess.Popen, spec: TaskSpec) -> None:
    """End a provider that outlived its terminal result, keeping what its launcher still has to write.

    A read-only launcher (AGY's or the write boundary's) records its receipt once the sandbox exits, so only
    the sandbox's tree is killed and the launcher is given time to finish; a write provider gets the same
    grace. Anything still alive after that is killed whole.
    """
    if spec.mode == "workspace-write" or spec.mode == "read-only":
        with contextlib.suppress(psutil.Error):
            for process in psutil.Process(child.pid).children():
                process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=TERMINAL_EXIT_GRACE_S)
    if child.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def _verdict_failure(record: dict, text: str | None) -> str | None:
    """Why a successful review must fail its requested verdict gate (``--require-review-verdict``), or None."""
    if record.get("schema_version") != 2 or record.get("require_review_verdict") is not True:
        return None
    try:
        from lu_machinery.review.verdict_parser import recognized_verdicts

        verdicts = set(recognized_verdicts(text))
    except Exception:
        return "review_gate_error"
    if not verdicts:
        return None if _item_judgments_verdict(record, text) else "review_missing_verdict_line"
    return "review_ambiguous_verdict" if len(verdicts) > 1 else None


def _item_judgments_verdict(record: dict, text: str | None) -> str | None:
    """The verdict a valid ``ukrainian-item-judgments`` block carries, or None.

    The Ukrainian contract with an item denominator has the reviewer return only that
    block (``review-loop/references/ukrainian.md``). The recorder's own parser judges it
    against the brief's frozen packet, as the recorder does.
    """
    if record.get("review_profile") != "ukrainian" or not text:
        return None
    from lu_machinery.review.review_contract import ContractError, item_judgments_verdict, load_frozen_item_packet

    try:
        packet = load_frozen_item_packet(record)
        return item_judgments_verdict(packet, text) if packet is not None else None
    except ContractError:
        return None


def _correction_stdin(prior: str) -> bytes:
    """One format repair. The verdict, findings, and their meaning stay as they were."""
    return (
        "The previous reply is not a recordable review.\n"
        "Return only one JSON object matching code-review-findings.v1.\n"
        "Keep the same verdict, the same findings, and the same meaning.\n"
        "Include confidence and verbatim where the schema requires them.\n\n"
        "Previous reply:\n"
        f"{prior}\n"
    ).encode()


def _format_correction(record: dict, schema: dict | None, text: str | None, launches: int) -> tuple[bytes, dict] | None:
    """A second launch when a code review cannot bind a schema and the reply cannot be a receipt.

    Schema-capable providers fail closed on their own schema check. An ambiguous
    reply is not rewritten into one verdict. One launch is the limit.
    """
    if schema is not None or launches >= 1 or record.get("require_review_verdict") is not True:
        return None
    if not text or not text.strip():
        return None
    from lu_machinery.review.review_contract import (
        ContractError,
        canonical_receipt_payload,
        decision_lock,
        schema_path,
    )

    if schema_path(profile=record.get("review_profile"), task=record) is None:
        return None
    try:
        canonical_receipt_payload(text)
    except ContractError:
        pass
    else:
        return None
    lock = decision_lock(text)
    # A correction is accepted only when this reply already states one decision.
    # With nothing to lock, a second reply would be a new verdict.
    if lock["ambiguous"] or (lock.get("payload") is None and len(lock.get("verdicts") or []) != 1):
        return None
    return _correction_stdin(text), lock


def _receipt_failure(record: dict, text: str | None) -> str | None:
    """A code review settles only when the reply is a canonical receipt.

    A verdict line alone is not a receipt. Ukrainian reviews do not use this
    object, so they keep the verdict gate.
    """
    if record.get("schema_version") != 2 or record.get("require_review_verdict") is not True:
        return None
    from lu_machinery.review.review_contract import ContractError, canonical_receipt_payload, schema_path

    if schema_path(profile=record.get("review_profile"), task=record) is None:
        return None
    try:
        canonical_receipt_payload(text or "")
    except ContractError:
        return _verdict_failure(record, text) or "review_gate_error"
    return None


def _corrected_reply_refusal(lock: dict, text: str | None) -> str | None:
    """Refuse a correction that is still not a receipt, or that changes the locked decision."""
    from lu_machinery.review.review_contract import ContractError, canonical_receipt_payload, same_decision

    try:
        payload = canonical_receipt_payload(text or "")
    except ContractError:
        return _verdict_failure({"schema_version": 2, "require_review_verdict": True}, text) or "review_gate_error"
    if not same_decision(lock, payload):
        return "review_ambiguous_verdict"
    return None


def _adopt_orphans() -> bool:
    """Make this supervisor the parent of every orphaned descendant (``PR_SET_CHILD_SUBREAPER``).

    A Popen-fallback launch has no cgroup. A double-forked descendant would otherwise reparent to init or the
    user manager, out of the supervisor's process tree, and settlement could not tell it from an unrelated process.
    False when the kernel or a confinement policy refuses.
    """
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False


def _read_only_boundary(spec: TaskSpec) -> bool:
    """Whether the supervisor runs this provider inside ``write_boundary``: native read-only routes only.

    Read-only AGY and Muse keep their own sandboxes. A workspace-write worker runs as the dispatching user, in
    its dispatch worktree, with its own provider login, network, ``git`` and ``gh`` (operator order
    NO-WORKER-SANDBOX-1115); the worktree, owned-path hooks, push scan, review and merge queue still apply.
    """
    return spec.mode == "read-only" and spec.agent not in {"agy", "muse"}


def _provider_env(spec: TaskSpec) -> dict[str, str]:
    """Mark the trusted Claude launch before build; supervise always wraps it before Popen."""
    env = child_env(spec)
    if spec.mode == "read-only" and spec.agent == "claude":
        env[write_boundary.BROKER_ENV] = write_boundary._BROKER_INSIDE
    return env


# Weekly plan meters the Monitor routing-budget publishes per pool (model_pause.weekly_used_pct_reading, #581).
_CODEX_ALLOWANCE_POOLS = {"sub": "codex:a", "b-sub": "codex:b"}


def _allowance_pool(record: dict) -> tuple[str, str, claude_account_lane.ClaudeAccount | None] | None:
    """(pool, meter lane, Claude account) for the task's seat, or None when no plan meter funds it.

    An API-key Claude account and an API Codex account are billed per token: they have no allowance.
    """
    agent = record.get("agent")
    if agent == "claude":
        account = claude_account_lane.account_from_record(record, agent="claude")
        if account.refusal is not None or account.label is None or account.api_key:
            return None
        return f"claude:{account.label}", "claude", account
    if agent == "codex":
        try:
            lane = _CODEX_ALLOWANCE_POOLS.get(resolve_codex_account_label(record.get("codex_account")))
        except CodexAccountRefused:
            return None
        return (lane, lane, None) if lane else None
    if agent == "agy":
        model = record.get("model")
        lane = model_pause.AGY_CLAUDE_WINDOW_LANE if isinstance(model, str) and model.startswith("claude-") else "gemini"
        return lane, lane, None
    if agent == "cursor":
        return "cursor", "cursor", None
    return None


def _allowance_reading(pool: tuple[str, str, claude_account_lane.ClaudeAccount | None] | None) -> dict | str:
    """The pool's remaining weekly allowance now, or ``"unknown"``; a missing reading is never 0. Never raises."""
    if pool is None:
        return "unknown"
    try:
        # One bounded telemetry read: never the admission reader's readiness wait or its down memo.
        used = model_pause.weekly_used_pct_reading(pool[1], account=pool[2])
    except Exception:  # the Monitor or its snapshot is unavailable: no reading
        return "unknown"
    if used is None or not math.isfinite(used):
        return "unknown"
    return {"remaining_pct": round(100.0 - min(100.0, max(0.0, used)), 2), "at": store.now()}


def _allowance(record: dict, side: str) -> dict:
    """The record's allowance block with ``side`` ("start" or "finish") read now."""
    previous: dict = record["allowance"] if isinstance(record.get("allowance"), dict) else {}
    try:
        pool = _allowance_pool(record)
    except Exception:  # an unreadable account binding funds no known pool
        pool = None
    block = {"pool": pool[0] if pool else None, "meter": "weekly_remaining_pct",
             "start": previous.get("start", "unknown"), "finish": previous.get("finish", "unknown")}
    block[side] = _allowance_reading(pool)
    return block


def supervise(path: Path, options: dict | None = None, hard_timeout: float | None = None) -> None:
    os.umask(0o077)
    record = json_depth.loads(path.read_text(), max_depth=512)
    load_post_start_code(bool(record.get("review_attempt")))
    if record.get("require_review_verdict") is True:
        # The code this supervisor loaded; settlement records it again (#12399).
        record["dispatcher_code_start"] = _dispatcher_code()
    spec = _spec(record, options or {})
    options = spec.options
    record["allowance"] = _allowance(record, "start")
    hard_timeout, silence_timeout, initial_response_timeout = deadlines({
        **options, **{key: record[key] for key in ("hard_timeout", "silence_timeout", "initial_response_timeout")
                     if key in record},
        **({"hard_timeout": hard_timeout} if hard_timeout is not None else {}),
    })
    child, status, returncode, error, usage, text = None, "crashed", None, None, None, None
    slots = None
    resolved_model = sources_calls = source = None
    started = time.monotonic()
    cancel_requested = interruptible = False

    def cancelled(_signum, _frame):
        # A handler's exception lands at an arbitrary bytecode, where Python can drop it (a weakref
        # callback) or a caller can convert it (#11994). Record the request; checkpoint() raises it,
        # settlement honours it, and only a wait marked interruptible, built to unwind there, is
        # stopped in place.
        nonlocal cancel_requested, interruptible
        cancel_requested = True
        if interruptible:
            interruptible = False  # one raise per run: a later signal cannot interrupt cleanup
            raise _Cancelled()

    def checkpoint():
        if cancel_requested:
            raise _Cancelled()

    def terminate():
        # Only an unreaped leader still pins its group id; a reaped one's id may name another group.
        if child and child.returncode is None and (
            spec.mode == "workspace-write" or spec.mode == "read-only"
        ):
            # A read-only launcher ends its sandbox and records its receipt; a write provider may exit cleanly.
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=TERMINAL_EXIT_GRACE_S)
        if child and child.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()

    previous_term = signal.signal(signal.SIGTERM, cancelled)
    previous_int = signal.signal(signal.SIGINT, cancelled)
    stdout_path = path.parent / "logs" / f"{spec.task_id}.stdout.log"
    stderr_path = path.parent / "logs" / f"{spec.task_id}.stderr.log"
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
        if record.get("launch_mode") == "popen-fallback" and not _adopt_orphans():
            # Without it a double-forked descendant would escape settlement's sweep unseen.
            status, error = "failed", "fallback_adoption_unavailable: PR_SET_CHILD_SUBREAPER was refused"
            return
        try:
            headroom.check_plan(spec.agent, spec.model, record, review=admission.review_typed(record))
            headroom.check_runtime(spec.agent, spec.model, codex_account=record.get("codex_account"))
        except ValueError as exc:
            status, error = "failed", str(exc)
            return
        except headroom.RateLimitedError as exc:
            status, error = "rate_limited", str(exc)
            return
        if record.get("review_attempt"):
            # A bound formal attempt runs only inside the agent runtime's attempt boundary, with the rendered
            # prompt's exact bytes: no dispatch context, no task-store grant, no ordinary launch (#12114).
            from lu_machinery.dispatcher import review_attempt

            prompt = spec.prompt_file.read_bytes()
            record.update(status="running", runtime_input_sha256=hashlib.sha256(prompt).hexdigest())
            store.replace(record, path, require_existing=True)
            interruptible = True  # cancel unwinds the runtime, which kills the seat and tears the boundary down
            try:
                checkpoint()
                outcome = review_attempt.run(spec, record, prompt.decode("utf-8"),
                                             (hard_timeout, silence_timeout, initial_response_timeout))
            finally:
                interruptible = False
            status, error, text, sources_calls = outcome.status, outcome.error, outcome.text, outcome.sources_calls
            record.update(outcome.identity)
            if outcome.trace:
                # The runtime keeps the seat's stream to itself; its redacted tool-call trace takes the stream's
                # place in the task's private stdout log.
                with stdout_path.open("w", encoding="utf-8") as trace:
                    trace.writelines(json.dumps(call, ensure_ascii=False) + "\n" for call in outcome.trace)
            if status == "done" and (refusal := _verdict_failure(record, text)):
                status, error = "failed", refusal
            return
        schema = None
        if options.get("output_schema_path") is not None:
            from lu_machinery.dispatcher.output_schema import load

            try:
                schema = load(options)
            except (OSError, ValueError):
                status, error = "failed", "output schema binding invalid"
                return
        provider = providers.load(spec.agent)
        if spec.mode == "workspace-write":
            try:
                # The provider's TMPDIR (environment.child_env), made before any launch check runs a provider CLI
                # with it, and persisted so settlement removes only this directory.
                scratch.start(record, spec.task_id)
                store.replace(record, path, require_existing=True)
            except (OSError, ValueError) as exc:
                status, error = "failed", f"task_scratch_unavailable: {exc}"
                return
        invocation = runtime_context.build(spec, _provider_env(spec))
        record["runtime_input_sha256"] = hashlib.sha256(invocation.stdin or b"").hexdigest()
        pending_conversation = None
        pending_correction = None
        correction_launches = 0
        locked = None
        continuations = 0
        parsed = None
        try:
            invocation = admission.claude_launch(spec, invocation)
            if spec.agent == "claude" and spec.claude_account not in (None, "a"):
                from lu_machinery.agent_runtime.attempt_boundary import claude_account_auth_receipt

                # Value-free: label, source and the verified method (``api_key_helper`` bills that key).
                record["claude_account_auth"] = claude_account_auth_receipt(
                    spec.claude_account, source=record.get("claude_account_source")
                )
            # Checked again with the launch's own environment: a managed policy may appear after dispatch.
            if spec.mode == "read-only" and (refusal := providers.read_only_refusal(spec.agent, invocation.env)):
                raise ValueError(refusal)
        except ValueError as exc:
            status, error = "failed", str(exc)
            return
        if spec.agent == "codex" and spec.mode == "read-only":
            try:
                # Persisted before the lease exists, so crash recovery under another scratch root still finds it.
                record["read_only_scratch"] = str(dispatch_lease(spec.task_id))
                store.replace(record, path, require_existing=True)
                create_dispatch_lease(spec.task_id, spec.worktree)
            except (OSError, ValueError) as exc:
                status, error = "failed", f"read_only_scratch_unavailable: {exc}"
                return
        # The slot queue can wait for an hour and discards its ticket on any exception, so cancel stops it in place.
        interruptible = True
        try:
            checkpoint()  # after the region opens: a request from before it cannot wait out the queue
            slots, slots_record = admission.hold_review_test_slots(
                agent=spec.agent, mode=spec.mode, review_typed=admission.review_typed(record),
                sealed_attempt=bool(record.get("review_attempt")), task_id=spec.task_id,
            )
        finally:
            interruptible = False
        if slots_record is not None:
            record["review_test_slots"] = slots_record
        while True:
            correcting = pending_correction is not None
            if pending_conversation:
                invocation = runtime_context.build(spec, _provider_env(spec), conversation=pending_conversation)
            elif correcting:
                invocation = runtime_context.build(spec, _provider_env(spec), launch_prompt=pending_correction)
                pending_correction = None
            # The first launch keeps the timeout the caller passed. Only a continuation
            # spends what remains of that same deadline, and a spent deadline does not
            # become the unlimited timeout a zero means.
            launch_hard = hard_timeout
            if (pending_conversation or correcting) and hard_timeout > 0:
                launch_hard = max(0.0, hard_timeout - (time.monotonic() - started))
                if launch_hard <= 0 and parsed is not None:
                    if pending_conversation:
                        parsed = _agy_unfinished(parsed)
                    text = parsed.text
                    break
            with tempfile.TemporaryFile(dir=ensure_scratch_root()) as stdin, stdout_path.open("wb") as out, stderr_path.open(
                "wb" if pending_conversation else "ab"
            ) as err:
                stdin.write(invocation.stdin or b"")
                stdin.seek(0)
                output_sizes = out.tell(), err.tell()
                checkpoint()  # the last one before the provider starts: a cancelled run launches nothing
                if _read_only_boundary(spec):
                    # Native reviewers run inside the OS boundary (#423, #100); there is no unsandboxed fallback.
                    # The launcher refuses with its receipt when the boundary cannot start. A relaunch always
                    # rebuilds the invocation first, so it is wrapped exactly once.
                    try:
                        invocation = replace(invocation, argv=write_boundary.launcher(spec, invocation, path))
                    except ValueError as exc:
                        status, error = "failed", str(exc)
                        return
                child = subprocess.Popen(
                    invocation.argv,
                    stdin=stdin,
                    stdout=out,
                    stderr=err,
                    cwd=spec.worktree,
                    # The CLI's governed test runs join this hold through its marker (#11230).
                    env={**invocation.env, **slots.environment()} if slots else invocation.env,
                    start_new_session=True,
                )
                record.update(status="running", provider_pgid=child.pid)
                store.replace(record, path, require_existing=True)
                terminal_observer = getattr(provider, "terminal_result_observer", None)
                source = getattr(provider, "MODEL_SOURCE", None)
                observe_model = provider.model_observer(stdout_path) if source else None

                def watch(observe_model=observe_model, source=source):
                    # A running author's commits are reviewable only once its record attests the model (#12014).
                    nonlocal resolved_model
                    checkpoint()
                    if observe_model is None or source is None:
                        return
                    resolved_model = observe_model()
                    fields = _stream_attestation(source, resolved_model)
                    if any(record.get(key) != value for key, value in fields.items()):
                        record.update(fields)
                        store.replace(record, path, require_existing=True)

                lingered = communicate(child, invocation, stdout_path, stderr_path, launch_hard, silence_timeout,
                                       initial_response_timeout, liveness_paths(spec, invocation), output_sizes,
                                       terminal_observer(stdout_path) if terminal_observer else None, watch)
            if lingered:
                _end_lingering(child, spec)
            returncode = child.returncode
            # A provider ended after its terminal result is judged by that result, not by how it was ended.
            exit_code = 0 if lingered else returncode
            final = invocation.final_file
            final_text = final.read_text(errors="replace") if final and final.exists() else None
            stdout = stdout_path.read_text(errors="replace")
            parse_options = (
                {"output_schema": schema} if spec.agent in {"agy", "claude"} and schema is not None else {}
            )
            parsed = provider.parse(stdout, stderr_path.read_text(errors="replace"), exit_code, final_text, **parse_options)
            text, usage, sources_calls = parsed.text, parsed.usage, parsed.sources_mcp_call_count
            # Once parsed, the whole stream decides the model, whatever settlement follows (not the running value).
            resolved_model = parsed.resolved_model
            conversation, parsed = _agy_follow_up(
                spec, record, parsed, started, hard_timeout, continuations, checkpoint)
            if conversation is not None:
                pending_conversation = conversation
                continuations += 1
                continue
            text = parsed.text
            if parsed.error is None and (
                correction := _format_correction(record, schema, text, correction_launches)
            ):
                pending_correction, locked = correction
                correction_launches += 1
                continue
            break
        store.begin_settlement(record)
        if _read_only_boundary(spec):
            # Only the receipt the launcher writes outside the sandbox proves the run was contained (#423).
            boundary, refusal = write_boundary.verify(path, spec.task_id, spec.mode)
            record["write_boundary"] = boundary
            if refusal:
                status, error = "failed", refusal
                return
        if spec.agent == "agy" and spec.mode == "read-only":
            # Only the boundary's receipt, written outside the sandbox, proves the run read-only and
            # supplies its Sources count (C-3027); without a verified one the run fails.
            boundary, sources_calls, refusal = agy_sandbox.verify(spec.final_file, spec.task_id, record)
            record["read_only_boundary"] = boundary
            if refusal:
                status, error = "failed", refusal
                return
        if parsed.rate_limited and exit_code >= 0:
            # Before the schema check, which would record the same terminal failure as invalid output.
            status, error = "rate_limited", parsed.error
            return
        status = "done" if exit_code == 0 and not parsed.error and text and text.strip() else "failed"
        if exit_code < 0:
            status = "crashed"
        if spec.mode == "read-only" and exit_code == 0 and not (text and text.strip()) and parsed.error in {
            None, "codex: missing completion or final text", "claude: missing final text", "cursor: missing result",
            "agy: missing final text", "muse missing final response", "opencode missing final response",
        }:
            status = "no_deliverable"
            record["no_deliverable_reason"] = "missing_final_message"
        # A provider failure can be answered by merged delivery only after the same integrity
        # gates as success. Read-only failures cannot be rescued by merged delivery. A run that ended
        # with its work still open (a child task or background command) did not finish, so merged work
        # is not its result (#12156).
        record["provider_terminal_failure"] = status == "failed" and not parsed.incomplete
        if status != "done":
            error = parsed.error or (
                "provider exited unsuccessfully" if exit_code else "provider returned no successful final response"
            )
        if record["provider_terminal_failure"]:
            record.update(provider_terminal_status="failed", provider_terminal_error=error)
        validate = status == "done" or (spec.mode == "workspace-write" and record["provider_terminal_failure"])
        if validate and schema is not None and (spec.agent != "agy" or record["provider_terminal_failure"]):
            from lu_machinery.agent_runtime.adapters._output_schema import json_value, structured_result

            # A provider failure is already recorded in provider_terminal_error; for its merged-delivery
            # rescue this gate checks only the reply's content, so the exit code alone cannot refuse it.
            # The provider's own exit code stays recorded (returncode) for lane health.
            result = structured_result(
                json_value(text or ""), schema,
                returncode=0 if record["provider_terminal_failure"] else exit_code, terminal_ok=True,
            )
            if not result.ok:
                refusal = f"{result.failure_code}: {result.stderr_excerpt}"
                if record["provider_terminal_failure"]:
                    record["provider_failure_rescue_refusal"] = refusal
                else:
                    status, error = "failed", refusal
                record["provider_terminal_failure"] = False
                return
            text = result.response
        if validate:
            refusal = None
            if source == "agy-stream-json":
                from lu_machinery.dispatcher.providers.agy import identity_failure

                refusal = identity_failure(spec.model, resolved_model)
            if not refusal and spec.agent == "claude":
                # Another executor model never settles done, including after a provider error (#12094).
                from lu_machinery.dispatcher.providers.claude import MODEL_USAGE_SOURCE, executor_failure

                if (attested := executor_failure(spec, stdout)) is not None:
                    record.update(_stream_attestation(MODEL_USAGE_SOURCE, attested[0]))
                    refusal = attested[1]
            if not refusal and locked is not None:
                refusal = _corrected_reply_refusal(locked, text)
            if not refusal:
                refusal = _receipt_failure(record, text)
            if not refusal:
                refusal = _verdict_failure(record, text)
            if refusal:
                if record["provider_terminal_failure"]:
                    record["provider_failure_rescue_refusal"] = refusal
                else:
                    status, error = "failed", refusal
                record["provider_terminal_failure"] = False
    except _Cancelled:
        status, error = "cancelled", "cancelled by client"
    except DeadlineExpired as exc:
        status, error = "timeout", exc.diagnostic
        record.update(kill_reason=exc.kind, stderr_excerpt=exc.diagnostic)
    except subprocess.TimeoutExpired:
        status, error = "timeout", "process deadline exceeded"
    except store.TaskRecordError as exc:
        status, error = "crashed", exc.code
    except Exception:
        status, error = "crashed", "supervisor or provider processing failed"
    finally:
        if cancel_requested:
            # A cancel requested before settlement decides the run, whatever exit the work took after it.
            status, error = "cancelled", "cancelled by client"
        try:
            if child and (child.returncode is None or status in {"cancelled", "timeout", "crashed"}):
                terminate()
                returncode = child.returncode
            if not record.get("settling_at"):
                try:
                    store.begin_settlement(record)
                except store.TaskRecordError as exc:
                    status, error = "crashed", exc.code
            # A killed launcher leaves its scratch; residue is recorded, never raised.
            if _read_only_boundary(spec) and (residue := write_boundary.discard(spec.task_id)):
                record["write_boundary_cleanup"] = residue
            if spec.agent == "agy" and spec.mode == "read-only":
                # A bound attempt already counted its Sources bodies and replaced the stream with
                # that trace. The stream merger only understands step updates, so running it here
                # would replace the attempt's counts with zeros.
                if not record.get("review_attempt"):
                    # The launcher exports saved tool bodies before it deletes the scratch. Fold them
                    # into the retained stream first; discard then removes whatever scratch remains.
                    try:
                        retained, missing = agy_sandbox.apply_tool_results(stdout_path, spec.final_file, spec.task_id)
                        record["sources_bodies_retained"] = retained
                        record["sources_bodies_missing"] = missing
                    except (OSError, ValueError):
                        record["sources_bodies_missing"] = None
                if residue := agy_sandbox.discard(spec.task_id):
                    record["read_only_boundary_cleanup"] = residue
            _discard_read_only_scratch(record)
            if slots is not None:
                # The hold is the supervisor's: a killed supervisor ends it, and the governed runs in flight
                # keep their slots shared until they exit (test_slots).
                slots.release()
            if text is not None:
                spec.final_file.write_text(text)
                spec.final_file.chmod(0o600)
                if record.get("advisory_role") is not None:
                    _seal_advisory_result(record, spec.final_file)
            record["allowance"] = _allowance(record, "finish")
            record.update(
                status=status,
                returncode=returncode,
                error=error,
                usage=usage,
                finished_at=store.now(),
                duration_s=round(time.monotonic() - started, 3),
                result_sha256=store.digest(spec.final_file.read_bytes()) if spec.final_file.exists() else None,
            )
            if sources_calls is not None:
                record["sources_mcp_call_count"] = sources_calls
            if record.get("require_review_verdict") is True:
                record["dispatcher_code"] = _dispatcher_code()
            record.update(git.snapshot_delivery(spec, record))
            if error and error.startswith("TASK_RECORD_"):
                record.update(worktree_disposition="retained", worktree_disposition_reason="task_record_damaged")
            else:
                _enforce_delivery(record)
            if children := _running_children(record):
                # A run that ends while a task it dispatched still runs left that result unconsumed (#12156).
                record["orphaned_child_tasks"] = children
            if source:
                record.update(_stream_attestation(source, resolved_model))
            record["provider_pgid"] = child.pid if child else None
            # The removal chokepoint releases a tree only to a finished owner record, so publish it, then
            # settle; wait() reads a terminal record under this lock and never sees the tree mid-removal.
            with store.task_state_lock(path):
                # A cancel arriving during cleanup still decides the run until publication, never after: a
                # published terminal record is never rewritten, and cancel() returns one unchanged. Signals
                # held across the check and the write make publication that exact cutoff (the mask is this
                # thread's; the supervisor runs no other).
                mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
                try:
                    if cancel_requested and record["status"] != "cancelled":
                        record.pop("no_deliverable_reason", None)
                        record.update(status="cancelled", error="cancelled by client")
                    if (children := record.get("orphaned_child_tasks")) and record["status"] != "done":
                        record["error"] = f"orphaned_child_tasks: {', '.join(children)}; {record['error']}"
                    try:
                        store.replace(record, path, require_existing=True)
                    except store.TaskRecordError:
                        store.publish_failed_record(record, path)
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                if record.get("worktree_disposition_reason") != "task_record_damaged":
                    _reap_settled(record, patience=SETTLE_PATIENCE)
                try:
                    store.replace(record, path, require_existing=True)
                except store.TaskRecordError:
                    store.publish_failed_record(record, path)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)


def _read_only(record: dict | None) -> dict:
    """A record as read, with no settling: what a waiter inside a write boundary sees."""
    if record is None:  # store.read raises for a missing record unless asked not to
        raise ValueError("task record is missing")
    return record


def wait(task_id: str, nonce: str | None = None, timeout: float = 0, poll_interval: float = 1) -> tuple[dict, int]:
    deadline = time.monotonic() + timeout if timeout else None
    # Inside a write boundary (#423) the task store is read-only and host processes are in another PID namespace,
    # so a waiter there only reads: settling a crashed supervisor stays with the host's own observers.
    inside = bool(os.environ.get(write_boundary.BROKER_ENV))
    while True:
        record = _read_only(store.read(task_id, nonce)) if inside else _observe(store.read(task_id, nonce))
        if record["status"] not in store.ACTIVE:
            if record.get("schema_version") == 2 and not inside:
                # Crash recovery settles a terminal task under this lock; report it only after that ends.
                with store.task_state_lock(store.record_path(task_id)):
                    record = store.read(task_id, nonce)
            return record, 0 if record["status"] == "done" else 1
        remaining = deadline - time.monotonic() if deadline else None
        if remaining is not None and remaining <= 0:
            return record, 124
        time.sleep(min(poll_interval, remaining) if remaining is not None else poll_interval)


def cancel(task_id: str, nonce: str | None = None) -> dict:
    record = _observe(store.read(task_id, nonce))
    if record.get("schema_version") != 2:
        raise ValueError("legacy task cancellation belongs to its original supervisor")
    if record["status"] in store.ACTIVE:
        if not record.get("pid") or not record.get("process_start"):
            raise ValueError("supervisor identity is unavailable; cancellation refused")
        try:
            pidfd = os.pidfd_open(record["pid"])
            try:
                if _identity(record["pid"]) != record["process_start"]:
                    raise ValueError("supervisor identity changed; cancellation refused")
                signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            finally:
                os.close(pidfd)
        except ProcessLookupError:
            return _observe(record)
        return wait(task_id, nonce, timeout=10, poll_interval=0.05)[0]
    return record


if __name__ == "__main__":
    # Run as -m, this module is __main__; a later `import scripts.dispatcher.worker` (the removal chokepoint's
    # liveness proof) must get this running copy, not a second one loaded from a checkout that has moved since.
    sys.modules.setdefault("lu_machinery.dispatcher.worker", sys.modules[__name__])
    settings = json_depth.load(sys.stdin, max_depth=512)
    supervise(Path(sys.argv[1]), **settings)
