"""Legacy host/credit admission and the atomic pre-launch write-slot hold.

Checks host/credit admission before reserving a write-slot hold.
The retained admission scanner and ownership ledger read the same hold format.
"""

import contextlib
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from lu_machinery.common import json_depth, safe_open
from lu_machinery.common.task_store_paths import tasks_dir
from lu_machinery.dispatcher import store
from lu_machinery.orchestration import dispatch_admission as legacy
from lu_machinery.orchestration import worktree_prep
from lu_machinery.orchestration.dead_worker_state import (
    mark_dead_worker_terminal,
    mark_orphaned_admission_hold_crashed,
    mark_orphaned_worktree_prep_crashed,
    worker_liveness,
)
from lu_machinery.orchestration.task_record_store import task_state_lock


class Refused(ValueError):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def _write(path, record):
    # Shared healers recheck the nonce/status/identity under task_state_lock.
    if record.get("schema_version") == 2:
        store.replace(record, path)
    else:
        from lu_machinery.orchestration.task_record_store import write_record_unlocked

        write_record_unlocked(path, record)


def heal_dead(path, state):
    """Adapter for legacy _heal_dead_task: only proven death permits a write."""
    if state.get("status") not in legacy.ACTIVE_STATUSES:
        return
    if state.get("pid"):
        mark_dead_worker_terminal(
            path, state, source="admission", terminal_status="crashed",
            allowed_statuses=("running", "spawning"), pid_alive=legacy.process_alive,
            resolve_head=_head, write=_write,
        )
    elif worktree_prep.is_orphaned_prep_record(state):
        mark_orphaned_worktree_prep_crashed(
            path, state, source="admission", is_orphaned=worktree_prep.is_orphaned_prep_record, write=_write,
        )
    elif legacy.is_orphaned_admission_hold(state):
        mark_orphaned_admission_hold_crashed(
            path, state, source="admission", is_orphaned=legacy.is_orphaned_admission_hold,
            reason=legacy.ORPHANED_HOLD_REASON, write=_write,
        )


def _head(root):
    from lu_machinery.dispatcher.git import _git

    try:
        return _git(root, "rev-parse", "HEAD")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _monitor_json(read):
    try:
        return read()
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return None


def _meter_budget(spec):
    """Read the routing budget outside the admission lock for a metered write."""
    if spec is None or spec.mode not in legacy.WRITE_CAPABLE_MODES:
        return None, None
    meter = legacy.subscription_meter(
        spec.agent, model=spec.model, claude_account=spec.claude_account, codex_account=spec.codex_account,
    )
    if meter is None:
        return None, None
    from lu_machinery.dispatcher.pool_selector import read_budget

    return meter, _monitor_json(read_budget)


def evaluate(mode, *, sweep, thresholds=None, disk_guard=True, meter=None, budget=None, orchestrator=False):
    """Copy-adapted from delegate._evaluate_dispatch_admission, including v2 liveness.

    Disk admission measures, alerts and refuses; it never removes anything. Each task's creator
    removes its own worktree at settlement (worker._reap_settled), so no reaper runs here.
    """
    return legacy.evaluate(
        mode, tasks_dir(), pid_alive=legacy.process_alive,
        record_liveness=lambda record: worker_liveness(record, pid_alive=legacy.process_alive),
        on_dead=heal_dead if sweep else None, thresholds=thresholds, disk_guard=disk_guard,
        meter=meter, meter_budget=budget, orchestrator=orchestrator,
    )


def allows(decision, force_reason):
    """Force may override host checks, except the disk and subscription meter."""
    return decision.exempt or decision.admitted or (
        force_reason is not None and not (decision.disk_refused or decision.meter_refused)
    )


