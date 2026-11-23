"""Orchestrator dispatch mode: a CTO-started stream turn that writes no product change (DRIVERS-1122).

The tests own their data: a private task store, a Monitor (an in-process fake, or the production epics router over
a test-owned store on a loopback port) and the dispatcher sandbox's stub provider.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.dispatcher import git, host_admission, ownership, store, worker
from lu_machinery.orchestration import dispatch_admission
from lu_machinery.session_supervisor import remote
from tests.dispatcher.conftest import await_file, run_git
from tests.epics_monitor_stub import isolated_monitor_state  # noqa: F401
from tests.helpers.product_tree import configured_roots  # noqa: F401

STREAM = "epic:7"
TASK_ENV = ownership.TASK_ID_ENV


_MISSING = object()
_DEAD_PID = 2**22 + 1  # above the default pid_max, so never a live process
_LAUNCHED = dict(pid=_DEAD_PID, process_start="boot:1", launch_mode="scope", launch_unit="lu-worker-prior",
                 provider_pgid=None)


def _lease(state="active", session_state="open", expires_in=600):
    expires = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z")
    return {"state": state, "session_state": session_state, "expires_at": expires, "holder": {"agent": "claude"}}


@pytest.fixture
def cto_seat(monkeypatch):
    """The caller is the CTO seat: no dispatched-task or write-boundary marker in its environment."""
    for name in (TASK_ENV, "LU_WRITE_BOUNDARY_BROKER", "SESSION_SEAT", "SESSION_EPIC"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def task_store(tmp_path, monkeypatch, configured_roots):  # noqa: F811
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    return tasks


def _orchestrator_record(task_id, **fields):
    record = dict(schema_version=2, task_id=task_id, agent="claude", mode="workspace-write", status="running",
                  run_nonce=f"nonce-{task_id}", pid=None, started_at=store.now(), owned_paths=[],
                  orchestrator={"stream": STREAM})
    record.update(fields)
    return record


def _write(record):
    store._write(store.record_path(record["task_id"]), record)


# --- entry ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (["--mode", "orchestrator"], "--mode orchestrator and --stream go together"),
        (["--mode", "workspace-write", "--stream", STREAM], "--mode orchestrator and --stream go together"),
        (["--mode", "orchestrator", "--stream", "practice"], "canonical epic:<N> stream id"),
        (["--mode", "orchestrator", "--stream", "shared:practice"], "canonical epic:<N> stream id"),
        (["--mode", "orchestrator", "--stream", "epic:7\n"], "canonical epic:<N> stream id"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto-1"], "only the CTO seat"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--owned-path", "x.py"],
         "takes no --owned-path"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--branch", "codex/x"],
         "takes no --branch"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--pr", "0"], "takes no --pr"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--require-review-verdict"],
         "takes no --require-review-verdict"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--task-id", "turn-1.sandbox"],
         "ends like a dispatcher sidecar"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--research-role", "advisory"],
         "--research-role is driver"),
    ],
)
def test_entry_refuses_anything_but_the_cto_seat_on_a_canonical_stream(task_store, cto_seat, monkeypatch, capsys,
                                                                       flags, reason):
    """Defect class: a caller other than the CTO, or a non-canonical stream or product scope, enters the mode."""
    from lu_machinery.dispatcher import cli

    for target, name in ((git, "create_worktree"), (worker, "start"), (host_admission, "reserve")):
        monkeypatch.setattr(target, name, lambda *_a, _name=name, **_k: pytest.fail(f"{_name} ran before refusal"))
    task_id = [] if "--task-id" in flags else ["--task-id", "turn-1"]
    code = cli.main(["dispatch", "--agent", "claude", "--model", "claude-sonnet-5-5", *task_id,
                     "--prompt", "drive", *flags])
    assert code == 2
    assert reason in capsys.readouterr().err
    assert not store.read("turn-1", missing_ok=True)


@pytest.mark.parametrize(("marker", "value"), [(TASK_ENV, "some-worker"), (TASK_ENV, ""),
                                               ("LU_WRITE_BOUNDARY_BROKER", "/broker.sock"),
                                               ("LU_WRITE_BOUNDARY_BROKER", "")])
def test_entry_refuses_a_caller_inside_a_dispatched_task(task_store, cto_seat, monkeypatch, capsys, marker, value):
    """Defect class: a running task (a turn or a worker copying --initiator claude-cto) starts a turn."""
    from lu_machinery.dispatcher import cli

    monkeypatch.setenv(marker, value)
    monkeypatch.setattr(worker, "start", lambda *_a, **_k: pytest.fail("spawned"))
    assert cli.main(["dispatch", "--agent", "claude", "--model", "claude-sonnet-5-5", "--task-id", "turn-1",
                     "--prompt", "drive", "--mode", "orchestrator", "--stream", STREAM,
                     "--initiator", "claude-cto"]) == 2
    assert "only the CTO seat" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (["--task-id", "worker-1"], "a turn's --task-id starts with 'turn-'"),  # T6: its tree would not read as a turn's
        (["--task-id", "turn-1", "--worktree", "/trees/elsewhere"], "takes no --worktree path"),  # T6
        (["--task-id", "turn-1", "--hard-timeout", "nan"], "orchestrator_timeout_refused"),  # T5
        (["--task-id", "turn-1", "--hard-timeout", "inf"], "orchestrator_timeout_refused"),  # T5
        (["--task-id", "turn-1", "--hard-timeout", "0"], "orchestrator_timeout_refused"),  # T5: 0 disables a deadline
        (["--task-id", "turn-1", "--hard-timeout", "86400"], "orchestrator_timeout_refused"),  # T5
    ],
)
def test_entry_refuses_a_turn_without_a_turn_id_its_own_tree_or_a_bounded_deadline(task_store, cto_seat, monkeypatch,
                                                                                   capsys, flags, reason):
    """Defect classes T5, T6: a turn starts under a task id or tree path that does not mark its tree as a turn's, or
    with a deadline that leaves its scope unbounded or its lease beyond Monitor's limit."""
    from lu_machinery.dispatcher import cli

    for target, name in ((git, "create_worktree"), (worker, "start"), (host_admission, "reserve")):
        monkeypatch.setattr(target, name, lambda *_a, _name=name, **_k: pytest.fail(f"{_name} ran before refusal"))
    assert cli.main(["dispatch", "--agent", "claude", "--model", "claude-sonnet-5-5", "--prompt", "drive", "--mode",
                     "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", *flags]) == 2
    assert reason in capsys.readouterr().err


# --- admission -----------------------------------------------------------------------------------------------------


