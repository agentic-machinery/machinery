"""Legacy host/credit admission and the atomic pre-launch write-slot hold.

Checks host/credit admission before reserving a write-slot hold.
The retained admission scanner and ownership ledger read the same hold format.
"""

import contextlib
import http.client
import math
import os
import stat
import subprocess
import sys
import urllib.error
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeGuard

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
    def __init__(self, message: str, code: int, *, admission_record: dict | None = None):
        super().__init__(message)
        self.code = code
        self.admission_record = admission_record


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


def _monitor_json(read) -> tuple[Mapping | None, str | None]:
    try:
        data = read()
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return None, "timeout"
        return None, "unreachable"
    except TimeoutError:
        return None, "timeout"
    except http.client.HTTPException:
        return None, "http_error"
    except OSError:
        return None, "unreachable"
    except (ValueError, RecursionError):
        return None, "invalid_budget"
    if not isinstance(data, Mapping):
        return None, "invalid_budget"
    return data, None


def _meter_budget(spec):
    """Read the routing budget outside the admission lock for a metered write."""
    if spec is None or spec.mode not in legacy.WRITE_CAPABLE_MODES:
        return None, None, None
    meter = legacy.subscription_meter(
        spec.agent, model=spec.model, claude_account=spec.claude_account, codex_account=spec.codex_account,
    )
    if meter is None:
        return None, None, None
    from lu_machinery.dispatcher.pool_selector import read_budget

    budget, budget_error = _monitor_json(read_budget)
    return meter, budget, budget_error


def evaluate(mode, *, sweep, thresholds=None, disk_guard=True, meter=None, budget=None, budget_error=None, orchestrator=False):
    """Copy-adapted from delegate._evaluate_dispatch_admission, including v2 liveness.

    Disk admission measures, alerts and refuses; it never removes anything. Each task's creator
    removes its own worktree at settlement (worker._reap_settled), so no reaper runs here.
    """
    return legacy.evaluate(
        mode, tasks_dir(), pid_alive=legacy.process_alive,
        record_liveness=lambda record: worker_liveness(record, pid_alive=legacy.process_alive),
        on_dead=heal_dead if sweep else None, thresholds=thresholds, disk_guard=disk_guard,
        meter=meter, meter_budget=budget, meter_budget_error=budget_error, orchestrator=orchestrator,
    )


def allows(decision, force_reason):
    """Force may override host checks, except the disk and subscription meter."""
    return decision.exempt or decision.admitted or (
        force_reason is not None and not (decision.disk_refused or decision.meter_refused)
    )


def _refusal_admission_record(
    decision: legacy.AdmissionDecision | None,
    *,
    failure: str,
) -> tuple[str, dict]:
    """Meter status clause and unforced refusal admission record for non-decision exits."""
    if decision is not None and not decision.exempt:
        clause = decision.meter_status_clause()
        record = decision.to_record(force_reason=None)
        record["admitted"] = False
        record["forced"] = False
        if failure not in record["failures"]:
            record["failures"] = [*record["failures"], failure]
        return clause, record
    clause = f"meter telemetry={legacy.METER_TELEMETRY_NOT_CHECKED}, effective cap=none"
    return clause, {
        "checked_at": datetime.now(UTC).isoformat(),
        "admitted": False,
        "forced": False,
        "force_reason": None,
        "failures": [failure],
        "meter": None,
        "meter_live_write_workers": None,
        "meter_effective_cap": None,
        "meter_normal_cap": None,
        "meter_unknown_cap": None,
        "meter_telemetry": legacy.METER_TELEMETRY_NOT_CHECKED,
        "meter_defer": None,
        "meter_refused": False,
    }


def report(decision, *, force_reason=None):
    """Legacy refusal/admit/override text and exits."""
    if decision.exempt:
        return
    if decision.admitted:
        summary = decision.summary()
        if decision.meter is None:
            summary = f"{summary}, {decision.meter_status_clause()}"
        print(f"🚦 dispatch admission: admitted — {summary}", file=sys.stderr)
    elif allows(decision, force_reason):
        print(
            f"⚠️  dispatch admission overridden by --force-admission ({force_reason!r}): "
            f"{'; '.join(decision.failures)}; {decision.meter_status_clause()}",
            file=sys.stderr,
        )
    else:
        raise Refused(
            f"❌ {decision.refusal_line()}",
            4 if decision.disk_refused else 3,
            admission_record=decision.to_record(force_reason=force_reason),
        )