def report(decision, *, force_reason=None):
    """Legacy refusal/admit/override text and exits."""
    if decision.exempt:
        return
    if decision.admitted:
        print(f"🚦 dispatch admission: admitted — {decision.summary()}", file=sys.stderr)
    elif allows(decision, force_reason):
        print(
            f"⚠️  dispatch admission overridden by --force-admission ({force_reason!r}): {'; '.join(decision.failures)}",
            file=sys.stderr,
        )
    else:
        raise Refused(f"❌ {decision.refusal_line()}", 4 if decision.disk_refused else 3)


def precheck(mode, *, force_reason=None):
    if force_reason is not None:
        return
    try:
        decision = legacy.precheck(mode)
    except ValueError as exc:
        raise Refused(f"❌ invalid dispatch admission threshold: {exc}", 2) from exc
    if decision is not None:
        report(decision)


def check(mode, *, dry_run, disk_guard, force_reason=None, spec=None, orchestrator=None):
    try:
        meter, budget = _meter_budget(spec)
        decision = evaluate(mode, sweep=not dry_run, disk_guard=disk_guard, meter=meter, budget=budget,
                            orchestrator=orchestrator is not None)
    except ValueError as exc:
        raise Refused(f"❌ invalid dispatch admission threshold: {exc}", 2) from exc
    report(decision, force_reason=force_reason)
    return decision


# One orchestrator turn's stream reservation (DRIVERS-1122): batch_state/streams/<stream>.reservation, beside
# the task store whose admission lock serializes every dispatcher. That file alone says a stream is occupied:
# no task record, archive or record damage frees or holds a stream. reserve() publishes it under the admission
# lock before the turn's hold; settlement unlinks it last, once the run is proven gone (worker._release_stream).
# Nothing else removes it: there is no expiry, reclaim or reconcile command in PR 1.
# {"schema": RESERVATION_SCHEMA, "stream": "epic:<N>", "task_id": <id>, "run_nonce": <32 hex>,
#  "worktree_path": <the turn's absolute tree path>, "reserved_at": <ISO-8601 UTC>}
RESERVATION_SCHEMA = "lu.dispatch.stream-reservation.v1"
RESERVATIONS_DIR_NAME = "streams"
RESERVATION_SUFFIX = ".reservation"
_RESERVATION_MAX_BYTES = 16 * 1024
_RESERVATION_FIELDS = frozenset({"schema", "stream", "task_id", "run_nonce", "worktree_path", "reserved_at"})
# The most entries a reservation directory holds before a scan refuses: one per stream, never this many.
_RESERVATION_MAX_ENTRIES = 1024
_NONCE = re.compile(r"[0-9a-f]{32}")