def test_a_turn_takes_no_write_slot_but_keeps_the_host_checks(task_store, monkeypatch):
    """Defect class: turns starve workers of write slots, or the slot exemption skips host admission."""
    monkeypatch.setenv("DISPATCH_MAX_LIVE_WRITE_WORKERS", "1")
    live = _orchestrator_record("turn-live", pid=os.getpid())
    live["process_start"] = worker._identity(os.getpid())
    _write(live)
    worker_decision = host_admission.evaluate("workspace-write", sweep=False)
    assert worker_decision.admitted, worker_decision.failures  # the live turn holds no slot
    assert worker_decision.live_task_ids == ()
    monkeypatch.setenv("DISPATCH_MAX_LIVE_WRITE_WORKERS", "0")
    assert not host_admission.evaluate("workspace-write", sweep=False).admitted
    assert host_admission.evaluate("workspace-write", sweep=False, orchestrator=True).admitted
    real_failures = dispatch_admission.host_failures
    monkeypatch.setattr(dispatch_admission, "host_failures", lambda *_a: ["MemAvailable below the floor"])
    refused = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    assert not refused.admitted and refused.failures == ("MemAvailable below the floor",)
    monkeypatch.setattr(dispatch_admission, "host_failures", real_failures)
    # A malformed orchestrator record is not exempt: it still counts as a writer.
    _write(_orchestrator_record("turn-bad", pid=os.getpid(), process_start=worker._identity(os.getpid()),
                                orchestrator={"stream": None}))
    assert host_admission.evaluate("workspace-write", sweep=False).live_task_ids == ("turn-bad",)
    # A turn's provider launch still paces its meter: cadence counts it, the slot counters do not.
    _write(_orchestrator_record("turn-cursor", agent="cursor", pid=os.getpid(),
                                process_start=worker._identity(os.getpid())))
    scan = dispatch_admission.scan_task_records(task_store, record_liveness=lambda _r: True, meter="grok+cursor",
                                                min_interval_s=60)
    assert (scan.live_on_meter, scan.last_meter_launch_age_s is not None) == (0, True)


# --- the stream lease: one Monitor claim per stream (DRIVERS-1122 PR 2) --------------------------------------------


def _monitor_store(tmp_path):
    from lu_machinery.session_streams.db import SessionStreamDatabase
    from lu_machinery.session_streams.store import SessionStreamStore

    return SessionStreamStore(SessionStreamDatabase(tmp_path / "monitor" / "epics.sqlite3"))


@pytest.fixture
def monitor(tmp_path, monkeypatch, request):
    """A loopback Monitor serving the production epics router over a test-owned store."""
    from tests.epics_monitor_stub import epics_monitor_stub

    request.getfixturevalue("isolated_monitor_state")

    (tmp_path / "monitor").mkdir()
    store_ = _monitor_store(tmp_path)
    with epics_monitor_stub(store_) as base:
        monkeypatch.setenv("LU_MONITOR_LOOPBACK", base)
        yield SimpleNamespace(base=base, store=store_, client=remote.RemoteEpicClient(base=base))


def _legacy(name="legacy"):
    from lu_machinery.session_streams.model import LeaseHolder

    return LeaseHolder("codex", "codex-cli", name, process_id=os.getpid())


def _legacy_claim(client, stream=STREAM, name="legacy", ttl=900):
    return client.claim(stream_id=stream, holder=_legacy(name), lineage_id=f"{name}-lineage", ttl_seconds=ttl,
                        session_id=f"{name}-session", lease_id=f"{name}-lease")


def _reserve(task_id, stream=STREAM, *, launch=None, nonce=None, ttl=3600, force_reason=None):
    """Reserve ``stream`` for turn ``task_id`` as dispatch does, then hand the record on as to a supervisor."""
    decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    with host_admission.reserve(task_id, "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                force_reason=force_reason, orchestrator={"stream": stream},
                                run_nonce=nonce or uuid.uuid4().hex, turn_ttl_s=ttl, launch=launch) as hold:
        store.replace({**hold, "prompt_file": "/prompts/turn.md"}, store.record_path(task_id))
    return hold


# A dispatcher process that reserves a stream on "go", reports, and holds it until "done" (stdin).
_CONTENDER = """
import sys, uuid
from lu_machinery.dispatcher import host_admission
task_id, stream = sys.argv[1:3]
decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
print("ready", flush=True)
sys.stdin.readline()
try:
    with host_admission.reserve(task_id, "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                orchestrator={"stream": stream}, run_nonce=uuid.uuid4().hex, turn_ttl_s=3600,
                                launch={"started": False}):
        print("held", flush=True)
        sys.stdin.readline()
except host_admission.Refused as exc:
    print("refused", exc.code, exc, flush=True)
"""