def precheck(mode, *, force_reason=None):
    if force_reason is not None:
        return
    try:
        decision = legacy.precheck(mode)
    except ValueError as exc:
        failure = f"invalid dispatch admission threshold: {exc}"
        clause, record = _refusal_admission_record(None, failure=failure)
        raise Refused(f"❌ {failure} ({clause})", 2, admission_record=record) from exc
    if decision is not None:
        report(decision)


def check(mode, *, dry_run, disk_guard, force_reason=None, spec=None, orchestrator=None):
    try:
        meter, budget, budget_error = _meter_budget(spec)
        decision = evaluate(
            mode, sweep=not dry_run, disk_guard=disk_guard,
            meter=meter, budget=budget, budget_error=budget_error,
            orchestrator=orchestrator is not None,
        )
    except ValueError as exc:
        failure = f"invalid dispatch admission threshold: {exc}"
        clause, record = _refusal_admission_record(None, failure=failure)
        raise Refused(f"❌ {failure} ({clause})", 2, admission_record=record) from exc
    report(decision, force_reason=force_reason)
    return decision


# --- An orchestrator turn's stream lease (DRIVERS-1122) -------------------------------------------------------------
# Monitor's stream lease is the one exclusion authority for a stream, shared with the legacy launchers. reserve()
# claims it through Monitor's CAS claim before the admission lock, as holder harness TURN_HOLDER_HARNESS with the
# turn's run nonce and task id. Only the exact holder's release (settlement's proof that the run is gone, or a
# provably pre-launch abort) or an attributed operator force release frees it: Monitor never supersedes an active
# turn lease, expired or not, and a turn never supersedes an active lease (session_streams.store).
# The claimed lease and the Monitor that granted it are the hold's and the record's TURN_LEASE_KEY:
# {"monitor": "http://127.0.0.1:<port>", "lease": {stream_id, session_id, lease_id, generation, fencing_token,
#  holder, expires_at, ttl_seconds}}.
TURN_LEASE_KEY = "turn_lease"
# Every turn's task id, and so its tree's name, starts with this. A --cwd attachment names the tree it borrows: a
# tree so named is a turn's whatever its task record says, or whether one exists.
TURN_TASK_PREFIX = "turn-"
# A turn's lease TTL covers, in order: claim to scope start (admission, worktree creation, the record), the run
# (--hard-timeout) and the supervisor's settlement after it (the scope's RuntimeMaxSec covers both), the scope's
# stop (TimeoutStopSec, then SIGKILL) and clock and rounding margin. Expiry only says someone should look.
TURN_SETUP_BUDGET_S = 600
TURN_SUPERVISOR_GRACE_S = 600
TURN_STOP_TIMEOUT_S = 30
TURN_TTL_MARGIN_S = 60
_TURN_LEASE_FIELDS = frozenset({"stream_id", "session_id", "lease_id", "generation", "fencing_token", "holder",
                                "expires_at", "ttl_seconds"})
_HOLDER_FIELDS = ("agent", "harness", "instance_id", "task_id", "process_id", "holder_kind", "host_id")


def turn_timing(hard_timeout) -> tuple[int, int]:
    """A turn's scope ``RuntimeMaxSec`` and lease TTL in whole seconds; a typed ValueError for an unusable deadline."""
    from lu_machinery.session_streams.store import MAX_TTL_SECONDS

    fixed = TURN_SUPERVISOR_GRACE_S + TURN_SETUP_BUDGET_S + TURN_STOP_TIMEOUT_S + TURN_TTL_MARGIN_S
    if (isinstance(hard_timeout, bool) or not isinstance(hard_timeout, int | float) or not math.isfinite(hard_timeout)
            or not 0 < hard_timeout <= MAX_TTL_SECONDS - fixed):
        raise ValueError(f"orchestrator_timeout_refused: a turn needs a finite --hard-timeout above 0 and at most "
                         f"{MAX_TTL_SECONDS - fixed} s, so its lease fits Monitor's {MAX_TTL_SECONDS} s limit")
    runtime = math.ceil(hard_timeout) + TURN_SUPERVISOR_GRACE_S
    return runtime, runtime + TURN_SETUP_BUDGET_S + TURN_STOP_TIMEOUT_S + TURN_TTL_MARGIN_S