class ReservationUnsafe(Exception):
    """A reservation (or its directory) that cannot be read as one: it holds, with this typed reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def reservations_dir() -> Path:
    """``batch_state/streams``: beside the task store, whose admission lock guards every reservation write."""
    return tasks_dir().parent / RESERVATIONS_DIR_NAME


def _reservations_fd(*, create: bool) -> int | None:
    """A descriptor of the verified reservation directory, or None when it does not exist (never with ``create``).

    The directory is ``streams`` beside the task store, opened without following a symlink; it must be a
    directory the current user owns that no group or other user can write.
    """
    parent = tasks_dir().parent
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        if create:
            raise ReservationUnsafe("orchestrator_reservation_dir_unsafe: the task store's parent is missing") from None
        return None
    except OSError as exc:
        raise ReservationUnsafe(f"orchestrator_reservation_dir_unreadable: {type(exc).__name__}") from None
    try:
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(RESERVATIONS_DIR_NAME, 0o700, dir_fd=parent_fd)
        try:
            fd = os.open(RESERVATIONS_DIR_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=parent_fd)
        except FileNotFoundError:
            if create:
                raise ReservationUnsafe("orchestrator_reservation_dir_unsafe: the directory vanished") from None
            return None
        except OSError as exc:  # ELOOP: a symlink; ENOTDIR: a file
            raise ReservationUnsafe(f"orchestrator_reservation_dir_unsafe: not a directory ({type(exc).__name__})") \
                from None
    finally:
        os.close(parent_fd)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        os.close(fd)
        raise ReservationUnsafe("orchestrator_reservation_dir_unsafe: not owned by this user, or writable by others")
    return fd


def _unique_keys(pairs):
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _read_reservation(dir_fd: int, name: str) -> dict | None:
    """The valid reservation stored at ``name``, or None when that entry is missing.

    Read without following a symlink or blocking on a FIFO, and at most ``_RESERVATION_MAX_BYTES``; any other
    state raises ``ReservationUnsafe``.
    """
    stream = name.removesuffix(RESERVATION_SUFFIX)
    try:
        fd = safe_open.safe_open_below(dir_fd, name, os.O_RDONLY, blocking=False)
    except FileNotFoundError:
        return None
    except safe_open.UnsafeEntryError:
        raise ReservationUnsafe(f"orchestrator_reservation_unsafe: {stream:.80}'s reservation is a symlink, not a "
                                "regular file, or not owned by this user") from None
    except OSError as exc:
        raise ReservationUnsafe(f"orchestrator_reservation_unreadable: {stream:.80} ({type(exc).__name__})") from None
    chunks, size = [], 0
    try:
        while size <= _RESERVATION_MAX_BYTES and (chunk := os.read(fd, _RESERVATION_MAX_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        raise ReservationUnsafe(f"orchestrator_reservation_unreadable: {stream:.80} ({type(exc).__name__})") from None
    finally:
        os.close(fd)
    if size > _RESERVATION_MAX_BYTES:
        raise ReservationUnsafe(f"orchestrator_reservation_oversized: {stream:.80}")
    try:
        value = json_depth.loads(b"".join(chunks).decode("utf-8"), max_depth=2, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):
        value = None
    if not (isinstance(value, dict) and set(value) == _RESERVATION_FIELDS and value["schema"] == RESERVATION_SCHEMA
            and value["stream"] == stream and _valid_task_id(value["task_id"])
            and isinstance(value["run_nonce"], str) and _NONCE.fullmatch(value["run_nonce"])
            and _usable_path(value["worktree_path"]) and store.parse_utc_timestamp(value["reserved_at"]) is not None):
        raise ReservationUnsafe(f"orchestrator_reservation_malformed: {stream:.80}")
    return value


def _usable_path(value) -> bool:
    """An absolute path string the filesystem calls accept: no NUL, and encodable as a file name."""
    if not (isinstance(value, str) and os.path.isabs(value) and "\x00" not in value):
        return False
    try:
        os.fsencode(value)
    except UnicodeError:
        return False
    return True


def _valid_task_id(value) -> bool:
    try:
        return isinstance(value, str) and bool(store.record_path(value))
    except ValueError:
        return False


def _busy(reservation: dict) -> str:
    return (f"orchestrator_stream_busy: {reservation['stream']} is reserved by turn {reservation['task_id']} "
            f"(run {reservation['run_nonce']}) until its settlement proves the run gone")


def stream_refusal(stream: str) -> str | None:
    """Why ``stream`` is not free (reserved, or its reservation unreadable), or None; reads and writes nothing else."""
    try:
        fd = _reservations_fd(create=False)
        if fd is None:
            return None
        try:
            current = _read_reservation(fd, stream + RESERVATION_SUFFIX)
        finally:
            os.close(fd)
    except ReservationUnsafe as exc:
        return exc.reason
    except OSError as exc:
        return f"orchestrator_reservation_unreadable: {type(exc).__name__}"
    return None if current is None else _busy(current)


def _publish_reservation(dir_fd: int, stream: str, task_id: str, run_nonce: str, worktree_path: str) -> None:
    """Create ``stream``'s reservation exclusively and make it durable; a crash part way leaves it held (malformed)."""
    name = stream + RESERVATION_SUFFIX
    if not _usable_path(worktree_path):
        raise ReservationUnsafe("orchestrator_reservation_refused: the turn's tree path must be an absolute file name")
    body = json.dumps(dict(schema=RESERVATION_SCHEMA, stream=stream, task_id=task_id, run_nonce=run_nonce,
                           worktree_path=worktree_path, reserved_at=store.now()), sort_keys=True).encode() + b"\n"
    if len(body) > _RESERVATION_MAX_BYTES:
        raise ReservationUnsafe("orchestrator_reservation_oversized: the turn's tree path is too long")
    fd = safe_open.safe_open_below(dir_fd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(dir_fd)
    except OSError:
        # This call created the entry (O_EXCL) under the admission lock, and nothing has launched yet.
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)
        raise