def test_dispatch_processes_and_a_legacy_launcher_contending_for_one_stream_get_one_lease(task_store, monitor):
    """Defect class T1: two dispatcher processes both start a turn on one stream (check-then-act across processes), a
    legacy launcher claims beside a turn or a turn beside a legacy lease, or one stream's lease blocks another."""
    contenders = [(f"turn-{n}", STREAM) for n in range(4)] + [("turn-other", "epic:8")]
    procs = [subprocess.Popen([sys.executable, "-c", _CONTENDER, task_id, stream], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for task_id, stream in contenders]
    # Bounds every pipe read: a contender that hangs is killed, which ends its output.
    watchdog = threading.Timer(120, lambda: [proc.kill() for proc in procs])
    watchdog.start()
    try:
        for proc in procs:
            assert proc.stdout.readline() == "ready\n", proc.stderr.read()
        for proc in procs:  # every process has its admission decision; now they all claim at once
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = [proc.stdout.readline() for proc in procs]
        held = [task_id for (task_id, _stream), line in zip(contenders, results, strict=True) if line == "held\n"]
        assert len(held) == 2 and "turn-other" in held, results
        [winner] = [task_id for task_id in held if task_id != "turn-other"]
        refused = [line for line in results if line.startswith("refused")]
        assert len(refused) == 3
        assert all(line.startswith(f"refused 3 ❌ orchestrator_stream_leased: {STREAM} has an active lease")
                   for line in refused), refused
        assert monitor.client.stream(STREAM)["lease"]["holder"]["task_id"] == winner
        # A legacy launcher's claim is refused while the turn holds the stream, with the turn conflict's detail.
        with pytest.raises(remote.RemoteClaimConflictError, match="turn_lease_uncleared"):
            _legacy_claim(monitor.client)
    finally:
        for proc in procs:
            with contextlib.suppress(BrokenPipeError, ValueError):
                proc.stdin.write("done\n")
                proc.stdin.close()
            proc.wait(timeout=60)
        watchdog.cancel()
    # Each hold ended before any supervisor could start, so both leases were released exactly.
    assert {monitor.client.stream(stream)["lease"]["state"] for stream in (STREAM, "epic:8")} == {"released"}
    # The other order: a legacy lease holds the stream, and a turn is refused beside it.
    _legacy_claim(monitor.client)
    with pytest.raises(host_admission.Refused, match=f"orchestrator_stream_leased: {STREAM} has an active lease"):
        _reserve("turn-next", force_reason="operator says go")
    assert store.read("turn-next", missing_ok=True) is None  # refused before any hold


def _answer(nonce, ttl=3600, **changes):
    """Monitor's claim answer for this turn's new lease, with ``changes`` applied (``lease.<field>`` for its lease)."""
    from lu_machinery.session_streams.store import TURN_HOLDER_HARNESS

    expires = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z")
    lease = dict(stream_id=STREAM, session_id=f"turn-session-{nonce}", lease_id=f"turn-lease-{nonce}", generation=3,
                 fencing_token=3, state="active", session_state="open", heartbeat_at=store.now(), expires_at=expires,
                 ttl_seconds=ttl, version=1, age_seconds=0,
                 holder=dict(agent="claude", harness=TURN_HOLDER_HARNESS, instance_id=nonce, task_id="turn-1",
                             process_id=os.getpid(), holder_kind="process", host_id=None))
    answer = {"stream_id": STREAM, "outcome": "claimed", "lease": lease}
    for key, value in changes.items():
        target, name = (lease, key.removeprefix("lease.")) if key.startswith("lease.") else (answer, key)
        if name.startswith("holder."):
            target = lease["holder"]
            name = name.removeprefix("holder.")
        if value is _MISSING:
            target.pop(name)
        else:
            target[name] = value
    return answer


def _conflict(detail):
    return remote.RemoteClaimConflictError(f"Monitor API refused request (409): {detail}", detail=detail)


@pytest.mark.parametrize(
    ("answer", "refused"),
    [
        ({}, None),
        (_conflict("turn_lease_uncleared: an active lease ...; current holder=codex/codex-cli"),
         "orchestrator_stream_leased"),
        (_conflict("epic claim rejected by a session-stream invariant"), "orchestrator_lease_unknown"),
        (_conflict("epic stream already has live session; current holder=codex/codex-cli"), "orchestrator_lease_unknown"),
        (_conflict("turn_lease_unclearedX"), "orchestrator_lease_unknown"),
        (remote.RemoteSupervisorError("Monitor API refused request (403): loopback mutation required"),
         "orchestrator_lease_unknown"),
        (remote.RemoteUnreachableError("Monitor API unreachable; no remote claim was made"), "orchestrator_lease_unknown"),
        (TimeoutError("the answer was lost"), "orchestrator_lease_unknown"),
        (AttributeError("'list' object has no attribute 'get'"), "orchestrator_lease_unknown"),
        ({"outcome": "replayed"}, "orchestrator_lease_unknown"),
        ({"outcome": "recovered"}, "orchestrator_lease_unknown"),
        ({"outcome": _MISSING}, "orchestrator_lease_unknown"),
        ({"stream_id": "epic:8"}, "orchestrator_lease_unknown"),
        ({"lease": None}, "orchestrator_lease_unknown"),
        ({"lease.stream_id": "epic:8"}, "orchestrator_lease_unknown"),
        ({"lease.session_id": "session-other"}, "orchestrator_lease_unknown"),
        ({"lease.lease_id": "lease-other"}, "orchestrator_lease_unknown"),
        ({"lease.state": "expired"}, "orchestrator_lease_unknown"),
        ({"lease.session_state": "rolling"}, "orchestrator_lease_unknown"),
        ({"lease.holder.harness": "codex-cli"}, "orchestrator_lease_unknown"),
        ({"lease.holder.instance_id": "f" * 32}, "orchestrator_lease_unknown"),
        ({"lease.holder.task_id": "turn-2"}, "orchestrator_lease_unknown"),
        ({"lease.holder.host_id": "elsewhere"}, "orchestrator_lease_unknown"),
        ({"lease.holder.process_id": _MISSING}, "orchestrator_lease_unknown"),
        ({"lease.generation": True}, "orchestrator_lease_unknown"),
        ({"lease.fencing_token": 0}, "orchestrator_lease_unknown"),
        ({"lease.ttl_seconds": 7200}, "orchestrator_lease_unknown"),
        ({"lease.expires_at": "soon"}, "orchestrator_lease_unknown"),
        ({"lease.expires_at": "2026-01-01T00:00:00Z"}, "orchestrator_lease_unknown"),  # already past
        ({"lease.expires_at": "2099-01-01T00:00:00Z"}, "orchestrator_lease_unknown"),  # beyond the TTL asked for
    ],
)
def test_a_claim_answer_that_is_not_exactly_this_turns_new_lease_starts_nothing(task_store, monkeypatch, answer,
                                                                                refused):
    """Defect class T3: a replayed, recovered, malformed, foreign or lost claim answer, or a 409 that is not a turn
    conflict, reads as this turn's lease (or as a held stream), or the dispatcher retries the claim."""
    nonce = "a" * 32
    calls = []

    def claim(self, **kwargs):
        calls.append(kwargs)
        if isinstance(answer, BaseException):
            raise answer
        return None, _answer(nonce, **answer)

    monkeypatch.setattr(remote.RemoteEpicClient, "claim", claim)
    if refused is None:
        envelope = host_admission.claim_turn(STREAM, "turn-1", nonce, agent="claude", ttl_s=3600)
        assert envelope["lease"]["holder"]["instance_id"] == nonce and set(envelope) == {"monitor", "lease"}
    else:
        with pytest.raises(host_admission.Refused, match=f"^❌ {refused}: ") as error:
            host_admission.claim_turn(STREAM, "turn-1", nonce, agent="claude", ttl_s=3600)
        assert error.value.code == 3
    assert len(calls) == 1  # never retried
    assert calls[0]["holder"].instance_id == nonce and calls[0]["session_id"] == f"turn-session-{nonce}"


def test_a_monitor_outage_before_or_after_the_claim_commits_is_unknown(task_store, monitor, monkeypatch):
    """Defect class T3: an outage before the claim reads as 'no lease', or a claim Monitor committed whose answer
    was lost frees the stream, is retried, or starts the turn."""
    real_request = remote.RemoteEpicClient._request
    lost = {"claim": True}

    def request(self, method, path, payload=None):
        answer = real_request(self, method, path, payload)
        if path.endswith("/claim") and lost["claim"]:
            raise TimeoutError("the claim's answer was lost after Monitor committed it")
        return answer

    monkeypatch.setattr(remote.RemoteEpicClient, "_request", request)
    with pytest.raises(host_admission.Refused, match="orchestrator_lease_unknown"):
        _reserve("turn-1")
    assert store.read("turn-1", missing_ok=True) is None  # nothing started
    # The committed claim holds the stream: no turn and no legacy launcher passes it until an operator releases it.
    lost["claim"] = False
    with pytest.raises(host_admission.Refused, match="orchestrator_stream_leased"):
        _reserve("turn-2")
    with pytest.raises(remote.RemoteClaimConflictError, match="turn_lease_uncleared"):
        _legacy_claim(monitor.client)
    monitor.client.release(stream_id=STREAM, force=True, actor_agent="claude-cto", actor_host_id="host-a",
                           reason="the lost claim's turn never started")
    assert _reserve("turn-3")["turn_lease"]["lease"]["holder"]["task_id"] == "turn-3"
    # Before the commit: an unreachable Monitor claims nothing.
    monkeypatch.setenv("LU_MONITOR_LOOPBACK", "http://127.0.0.1:9")
    with pytest.raises(host_admission.Refused, match="orchestrator_lease_unknown"):
        _reserve("turn-4", stream="epic:8")
    monkeypatch.setenv("LU_MONITOR_LOOPBACK", monitor.base)
    assert monitor.store.lease_projection("epic:8") is None


@pytest.mark.parametrize(
    ("lease", "refused"),
    [
        (None, None),
        (_lease(), "orchestrator_stream_leased"),
        (_lease(session_state="rolling"), "orchestrator_stream_leased"),
        (_lease(state="expired", expires_in=-5), "orchestrator_stream_leased"),  # a turn never supersedes it
        (_lease(state="released"), None),
        ({**_lease(), "expires_at": "soon"}, "orchestrator_lease_unknown"),
        ("lease", "orchestrator_lease_unknown"),
        (_MISSING, "orchestrator_lease_unknown"),  # no lease field is not the projection's explicit null
        ({key: value for key, value in _lease().items() if key != "session_state"}, "orchestrator_lease_unknown"),
        (_lease(state="held"), "orchestrator_lease_unknown"),
        ({**_lease(state="released"), "expires_at": None}, "orchestrator_lease_unknown"),
    ],
)
def test_a_dry_run_reads_any_active_lease_as_busy(monkeypatch, lease, refused):
    """Defect class T1: a dry run reports a stream free that a turn's claim would be refused on (an expired but
    uncleared lease), or reads an unreadable lease as none."""
    monkeypatch.setattr(remote.RemoteEpicClient, "stream", lambda self, stream_id, digest_limit=20: {
        "stream_id": stream_id, **({} if lease is _MISSING else {"lease": lease})})
    reason = host_admission.turn_lease_probe(STREAM)
    if refused is None:
        assert reason is None
    else:
        assert reason.startswith(refused), reason


@pytest.mark.parametrize(("progress", "record", "released"), [
    ({"started": False}, "kept", True),  # this dispatch never started a supervisor
    ({"started": False}, "deleted", True),
    ({"started": True}, "kept", False),  # a supervisor may run, whatever the record says
    ({"started": True}, "deleted", False),
    ({"started": True}, "replaced", False),
    ({"started": True}, "damaged", False),  # and the damaged record never escapes the dispatch as an error
    ({"started": False}, "damaged", True),
    ({"started": None}, "kept", False),  # damaged progress is not proof
    (None, "kept", False),  # no progress given
])
def test_a_pre_launch_abort_releases_the_lease_only_on_the_dispatchers_own_progress(task_store, monkeypatch, progress,
                                                                                     record, released):
    """Defect class T4: a lost, replaced or stale hold record reads as 'never launched' and releases the stream's
    lease while a supervisor this dispatch started may still run."""
    envelope = {"monitor": "http://127.0.0.1:8765", "lease": {"stream_id": STREAM}}
    releases = []
    monkeypatch.setattr(host_admission, "claim_turn", lambda *_a, **_k: envelope)
    monkeypatch.setattr(host_admission, "release_turn", lambda lease, **kwargs: releases.append((lease, kwargs)))
    decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    nonce = "b" * 32
    with host_admission.reserve("turn-1", "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                orchestrator={"stream": STREAM}, run_nonce=nonce, turn_ttl_s=3600,
                                launch=progress) as hold:
        assert (hold["run_nonce"], hold["turn_lease"]) == (nonce, envelope)
        path = store.record_path("turn-1")
        if record == "deleted":
            path.unlink()
        elif record == "replaced":
            path.write_text(json.dumps({**hold, "run_nonce": "f" * 32}))
        elif record == "damaged":
            path.write_text("{")
    expected = [(envelope, {"stream": STREAM, "task_id": "turn-1", "run_nonce": nonce})]
    assert releases == (expected if released else [])


@pytest.mark.parametrize(
    ("change", "released"),
    [
        ({}, True),  # terminal, a scope launch whose probe proves every descendant gone, no task service
        ({"status": "timeout"}, True),
        ({"probe": True}, False),  # a process of the run lingers
        ({"probe": None}, False),  # liveness unknown
        ({"probe": "raise"}, False),  # a probe error is not proof
        ({"launch_mode": "popen-fallback"}, False),  # a descendant can leave a fallback launch's process group
        ({"launch_mode": None}, False),
        ({"status": "running"}, False),  # not terminal on disk
        ({"status": ["done"]}, False),
        ({"task_services": ["lu-task-x"]}, False),  # a service is its own unit, outside the scope probe
        ({"task_services": "lu-task-x"}, False),  # an unreadable service list
        ({"orchestrator": {"stream": "epic:8"}}, False),  # the published record names another stream
        ({"orchestrator": None}, False),
        ({"run_nonce": "f" * 32}, False),  # the published record is another run's
    ],
)
def test_settlement_releases_the_lease_only_on_proof_the_run_is_gone(task_store, monkeypatch, change, released):
    """Defect class T4: settlement releases a stream's lease while a process or service of the turn may still write,
    or from a record that is not this run's terminal one."""
    envelope = {"monitor": "http://127.0.0.1:8765", "lease": {"stream_id": STREAM}}
    releases = []
    monkeypatch.setattr(host_admission, "release_turn", lambda lease, **kwargs: releases.append((lease, kwargs)))
    record = _orchestrator_record("turn-1", **{**_LAUNCHED, "status": "done", "launch_unit": "lu-worker-turn"},
                                  turn_lease=envelope)
    published = {**record, **{key: value for key, value in change.items() if key != "probe"}}
    store.record_path("turn-1").write_text(json.dumps(published))  # raw: the store's writer refuses some states

    def probe(seen):
        assert seen["run_nonce"] == published["run_nonce"]
        if change.get("probe") == "raise":
            raise ValueError("a damaged liveness field")
        return change.get("probe", False)

    monkeypatch.setattr(worker, "_provider_alive", probe)
    worker._release_stream(record, evidence_lost=True)  # the record had to be rebuilt at publication
    assert releases == []
    worker._release_stream(record, evidence_lost=False)
    expected = [(envelope, {"stream": STREAM, "task_id": "turn-1", "run_nonce": record["run_nonce"]})]
    assert releases == (expected if released else [])


def test_a_release_is_exact_one_shot_and_fenced_from_a_successor(task_store, monitor, monkeypatch):
    """Defect class T4: a release reaches another Monitor, names another run's lease, retries or force-releases after
    an outage, or a late release frees the successor that an operator's force release let in."""
    hold = _reserve("turn-1", launch={"started": True})  # launched: only settlement releases it
    envelope, nonce = hold["turn_lease"], hold["run_nonce"]
    exact = dict(stream=STREAM, task_id="turn-1", run_nonce=nonce)
    assert monitor.client.stream(STREAM)["lease"]["state"] == "active"
    for wrong in (dict(exact, task_id="turn-2"), dict(exact, run_nonce="f" * 32), dict(exact, stream="epic:8")):
        assert host_admission.release_turn(envelope, **wrong).startswith("orchestrator_lease_release_refused")
    for monitor_spelling in (monitor.base.replace("127.0.0.1", "localhost"), "http://127.0.0.1:9"):
        reason = host_admission.release_turn({**envelope, "monitor": monitor_spelling}, **exact)
        assert reason.startswith(("orchestrator_lease_release_refused", "orchestrator_lease_release_unknown")), reason
    calls = []

    def unreachable(self, *args, **kwargs):
        calls.append(kwargs)
        raise remote.RemoteUnreachableError("Monitor API unreachable")

    with monkeypatch.context() as patch:
        patch.setattr(remote.RemoteEpicClient, "release", unreachable)
        assert host_admission.release_turn(envelope, **exact).startswith("orchestrator_lease_release_unknown")
    assert len(calls) == 1 and "force" not in calls[0]
    assert monitor.client.stream(STREAM)["lease"]["state"] == "active"  # the outage kept it
    monitor.client.release(stream_id=STREAM, force=True, actor_agent="claude-cto", actor_host_id="host-a",
                           reason="the turn's scope is proven gone")
    successor = _legacy_claim(monitor.client)[0]
    before = monitor.store.dump_stream(STREAM)
    assert host_admission.release_turn(envelope, **exact).startswith("orchestrator_lease_release_fenced")
    assert monitor.store.dump_stream(STREAM) == before
    monitor.client.release(successor)
    assert _reserve("turn-2", launch={"started": False}) is not None  # pre-launch abort released it again
    assert host_admission.release_turn(_reserve("turn-3", launch={"started": True})["turn_lease"], stream=STREAM,
                                       task_id="turn-3", run_nonce=store.read("turn-3")["run_nonce"]) is None


# --- the turn's bounds --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hard_timeout", "timing"),
    [
        (7200, (7800, 8490)),
        (1.5, (602, 1292)),  # fractions round up, never down
        (85110, (85710, 86400)),  # the largest deadline whose lease fits Monitor's limit
        (85110.5, None),
        (86400, None),
        (0, None),
        (-1, None),
        (float("nan"), None),
        (float("inf"), None),
        (True, None),
        ("7200", None),
    ],
)
def test_a_turn_needs_a_finite_deadline_its_lease_covers(hard_timeout, timing):
    """Defect class T5: a zero, negative, non-finite, fractional or oversized --hard-timeout gives a turn an unbounded
    scope or a lease TTL shorter than its run and stop, or one Monitor refuses."""
    if timing is None:
        with pytest.raises(ValueError, match=r"^orchestrator_timeout_refused: "):
            host_admission.turn_timing(hard_timeout)
    else:
        assert host_admission.turn_timing(hard_timeout) == timing
        runtime, ttl = timing
        assert ttl - runtime == (host_admission.TURN_SETUP_BUDGET_S + host_admission.TURN_STOP_TIMEOUT_S
                                 + host_admission.TURN_TTL_MARGIN_S)