def _turn_holder(agent: str, task_id: str, run_nonce: str) -> dict:
    """The holder fields a turn claims with, in the order Monitor's projection names them."""
    from lu_machinery.session_streams.store import TURN_HOLDER_HARNESS

    return {"agent": agent, "harness": TURN_HOLDER_HARNESS, "instance_id": run_nonce, "task_id": task_id,
            "process_id": os.getpid(), "holder_kind": "process", "host_id": None}


def _lease_refused(reason: str) -> Refused:
    clause, record = _refusal_admission_record(None, failure=reason)
    return Refused(f"❌ {reason} ({clause})", 3, admission_record=record)


def same_typed(value, expected) -> bool:
    """``value`` equals ``expected`` with the same JSON type at every level: 1.0 or True never stands in for 1."""
    if isinstance(expected, dict):
        return (isinstance(value, dict) and set(value) == set(expected)
                and all(same_typed(value[key], item) for key, item in expected.items()))
    return type(value) is type(expected) and value == expected


def _valid_token(value) -> bool:
    """A public Monitor projection token, by Monitor's own sanitizer (epics_router._token)."""
    from lu_machinery.api.occupancy_sanitize import safe_field

    return isinstance(value, str) and safe_field(value, role="task_id") == value


def _positive(value) -> bool:
    return type(value) is int and value > 0


def _non_negative(value) -> bool:
    return type(value) is int and value >= 0


def _valid_holder(holder) -> bool:
    """A holder as Monitor's projection writes it (epics_router._lease_payload), by Monitor's own rule.

    ``LeaseHolder.validate`` decides identities, an app thread's canonical UUID task id and a process holder's
    pid; Monitor's projection sanitizers decide public tokens and opaque host ids; a JSON boolean is never a pid.
    """
    from lu_machinery.api.occupancy_sanitize import opaque_host_id
    from lu_machinery.session_streams.model import HolderKind, LeaseHolder

    if not (isinstance(holder, dict) and set(holder) == set(_HOLDER_FIELDS)
            and all(_valid_token(holder[name]) for name in ("agent", "harness", "instance_id"))
            and (holder["task_id"] is None or _valid_token(holder["task_id"]))
            and (holder["host_id"] is None or (isinstance(holder["host_id"], str) and opaque_host_id(holder["host_id"])))
            and isinstance(holder["holder_kind"], str)
            and holder["holder_kind"] in {kind.value for kind in HolderKind}
            and (holder["process_id"] is None or _positive(holder["process_id"]))):
        return False
    try:
        LeaseHolder(agent=holder["agent"], harness=holder["harness"], instance_id=holder["instance_id"],
                    process_id=holder["process_id"], task_id=holder["task_id"],
                    holder_kind=HolderKind(holder["holder_kind"]), host_id=holder["host_id"]).validate()
    except (ValueError, TypeError):
        return False
    return True


def _valid_lease_identity(lease, stream: str) -> bool:
    """A lease on ``stream`` whose identity, fence and holder are all well formed."""
    return (isinstance(lease, dict) and lease.get("stream_id") == stream
            and _valid_token(lease.get("session_id")) and _valid_token(lease.get("lease_id"))
            and _positive(lease.get("generation")) and _positive(lease.get("fencing_token"))
            and _valid_holder(lease.get("holder")))


def _valid_projection_lease(lease: object, stream: str, *, match_ttl: bool = True) -> TypeGuard[dict[str, Any]]:
    """A lease payload as Monitor's projection writes it (epics_router._lease_payload).

    For claim and stored stream projections (``match_ttl=True``), Monitor computes ``expires_at`` by adding
    ``ttl_seconds`` to ``heartbeat_at``. Release acknowledgements echo default timestamps, so ``match_ttl=False``
    checks only that ``expires_at`` does not precede ``heartbeat_at``.
    """
    from lu_machinery.session_streams.store import MAX_TTL_SECONDS

    if not isinstance(lease, dict) or not _valid_lease_identity(lease, stream):
        return False
    state, session = lease.get("state"), lease.get("session_state")
    heartbeat = store.parse_utc_timestamp(lease.get("heartbeat_at"))
    expires = store.parse_utc_timestamp(lease.get("expires_at"))
    if not (isinstance(state, str) and state in _LEASE_STATES
            and isinstance(session, str) and session in _SESSION_STATES
            and (session in ("closed", "expired") if state == "released" else session in ("open", "rolling"))
            and _positive(lease.get("version"))
            and _positive(lease.get("ttl_seconds")) and lease["ttl_seconds"] <= MAX_TTL_SECONDS
            and _non_negative(lease.get("age_seconds"))
            and heartbeat is not None and expires is not None):
        return False
    if match_ttl:
        return expires - heartbeat == timedelta(seconds=lease["ttl_seconds"])
    return expires >= heartbeat