def release_stream(stream: str, task_id: str, run_nonce: str) -> str | None:
    """Unlink ``stream``'s reservation when it is exactly this run's; None once released, else why it stays.

    Taken under the admission lock (never inside a task-state lock), like publication.
    """
    try:
        with legacy.admission_lock(tasks_dir()):
            fd = _reservations_fd(create=False)
            if fd is None:
                return "orchestrator_reservation_missing"
            try:
                name = stream + RESERVATION_SUFFIX
                current = _read_reservation(fd, name)
                if current is None:
                    return "orchestrator_reservation_missing"
                if current["task_id"] != task_id or current["run_nonce"] != run_nonce:
                    return "orchestrator_reservation_foreign: the reservation names another task or run"
                os.unlink(name, dir_fd=fd)
                os.fsync(fd)
            finally:
                os.close(fd)
    except ReservationUnsafe as exc:
        return exc.reason
    except (OSError, legacy.AdmissionLockTimeout) as exc:
        return f"orchestrator_reservation_release_failed: {type(exc).__name__}"
    return None


def turn_writer_refusal(*, task_ids=(), tree=None) -> str | None:
    """Why a task service or a ``--cwd`` attachment would add a writer to an orchestrator turn, or None.

    It would when a reservation names one of ``task_ids`` or the tree ``tree``. Attribution is the
    reservation, so a turn record that lost its ``orchestrator`` key is still a turn. A missing task store,
    an unsafe or unlistable directory, a foreign entry or any unreadable reservation refuses as well:
    whose it is is unknown.
    """
    from lu_machinery.session_streams.model import STREAM_ID_RE

    if tree is not None:
        from lu_machinery.dispatcher.worker import _same_tree

    if not tasks_dir().is_dir():
        return "orchestrator_reservation_unknown: the task store is missing"
    try:
        fd = _reservations_fd(create=False)
        if fd is None:
            return None
        try:
            names = os.listdir(fd)
            if len(names) > _RESERVATION_MAX_ENTRIES:
                return "orchestrator_reservation_unknown: the reservation directory holds too many entries"
            for name in sorted(names):
                stream = name.removesuffix(RESERVATION_SUFFIX)
                if name == stream or not STREAM_ID_RE.fullmatch(stream) or not stream.startswith("epic:"):
                    return "orchestrator_reservation_unknown: the reservation directory holds a foreign entry"
                current = _read_reservation(fd, name)
                if current is None:
                    continue  # released since the listing
                if current["task_id"] in task_ids or (tree is not None and _same_tree(current["worktree_path"], tree)):
                    return (f"orchestrator_turn_writer_refused: {current['task_id']} is the orchestrator turn on "
                            f"{stream}, which writes no product change")
        finally:
            os.close(fd)
    except ReservationUnsafe as exc:
        return exc.reason
    except (OSError, ValueError) as exc:
        return f"orchestrator_reservation_unknown: {type(exc).__name__}"
    return None


# The states Monitor's /api/epics/v1 projection names (epics_router._lease_payload; SessionState).
_LEASE_STATES = frozenset({"active", "released", "expired"})
_SESSION_STATES = frozenset({"open", "rolling", "closed", "expired"})