@pytest.mark.parametrize(("remaining", "refused"), [
    (8490, None), (7891, None), (7889, "orchestrator_lease_budget_short"), (-60, "orchestrator_lease_budget_short"),
    (None, "orchestrator_lease_budget_unknown")])
def test_a_delayed_setup_never_starts_a_turn_its_lease_cannot_outlast(remaining, refused):
    """Defect class T5: a setup that ran past its budget (a slow worktree) starts a turn whose lease expires before its
    run and the scope's stop end."""
    expires = None if remaining is None else (datetime.now(UTC) + timedelta(seconds=remaining)).isoformat()
    reason = host_admission.turn_launch_refusal({"lease": {"expires_at": expires}}, 7800)
    assert (reason or "").startswith(refused or ""), reason
    assert (reason is None) is (refused is None)


def test_a_turn_launch_passes_its_bounds_and_refuses_fallback(monkeypatch, tmp_path):
    """Defect class T5: the turn's scope bounds never reach systemd-run, or a turn's supervisor starts as a plain
    process when its scope cannot start."""
    from lu_machinery.dispatcher import admission
    from lu_machinery.orchestration import dispatch_isolation

    seen = []

    def spawn(cmd, **kwargs):
        seen.append(kwargs)
        raise dispatch_isolation.DispatchIsolationError("forced: no scope")

    monkeypatch.setattr(dispatch_isolation, "spawn_detached_worker", spawn)
    for limits in ((7800, 30), None):
        with pytest.raises(admission.LaunchRefused):
            admission.spawn_supervisor(["true"], task_id="turn-1", run_nonce="n", cwd=tmp_path, stderr=None,
                                       stderr_log=tmp_path / "log", env={}, scope_limits=limits)
    bounded, ordinary = seen
    assert (bounded["allow_fallback"], bounded["runtime_max_s"], bounded["stop_timeout_s"]) == (False, 7800, 30)
    assert not {"allow_fallback", "runtime_max_s", "stop_timeout_s"} & set(ordinary)  # every other task unchanged