def _claimed_lease(response, stream: str, holder: dict, session_id: str, lease_id: str, ttl_s: int) -> dict | None:
    """The lease envelope part of a claim answer, when it is exactly the new lease this claim asked for."""
    lease = response.get("lease") if isinstance(response, dict) else None
    if not (isinstance(response, dict) and response.get("outcome") == "claimed" and response.get("stream_id") == stream
            and response.get("session_state") == "open" and _valid_projection_lease(lease, stream)):
        return None
    expires = store.parse_utc_timestamp(lease.get("expires_at"))
    remaining = None if expires is None else (expires - datetime.now(UTC)).total_seconds()
    if not (lease["session_id"] == session_id and lease["lease_id"] == lease_id
            and lease["state"] == "active" and lease["session_state"] == "open"
            and same_typed(lease["holder"], holder) and lease["ttl_seconds"] == ttl_s
            and remaining is not None and 0 < remaining <= ttl_s + TURN_TTL_MARGIN_S):
        return None
    return {name: lease[name] for name in _TURN_LEASE_FIELDS}


def claim_turn(stream: str, task_id: str, run_nonce: str, *, agent: str, ttl_s: int) -> dict:
    """Claim ``stream``'s Monitor lease for this turn: the envelope to keep, or a typed Refused (exit 3).

    ``orchestrator_stream_leased`` only for a verified conflict (Monitor's turn-conflict detail); every other
    answer, an outage, a malformed or replayed one included, is ``orchestrator_lease_unknown``. Neither retries:
    an unknown answer may hide a committed claim, which then holds the stream until an operator releases it.
    """
    from lu_machinery.session_streams.store import TURN_CONFLICT_PREFIX
    from lu_machinery.session_supervisor.remote import RemoteClaimConflictError, RemoteEpicClient

    holder = _turn_holder(agent, task_id, run_nonce)
    session_id, lease_id = f"turn-session-{run_nonce}", f"turn-lease-{run_nonce}"
    try:
        from lu_machinery.session_streams.model import HolderKind, LeaseHolder

        client = RemoteEpicClient()
        _lease, response = client.claim(
            stream_id=stream, lineage_id=f"dispatch-turn-{run_nonce}", ttl_seconds=ttl_s, session_id=session_id,
            lease_id=lease_id, digest_limit=0,
            holder=LeaseHolder(agent=holder["agent"], harness=holder["harness"], instance_id=run_nonce,
                               process_id=holder["process_id"], task_id=task_id, holder_kind=HolderKind.PROCESS),
        )
    except RemoteClaimConflictError as exc:
        if exc.detail.startswith(f"{TURN_CONFLICT_PREFIX}:"):
            raise _lease_refused(f"orchestrator_stream_leased: {stream} has an active lease that no turn supersedes "
                                 f"({exc.detail[len(TURN_CONFLICT_PREFIX) + 1:].strip():.300})") from None
        raise _lease_refused(f"orchestrator_lease_unknown: Monitor refused the claim without naming a turn conflict "
                             f"({exc.detail:.160}); nothing started") from None
    except Exception as exc:  # the lease authority's boundary: any failure is unknown, never "no lease"
        raise _lease_refused(f"orchestrator_lease_unknown: the Monitor claim failed ({type(exc).__name__}); nothing "
                             "started, and a claim Monitor committed holds the stream until an operator releases "
                             "it") from None
    lease = _claimed_lease(response, stream, holder, session_id, lease_id, ttl_s)
    envelope = None if lease is None else {"monitor": client.base, "lease": lease}
    try:
        if envelope is None:
            raise ValueError("not this turn's new lease")
        _turn_lease(envelope, stream=stream, task_id=task_id, run_nonce=run_nonce)  # exactly what release accepts
    except (ValueError, TypeError):
        raise _lease_refused("orchestrator_lease_unknown: Monitor's claim answer is not exactly this turn's new "
                             "lease; nothing started, and the stream stays held until an operator releases it") from None
    return envelope


