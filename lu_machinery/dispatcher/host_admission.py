"""Legacy host/credit admission and the atomic pre-launch write-slot hold.

Checks host/credit admission before reserving a write-slot hold.
The retained admission scanner and ownership ledger read the same hold format.
"""

import contextlib
import http.client
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

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


def _orchestrator_marked(raw: bytes) -> bool:
    return f'"{legacy.ORCHESTRATOR_KEY}"'.encode() in raw


def stream_holders(stream, task_id):
    """The other orchestrator turns that may still run on ``stream`` (DRIVERS-1122).

    A turn holds its stream while its record is active (an admission hold included) and,
    after a supervisor crash, until its provider is proven gone. An unknown status, a
    malformed orchestrator record and an unreadable record that names an orchestrator
    each hold every stream: the reservation fails closed. Reads hot records only; a
    record that names no orchestrator is never parsed.
    """
    from lu_machinery.dispatcher.worker import _provider_alive
    from lu_machinery.orchestration.task_record_store import iter_task_records, parse_task_record

    holders = []
    for path in iter_task_records(tasks_dir(), include_archive=False):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            continue  # settled and archived since it was listed
        except OSError:
            holders.append(f"{path.stem} (record unreadable)")
            continue
        if not _orchestrator_marked(raw):
            continue
        try:
            record = parse_task_record(path, raw)
            other = legacy.orchestrator_stream(record) if isinstance(record, dict) else None
        except ValueError:
            holders.append(f"{path.stem} (orchestrator record malformed)")
            continue
        if other is None or record.get("task_id") == task_id or other != stream:
            continue
        status = record.get("status")
        if status in store.ACTIVE or status not in store.TERMINAL:
            holders.append(str(record.get("task_id")))
        elif status == "crashed" and _provider_alive(record) is not False:
            holders.append(f"{record.get('task_id')} (crashed; provider not proven gone)")
    return holders


def legacy_lease_refusal(stream):
    """Why a live legacy driver lease on ``stream`` refuses an orchestrator turn, or None when there is none.

    Monitor is the lease authority (a local process never proves a remote lease gone); an
    unreachable, refused or malformed answer refuses, as does an active lease of an open or
    rolling session that has not expired.
    """
    from lu_machinery.session_streams.model import parse_timestamp
    from lu_machinery.session_supervisor.remote import RemoteEpicClient, RemoteSupervisorError

    try:
        response = RemoteEpicClient().stream(stream, digest_limit=1)
    except (RemoteSupervisorError, ValueError, RecursionError) as exc:
        return f"orchestrator_lease_unknown: the Monitor stream lease probe failed ({type(exc).__name__})"
    if not isinstance(response, dict) or response.get("stream_id") != stream:
        return "orchestrator_lease_unknown: the Monitor answer names another stream"
    lease = response.get("lease")
    if lease is None:
        return None
    if not isinstance(lease, dict):
        return "orchestrator_lease_unknown: the Monitor lease is malformed"
    if lease.get("state") != "active" or lease.get("session_state") not in ("open", "rolling"):
        return None
    try:
        expired = datetime.now(UTC) >= parse_timestamp(lease["expires_at"])
    except (KeyError, TypeError, ValueError):
        return "orchestrator_lease_unknown: the Monitor lease expiry is unreadable"
    if expired:
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


@contextlib.contextmanager
def reserve(task_id, mode, decision, *, agent, disk_guard, force_reason=None, spec=None, orchestrator=None,
            linkage=None):
    """Publish a creation hold for either mode; release unused setup only when its tree is absent.

    An orchestrator turn (``orchestrator`` is its ``{"stream": ...}`` record field) reserves its stream
    in the same step: under the admission lock, after the sweep settles dead records, another turn that
    may still run on the stream refuses it, and the hold it publishes names the stream, so a concurrent
    dispatch sees it. No ``--force-admission`` reason overrides the reservation. ``linkage`` (the parent
    fields of a task dispatched from inside another run) is in the hold before anything spawns.
    """
    hold = None
    meter, budget = _meter_budget(spec)
    try:
        refusal = None
        try:
            with legacy.admission_lock(tasks_dir()):
                current = evaluate(mode, sweep=True, thresholds=decision.thresholds, disk_guard=disk_guard(),
                                   meter=meter, budget=budget, orchestrator=orchestrator is not None)
                holders = stream_holders(orchestrator["stream"], task_id) if orchestrator is not None else []
                if holders:
                    refusal = Refused(f"❌ orchestrator_stream_busy: {orchestrator['stream']} has another orchestrator "
                                      f"turn that may still run: {', '.join(holders[:10])}", 3)
                elif allows(current, force_reason):
                    nonce = uuid.uuid4().hex
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
        except legacy.AdmissionLockTimeout as exc:
            raise Refused(f"❌ dispatch admission lock unavailable: {exc}; retry the dispatch", 3) from exc
        except (OSError, RuntimeError) as exc:
            raise Refused(f"❌ could not publish the admission hold for {task_id!r}: {exc}", 1) from exc
        if refusal is not None:
            raise refusal
        yield hold
    finally:
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