# --- writers ------------------------------------------------------------------------------------------------------


def test_no_task_borrows_a_turn_tree(task_store, tmp_path):
    """Defect class T6: a --cwd task attaches to an orchestrator turn's tree because the turn's record is missing,
    archived, damaged or lost its orchestrator key, so the turn's tree changes after it was judged."""
    trees = tmp_path / "trees"
    assert host_admission.turn_tree_refusal(trees / "turn-1").startswith("orchestrator_turn_writer_refused")  # no record
    for alias in (trees / "turn-1" / "sub" / "..", trees / "turn-1" / "."):  # any spelling of the turn's tree
        assert host_admission.turn_tree_refusal(alias).startswith("orchestrator_turn_writer_refused"), alias
    _write(_orchestrator_record("turn-1"))
    assert host_admission.turn_tree_refusal(trees / "turn-1").startswith("orchestrator_turn_writer_refused")
    _write(_orchestrator_record("legacy-turn"))  # a turn from before the reserved prefix
    assert "orchestrator turn on epic:7" in host_admission.turn_tree_refusal(trees / "legacy-turn")
    store.record_path("damaged").write_text("{")
    assert "cannot be read" in host_admission.turn_tree_refusal(trees / "damaged")
    plain = _orchestrator_record("worker-1")
    plain.pop("orchestrator")
    _write(plain)
    for ordinary in ("worker-1", "prior-round", "-not-a-task-id"):  # an ordinary task's, or a tree no record names
        assert host_admission.turn_tree_refusal(trees / ordinary) is None