def _turn_lease(envelope, *, stream: str, task_id: str, run_nonce: str):
    """The Monitor base and Lease an envelope names, when it is exactly this turn's; ValueError otherwise."""
    from lu_machinery.session_streams.model import HolderKind, Lease, LeaseHolder
    from lu_machinery.session_streams.store import MAX_TTL_SECONDS, TURN_HOLDER_HARNESS
    from lu_machinery.session_supervisor.remote import RemoteSupervisorError, monitor_url

    lease = envelope.get("lease") if isinstance(envelope, dict) else None
    holder = lease.get("holder") if isinstance(lease, dict) else None
    if not (isinstance(envelope, dict) and set(envelope) == {"monitor", "lease"} and isinstance(lease, dict)
            and set(lease) == _TURN_LEASE_FIELDS and isinstance(holder, dict) and set(holder) == set(_HOLDER_FIELDS)
            and _valid_lease_identity(lease, stream) and holder["harness"] == TURN_HOLDER_HARNESS
            and holder["instance_id"] == run_nonce and holder["task_id"] == task_id
            and holder["holder_kind"] == "process" and holder["host_id"] is None
            and store.parse_utc_timestamp(lease["expires_at"]) is not None
            and _positive(lease["ttl_seconds"]) and lease["ttl_seconds"] <= MAX_TTL_SECONDS
            and isinstance(envelope["monitor"], str)):
        raise ValueError("the turn's lease envelope is not this run's")
    try:
        base = monitor_url(envelope["monitor"])
    except RemoteSupervisorError:
        raise ValueError("the turn's lease names no loopback Monitor") from None
    if base != envelope["monitor"]:
        raise ValueError("the turn's lease names another Monitor spelling")
    return base, Lease(
        stream_id=stream, session_id=lease["session_id"], lease_id=lease["lease_id"], generation=lease["generation"],
        fencing_token=lease["fencing_token"], expires_at=lease["expires_at"], heartbeat_at=lease["expires_at"],
        ttl_seconds=lease["ttl_seconds"], version=1,
        holder=LeaseHolder(agent=holder["agent"], harness=TURN_HOLDER_HARNESS, instance_id=run_nonce,
                           process_id=holder["process_id"], task_id=task_id, holder_kind=HolderKind.PROCESS),
    )


def release_turn(envelope, *, stream: str, task_id: str, run_nonce: str) -> str | None:
    """Release exactly this turn's lease at the Monitor that granted it: None once released, else why it stays.

    One attempt: an outage or a lost answer leaves the lease held (Monitor fences any late release away from a
    successor), and nothing re-claims or force-releases it.
    """
    from lu_machinery.session_supervisor.remote import RemoteEpicClient, RemoteLeaseLostError

    try:
        base, lease = _turn_lease(envelope, stream=stream, task_id=task_id, run_nonce=run_nonce)
    except (ValueError, TypeError) as exc:
        return f"orchestrator_lease_release_refused: {exc}"
    try:
        response = RemoteEpicClient(base=base).release(lease)
    except RemoteLeaseLostError:
        return ("orchestrator_lease_release_fenced: Monitor no longer holds this exact lease (an operator force "
                "release or a successor)")
    except Exception as exc:  # the lease authority's boundary: any failure is unknown, never "released"
        return (f"orchestrator_lease_release_unknown: ({type(exc).__name__}); the lease stays held until its exact "
                "holder or an operator releases it")
    acknowledged = response.get("lease") if isinstance(response, dict) else None
    if not (isinstance(response, dict) and response.get("outcome") == "released"
            and ("stream_id" not in response or response["stream_id"] == stream)
            and ("session_state" not in response or response["session_state"] == "closed")
            and _valid_projection_lease(acknowledged, stream, match_ttl=False)
            and acknowledged["state"] == "released" and acknowledged["session_state"] == "closed"
            and all(same_typed(acknowledged.get(name), envelope["lease"][name])
                    for name in ("stream_id", "session_id", "lease_id", "generation", "fencing_token", "holder"))):
        return "orchestrator_lease_release_unknown: Monitor's answer does not acknowledge this exact lease"
    return None


def turn_start_deadline(envelope, runtime_s: int) -> float | None:
    """The latest time (POSIX seconds) the turn's scope may start so its lease outlasts run, stop and margin.

    None when the lease's expiry is unreadable. The scope launch itself enforces it again, right before it starts
    the scope (``dispatch_isolation``), after every blocking step of the launch.
    """
    lease = envelope.get("lease") if isinstance(envelope, dict) else None
    expires = store.parse_utc_timestamp(lease.get("expires_at")) if isinstance(lease, dict) else None
    if expires is None:
        return None
    return expires.timestamp() - (runtime_s + TURN_STOP_TIMEOUT_S + TURN_TTL_MARGIN_S)