def legacy_lease_refusal(stream):
    """Why a live legacy driver lease on ``stream`` refuses an orchestrator turn, or None when there is none.

    Monitor is the lease authority (a local process never proves a remote lease gone); an
    unreachable, refused or malformed answer refuses, as does an active lease of an open or
    rolling session that has not expired.
    """
    from lu_machinery.session_supervisor.remote import RemoteEpicClient

    try:
        response = RemoteEpicClient().stream(stream, digest_limit=1)
    except Exception as exc:  # the lease authority's boundary: any failure is unknown, never "no lease"
        return f"orchestrator_lease_unknown: the Monitor stream lease probe failed ({type(exc).__name__})"
    if not isinstance(response, dict) or response.get("stream_id") != stream:
        return "orchestrator_lease_unknown: the Monitor answer names another stream"
    if "lease" not in response:
        return "orchestrator_lease_unknown: the Monitor answer has no lease field"
    lease = response["lease"]
    if lease is None:  # the projection's explicit "no lease"
        return None
    if not isinstance(lease, dict):
        return "orchestrator_lease_unknown: the Monitor lease is malformed"
    # Every part of a lease is checked before any of it decides, as Monitor writes each one for every lease.
    state, session = lease.get("state"), lease.get("session_state")
    if not (isinstance(state, str) and state in _LEASE_STATES and isinstance(session, str) and session in _SESSION_STATES):
        return "orchestrator_lease_unknown: the Monitor lease or its session state is malformed"
    expires = store.parse_utc_timestamp(lease.get("expires_at"))
    if expires is None:
        return "orchestrator_lease_unknown: the Monitor lease expiry is unreadable"
    if state != "active" or session not in ("open", "rolling") or datetime.now(UTC) >= expires:
        return None
    holder = lease.get("holder") if isinstance(lease.get("holder"), dict) else {}
    return (f"orchestrator_stream_leased: {stream} has a live legacy driver lease "
            f"(holder {holder.get('agent')!s:.64}, expires {lease['expires_at']!s:.40})")


def credit_period_refusal(agent, model, *, codex_account=None):
    """Import errors propagate; only the reader owns unknown-state policy."""
    from lu_machinery.fleet import credit_lane

    if not model:
        from lu_machinery.agent_runtime.telemetry import _default_model_for
        from lu_machinery.review.model_catalog import substitution_default_model

        model = substitution_default_model(agent) or _default_model_for(agent)
    return credit_lane.dispatch_refusal(agent, model, codex_account=codex_account)


def check_credit(agent, model, codex_account=None):
    if refusal := credit_period_refusal(agent, model, codex_account=codex_account):
        raise Refused(f"❌ dispatch refused: {refusal}", 2)


def _claim_stream(stream, task_id, run_nonce, tree):
    """Publish ``stream``'s reservation for this run; None once published, else why the stream is not free.

    The caller holds the admission lock. Only a missing entry in the verified directory is free: a valid
    reservation, whatever task id or record it names, and an unsafe, unreadable or malformed one refuse.
    """
    try:
        fd = _reservations_fd(create=True)
        try:
            current = _read_reservation(fd, stream + RESERVATION_SUFFIX)
            if current is not None:
                return _busy(current)
            _publish_reservation(fd, stream, task_id, run_nonce, tree)
        finally:
            os.close(fd)
    except ReservationUnsafe as exc:
        return exc.reason
    return None