@pytest.mark.skipif(not os.path.exists("/sys/fs/cgroup/cgroup.controllers"), reason="needs cgroup v2")
def test_the_scope_probe_proves_exit_only_for_a_leaf_scope(task_store, monkeypatch):
    """Defect class: a process moved into a child cgroup of the turn's scope is missing from its cgroup.procs, so
    the probe reports the scope empty and settlement frees the stream."""
    from lu_machinery.orchestration import dispatch_isolation as iso

    own = next(line.split("::", 1)[1] for line in Path("/proc/self/cgroup").read_text().splitlines()
               if line.startswith("0::"))
    parent = own.rpartition("/")[0]  # holds the test's own cgroup as a child, and no process of its own
    procs = Path(f"/sys/fs/cgroup{parent}/cgroup.procs")
    if not parent or not procs.is_file() or procs.read_text().split():
        pytest.skip("the test's cgroup has no parent that holds only child cgroups")
    nonce = "0123456789abcdef0123456789abcdef"
    unit = iso.scope_unit_name("turn", nonce)
    monkeypatch.setattr(worker, "_run_scope_cgroups", lambda *_args: [parent])
    launch = {"task_id": "turn", "run_nonce": nonce, "launch_mode": "scope", "launch_unit": unit,
              "launch_cgroup": iso.scope_cgroup(unit, uid=os.getuid())}
    assert worker._provider_alive(launch) is None


# --- handoff receipt -----------------------------------------------------------------------------------------------


def test_the_receipt_is_written_once_by_the_turn_and_survives_stale_writers(task_store, monkeypatch):
    """Defect class: a second, foreign or late receipt is accepted, or a stale record copy erases it."""
    record = _orchestrator_record("turn")
    _write(record)
    args = ("turn", record["run_nonce"], STREAM, "note-1")
    with pytest.raises(ValueError, match="only the orchestrator turn itself"):
        store.record_handoff_receipt(*args)
    monkeypatch.setenv(TASK_ENV, "turn")
    with pytest.raises(ValueError, match="not an orchestrator turn on this stream"):
        store.record_handoff_receipt("turn", record["run_nonce"], "epic:8", "note-1")
    with pytest.raises(ValueError, match="run nonce mismatch"):
        store.record_handoff_receipt("turn", "other-nonce", STREAM, "note-1")
    with pytest.raises(ValueError, match="--handoff-ref"):
        store.record_handoff_receipt("turn", record["run_nonce"], STREAM, "note 1")
    receipt = store.record_handoff_receipt(*args)
    assert receipt["receipt_id"] == store.handoff_receipt_id(STREAM, "turn", record["run_nonce"], "note-1")
    assert store.record_handoff_receipt(*args) == receipt  # an identical retry
    with pytest.raises(ValueError, match="handoff_receipt_conflict"):
        store.record_handoff_receipt("turn", record["run_nonce"], STREAM, "note-2")
    store.replace(record)  # the supervisor's stale in-memory copy has no receipt
    assert store.read("turn")[store.HANDOFF_RECEIPT_KEY] == receipt
    plain = _orchestrator_record("worker")
    plain.pop("orchestrator")
    _write(plain)
    monkeypatch.setenv(TASK_ENV, "worker")
    with pytest.raises(ValueError, match="not an orchestrator turn"):
        store.record_handoff_receipt("worker", plain["run_nonce"], STREAM, "note-1")
    late = _orchestrator_record("late", settling_at=store.now())  # an active record whose settlement began
    _write(late)
    monkeypatch.setenv(TASK_ENV, "late")
    with pytest.raises(ValueError, match="settlement has started"):
        store.record_handoff_receipt("late", late["run_nonce"], STREAM, "note-1")
    # A stored receipt that fails validation is never returned as an identical retry.
    partial = _orchestrator_record("partial")
    partial[store.HANDOFF_RECEIPT_KEY] = {key: value for key, value in receipt.items() if key != "recorded_at"}
    partial[store.HANDOFF_RECEIPT_KEY].update(task_id="partial", run_nonce=partial["run_nonce"], receipt_id=
                                              store.handoff_receipt_id(STREAM, "partial", partial["run_nonce"], "note-1"))
    _write(partial)
    monkeypatch.setenv(TASK_ENV, "partial")
    with pytest.raises(ValueError, match="handoff_receipt_conflict"):
        store.record_handoff_receipt("partial", partial["run_nonce"], STREAM, "note-1")


def _settled(**fields):
    record = _orchestrator_record("turn", status="done", error=None, commits_ahead=0, worktree_dirty_on_exit=False,
                                  head_sha="5" * 40, worktree_start_sha="5" * 40)
    record[store.HANDOFF_RECEIPT_KEY] = dict(
        schema=store.HANDOFF_RECEIPT_SCHEMA, receipt_id=store.handoff_receipt_id(STREAM, "turn", record["run_nonce"], "n1"),
        stream=STREAM, task_id="turn", run_nonce=record["run_nonce"], handoff_ref="n1", recorded_at=store.now())
    for key, value in fields.items():
        if key.startswith("receipt."):  # one receipt field changed, or dropped with _MISSING
            record[store.HANDOFF_RECEIPT_KEY][key.removeprefix("receipt.")] = value
            if value is _MISSING:
                record[store.HANDOFF_RECEIPT_KEY].pop(key.removeprefix("receipt."))
        else:
            record[key] = value
    return record


@pytest.mark.parametrize(
    ("change", "status", "reason"),
    [
        ({}, "done", None),
        ({store.HANDOFF_RECEIPT_KEY: None}, "no_deliverable", "orchestrator_handoff_receipt_missing"),
        ({"run_nonce": "replaced"}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"orchestrator": {"stream": "epic:8"}}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"worktree_dirty_on_exit": True}, "no_deliverable", "orchestrator_product_write"),
        ({"worktree_dirty_on_exit": None}, "no_deliverable", "orchestrator_product_write"),
        ({"commits_ahead": 1}, "no_deliverable", "orchestrator_product_write"),
        ({"commits_ahead": None}, "no_deliverable", "orchestrator_product_write"),
        ({"head_sha": "a" * 40}, "no_deliverable", "orchestrator_product_write"),  # checked out another commit
        ({"head_sha": None}, "no_deliverable", "orchestrator_product_write"),
        ({"receipt.recorded_at": _MISSING}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"receipt.recorded_at": "soon"}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"receipt.recorded_at": "2026-11-22T20:00:00+02:00"}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"receipt.recorded_at": "2026-11-22\x0020:00:00+00:00"}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"receipt.recorded_at": "2026-11-22x20:00:00+00:00"}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"receipt.recorded_at": "２026-11-22T20:00:00+00:00"}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"receipt.recorded_at": "2026-13-22T20:00:00+00:00"}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"receipt.recorded_at": "2026-11-22T20:00:00Z"}, "done", None),
        ({"receipt.extra": "field"}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
        ({"status": "failed", "error": "provider exited unsuccessfully"}, "failed", None),
    ],
)
def test_a_turn_settles_done_only_on_its_receipt_with_an_untouched_tree(monkeypatch, change, status, reason):
    """Defect class: a turn settles done without its handoff, with product writes, or through a merged PR."""
    monkeypatch.setattr(git, "merged_delivery", lambda _record: pytest.fail("merged delivery consulted"))
    record = _settled(**change)
    if change.get(store.HANDOFF_RECEIPT_KEY, ...) is None:
        record.pop(store.HANDOFF_RECEIPT_KEY)
    worker._enforce_delivery(record)
    assert record["status"] == status
    assert (record.get("no_deliverable_reason") or "").startswith(reason or "")
    assert record["owned_path_grants_used"] == []