def turn_launch_refusal(envelope, runtime_s: int) -> str | None:
    """Why the turn's lease no longer covers its run and stop (checked before its record is published), or None."""
    deadline = turn_start_deadline(envelope, runtime_s)
    if deadline is None:
        return "orchestrator_lease_budget_unknown: the turn's lease expiry is unreadable; nothing started"
    if (late := datetime.now(UTC).timestamp() - deadline) > 0:
        return (f"orchestrator_lease_budget_short: the turn's lease no longer covers its run and stop "
                f"({runtime_s + TURN_STOP_TIMEOUT_S + TURN_TTL_MARGIN_S} s) by {late:.0f} s; nothing started")
    return None


def turn_tree_refusal(tree) -> str | None:
    """Why a ``--cwd`` attachment to ``tree`` would add a writer to an orchestrator turn's tree, or None.

    The caller holds the repository's worktree lock. A tree named like a turn (TURN_TASK_PREFIX) is a turn's,
    whatever records exist; a tree whose name is a task id whose record is a turn, or cannot be read, refuses too.
    The name is the tree's real one, whatever spelling (``TREE/sub/..``) the caller used.
    """
    from lu_machinery.orchestration.task_record_store import archived_task_record_path, task_archive_lock

    try:
        name = Path(os.path.realpath(tree)).name
    except (OSError, ValueError):  # a spelling the filesystem cannot resolve names no tree it can vouch for
        return "orchestrator_turn_writer_refused: the tree's real path cannot be resolved"
    if name.startswith(TURN_TASK_PREFIX):
        return f"orchestrator_turn_writer_refused: {name:.128} is an orchestrator turn's tree, which no other task writes"
    try:
        hot = store.record_path(name)
    except ValueError:
        return None  # no task id names it
    unreadable = f"orchestrator_turn_writer_refused: the record of {name:.128}, which may own this tree, cannot be read"
    try:
        # Archive and restore move records under this lock, so no owner is between the two names while both are read.
        with task_archive_lock(hot.parent):
            return _owner_refusal(name, (hot, archived_task_record_path(hot.parent, name)), unreadable)
    except (OSError, ValueError):
        return unreadable


def _owner_refusal(name: str, locations, unreadable: str) -> str | None:
    """The refusal the first present owner record of ``name`` gives, in ``locations`` order (hot, then archive).

    Only a location proven absent (``lstat``: not found) yields to the next. A record once observed decides: it must
    be a regular file that validates through ``parse_task_record`` and names ``name``, or the tree's owner is unknown
    and the tree refused.
    """
    from lu_machinery.orchestration.dispatch_admission import orchestrator_stream
    from lu_machinery.orchestration.task_record_store import parse_task_record

    for candidate in locations:
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):  # an inaccessible record is not an absent one
            return unreadable
        try:
            if not stat.S_ISREG(info.st_mode):
                return unreadable
            record = parse_task_record(candidate, candidate.read_bytes())
            if not (isinstance(record, dict) and record.get("task_id") == name):
                return unreadable
            stream = orchestrator_stream(record)
        except (OSError, ValueError, RecursionError):  # vanished since observed, damaged, or an unknown turn stream
            return unreadable
        if stream is not None:
            return f"orchestrator_turn_writer_refused: {name:.128} is the orchestrator turn on {stream}"
        return None
    return None  # no record names it, and every turn's tree carries the turn prefix


def turn_lease_probe(stream):
    """A dry run's read of ``stream``'s lease: why a turn would be refused, or None when it would claim.

    Any active lease is busy, expired or not (a turn never supersedes one); an unreachable, refused or malformed
    answer is unknown. Reads only.
    """
    from lu_machinery.session_supervisor.remote import RemoteEpicClient

    try:
        response = RemoteEpicClient().stream(stream, digest_limit=0)
    except Exception as exc:  # the lease authority's boundary: any failure is unknown, never "no lease"
        return f"orchestrator_lease_unknown: the Monitor stream lease read failed ({type(exc).__name__})"
    if not isinstance(response, dict) or response.get("stream_id") != stream:
        return "orchestrator_lease_unknown: the Monitor answer names another stream"
    if "lease" not in response or "session_state" not in response:
        return "orchestrator_lease_unknown: the Monitor answer has no lease or session state field"
    lease = response["lease"]
    if lease is None:  # the projection's explicit "no lease", which has no session either
        return None if response["session_state"] is None else (
            "orchestrator_lease_unknown: the Monitor answer names a session but no lease")
    if not (_valid_projection_lease(lease, stream) and response["session_state"] == lease["session_state"]):
        return "orchestrator_lease_unknown: the Monitor lease is malformed or its states disagree"
    state = lease["state"]
    if state == "released":
        return None
    holder = lease["holder"]
    return (f"orchestrator_stream_leased: {stream} has an active lease, which no turn supersedes "
            f"(holder {holder.get('agent')!s:.64}/{holder.get('harness')!s:.64}, {state}, "
            f"expires {lease['expires_at']!s:.40})")