@contextlib.contextmanager
def reserve(task_id, mode, decision, *, agent, disk_guard, force_reason=None, spec=None, orchestrator=None,
            linkage=None, tree=None, launch=None):
    """Publish a creation hold for either mode; release unused setup only when its tree is absent.

    An orchestrator turn (``orchestrator`` is its ``{"stream": ...}`` record field, ``tree`` its planned
    worktree path) first publishes its stream's reservation file in the same admission-locked step, so a
    crash after it leaves the stream held; a stream with any reservation file refuses, and no
    ``--force-admission`` reason or reused task id overrides it. ``launch`` is the caller's own launch
    progress: it sets ``launch["started"] = True`` before it starts a supervisor. Only a dispatch that ends
    before that releases the reservation here; otherwise only the turn's settlement releases it. ``linkage`` (the parent fields of a task dispatched
    from inside another run) is in the hold before anything spawns.
    """
    hold = reservation = None
    meter, budget = _meter_budget(spec)
    try:
        refusal = None
        try:
            with legacy.admission_lock(tasks_dir()):
                current = evaluate(mode, sweep=True, thresholds=decision.thresholds, disk_guard=disk_guard(),
                                   meter=meter, budget=budget, orchestrator=orchestrator is not None)
                if allows(current, force_reason):
                    nonce = uuid.uuid4().hex
                    if orchestrator is not None:
                        if busy := _claim_stream(orchestrator["stream"], task_id, nonce, tree):
                            raise _StreamBusy(busy)
                        reservation = (orchestrator["stream"], task_id, nonce)
                    owner = legacy.new_admission_hold(nonce)
                    hold = dict(schema_version=2, task_id=task_id, agent=agent, run_nonce=nonce, status="spawning", pid=None,
                                mode=mode, started_at=owner["admitted_at"], finished_at=None, admission_hold=owner,
                                **(linkage or {}))
                    if orchestrator is not None:
                        hold[legacy.ORCHESTRATOR_KEY] = orchestrator
                    if spec is not None and meter is not None:
                        hold.update(model=spec.model, claude_account=spec.claude_account, codex_account=spec.codex_account)
                    if not current.exempt:
                        hold["admission"] = current.to_record(force_reason=force_reason)
                    path = store.record_path(task_id)
                    with task_state_lock(path):
                        if store.read(task_id, missing_ok=True) is not None:
                            raise RuntimeError(f"task record for {task_id!r} exists; refusing to overwrite it")
                        store._write(path, hold, exclusive=True)
                else:
                    refusal = Refused(f"❌ {current.refusal_line()}", 4 if current.disk_refused else 3)
        except _StreamBusy as exc:
            refusal = Refused(f"❌ {exc}", 3)
        except legacy.AdmissionLockTimeout as exc:
            raise Refused(f"❌ dispatch admission lock unavailable: {exc}; retry the dispatch", 3) from exc
        except (OSError, RuntimeError) as exc:
            raise Refused(f"❌ could not publish the admission hold for {task_id!r}: {exc}", 1) from exc
        if refusal is not None:
            raise refusal
        yield hold
    finally:
        # Read before anything else here can fail: missing progress means a supervisor may have started.
        launch_started = launch is None or launch.get("started") is not False
        if hold is not None:
            path = store.record_path(task_id)
            with contextlib.suppress(OSError), task_state_lock(path):
                current = store.read(task_id, missing_ok=True)
                if (current is not None and current.get("run_nonce") == hold["run_nonce"]
                        and current.get("status") in store.ACTIVE and legacy.is_admission_hold_record(current)
                        and "prompt_file" not in current):  # store.create handed this record to the supervisor
                    tree_absent = not current.get("worktree_path")
                    if not tree_absent:
                        try:
                            Path(current["worktree_path"]).lstat()
                        except FileNotFoundError:
                            tree_absent = True
                    if tree_absent:
                        path.unlink(missing_ok=True)
        # A known pre-launch abort: this dispatch never started a supervisor, so nothing of the run can write.
        if reservation is not None and not launch_started and (reason := release_stream(*reservation)):
            print(f"dispatcher: {reservation[0]} stays reserved: {reason}", file=sys.stderr)


class _StreamBusy(Exception):
    """The stream already has a reservation, or one that cannot be read (its typed reason)."""