# --- product writes ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["staged", "push"])
def test_the_commit_and_push_hooks_refuse_every_write_by_a_turn(task_store, tmp_path, monkeypatch, capsys, stage):
    """Defect class: an orchestrator turn (empty owned paths: 'unrestricted') commits or pushes a product change."""
    tree = tmp_path / "turn-tree"
    tree.mkdir()
    run_git(tree, "init", "-q", "-b", "main")
    elsewhere = tmp_path / "child-tree"
    elsewhere.mkdir()
    run_git(elsewhere, "init", "-q", "-b", "main")
    _write(_orchestrator_record("turn", worktree_path=str(tree)))
    monkeypatch.setenv(TASK_ENV, "turn")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    for root in (tree, elsewhere):  # its own tree, and any other tree its processes reach
        assert ownership.main(["--worktree", str(root), stage]) == 1
        assert "orchestrator turn writes no product change" in capsys.readouterr().err
    plain = _orchestrator_record("worker", worktree_path=str(tree))
    plain.pop("orchestrator")
    _write(plain)
    monkeypatch.setenv(TASK_ENV, "worker")
    assert ownership.main(["--worktree", str(tree), stage]) == 0  # unchanged: empty owned paths stay unrestricted


def test_cleanup_never_salvages_or_publishes_a_rejected_turn(monkeypatch):
    """Defect class: scheduled cleanup commits and pushes the edits settlement refused (salvage, merged delivery)."""
    seen = {}
    monkeypatch.setattr(worker, "_stop_task_services", lambda _record: None)
    monkeypatch.setattr(worker.scratch, "settleable", lambda _record: False)
    monkeypatch.setattr(worker, "_reap_worktree", lambda _record, patience=0, salvage=False, **kwargs: seen.update(
        salvage=salvage, **kwargs))
    monkeypatch.setattr(git, "merged_delivery", lambda _record: pytest.fail("merged delivery consulted"))
    worker._reap_settled(_settled(status="no_deliverable", worktree_dirty_on_exit=True), salvage=True)
    assert seen == {"salvage": False, "verified_merged_head": None}
    monkeypatch.setattr(git, "merged_delivery", lambda _record: None)
    plain = _settled(status="no_deliverable")
    plain.pop("orchestrator")
    worker._reap_settled(plain, salvage=True)
    assert seen["salvage"] is True  # unchanged for every other task


@pytest.mark.parametrize(
    ("launch_mode", "service_stop", "alive", "unproven"),
    [
        ("scope", None, False, False),  # services and processes ended and proven gone: the snapshot is final
        ("scope", None, True, True),
        ("scope", None, None, True),
        ("scope", "false", False, True),  # a service the turn started is its own unit, not in the scope probe
        ("popen-fallback", None, False, True),  # a descendant can leave the fallback launch's process group
        (None, None, False, True),
    ],
)
def test_a_turn_tree_is_judged_only_once_its_processes_are_proven_gone(task_store, monkeypatch, launch_mode,
                                                                        service_stop, alive, unproven):
    """Defect class: a leftover process or service writes the tree after delivery's snapshot, and the turn is done."""
    monkeypatch.setattr(worker, "SETTLE_PATIENCE", 0)
    monkeypatch.setattr(worker, "_end_own_scope", lambda _record: None)
    monkeypatch.setattr(worker, "_provider_alive", lambda _record: alive)

    def stop_services(record):
        if service_stop is not None:
            record["task_service_stops"] = [{"unit": "lu-task-x.service", "postcondition": service_stop}]

    monkeypatch.setattr(worker, "_stop_task_services", stop_services)
    assert worker._turn_tree_unproven(_settled(launch_mode=launch_mode)) is unproven


def test_a_turn_gets_no_path_grant(task_store, tmp_path, monkeypatch):
    """Defect class: a coordinator grant gives an orchestrator turn product scope."""
    _write(_orchestrator_record("turn", worktree_path=str(tmp_path)))
    monkeypatch.delenv(TASK_ENV, raising=False)
    with pytest.raises(ValueError, match="refused for orchestrator turns"):
        store.grant_paths("turn", "nonce-turn", ["docs/x.md"], "reason", "approval")


# --- the turn end to end: lease, child linkage, receipt, settlement -----------------------------------------------


# Loaded by the sandbox interpreter's site (a .pth import), so the dispatcher subprocess sees it: a turn's bounded
# launch is recorded and started as a plain process that claims to be its scope (CI has no user manager). Every
# other launch keeps the sandbox's forced fallback. The double cannot prove the turn's processes gone, which is the
# point: settlement then keeps the lease.
_SCOPE_DOUBLE = '''
import json, os
if os.environ.get("TURN_SCOPE_DOUBLE_LOG"):
    from lu_machinery.orchestration import dispatch_isolation as iso

    _real_spawn = iso.spawn_detached_worker
    iso._probe_uncached = lambda *_a, **_k: iso.ProbeResult(ready=True)

    def _spawn(cmd, *, task_id, run_nonce, popen, env=None, stdin=None, stdout=None, stderr=None, **kwargs):
        if kwargs.get("runtime_max_s") is None:
            return _real_spawn(cmd, task_id=task_id, run_nonce=run_nonce, popen=popen, env=env, stdin=stdin,
                               stdout=stdout, stderr=stderr, probe_env={"LU_DISPATCH_ISOLATION": "fallback"}, **kwargs)
        with open(os.environ["TURN_SCOPE_DOUBLE_LOG"], "a") as log:
            log.write(json.dumps(dict(task_id=task_id, run_nonce=run_nonce, allow_fallback=kwargs["allow_fallback"],
                                      argv=iso.build_scope_argv(["worker"], unit="u", runtime_max_s=kwargs["runtime_max_s"],
                                                                stop_timeout_s=kwargs["stop_timeout_s"]))) + "\\n")
        proc = popen(list(cmd), env=env, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True,
                     close_fds=True)
        return proc, iso.WorkerLaunch(mode=iso.LAUNCH_SCOPE, unit=iso.scope_unit_name(task_id, run_nonce))

    iso.spawn_detached_worker = _spawn
'''