# The states Monitor's /api/epics/v1 projection names (epics_router._lease_payload; SessionState).
_LEASE_STATES = frozenset({"active", "released", "expired"})
_SESSION_STATES = frozenset({"open", "rolling", "closed", "expired"})


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


@contextlib.contextmanager
def reserve(task_id, mode, decision, *, agent, disk_guard, force_reason=None, spec=None, orchestrator=None,
            linkage=None, launch=None, run_nonce=None, turn_ttl_s=None):
    """Publish a creation hold for either mode; release unused setup only when its tree is absent.

    An orchestrator turn (``orchestrator`` is its ``{"stream": ...}`` record field) first claims its stream's
    Monitor lease (claim_turn) with ``run_nonce``, minted by the caller and kept by the hold, the record and the
    scope, for ``turn_ttl_s`` seconds: outside the admission lock, as its CAS is the stream's exclusion, and before
    anything else is held. No ``--force-admission`` reason or reused task id overrides a refused claim. The hold
    keeps the lease envelope (TURN_LEASE_KEY). ``launch`` is the caller's own launch progress: it sets
    ``launch["record"] = True`` before it can publish a record naming the prompt file and ``launch["started"] =
    True`` before it starts a supervisor. Only a dispatch that ends before a supervisor start, and either before
    that record or with this run's unclaimed hold read back, releases the lease here. So does one whose launch was
    refused before any of its processes started (``launch["refused_before_start"]``) when this run's record reads
    back as that failed, pid-less launch holding this lease (C-6991). Otherwise only the turn's settlement does. ``linkage`` (the parent fields of a task dispatched from inside another run) is in the hold
    before anything spawns.
    """
    if orchestrator is not None and not (isinstance(run_nonce, str) and isinstance(turn_ttl_s, int)):
        raise ValueError("an orchestrator turn's hold needs its run nonce and lease TTL")
    hold = turn_lease = None
    meter, budget, budget_error = _meter_budget(spec)
    try:
        if orchestrator is not None:
            turn_lease = claim_turn(orchestrator["stream"], task_id, run_nonce, agent=agent, ttl_s=turn_ttl_s)
        refusal = None
        locked = None
        locked_eval = None
        try:
            with legacy.admission_lock(tasks_dir()):
                current = evaluate(
                    mode, sweep=True, thresholds=decision.thresholds, disk_guard=disk_guard(),
                    meter=meter, budget=budget, budget_error=budget_error,
                    orchestrator=orchestrator is not None,
                )
                locked_eval = current
                if allows(current, force_reason):
                    locked = current
                    nonce = run_nonce or uuid.uuid4().hex
                    owner = legacy.new_admission_hold(nonce)
                    hold = dict(schema_version=2, task_id=task_id, agent=agent, run_nonce=nonce, status="spawning", pid=None,
                                mode=mode, started_at=owner["admitted_at"], finished_at=None, admission_hold=owner,
                                **(linkage or {}))
                    if orchestrator is not None:
                        hold[legacy.ORCHESTRATOR_KEY] = orchestrator
                        hold[TURN_LEASE_KEY] = turn_lease
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
                    refusal = Refused(
                        f"❌ {current.refusal_line()}",
                        4 if current.disk_refused else 3,
                        admission_record=current.to_record(force_reason=force_reason),
                    )
        except legacy.AdmissionLockTimeout as exc:
            failure = f"dispatch admission lock unavailable: {exc}; retry the dispatch"
            clause, record = _refusal_admission_record(None, failure=failure)
            raise Refused(f"❌ {failure} ({clause})", 3, admission_record=record) from exc
        except (OSError, RuntimeError) as exc:
            failure = f"could not publish the admission hold for {task_id!r}: {exc}"
            clause, record = _refusal_admission_record(locked_eval, failure=failure)
            raise Refused(f"❌ {failure} ({clause})", 1, admission_record=record) from exc
        if refusal is not None:
            raise refusal
        if (locked is not None and not locked.exempt and decision is not None
                and (locked.admitted, locked.meter_telemetry, locked.meter_effective_cap)
                != (decision.admitted, decision.meter_telemetry, decision.meter_effective_cap)):
            report(locked, force_reason=force_reason)
        yield hold
    finally:
        # Read before anything else here can fail: missing progress means a supervisor may have started.
        launch_started = launch is None or launch.get("started") is not False
        refused_unstarted = launch is not None and launch.get("refused_before_start") is True
        # Once the record may have been handed over (store.create), only reading it back as this run's unclaimed
        # hold proves that no record of the run names a prompt file or pid; a lost or damaged one is not proof.
        unclaimed = launch is not None and launch.get("record", False) is False
        try:
            if hold is not None:
                unclaimed = _discard_unused_hold(task_id, hold["run_nonce"]) or unclaimed
        finally:
            # A known pre-launch abort: this dispatch never started a supervisor and no record of the run can
            # start one. The hold's cleanup is independent: whatever it finds or raises, this still runs.
            if (turn_lease is not None
                    and ((not launch_started and unclaimed)
                         or (refused_unstarted
                             and _launch_failed_unstarted(task_id, run_nonce, orchestrator["stream"], turn_lease)))
                    and (reason := release_turn(turn_lease, stream=orchestrator["stream"], task_id=task_id,
                                                run_nonce=run_nonce))):
                print(f"dispatcher: {orchestrator['stream']} stays leased: {reason}", file=sys.stderr)


# The failure codes worker._record_failed_launch writes for a placement refusal (admission.spawn_supervisor).
_UNSTARTED_LAUNCH_REASONS = frozenset({"dispatch_fallback_refused", "dispatch_isolation_failed"})


def _launch_failed_unstarted(task_id, run_nonce, stream, envelope) -> bool:
    """True when this run's record reads back as its own launch that failed before any process started (C-6991).

    That record is this task's and run's, a turn on ``stream``, terminal ``failed``, names no pid, holds exactly
    the claimed lease (same JSON types) and a placement refusal's failure code, so no supervisor runs for it or can
    start from it. Anything else, a malformed or unreadable record included, is False and never raises.
    """
    from lu_machinery.orchestration.dispatch_admission import orchestrator_stream

    try:
        with task_state_lock(store.record_path(task_id)):
            current = store.read(task_id, missing_ok=True)
        return bool(isinstance(current, dict) and same_typed(current.get("task_id"), task_id)
                    and same_typed(current.get("run_nonce"), run_nonce) and same_typed(current.get("status"), "failed")
                    and "pid" in current and current["pid"] is None and orchestrator_stream(current) == stream
                    and same_typed(current.get(TURN_LEASE_KEY), envelope)
                    and isinstance(reason := current.get("returncode_reason"), str)
                    and reason in _UNSTARTED_LAUNCH_REASONS)
    except Exception:  # the proof's boundary: anything unreadable or unexpected is not proof
        return False


def _discard_unused_hold(task_id, run_nonce):
    """True when this run's record reads back naming no pid and no prompt file: no supervisor can start for it.

    That record, if it is still the active creation hold, is removed when its tree does not exist; one whose
    status or tree path has the wrong type is kept. A record that is missing, unreadable, damaged (TaskRecordError is a ValueError),
    another run's or handed to the supervisor is False and left as it is.
    """
    path = store.record_path(task_id)
    unclaimed = False
    with contextlib.suppress(OSError, ValueError), task_state_lock(path):
        current = store.read(task_id, missing_ok=True)
        if not isinstance(current, dict):
            return False
        status, tree = current.get("status"), current.get("worktree_path")
        unclaimed = (current.get("run_nonce") == run_nonce and "pid" in current and current["pid"] is None
                     and "prompt_file" not in current)  # store.create handed this record to the supervisor
        if (unclaimed and isinstance(status, str) and status in store.ACTIVE
                and legacy.is_admission_hold_record(current) and (tree is None or isinstance(tree, str))):
            tree_absent = not tree
            if not tree_absent:
                try:
                    Path(tree).lstat()
                except FileNotFoundError:
                    tree_absent = True
            if tree_absent:
                path.unlink(missing_ok=True)
    return unclaimed