@pytest.fixture
def turn_box(sandbox, monitor):
    for name in (TASK_ENV, "LU_WRITE_BOUNDARY_BROKER", "SESSION_SEAT", "SESSION_EPIC"):
        sandbox.env.pop(name, None)
    sandbox.env["LU_MONITOR_LOOPBACK"] = monitor.base
    # A turn takes no write slot: no write worker could start at this cap.
    sandbox.env["DISPATCH_MAX_LIVE_WRITE_WORKERS"] = "0"
    # A turn's launch paces its meter, which would defer the duplicate turn before its stream is checked.
    sandbox.env["DISPATCH_MIN_METER_LAUNCH_INTERVAL_S"] = "0"
    site = next(Path(sandbox.python).parent.parent.glob("lib/python*/site-packages"))
    (site / "_turn_scope_double.py").write_text(_SCOPE_DOUBLE)
    (site / "turn-scope-double.pth").write_text("import _turn_scope_double\n")
    launches = sandbox.tasks.parent / "turn-launches.jsonl"

    def run(*args, task_env=None, expected=0, isolated=False):
        env = {**sandbox.env, **({TASK_ENV: task_env} if task_env else {})}
        if isolated:  # the turn's own dispatcher: a host that can isolate it, through the double
            env.pop("LU_DISPATCH_ISOLATION", None)
            env["TURN_SCOPE_DOUBLE_LOG"] = str(launches)
        result = subprocess.run([sandbox.python, "lu_machinery/dispatch.py", *args], cwd=sandbox.repo, env=env,
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == expected, (result.stdout, result.stderr)
        return result

    def turn(task_id, expected=0, stream=STREAM, isolated=True):
        return run("dispatch", "--agent", "sol", "--model", "blocked", "--mode", "orchestrator", "--stream", stream,
                   "--initiator", "claude-cto", "--task-id", task_id, "--prompt", "drive one wave",
                   "--hard-timeout", "600", expected=expected, isolated=isolated)

    sandbox.run, sandbox.turn, sandbox.monitor, sandbox.launches = run, turn, monitor, launches
    return sandbox


def test_a_turn_end_to_end_holds_its_lease_links_children_and_never_shares_its_tree(turn_box):
    """Defect classes T1, T4, T5, T6: a duplicate turn or a legacy launcher shares the stream, the scope runs unbounded
    or as a fallback, a child or another task borrows the turn's tree, a late child of a settled turn starts, or a
    turn whose processes are not proven gone frees its stream."""
    box = turn_box
    # A host that cannot isolate a turn refuses it before its lease is claimed.
    assert "orchestrator_isolation_unavailable" in box.turn("turn-0", expected=3, isolated=False).stderr
    assert box.monitor.store.lease_projection(STREAM) is None
    task_id, nonce = box.turn("turn-1").stdout.splitlines()
    record = store.read("turn-1", tasks_root=box.tasks)
    assert record["orchestrator"] == {"stream": STREAM} and record["mode"] == "workspace-write"
    assert record["research_role"] == "driver"  # role policy sees a driving turn, not only authoring
    lease = box.monitor.client.stream(STREAM)["lease"]
    assert (lease["state"], lease["holder"]["harness"], lease["holder"]["instance_id"], lease["holder"]["task_id"]) == (
        "active", "dispatch-turn", nonce, "turn-1")
    assert record["turn_lease"]["lease"]["session_id"] == lease["session_id"]
    [launch] = [json.loads(line) for line in box.launches.read_text().splitlines()]
    assert (launch["task_id"], launch["run_nonce"], launch["allow_fallback"]) == ("turn-1", nonce, False)
    assert {"--property=RuntimeMaxSec=1200", "--property=TimeoutStopSec=30",
            "--property=KillMode=control-group"} <= set(launch["argv"])
    assert "orchestrator_stream_leased: epic:7" in box.turn("turn-2", expected=3).stderr
    with pytest.raises(remote.RemoteClaimConflictError, match="turn_lease_uncleared"):
        _legacy_claim(box.monitor.client)
    await_file(box.tasks / "turn-1.started")  # the turn's provider runs, as when it dispatches
    attach = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "borrower", "--prompt", "edit",
                     "--cwd", record["worktree_path"], task_env="turn-1", expected=2)
    assert "orchestrator_turn_writer_refused: a task an orchestrator turn dispatches cannot borrow" in attach.stderr
    outsider = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "outsider", "--prompt", "edit",
                       "--cwd", record["worktree_path"], expected=2)
    assert "orchestrator_turn_writer_refused: turn-1 is an orchestrator turn's tree" in outsider.stderr
    child = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "child-1",
                    "--prompt", "map the flags", task_env="turn-1").stdout.splitlines()
    linked = json.loads(box.run("wait", *child[:1], "--run-nonce", child[1], "--timeout", "15").stdout)
    assert (linked["parent_task_id"], linked["parent_run_nonce"], linked["parent_stream"]) == ("turn-1", nonce, STREAM)
    receipt = json.loads(box.run("handoff-receipt", "turn-1", "--run-nonce", nonce, "--stream", STREAM,
                                 "--handoff-ref", "note-41", task_env="turn-1").stdout)
    assert receipt["schema"] == store.HANDOFF_RECEIPT_SCHEMA and receipt["handoff_ref"] == "note-41"
    _release(box, "turn-1")
    settled = json.loads(box.run("wait", task_id, "--run-nonce", nonce, "--timeout", "60", expected=1).stdout)
    # The double's processes are not in the scope it names, so they are never proven gone before the tree is judged.
    assert (settled["status"], settled["no_deliverable_reason"].split(":")[0]) == (
        "no_deliverable", "orchestrator_tree_unproven"), settled
    assert settled[store.HANDOFF_RECEIPT_KEY] == receipt
    late = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "child-late", "--prompt", "late",
                   task_env="turn-1", expected=2)
    assert "orchestrator_parent_not_running" in late.stderr
    # Nor afterwards: the settled turn keeps its stream's lease until an operator releases it.
    assert box.monitor.client.stream(STREAM)["lease"]["state"] == "active"
    assert "orchestrator_stream_leased" in box.turn("turn-3", expected=3).stderr
    box.monitor.client.release(stream_id=STREAM, force=True, actor_agent="claude-cto", actor_host_id="host-a",
                               reason="turn-1 scope proven gone")
    assert box.turn("turn-4").stdout.splitlines()[0] == "turn-4"
    _release(box, "turn-4")


def _release(box, task_id):
    (box.tasks / f"{task_id}.release").write_text("go")


pytestmark = pytest.mark.usefixtures("sources_product_root")
