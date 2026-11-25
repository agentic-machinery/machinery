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
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.dispatcher import git, host_admission, ownership, store, worker
from lu_machinery.orchestration import dispatch_admission
from lu_machinery.orchestration.issue_stream_audit import load_registry as _load_stream_registry
from lu_machinery.session_supervisor import remote
from tests.dispatcher.conftest import await_file, run_git
from tests.epics_monitor_stub import isolated_monitor_state  # noqa: F401
from tests.helpers.product_tree import configured_roots  # noqa: F401

_STREAMS = _load_stream_registry()
STREAM = f"epic:{_STREAMS['practice'][0]}"
OTHER_STREAM = f"epic:{_STREAMS['hramatka'][0]}"
STREAM_LANE = "practice"
TASK_ENV = ownership.TASK_ID_ENV


def _fake_driver_state_cli(tmp_path: Path, notes: dict[str, dict | None]) -> Path:
    """Write a test-owned fake ``lu-driver-state`` executable serving ``show -- <id>``."""
    db_file = tmp_path / "driver-state-notes.json"
    db_file.write_text(json.dumps(notes), encoding="utf-8")
    script = tmp_path / "fake-lu-driver-state"
    script.write_text(
        f"#!{sys.executable}\n"
        "from datetime import UTC, datetime\n"
        "import json, sys\n"
        f"notes = json.loads(open({str(db_file)!r}, encoding='utf-8').read())\n"
        "args = sys.argv[1:]\n"
        "if len(args) >= 2 and args[0] == 'show':\n"
        "    ref = args[-1]\n"
        "    item = notes.get(ref)\n"
        "    if item is None:\n"
        "        sys.stderr.write(f'not found: {ref}\\n')\n"
        "        raise SystemExit(2)\n"
        "    if isinstance(item, dict) and item.get('created_utc') == '__NOW__':\n"
        "        item = {**item, 'created_utc': datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S.%f+00:00')}\n"
        "    print(json.dumps(item))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _fresh_note(note_id: str, *, lane: str = STREAM_LANE, created_utc: str | None = None, **extra) -> dict:
    created = created_utc or "__NOW__"
    return {
        "id": note_id,
        "alias": None,
        "kind": "handoff_note",
        "lane": lane,
        "status": "open",
        "created_utc": created,
        "statement": "Verified milestone handoff.",
        **extra,
    }


_MISSING = object()
_DEAD_PID = 2**22 + 1  # above the default pid_max, so never a live process
_LAUNCHED = dict(pid=_DEAD_PID, process_start="boot:1", launch_mode="scope", launch_unit="lu-worker-prior",
                 provider_pgid=None)


def _lease(state="active", session_state="open", expires_in=600, **fields):
    """Monitor's projection of one lease on STREAM, as its stream route answers."""
    ttl = fields.get("ttl_seconds", 900)
    expires_dt = datetime.now(UTC) + timedelta(seconds=expires_in)
    heartbeat_dt = expires_dt - timedelta(seconds=ttl if type(ttl) is int else 900)
    expires = expires_dt.isoformat().replace("+00:00", "Z")
    heartbeat = heartbeat_dt.isoformat().replace("+00:00", "Z")
    return {"stream_id": STREAM, "session_id": "session-1", "lease_id": "lease-1", "generation": 2,
            "fencing_token": 2, "state": state, "session_state": session_state, "heartbeat_at": heartbeat,
            "expires_at": expires, "ttl_seconds": 900, "version": 1, "age_seconds": 0,
            "holder": {"agent": "claude", "harness": "codex-cli", "instance_id": "legacy-1", "task_id": None,
                       "process_id": 4321, "holder_kind": "process", "host_id": None}, **fields}


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
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--task-id", "turn-review.next"],
         "Monitor-safe token"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--task-id", "turn-1.2.3.4"],
         "Monitor-safe token"),
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--task-id", "turn-vps"],
         "Monitor-safe token"),
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

    now = datetime.now(UTC)
    heartbeat = now.isoformat().replace("+00:00", "Z")
    expires = (now + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z")
    lease = dict(stream_id=STREAM, session_id=f"turn-session-{nonce}", lease_id=f"turn-lease-{nonce}", generation=3,
                 fencing_token=3, state="active", session_state="open", heartbeat_at=heartbeat, expires_at=expires,
                 ttl_seconds=ttl, version=1, age_seconds=0,
                 holder=dict(agent="claude", harness=TURN_HOLDER_HARNESS, instance_id=nonce, task_id="turn-1",
                             process_id=os.getpid(), holder_kind="process", host_id=None))
    answer = {"schema": "remote-epic-lifecycle.v1", "stream_id": STREAM, "session_state": "open", "outcome": "claimed",
              "lease": lease}
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
        ({"schema": _MISSING}, "orchestrator_lease_unknown"),
        ({"schema": "remote-epic-lifecycle.v2"}, "orchestrator_lease_unknown"),
        ({"schema": True}, "orchestrator_lease_unknown"),
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
        ({"lease.holder.process_id": float(os.getpid())}, "orchestrator_lease_unknown"),  # equal, but not an integer
        ({"lease.generation": True}, "orchestrator_lease_unknown"),
        ({"lease.fencing_token": 0}, "orchestrator_lease_unknown"),
        ({"ttl": 7200}, "orchestrator_lease_unknown"),  # valid 7200s projection, wrong TTL for this 3600s claim
        ({"lease.ttl_seconds": True}, "orchestrator_lease_unknown"),
        ({"lease.version": True}, "orchestrator_lease_unknown"),
        ({"lease.version": 0}, "orchestrator_lease_unknown"),
        ({"lease.age_seconds": -1}, "orchestrator_lease_unknown"),
        ({"lease.age_seconds": True}, "orchestrator_lease_unknown"),
        ({"lease.heartbeat_at": "soon"}, "orchestrator_lease_unknown"),
        ({"lease.heartbeat_at": (datetime.now(UTC) + timedelta(seconds=7200)).isoformat().replace("+00:00", "Z")},
         "orchestrator_lease_unknown"),  # heartbeat after expiry
        ({"lease.heartbeat_at": (datetime.now(UTC) - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")},
         "orchestrator_lease_unknown"),  # expiry - heartbeat != ttl_seconds
        ({"session_state": "closed"}, "orchestrator_lease_unknown"),  # top-level disagrees with nested open
        ({"session_state": _MISSING}, "orchestrator_lease_unknown"),
        ({"lease.expires_at": "soon"}, "orchestrator_lease_unknown"),
        ({"lease.heartbeat_at": "2025-12-31T23:00:00Z", "lease.expires_at": "2026-01-01T00:00:00Z"},
         "orchestrator_lease_unknown"),  # valid 3600s projection, already past
        ({"lease.heartbeat_at": "2098-12-31T23:00:00Z", "lease.expires_at": "2099-01-01T00:00:00Z"},
         "orchestrator_lease_unknown"),  # valid 3600s projection, beyond the TTL asked for
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


def test_a_dry_run_reads_a_projection_whose_session_disagrees_with_its_lease_as_unknown(monkeypatch):
    """Defect class T1: a dry run reports a stream free from a projection whose own session state contradicts its
    lease (an open session beside a released lease, or beside no lease), or whose lifecycle schema is unsupported."""
    for lease, session in ((_lease(state="released", session_state="closed"), "open"), (None, "open")):
        monkeypatch.setattr(remote.RemoteEpicClient, "stream", lambda self, stream_id, digest_limit=20, lease=lease,
                            session=session: {"schema": "remote-epic-lifecycle.v1", "stream_id": stream_id,
                                              "session_state": session, "lease": lease})
        assert host_admission.turn_lease_probe(STREAM).startswith("orchestrator_lease_unknown"), (lease, session)
    for bad_schema in (_MISSING, "remote-epic-lifecycle.v2", True):
        for lease, session in ((None, None), (_lease(state="released", session_state="closed"), "closed")):
            payload = {"stream_id": STREAM, "session_state": session, "lease": lease}
            if bad_schema is not _MISSING:
                payload["schema"] = bad_schema
            monkeypatch.setattr(remote.RemoteEpicClient, "stream",
                                lambda self, stream_id, digest_limit=20, payload=payload: payload)
            assert host_admission.turn_lease_probe(STREAM).startswith("orchestrator_lease_unknown"), (bad_schema, lease)


@pytest.mark.parametrize(
    ("lease", "refused"),
    [
        (None, None),
        (_lease(), "orchestrator_stream_leased"),
        (_lease(session_state="rolling"), "orchestrator_stream_leased"),
        (_lease(state="expired", expires_in=-5), "orchestrator_stream_leased"),  # a turn never supersedes it
        (_lease(state="released", session_state="closed"), None),
        (_lease(state="released", session_state="open"), "orchestrator_lease_unknown"),  # the states disagree
        (_lease(state="active", session_state="closed"), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", stream_id="epic:8"), "orchestrator_lease_unknown"),
        ({key: value for key, value in _lease(state="released", session_state="closed").items()
          if key in {"state", "session_state", "expires_at"}}, "orchestrator_lease_unknown"),  # no identity
        (_lease(holder=None), "orchestrator_lease_unknown"),
        (_lease(holder={"agent": "claude"}), "orchestrator_lease_unknown"),
        (_lease(generation=True), "orchestrator_lease_unknown"),
        # A released lease decides "free" only with a well-formed identity, fence and holder.
        (_lease(state="released", session_state="closed", session_id=""), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", lease_id=""), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", fencing_token=0), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", generation=-1), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", holder={**_lease()["holder"], "process_id": "4321"}),
         "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", holder={**_lease()["holder"], "process_id": 0}),
         "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", holder={**_lease()["holder"], "holder_kind": "robot"}),
         "orchestrator_lease_unknown"),
        (_lease(holder={**_lease()["holder"], "agent": ""}), "orchestrator_lease_unknown"),
        # An app thread's holder carries a canonical UUID task id and no pid, as Monitor itself requires.
        (_lease(state="released", session_state="closed", holder={**_lease()["holder"], "holder_kind": "app_thread",
                                                                  "process_id": None, "task_id": None}),
         "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", holder={**_lease()["holder"], "holder_kind": "app_thread",
                                                                  "process_id": None, "task_id": "turn-1"}),
         "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", holder={**_lease()["holder"], "holder_kind": "app_thread",
                                                                  "process_id": None,
                                                                  "task_id": "0b9a3a9e-6a8e-4f53-9d0c-7d6f1e2a4b5c"}),
         None),
        (_lease(state="released", session_state="closed", holder={**_lease()["holder"], "process_id": True}),
         "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", ttl_seconds=True), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", version=True), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", age_seconds=True), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", heartbeat_at="soon"), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed",
                heartbeat_at=(datetime.now(UTC) + timedelta(seconds=7200)).isoformat().replace("+00:00", "Z")),
         "orchestrator_lease_unknown"),  # heartbeat after expiry
        (_lease(state="released", session_state="closed",
                heartbeat_at=(datetime.now(UTC) - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")),
         "orchestrator_lease_unknown"),  # expiry - heartbeat != ttl_seconds
        (_lease(state="released", session_state="closed", session_id="bad.example"), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", session_id="[redacted]"), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", lease_id="bad.example"), "orchestrator_lease_unknown"),
        (_lease(state="released", session_state="closed", holder={**_lease()["holder"], "host_id": "bad.host"}),
         "orchestrator_lease_unknown"),
        ({**_lease(), "expires_at": "soon"}, "orchestrator_lease_unknown"),
        ("lease", "orchestrator_lease_unknown"),
        (_MISSING, "orchestrator_lease_unknown"),  # no lease field is not the projection's explicit null
        ({key: value for key, value in _lease().items() if key != "session_state"}, "orchestrator_lease_unknown"),
        (_lease(state="held"), "orchestrator_lease_unknown"),
        ({**_lease(state="released", session_state="closed"), "expires_at": None}, "orchestrator_lease_unknown"),
    ],
)
def test_a_dry_run_reads_any_active_lease_as_busy(monkeypatch, lease, refused):
    """Defect class T1: a dry run reports a stream free that a turn's claim would be refused on (an expired but
    uncleared lease), or reads an unreadable lease as none."""
    session = lease.get("session_state") if isinstance(lease, dict) else None
    monkeypatch.setattr(remote.RemoteEpicClient, "stream", lambda self, stream_id, digest_limit=20: {
        "schema": "remote-epic-lifecycle.v1", "stream_id": stream_id, "session_state": session,
        **({} if lease is _MISSING else {"lease": lease})})
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


def _refused_launch(task_store, monkeypatch, tmp_path, error, *, record_change=None):
    """Run reserve() and worker.start as dispatch does, with the scope layer raising ``error``; the releases made."""
    from lu_machinery.dispatcher import admission
    from lu_machinery.dispatcher.types import TaskSpec
    from lu_machinery.orchestration import dispatch_isolation

    envelope = {"monitor": "http://127.0.0.1:8765", "lease": {"stream_id": STREAM, "generation": 3}}
    releases = []
    monkeypatch.setattr(host_admission, "claim_turn", lambda *_a, **_k: envelope)
    monkeypatch.setattr(host_admission, "release_turn", lambda lease, **kwargs: releases.append(lease))

    def spawn(*_args, **_kwargs):
        raise error

    real_cleanup = worker._guard_launch_cleanup

    def cleanup(path, spec, record, exc):  # what another writer leaves in the record before its read-back
        real_cleanup(path, spec, record, exc)
        if record_change:
            path.write_text(json.dumps({**json.loads(path.read_text()), **record_change}))

    monkeypatch.setattr(dispatch_isolation, "spawn_detached_worker", spawn)
    monkeypatch.setattr(worker, "_guard_launch_cleanup", cleanup)
    decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    launch = {"record": False, "started": False}
    with pytest.raises(admission.LaunchRefused), host_admission.reserve(
            "turn-1", "workspace-write", decision, agent="codex", disk_guard=lambda: False,
            orchestrator={"stream": STREAM}, run_nonce="c" * 32, turn_ttl_s=3600, launch=launch) as hold:
        path = store.record_path("turn-1")
        spec = TaskSpec("turn-1", "codex", "m", None, "workspace-write", tmp_path, path.with_suffix(".prompt"),
                        path.with_suffix(".result"))
        record = {**hold, "status": "spawning", "pid": None, "prompt_file": str(spec.prompt_file)}
        worker.start(spec, record, b"prompt", 60, progress=launch, scope_limits=(660, 30, 4e9))
    return envelope, releases


@pytest.mark.parametrize(("after_popen", "record_change", "released"), [
    (False, None, True),  # C-6991: the probe's fallback refusal or the start deadline, before any Popen
    (True, None, False),  # systemd-run was started (even if it exited): a scope may hold it
    (False, {"run_nonce": "f" * 32}, False),  # the record read back is another run's
    (False, {"turn_lease": {"monitor": "http://127.0.0.1:9", "lease": {}}}, False),  # it holds another lease
    (False, {"pid": 4242}, False),  # it names a process
    (False, {"task_id": "turn-2"}, False),  # it is another task's
    (False, {"orchestrator": None}, False),  # it is no longer a turn on this stream
    (False, {"orchestrator": {"stream": "epic:8"}}, False),
    (False, {"status": "running"}, False),
    (False, {"returncode_reason": ["dispatch_isolation_failed"]}, False),  # malformed: never raises, never proof
    (False, {"returncode_reason": "worker_spawn_failed"}, False),  # not a placement refusal
    (False, {"turn_lease": {"monitor": "http://127.0.0.1:8765", "lease": {"stream_id": STREAM, "generation": 3.0}}},
     False),  # equal, but not the same JSON value
])
def test_a_launch_refused_before_any_process_releases_only_its_own_lease(task_store, monkeypatch, tmp_path,
                                                                          after_popen, record_change, released):
    """Defect class T4: a launch the scope layer refused before any of its processes started keeps a lease no turn
    holds, or a refusal after systemd-run started, or a record that is not this run's unstarted failure, releases a
    lease a running scope may hold."""
    from lu_machinery.orchestration import dispatch_isolation

    error = (dispatch_isolation.DispatchIsolationError if after_popen else dispatch_isolation.LaunchNotStarted)(
        "refused")
    envelope, releases = _refused_launch(task_store, monkeypatch, tmp_path, error, record_change=record_change)
    assert releases == ([envelope] if released else [])
    if record_change is None:  # this run's own record: the failed launch is recorded
        state = store.read("turn-1")
        assert (state["status"], state["pid"], state["returncode_reason"]) == ("failed", None, "dispatch_isolation_failed")


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
        ({"turn_lease": {"monitor": "http://127.0.0.1:9", "lease": {"stream_id": STREAM}}}, False),  # rewritten
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
    contacted = []
    with monkeypatch.context() as patch:
        patch.setattr(remote.RemoteEpicClient, "release", lambda self, *a, **k: contacted.append(self.base))
        for wrong in (dict(exact, task_id="turn-2"), dict(exact, run_nonce="f" * 32), dict(exact, stream="epic:8")):
            assert host_admission.release_turn(envelope, **wrong).startswith("orchestrator_lease_release_refused")
        for monitor_spelling in (monitor.base.replace("127.0.0.1", "localhost"), "", "http://example.invalid:80", 7):
            reason = host_admission.release_turn({**envelope, "monitor": monitor_spelling}, **exact)
            assert reason.startswith("orchestrator_lease_release_refused"), (monitor_spelling, reason)
        base_ack = {**envelope["lease"], "state": "released", "session_state": "closed",
                    "heartbeat_at": "1970-01-01T00:00:00Z", "expires_at": "1970-01-01T00:00:00Z",
                    "version": 1, "age_seconds": 0}
        valid_ack = {"schema": "remote-epic-lifecycle.v1", "outcome": "released", "lease": base_ack}
        patch.setattr(remote.RemoteEpicClient, "release", lambda self, *a, **k: valid_ack)
        assert host_admission.release_turn(envelope, **exact) is None
        for answer in ({"outcome": "released", "lease": base_ack},
                       {**valid_ack, "schema": "remote-epic-lifecycle.v2"},
                       {**valid_ack, "schema": True},
                       {"schema": "remote-epic-lifecycle.v1", "outcome": "released"},
                       {**valid_ack, "lease": {**base_ack, "session_state": "open"}},
                       {**valid_ack, "lease": {**base_ack, "state": "active"}},
                       {**valid_ack, "lease": {**base_ack, "ttl_seconds": True}},
                       {**valid_ack, "lease": {**base_ack, "expires_at": "soon"}},
                       {**valid_ack, "lease": {**base_ack, "heartbeat_at": "2099-01-01T00:00:00Z"}},
                       {**valid_ack, "lease": {**base_ack, "version": True}},
                       {**valid_ack, "session_state": "open"}):
            patch.setattr(remote.RemoteEpicClient, "release", lambda self, *a, answer=answer, **k: answer)
            assert host_admission.release_turn(envelope, **exact).startswith("orchestrator_lease_release_unknown")
    assert contacted == []
    # A 409 whose detail is not Monitor's recognized LEASE LOST is unknown, never fenced (exercising _request).
    import urllib.error
    import urllib.request

    for mode in ("status", "http_error"):
        reached = []

        def unrelated_409(request, timeout=0, mode=mode, reached=reached):
            reached.append(request.full_url)
            if mode == "http_error":
                raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {},
                                             io.BytesIO(b'{"detail":"request refused"}'))
            return contextlib.nullcontext(SimpleNamespace(status=409, read=lambda: b'{"detail":"request refused"}'))

        with monkeypatch.context() as patch:
            patch.setattr(urllib.request, "urlopen", unrelated_409)
            reason = host_admission.release_turn(envelope, **exact)
            assert reason.startswith("orchestrator_lease_release_unknown: (RemoteSupervisorError)"), (mode, reason)
        assert len(reached) == 1, mode
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
    store.replace({**hold, "status": "crashed"})
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
    for limits in ((7800, 30, 1234.5), None):
        with pytest.raises(admission.LaunchRefused) as refused:
            admission.spawn_supervisor(["true"], task_id="turn-1", run_nonce="n", cwd=tmp_path, stderr=None,
                                       stderr_log=tmp_path / "log", env={}, scope_limits=limits)
        assert refused.value.never_started is False
    bounded, ordinary = seen
    assert (bounded["allow_fallback"], bounded["runtime_max_s"], bounded["stop_timeout_s"], bounded["start_by"]) == (
        False, 7800, 30, 1234.5)
    assert not {"allow_fallback", "runtime_max_s", "stop_timeout_s", "start_by"} & set(ordinary)  # others unchanged


# --- writers ------------------------------------------------------------------------------------------------------


def test_no_task_borrows_a_turn_tree(task_store, tmp_path, monkeypatch):
    """Defect class T6: a --cwd task attaches to an orchestrator turn's tree because the turn's record is missing,
    archived, damaged or lost its orchestrator key, so the turn's tree changes after it was judged."""
    trees = tmp_path / "trees"
    assert host_admission.turn_tree_refusal(trees / "turn-1").startswith("orchestrator_turn_writer_refused")  # no record
    for alias in (trees / "turn-1" / "sub" / "..", trees / "turn-1" / "."):  # any spelling of the turn's tree
        assert host_admission.turn_tree_refusal(alias).startswith("orchestrator_turn_writer_refused"), alias
    _write(_orchestrator_record("turn-1"))
    assert host_admission.turn_tree_refusal(trees / "turn-1").startswith("orchestrator_turn_writer_refused")
    _write(_orchestrator_record("legacy-turn"))  # a turn from before the reserved prefix
    assert f"orchestrator turn on {STREAM}" in host_admission.turn_tree_refusal(trees / "legacy-turn")
    store.record_path("damaged").write_text("{")
    assert "cannot be read" in host_admission.turn_tree_refusal(trees / "damaged")
    archive = task_store / "archive"
    archive.mkdir()
    store._write(archive / "archived-turn.json", _orchestrator_record("archived-turn"))
    assert f"orchestrator turn on {STREAM}" in host_admission.turn_tree_refusal(trees / "archived-turn")
    real_lstat = os.lstat

    def denied(path, *args, **kwargs):  # what an unreadable store directory answers (3.14 is_file() hides it)
        if os.fspath(path).endswith("hidden-turn.json"):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", denied)
        assert "cannot be read" in host_admission.turn_tree_refusal(trees / "hidden-turn")
    ordinary = _orchestrator_record("masked")
    ordinary.pop("orchestrator")
    store._write(archive / "masked.json", ordinary)
    store.record_path("masked").mkdir()  # a hot entry that is no record: the archived one never stands in
    assert "cannot be read" in host_admission.turn_tree_refusal(trees / "masked")
    store.record_path("mismatch").write_text(json.dumps({**ordinary, "task_id": "other"}))  # names another task
    assert "cannot be read" in host_admission.turn_tree_refusal(trees / "mismatch")
    store.record_path("invalid-schema").write_text(json.dumps({"task_id": "invalid-schema", "schema_version": True,
                                                               "status": "running"}))  # damaged record fails closed
    assert "cannot be read" in host_admission.turn_tree_refusal(trees / "invalid-schema")
    # A record observed in the hot store that is gone when read never lets an older archived copy decide.
    store._write(store.record_path("moving-turn"), _orchestrator_record("moving-turn"))
    stale = _orchestrator_record("moving-turn")
    stale.pop("orchestrator")
    store._write(archive / "moving-turn.json", stale)
    real_read_bytes = Path.read_bytes

    def vanishing(path, *args, **kwargs):
        if path == store.record_path("moving-turn"):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_read_bytes(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", vanishing)
        assert "cannot be read" in host_admission.turn_tree_refusal(trees / "moving-turn")
    # Archive and restore move records under the archive lock, which the lookup holds across both names.
    from lu_machinery.orchestration import task_record_store

    taken = []
    real_lock = task_record_store.task_archive_lock

    def lock(tasks_dir, **kwargs):
        taken.append((tasks_dir, kwargs))
        return real_lock(tasks_dir, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(task_record_store, "task_archive_lock", lock)
        host_admission.turn_tree_refusal(trees / "worker-0")
    assert taken == [(task_store, {})]
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


def test_the_receipt_is_written_once_by_the_turn_and_survives_stale_writers(task_store, tmp_path, monkeypatch):
    """Defect class: a second, foreign or late receipt is accepted, or a stale record copy erases it."""
    cli = _fake_driver_state_cli(tmp_path, {"note-1": _fresh_note("note-1"), "note-2": _fresh_note("note-2")})
    monkeypatch.setenv(store.DRIVER_STATE_CLI_ENV, str(cli))
    record = _orchestrator_record("turn")
    _write(record)
    args = ("turn", record["run_nonce"], STREAM, "note-1")
    with pytest.raises(ValueError, match="only the orchestrator turn itself"):
        store.record_handoff_receipt(*args)
    monkeypatch.setenv(TASK_ENV, "turn")
    with pytest.raises(ValueError, match="not an orchestrator turn on this stream"):
        store.record_handoff_receipt("turn", record["run_nonce"], OTHER_STREAM, "note-1")
    with pytest.raises(ValueError, match="run nonce mismatch"):
        store.record_handoff_receipt("turn", "other-nonce", STREAM, "note-1")
    with pytest.raises(ValueError, match="--handoff-ref"):
        store.record_handoff_receipt("turn", record["run_nonce"], STREAM, "note 1")
    receipt = store.record_handoff_receipt(*args)
    assert receipt["receipt_id"] == store.handoff_receipt_id(STREAM, "turn", record["run_nonce"], "note-1", "done")
    assert receipt["outcome"] == "done"
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
                                              store.handoff_receipt_id(STREAM, "partial", partial["run_nonce"], "note-1", "done"))
    _write(partial)
    monkeypatch.setenv(TASK_ENV, "partial")
    with pytest.raises(ValueError, match="handoff_receipt_conflict"):
        store.record_handoff_receipt("partial", partial["run_nonce"], STREAM, "note-1")


def _settled(**fields):
    record = _orchestrator_record("turn", status="done", error=None, commits_ahead=0, worktree_dirty_on_exit=False,
                                  head_sha="5" * 40, worktree_start_sha="5" * 40)
    outcome = fields.get("receipt.outcome", "done")
    record[store.HANDOFF_RECEIPT_KEY] = dict(
        schema=store.HANDOFF_RECEIPT_SCHEMA,
        receipt_id=store.handoff_receipt_id(STREAM, "turn", record["run_nonce"], "n1",
                                            outcome if isinstance(outcome, str) else "done"),
        stream=STREAM, task_id="turn", run_nonce=record["run_nonce"], handoff_ref="n1", outcome="done",
        recorded_at=store.now(),
    )
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
        ({"orchestrator": {"stream": OTHER_STREAM}}, "no_deliverable", "orchestrator_handoff_receipt_invalid"),
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
def test_a_turn_settles_done_only_on_its_receipt_with_an_untouched_tree(task_store, tmp_path, monkeypatch, change, status, reason):
    """Defect class: a turn settles done without its handoff, with product writes, or through a merged PR."""
    cli = _fake_driver_state_cli(tmp_path, {"n1": _fresh_note("n1")})
    monkeypatch.setenv(store.DRIVER_STATE_CLI_ENV, str(cli))
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
def turn_box(sandbox, monitor, tmp_path):
    for name in (TASK_ENV, "LU_WRITE_BOUNDARY_BROKER", "SESSION_SEAT", "SESSION_EPIC"):
        sandbox.env.pop(name, None)
    sandbox.env["LU_MONITOR_LOOPBACK"] = monitor.base
    cli = _fake_driver_state_cli(tmp_path, {"note-41": _fresh_note("note-41")})
    sandbox.env[store.DRIVER_STATE_CLI_ENV] = str(cli)
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
    assert f"orchestrator_stream_leased: {STREAM}" in box.turn("turn-2", expected=3).stderr
    with pytest.raises(remote.RemoteClaimConflictError, match="turn_lease_uncleared"):
        _legacy_claim(box.monitor.client)
    await_file(box.tasks / "turn-1.started")  # the turn's provider runs, as when it dispatches
    attach = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "borrower", "--prompt", "edit",
                     "--cwd", record["worktree_path"], task_env="turn-1", expected=2)
    assert "orchestrator_turn_writer_refused: a task an orchestrator turn dispatches cannot borrow" in attach.stderr
    orphan = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "orphan", "--prompt", "edit",
                     "--cwd", record["worktree_path"], task_env="turn-9", expected=2)  # turn-9 has no record
    assert "orchestrator_turn_writer_refused: a task an orchestrator turn dispatches cannot borrow" in orphan.stderr
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
    assert settled["child_tasks"] == [{"task_id": "child-1", "run_nonce": child[1], "status": "done"}]
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


# --- DRIVERS-1122 PR 3 lifecycle tests (one CTO-approved test per numbered item) ----------------------------------


def test_live_unknown_or_unreadable_child_blocks_turn_done_keeps_lease_and_records_child_nonces(
    task_store, tmp_path, monkeypatch
):
    """Item 1 defect class: a live, unknown or unreadable child lets an orchestrator turn settle done, releases its
    stream lease while children remain, bypasses _agy_follow_up child waiting, or drops full child nonces."""
    from lu_machinery.dispatcher.types import ParsedResult, TaskSpec

    cli = _fake_driver_state_cli(tmp_path, {"n1": _fresh_note("n1")})
    monkeypatch.setenv(store.DRIVER_STATE_CLI_ENV, str(cli))
    envelope = {"monitor": "http://127.0.0.1:8765", "lease": {"stream_id": STREAM}}
    releases: list[tuple] = []
    monkeypatch.setattr(host_admission, "release_turn", lambda lease, **kwargs: releases.append((lease, kwargs)))
    monkeypatch.setattr(worker, "_provider_alive", lambda _record: False)

    def _run_case(child_setup):
        for p in task_store.glob("*.json"):
            p.unlink()
        releases.clear()
        turn = _settled(**{**_LAUNCHED, "launch_unit": "lu-worker-turn", "turn_lease": envelope})
        _write(turn)
        child_setup(turn)
        worker._enforce_delivery(turn)
        store.replace(turn)
        worker._release_stream(turn, evidence_lost=False)
        return turn, list(releases)

    # 1. Live child: turn settles non-done, records child_tasks + full nonce + orphaned_child_tasks, keeps lease held.
    full_nonce = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    turn, rel = _run_case(
        lambda t: _write(dict(schema_version=2, task_id="child-live", agent="claude", mode="workspace-write",
                              status="running", run_nonce=full_nonce, parent_task_id=t["task_id"],
                              parent_run_nonce=t["run_nonce"], parent_stream=STREAM))
    )
    assert turn["status"] == "no_deliverable"
    assert turn["no_deliverable_reason"].startswith("orchestrator_orphaned_children:")
    assert turn["orphaned_child_tasks"] == ["child-live"]
    assert turn["child_tasks"] == [{"task_id": "child-live", "run_nonce": full_nonce, "status": "running"}]
    assert rel == []

    # 2. Unknown-status child: blocks done and keeps stream held.
    turn, rel = _run_case(
        lambda t: store.record_path("child-unknown").write_text(json.dumps(dict(
            schema_version=2, task_id="child-unknown", status="mystery", run_nonce=full_nonce,
            parent_task_id=t["task_id"], parent_run_nonce=t["run_nonce"],
        )))
    )
    assert turn["status"] == "no_deliverable"
    assert turn["unknown_child_tasks"] == ["child-unknown"]
    assert rel == []

    # 3. Unreadable hot task record: fails closed, blocks done, keeps stream held.
    turn, rel = _run_case(lambda _t: store.record_path("child-corrupt").write_text("{"))
    assert turn["status"] == "no_deliverable"
    assert "child-corrupt.json" in turn["unreadable_child_records"]
    assert rel == []

    # 4. Terminal child still retaining live/unknown processes fails closed (F2); settled terminal child passes.
    turn, rel = _run_case(
        lambda t: _write(dict(schema_version=2, task_id="child-lingering", agent="claude", mode="workspace-write",
                              status="done", run_nonce=full_nonce, parent_task_id=t["task_id"],
                              parent_run_nonce=t["run_nonce"], parent_stream=STREAM,
                              launch_mode="scope", pid=_DEAD_PID, process_start="boot:1",
                              worktree_disposition="retained", worktree_disposition_reason="provider_processes_alive"))
    )
    assert turn["status"] == "no_deliverable" and turn["orphaned_child_tasks"] == ["child-lingering"] and rel == []
    turn, rel = _run_case(
        lambda t: _write(dict(schema_version=2, task_id="child-liveness-unk", agent="claude", mode="workspace-write",
                              status="done", run_nonce=full_nonce, parent_task_id=t["task_id"],
                              parent_run_nonce=t["run_nonce"], parent_stream=STREAM,
                              launch_mode="scope", pid=_DEAD_PID, process_start="boot:1",
                              worktree_disposition="retained", worktree_disposition_reason="provider_liveness_unknown"))
    )
    assert turn["status"] == "no_deliverable" and turn["unknown_child_tasks"] == ["child-liveness-unk"] and rel == []
    # Malformed non-scalar terminal-child metadata classifies as unknown instead of raising TypeError (F2).
    for bad_field in ({"worktree_disposition_reason": ["bad"]}, {"launch_mode": ["bad"]}):
        turn, rel = _run_case(
            lambda t, bad=bad_field: _write(dict(
                schema_version=2, task_id="child-malformed-meta", agent="claude", mode="workspace-write",
                status="done", run_nonce=full_nonce, parent_task_id=t["task_id"],
                parent_run_nonce=t["run_nonce"], parent_stream=STREAM, **bad,
            ))
        )
        assert turn["status"] == "no_deliverable" and turn["unknown_child_tasks"] == ["child-malformed-meta"] and rel == []

    # Verified pre-launch terminal hold (dispatch_died_after_admission, no pid/launch) is cleared (F14).
    turn, rel = _run_case(
        lambda t: _write(dict(schema_version=2, task_id="child-prelaunch-died", agent="claude", mode="workspace-write",
                              status="crashed", pid=None, process_start=None,
                              returncode_reason="dispatch_died_after_admission", run_nonce=full_nonce,
                              parent_task_id=t["task_id"], parent_run_nonce=t["run_nonce"], parent_stream=STREAM))
    )
    assert turn["status"] == "done" and len(rel) == 1

    turn, rel = _run_case(
        lambda t: _write(dict(schema_version=2, task_id="child-done", agent="claude", mode="workspace-write",
                              status="done", run_nonce=full_nonce, parent_task_id=t["task_id"],
                              parent_run_nonce=t["run_nonce"], parent_stream=STREAM,
                              launch_mode="scope", pid=_DEAD_PID, process_start="boot:1",
                              launch_unit="lu-worker-child-done", worktree_disposition="removed"))
    )
    assert turn["status"] == "done"
    assert turn["child_tasks"] == [{"task_id": "child-done", "run_nonce": full_nonce, "status": "done"}]
    assert len(rel) == 1

    # 5. _agy_follow_up waits on running children for orchestrator turns, and settlement maps its child deadline
    # to no_deliverable / orchestrator_orphaned_children while genuine provider failures stay failed (F8).
    _write(dict(schema_version=2, task_id="child-agy-live", agent="claude", mode="workspace-write",
                status="running", run_nonce=full_nonce, parent_task_id=turn["task_id"],
                parent_run_nonce=turn["run_nonce"], parent_stream=STREAM))
    spec = TaskSpec(turn["task_id"], "agy", "gemini-4-neon-high", "high", "workspace-write",
                    tmp_path, tmp_path / "p.md", tmp_path / "r.md")
    conv, parsed = worker._agy_follow_up(spec, turn, ParsedResult("ok", None, None), started=0.0, hard_timeout=0.0,
                                         resumes=0, checkpoint=lambda: None)
    assert conv is None and parsed.incomplete is True
    assert parsed.error == "agy_child_still_running: child-agy-live"
    agy_expired = {**turn, "status": "failed", "error": parsed.error, "provider_terminal_failure": False}
    worker._enforce_delivery(agy_expired)
    assert agy_expired["status"] == "no_deliverable"
    assert agy_expired["no_deliverable_reason"].startswith("orchestrator_orphaned_children:")
    genuine_fail = {**turn, "status": "failed", "error": "provider exited unsuccessfully", "provider_terminal_failure": True}
    worker._enforce_delivery(genuine_fail)
    assert genuine_fail["status"] == "failed"
    # Terminal AGY provider failure with an active child is never overwritten by _agy_follow_up (F8).
    conv_err, parsed_err = worker._agy_follow_up(
        spec, turn, ParsedResult(None, None, "agy: provider error"), started=0.0, hard_timeout=0.0,
        resumes=0, checkpoint=lambda: None, exit_code=1,
    )
    assert conv_err is None and parsed_err.error == "agy: provider error" and parsed_err.incomplete is False
    conv_err0, parsed_err0 = worker._agy_follow_up(
        spec, turn, ParsedResult(None, None, "agy: missing final text"), started=0.0, hard_timeout=0.0,
        resumes=0, checkpoint=lambda: None, exit_code=0,
    )
    assert conv_err0 is None and parsed_err0.error == "agy: missing final text" and parsed_err0.incomplete is False

    # F15: AGY incomplete=True alongside nonzero exit (e.g. background-work diagnostic + exit 9) still waits
    # for active children and returns the resumable conversation when time remains.
    from lu_machinery.dispatcher.providers import agy as agy_provider

    agy_conv_id = "11111111-2222-3333-4444-555555555555"
    agy_stdout = (
        '{"event":"init","init":{"model":"gemini-4-neon-high"}}\n'
        '{"event":"result","result":{"status":"SUCCESS","response":"Working."}}\n'
    )
    parsed_f15 = agy_provider.parse(agy_stdout, "terminating 1 background task(s)\n", 9, None)
    assert parsed_f15.incomplete is True and parsed_f15.error == "agy: exited with status 9"
    spec.final_file.with_suffix(".log").write_text(f"Created conversation {agy_conv_id}\n", encoding="utf-8")
    waited_children: list[str] = []

    def _finish_child_during_wait(rec, _deadline, _checkpoint):
        waited_children.extend(worker._running_children(rec))
        return []

    monkeypatch.setattr(worker, "_children_at", _finish_child_during_wait)
    conv_f15, out_f15 = worker._agy_follow_up(
        spec, turn, parsed_f15, started=time.monotonic(), hard_timeout=60.0,
        resumes=0, checkpoint=lambda: None, exit_code=9,
    )
    assert waited_children == ["child-agy-live"]
    assert conv_f15 == agy_conv_id and out_f15.incomplete is True

    # Non-regular FIFO symlink in the hot task store fails closed in _scan_turn_children without blocking (F16 class).
    hot_fifo = tmp_path / "hot.fifo"
    os.mkfifo(hot_fifo)
    hot_fifo_link = task_store / "child-fifo.json"
    hot_fifo_link.symlink_to(hot_fifo)
    scan_fifo = worker._scan_turn_children(turn)
    assert "child-fifo.json" in scan_fifo["unreadable"]
    hot_fifo_link.unlink()

    # PR1304-F3: archived and hot runs sharing a task_id with distinct run_nonces are both recorded in child_tasks,
    # while an identical-nonce archive copy deduplicates cleanly.
    reuse_turn = _orchestrator_record("turn-reuse-child", run_nonce="parent-nonce-reuse")
    _write(reuse_turn)
    _write(dict(schema_version=2, task_id="child-reused", agent="claude", mode="workspace-write",
                status="done", run_nonce="nonce-hot-2", parent_task_id="turn-reuse-child",
                parent_run_nonce="parent-nonce-reuse", parent_stream=STREAM,
                launch_mode="scope", pid=_DEAD_PID, process_start="boot:1", worktree_disposition="removed"))
    arch_dir = task_store / "archive"
    arch_dir.mkdir(exist_ok=True)
    (arch_dir / "child-reused.20261123T000000Z.archived.json").write_text(json.dumps(dict(
        schema_version=2, task_id="child-reused", agent="claude", mode="workspace-write",
        status="done", run_nonce="nonce-arch-1", parent_task_id="turn-reuse-child",
        parent_run_nonce="parent-nonce-reuse", parent_stream=STREAM,
        launch_mode="scope", pid=_DEAD_PID, process_start="boot:1", worktree_disposition="removed",
    )), encoding="utf-8")
    (arch_dir / "child-reused.20261123T000001Z.archived.json").write_text(json.dumps(dict(
        schema_version=2, task_id="child-reused", agent="claude", mode="workspace-write",
        status="done", run_nonce="nonce-hot-2", parent_task_id="turn-reuse-child",
        parent_run_nonce="parent-nonce-reuse", parent_stream=STREAM,
        launch_mode="scope", pid=_DEAD_PID, process_start="boot:1", worktree_disposition="removed",
    )), encoding="utf-8")
    reuse_scan = worker._scan_turn_children(reuse_turn)
    assert [(c["task_id"], c["run_nonce"]) for c in reuse_scan["children"]] == [
        ("child-reused", "nonce-arch-1"),
        ("child-reused", "nonce-hot-2"),
    ]
    assert reuse_scan["live"] == [] and reuse_scan["unknown"] == [] and reuse_scan["unreadable"] == []
    # PR1306-F2 R2: a settled hot record never masks a conflicting/malformed archived copy of the same run.
    bad_dup_arch = arch_dir / "child-reused.20261123T000002Z.archived.json"
    bad_dup_arch.write_text(json.dumps(dict(
        schema_version=2, task_id="child-reused", agent="claude", mode="workspace-write",
        status="done", run_nonce="nonce-hot-2", parent_task_id="turn-reuse-child",
        parent_run_nonce="  ", parent_stream=STREAM,
        launch_mode="scope", pid=_DEAD_PID, process_start="boot:1", worktree_disposition="removed",
    )), encoding="utf-8")
    bad_dup_scan = worker._scan_turn_children(reuse_turn)
    assert "child-reused" in bad_dup_scan["unknown"]
    bad_dup_arch.unlink()
    bad_refusal_arch = arch_dir / "child-reused.20261123T000003Z.archived.json"
    bad_refusal_arch.write_text(json.dumps(dict(
        schema_version=2, task_id="child-reused", agent="claude", mode="workspace-write",
        status="failed", run_nonce="nonce-hot-2", never_started_reason="  ",
        parent_task_id="turn-reuse-child", parent_run_nonce="parent-nonce-reuse", parent_stream=STREAM,
    )), encoding="utf-8")
    bad_refusal_scan = worker._scan_turn_children(reuse_turn)
    assert "child-reused" in bad_refusal_scan["unknown"]
    bad_refusal_arch.unlink()
    # PR1306-F1: corrupt/unreadable archived record and unreadable archive dir fail closed in _scan_turn_children.
    corrupt_arch = arch_dir / "corrupt-archived.json"
    corrupt_arch.write_text("{", encoding="utf-8")
    corrupt_scan = worker._scan_turn_children(reuse_turn)
    assert "archive/corrupt-archived.json" in corrupt_scan["unreadable"]
    corrupt_arch.unlink()
    misnamed_arch = arch_dir / "renamed-archived.json"
    misnamed_arch.write_text(json.dumps(dict(
        schema_version=2, task_id="other-id", agent="claude", mode="workspace-write",
        status="done", run_nonce="nonce-other",
    )), encoding="utf-8")
    misnamed_scan = worker._scan_turn_children(reuse_turn)
    assert "archive/renamed-archived.json" in misnamed_scan["unreadable"]
    misnamed_arch.unlink()
    for p in arch_dir.glob("child-reused.*.json"):
        p.unlink()
    arch_dir.rmdir()
    arch_dir.symlink_to(tmp_path / "missing-archive-scan")
    dangling_arch_scan = worker._scan_turn_children(reuse_turn)
    assert "archive" in dangling_arch_scan["unreadable"]
    arch_dir.unlink()

    # PR1306-F2: whitespace in parent_run_nonce, run_nonce, or never_started_reason never hides a child.
    ws_turn = _orchestrator_record("turn-ws-child", run_nonce="parent-nonce-ws")
    _write(ws_turn)
    _write(dict(schema_version=2, task_id="child-ws-parent", agent="claude", mode="workspace-write",
                status="running", run_nonce="cn-ws-1", parent_task_id="turn-ws-child",
                parent_run_nonce="  ", parent_stream=STREAM))
    _write(dict(schema_version=2, task_id="child-ws-padded", agent="claude", mode="workspace-write",
                status="done", run_nonce="cn-ws-2", parent_task_id="turn-ws-child",
                parent_run_nonce=" parent-nonce-ws ", parent_stream=STREAM,
                launch_mode="scope", pid=_DEAD_PID, process_start="boot:1", worktree_disposition="removed"))
    _write(dict(schema_version=2, task_id="child-ws-nonce", agent="claude", mode="workspace-write",
                status="done", run_nonce="   ", parent_task_id="turn-ws-child",
                parent_run_nonce="parent-nonce-ws", parent_stream=STREAM,
                launch_mode="scope", pid=_DEAD_PID, process_start="boot:1", worktree_disposition="removed"))
    _write(dict(schema_version=2, task_id="child-ws-refusal", agent="claude", mode="workspace-write",
                status="failed", run_nonce="cn-ws-4", never_started_reason="   ",
                parent_task_id="turn-ws-child", parent_run_nonce="parent-nonce-ws", parent_stream=STREAM))
    ws_scan = worker._scan_turn_children(ws_turn)
    assert "child-ws-parent" in ws_scan["live"]
    assert {"child-ws-padded", "child-ws-nonce", "child-ws-refusal"}.issubset(set(ws_scan["unknown"]))


def test_handoff_receipt_verifies_driver_state_note_and_non_done_outcomes(task_store, tmp_path, monkeypatch):
    """Item 2 defect class: syntax-only handoff receipt certifies missing, wrong-kind, wrong-lane, dropped or reused
    notes, or settles blocked/handoff runs as done."""
    past = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
    notes = {
        "note-wrong-kind": _fresh_note("note-wrong-kind", kind="task"),
        "note-wrong-lane": _fresh_note("note-wrong-lane", lane="atlas"),
        "note-dropped": _fresh_note("note-dropped", status="dropped"),
        "note-completed": _fresh_note("note-completed", status="done"),
        "note-bad-alias": _fresh_note("note-bad-alias", alias=["not-a-string"]),
        "note-old": _fresh_note("note-old", created_utc=past),
        "note-future": _fresh_note("note-future", created_utc=future),
        "note-done": _fresh_note("note-done"),
        "note-blocked": _fresh_note("note-blocked"),
        "note-handoff": _fresh_note("note-handoff"),
    }
    cli = _fake_driver_state_cli(tmp_path, notes)
    monkeypatch.setenv(store.DRIVER_STATE_CLI_ENV, str(cli))

    turn = _orchestrator_record("turn-verify")
    _write(turn)
    monkeypatch.setenv(TASK_ENV, "turn-verify")
    for bad_ref, expected_code in (
        ("note-missing", "orchestrator_handoff_note_missing"),
        ("note-wrong-kind", "orchestrator_handoff_note_wrong_kind"),
        ("note-wrong-lane", "orchestrator_handoff_note_wrong_lane"),
        ("note-dropped", "orchestrator_handoff_note_invalid"),
        ("note-completed", "orchestrator_handoff_note_invalid"),
        ("note-bad-alias", "orchestrator_handoff_note_invalid"),
        ("note-old", "orchestrator_handoff_note_reused"),
        ("note-future", "orchestrator_handoff_note_future"),
    ):
        with pytest.raises(ValueError, match=expected_code):
            store.record_handoff_receipt("turn-verify", turn["run_nonce"], STREAM, bad_ref, outcome="done")

    # Unreadable archived history (corrupt record, dangling archive symlink, dangling record symlink,
    # zero-filled record, or FIFO symlink target) fails closed in both handoff verification and
    # strict_task_inventory without blocking (F3, F11, F16). A non-object JSON file makes no record
    # claim: the record parser calls it foreign and the strict inventory skips it, as list_tasks does.
    archive_dir = task_store / "archive"
    archive_dir.mkdir(exist_ok=True)
    bad_archive = archive_dir / "corrupt-prior.json"
    bad_archive.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="orchestrator_handoff_note_unverified"):
        store.record_handoff_receipt("turn-verify", turn["run_nonce"], STREAM, "note-done", outcome="done")
    bad_archive.unlink()
    dangling_rec = archive_dir / "dangling-child.json"
    dangling_rec.symlink_to(tmp_path / "missing-record.json")
    with pytest.raises(ValueError, match=r"unreadable task record dangling-child\.json"):
        store.strict_task_inventory(include_archive=True)
    with pytest.raises(ValueError, match="orchestrator_handoff_note_unverified"):
        store.record_handoff_receipt("turn-verify", turn["run_nonce"], STREAM, "note-done", outcome="done")
    dangling_rec.unlink()
    array_rec = archive_dir / "array-child.json"
    array_rec.write_text("[]", encoding="utf-8")
    assert [r["task_id"] for r in store.strict_task_inventory(include_archive=True)] == ["turn-verify"]
    array_rec.unlink()
    zeroed_rec = archive_dir / "zeroed-child.json"
    zeroed_rec.write_bytes(b"\x00" * 64)
    with pytest.raises(ValueError, match=r"unreadable task record zeroed-child\.json"):
        store.strict_task_inventory(include_archive=True)
    with pytest.raises(ValueError, match="orchestrator_handoff_note_unverified"):
        store.record_handoff_receipt("turn-verify", turn["run_nonce"], STREAM, "note-done", outcome="done")
    zeroed_rec.unlink()
    fifo_target = tmp_path / "handoff-check.fifo"
    os.mkfifo(fifo_target)
    fifo_link = archive_dir / "fifo-child.json"
    fifo_link.symlink_to(fifo_target)
    with pytest.raises(ValueError, match=r"unreadable task record fifo-child\.json"):
        store.strict_task_inventory(include_archive=True)
    with pytest.raises(ValueError, match="orchestrator_handoff_note_unverified"):
        store.record_handoff_receipt("turn-verify", turn["run_nonce"], STREAM, "note-done", outcome="done")
    fifo_link.unlink()
    archive_dir.rmdir()
    archive_dir.symlink_to(tmp_path / "missing-archive-target")
    with pytest.raises(ValueError, match="orchestrator_handoff_note_unverified"):
        store.record_handoff_receipt("turn-verify", turn["run_nonce"], STREAM, "note-done", outcome="done")
    with pytest.raises(OSError):
        store.strict_task_inventory(include_archive=True)
    archive_dir.unlink()
    assert [r["task_id"] for r in store.strict_task_inventory(include_archive=True)] == ["turn-verify"]

    with pytest.raises(ValueError, match="--outcome"):
        store.record_handoff_receipt("turn-verify", turn["run_nonce"], STREAM, "note-done", outcome="bogus")

    # Record a valid done receipt, then prove a second turn cannot reuse the same handoff_ref.
    done_receipt = store.record_handoff_receipt("turn-verify", turn["run_nonce"], STREAM, "note-done", outcome="done")
    assert done_receipt["outcome"] == "done"
    turn2 = _orchestrator_record("turn-reuse")
    _write(turn2)
    monkeypatch.setenv(TASK_ENV, "turn-reuse")
    with pytest.raises(ValueError, match="orchestrator_handoff_note_reused"):
        store.record_handoff_receipt("turn-reuse", turn2["run_nonce"], STREAM, "note-done", outcome="done")

    # Blocked and handoff outcomes settle non-done with typed reasons; done settles done.
    for ref, outcome, expected_status, expected_prefix in (
        ("note-done", "done", "done", ""),
        ("note-blocked", "blocked", "no_deliverable", "orchestrator_blocked:"),
        ("note-handoff", "handoff", "no_deliverable", "orchestrator_handoff:"),
    ):
        rec = _settled(task_id=f"turn-{outcome}", run_nonce=f"nonce-{outcome}")
        rec[store.HANDOFF_RECEIPT_KEY] = dict(
            schema=store.HANDOFF_RECEIPT_SCHEMA,
            receipt_id=store.handoff_receipt_id(STREAM, rec["task_id"], rec["run_nonce"], ref, outcome),
            stream=STREAM, task_id=rec["task_id"], run_nonce=rec["run_nonce"],
            handoff_ref=ref, outcome=outcome, recorded_at=store.now(),
        )
        # Clear prior turn-verify receipt before testing note-done reuse check.
        if ref == "note-done":
            store.record_path("turn-verify").unlink(missing_ok=True)
        worker._enforce_delivery(rec)
        assert rec["status"] == expected_status
        if expected_prefix:
            assert rec["no_deliverable_reason"].startswith(expected_prefix)
            assert rec["error"].startswith(expected_prefix)


def test_orchestrator_turn_exports_unique_inbox_worker_registered_slot_and_session_epic(
    task_store, tmp_path, monkeypatch
):
    """Item 3 defect class: orchestrator turns omit LU_INBOX_WORKER, SESSION_HANDOFF_AGENT or SESSION_EPIC, reuse
    inbox worker ids across runs, or accept unregistered streams."""
    from lu_machinery.dispatcher import environment
    from lu_machinery.dispatcher.types import TaskSpec

    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    spec_a = TaskSpec(
        "turn-env-1", "claude", "claude-sonnet-5-5", "high", "workspace-write",
        tmp_path, tmp_path / "p", tmp_path / "r",
        options={"orchestrator_stream": STREAM, "orchestrator_run_nonce": "nonce-aaa"},
    )
    spec_b = TaskSpec(
        "turn-env-1", "claude", "claude-sonnet-5-5", "high", "workspace-write",
        tmp_path, tmp_path / "p", tmp_path / "r",
        options={"orchestrator_stream": STREAM, "orchestrator_run_nonce": "nonce-bbb"},
    )
    env_a = environment.child_env(spec_a)
    env_b = environment.child_env(spec_b)
    assert env_a["SESSION_HANDOFF_AGENT"] == "claude-practice"
    assert env_a["SESSION_EPIC"] == "practice"
    assert env_a["LU_INBOX_WORKER"] != env_b["LU_INBOX_WORKER"]
    assert "nonce-aaa" in env_a["LU_INBOX_WORKER"] and "nonce-bbb" in env_b["LU_INBOX_WORKER"]

    # Stream whose launcher alias lane ("core") differs from SESSION_EPIC ("curriculum-upgrade").
    core_stream = f"epic:{_STREAMS['curriculum-upgrade'][0]}"
    spec_core = TaskSpec(
        "turn-core", "agy", "gemini-4-neon-high", "high", "workspace-write",
        tmp_path, tmp_path / "p", tmp_path / "r",
        options={"orchestrator_stream": core_stream, "orchestrator_run_nonce": "nonce-core"},
    )
    env_core = environment.child_env(spec_core)
    assert env_core["SESSION_HANDOFF_AGENT"] == "gemini-core"
    assert env_core["SESSION_EPIC"] == "curriculum-upgrade"

    # Registered overflow epic resolves via its stream area even when omitted from taxonomy epic_to_area (F6).
    overflow_stream = f"epic:{_STREAMS['epics'][-1]}"
    spec_overflow = TaskSpec(
        "turn-overflow", "claude", "claude-sonnet-5-5", "high", "workspace-write",
        tmp_path, tmp_path / "p", tmp_path / "r",
        options={"orchestrator_stream": overflow_stream, "orchestrator_run_nonce": "nonce-overflow"},
    )
    env_overflow = environment.child_env(spec_overflow)
    assert env_overflow["SESSION_HANDOFF_AGENT"] == "claude-epics"
    assert env_overflow["SESSION_EPIC"] == "epics"

    # Monitor stream retains legacy provider aliases alongside dedicated slots (F7).
    monitor_stream = f"epic:{_STREAMS['monitor'][0]}"
    for agent_name, model_name, expected_slot in (
        ("claude", "claude-sonnet-5-5", "claude-monitor"),
        ("codex", "gpt-6.2-sol", "codex-monitor"),
        ("agy", "gemini-4-neon-high", "gemini-monitor"),
    ):
        spec_mon = TaskSpec(
            f"turn-mon-{agent_name}", agent_name, model_name, "high", "workspace-write",
            tmp_path, tmp_path / "p", tmp_path / "r",
            options={"orchestrator_stream": monitor_stream, "orchestrator_run_nonce": f"nonce-mon-{agent_name}"},
        )
        env_mon = environment.child_env(spec_mon)
        assert env_mon["SESSION_HANDOFF_AGENT"] == expected_slot
        assert env_mon["SESSION_EPIC"] == "monitor"

    # Provider without a registered slot on this stream refuses instead of falling back to another seat (F7).
    spec_wrong_slot = TaskSpec(
        "turn-agy-practice", "agy", "gemini-4-neon-high", "high", "workspace-write",
        tmp_path, tmp_path / "p", tmp_path / "r",
        options={"orchestrator_stream": STREAM, "orchestrator_run_nonce": "nonce-wrong-slot"},
    )
    with pytest.raises(ValueError, match="orchestrator_slot_unregistered"):
        environment.child_env(spec_wrong_slot)

    # Ordinary worker exports none of the orchestrator identity vars.
    plain = TaskSpec("worker-1", "claude", "claude-sonnet-5-5", "high", "workspace-write",
                     tmp_path, tmp_path / "p", tmp_path / "r")
    plain_env = environment.child_env(plain)
    for var in ("LU_INBOX_WORKER", "SESSION_HANDOFF_AGENT", "SESSION_EPIC"):
        assert var not in plain_env

    # Unregistered stream fails closed.
    bad = TaskSpec(
        "turn-bad", "claude", "claude-sonnet-5-5", "high", "workspace-write",
        tmp_path, tmp_path / "p", tmp_path / "r",
        options={"orchestrator_stream": "epic:999999", "orchestrator_run_nonce": "nonce-bad"},  # allow-hardcoded-epic: unregistered stream refusal
    )
    with pytest.raises(ValueError, match="orchestrator_stream_unregistered"):
        environment.child_env(bad)


def test_bounded_wait_with_keepalive_survives_silence_timeout(task_store, tmp_path):
    """Item 4 defect class: a turn waiting on a healthy child is killed by stdout_silence_timeout because dispatch
    wait produces no keepalive activity."""
    from lu_machinery.dispatcher import watchdog
    from lu_machinery.dispatcher.types import Invocation

    live_child = dict(
        schema_version=2, task_id="child-slow", agent="claude", mode="workspace-write", status="running",
        run_nonce="nonce-slow", pid=os.getpid(), process_start=worker._identity(os.getpid()),
        started_at=store.now(),
    )
    _write(live_child)
    stdout_path = tmp_path / "wait.stdout.log"
    stderr_path = tmp_path / "wait.stderr.log"
    argv = [
        sys.executable, "-c",
        "from lu_machinery.dispatcher import cli; raise SystemExit(cli.main("
        "['wait', 'child-slow', '--run-nonce', 'nonce-slow', '--timeout', '2.0', "
        "'--poll-interval', '0.1', '--keepalive-interval', '0.25']))",
    ]
    env = {**os.environ, "LU_TASKS_DIR": str(task_store)}
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.Popen(argv, stdout=out, stderr=err, env=env)
        try:
            inv = Invocation(argv=argv, stdin=None, env=env, final_file=None)
            watchdog.communicate(
                proc, inv, stdout_path, stderr_path,
                hard_timeout=15.0, silence_timeout=1.0, initial_response_timeout=1.5,
                liveness=lambda: (), output_sizes=(0, 0),
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    assert proc.returncode == 124
    assert "wait keepalive: child-slow status=running" in stderr_path.read_text(encoding="utf-8")
    payload = json.loads(stdout_path.read_text(encoding="utf-8"))
    assert payload["task_id"] == "child-slow" and payload["status"] == "running"


def test_orchestrator_fleet_cap_and_per_turn_child_cap_refuse_excess_dispatches(task_store, tmp_path, monkeypatch):
    """Item 6 defect class: orchestrator turns bypass the fleet-wide live turn cap (ceiling 2) or dispatch more
    children than the per-turn child cap (ceiling 4), or miscount when the store has unreadable records."""
    # Defaults and frozen ceilings: at most 2 live orchestrators, at most 4 children per turn (PR1299-F1).
    defaults = dispatch_admission.load_thresholds({})
    assert (defaults.max_live_orchestrators, defaults.max_children_per_turn) == (2, 4)
    lower = dispatch_admission.load_thresholds({
        "DISPATCH_MAX_LIVE_ORCHESTRATORS": "1",
        "DISPATCH_MAX_CHILDREN_PER_TURN": "2",
    })
    assert (lower.max_live_orchestrators, lower.max_children_per_turn) == (1, 2)
    with pytest.raises(ValueError, match="DISPATCH_MAX_LIVE_ORCHESTRATORS"):
        dispatch_admission.load_thresholds({"DISPATCH_MAX_LIVE_ORCHESTRATORS": "3"})
    with pytest.raises(ValueError, match="DISPATCH_MAX_CHILDREN_PER_TURN"):
        dispatch_admission.load_thresholds({"DISPATCH_MAX_CHILDREN_PER_TURN": "5"})

    # Fleet-wide live orchestrator cap: 2 live turns block a 3rd even with force_reason, and env cannot raise it.
    live_pid = os.getpid()
    live_ident = worker._identity(live_pid)
    for idx, stream in enumerate((STREAM, OTHER_STREAM), start=1):
        _write(_orchestrator_record(f"turn-cap-{idx}", pid=live_pid, process_start=live_ident,
                                    orchestrator={"stream": stream}))
    refused = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    assert not refused.admitted and refused.orchestrator_cap_refused is True
    assert not host_admission.allows(refused, force_reason="operator override")  # F9: hard cap
    assert any("live orchestrator turns 2/2 reached the cap" in f for f in refused.failures), refused.failures

    # PR1304-F2: truncating one of two live turn records or corrupting its PID (list/bool/None) keeps the
    # live-turn cap closed (outside --force-admission).
    cap2_path = store.record_path("turn-cap-2")
    cap2_saved = cap2_path.read_text(encoding="utf-8")
    cap2_path.write_text("{", encoding="utf-8")
    corrupt_refused = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    assert not corrupt_refused.admitted and corrupt_refused.orchestrator_cap_refused is True
    assert not host_admission.allows(corrupt_refused, force_reason="operator override")
    assert any("orchestrator_cap_unknown:" in f for f in corrupt_refused.failures), corrupt_refused.failures
    for bad_pid in ([123], True, None):
        bad_doc = json.loads(cap2_saved)
        bad_doc["pid"] = bad_pid
        cap2_path.write_text(json.dumps(bad_doc), encoding="utf-8")
        pid_refused = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
        assert not pid_refused.admitted and pid_refused.orchestrator_cap_refused is True
        assert not host_admission.allows(pid_refused, force_reason="operator override")
        assert any("orchestrator_cap_unknown:" in f for f in pid_refused.failures), pid_refused.failures
    cap2_path.write_text(cap2_saved, encoding="utf-8")

    monkeypatch.setenv("DISPATCH_MAX_LIVE_ORCHESTRATORS", "3")
    with pytest.raises(ValueError, match="DISPATCH_MAX_LIVE_ORCHESTRATORS"):
        host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    monkeypatch.delenv("DISPATCH_MAX_LIVE_ORCHESTRATORS")

    # Sidecar-shaped child task IDs are refused before publishing any hold (F1).
    ro_decision = host_admission.evaluate("read-only", sweep=False)
    with (
        pytest.raises(ValueError, match="task_id_refused"),
        host_admission.reserve("child.write-boundary", "read-only", ro_decision, agent="claude",
                               disk_guard=lambda: False),
    ):
        pass

    # Per-turn child cap: 3 non-refused children + 1 refused attempt (never_started_reason) is admitted (3 < 4),
    # while a 4th non-refused run (including an archived run sharing a task_id with a hot run, PR1304-F3)
    # blocks the 5th (even read-only / --force-admission) and env cannot raise past 4.
    parent_nonce = "nonce-turn-cap-1"
    linkage = {"parent_task_id": "turn-cap-1", "parent_run_nonce": parent_nonce, "parent_stream": STREAM}
    for i in range(3):
        _write(dict(schema_version=2, task_id=f"c-{i}", agent="claude", mode="workspace-write",
                    status="done" if i < 2 else "running", run_nonce=f"cn-{i}", **linkage))
    _write(dict(schema_version=2, task_id="c-refused", agent="claude", mode="workspace-write",
                status="failed", never_started_reason="admission_capacity", run_nonce="cn-refused", **linkage))
    host_admission.check_turn_child_cap(linkage)  # 3 < 4 admitted (refused attempt not counted)

    # PR1304-F3: archived run sharing task_id "c-0" with a distinct run_nonce counts as a 4th child run,
    # whereas an archived copy of the same run_nonce ("cn-0") deduplicates cleanly.
    arch_dir = task_store / "archive"
    arch_dir.mkdir(exist_ok=True)
    same_run_copy = arch_dir / "c-0.20261123T000000Z.archived.json"
    same_run_copy.write_text(json.dumps(dict(
        schema_version=2, task_id="c-0", agent="claude", mode="workspace-write",
        status="done", run_nonce="cn-0", **linkage,
    )), encoding="utf-8")
    host_admission.check_turn_child_cap(linkage)  # still 3 < 4 (same run_nonce deduplicated)
    distinct_arch_run = arch_dir / "c-0.20261123T000001Z.archived.json"
    distinct_arch_run.write_text(json.dumps(dict(
        schema_version=2, task_id="c-0", agent="claude", mode="workspace-write",
        status="done", run_nonce="cn-0-prior", **linkage,
    )), encoding="utf-8")
    with pytest.raises(host_admission.Refused, match=r"orchestrator_child_cap_reached:.*4/4"):
        host_admission.check_turn_child_cap(linkage)
    same_run_copy.unlink()
    distinct_arch_run.unlink()

    # Unreadable hot record (dangling symlink or FIFO symlink) fails closed at child admission (PR1299-F2).
    dangling = task_store / "dangling-cap.json"
    dangling.symlink_to(tmp_path / "missing-cap.json")
    with pytest.raises(host_admission.Refused, match=r"orchestrator_child_cap_unknown:.*dangling-cap\.json"):
        host_admission.check_turn_child_cap(linkage)
    dangling.unlink()
    cap_fifo = tmp_path / "cap.fifo"
    os.mkfifo(cap_fifo)
    fifo_link = task_store / "fifo-cap.json"
    fifo_link.symlink_to(cap_fifo)
    with pytest.raises(host_admission.Refused, match=r"orchestrator_child_cap_unknown:.*fifo-cap\.json"):
        host_admission.check_turn_child_cap(linkage)
    fifo_link.unlink()

    # PR1306-F2: whitespace in parent_run_nonce, run_nonce, or never_started_reason fails closed at child admission,
    # even when never_started_reason is validly populated on the same record.
    for bad_id, bad_fields in (
        ("c-ws-parent", {"parent_run_nonce": "  ", "run_nonce": "cn-ok-1"}),
        ("c-ws-nonce", {"parent_run_nonce": parent_nonce, "run_nonce": "  "}),
        ("c-ws-refusal", {"parent_run_nonce": parent_nonce, "run_nonce": "cn-ok-2", "never_started_reason": "  "}),
        ("c-ref-ws-parent", {"parent_run_nonce": "  ", "run_nonce": "cn-ok-3", "never_started_reason": "admission_capacity"}),
        ("c-ref-ws-nonce", {"parent_run_nonce": parent_nonce, "run_nonce": "  ", "never_started_reason": "admission_capacity"}),
    ):
        bad_path = task_store / f"{bad_id}.json"
        _write(dict(schema_version=2, task_id=bad_id, agent="claude", mode="workspace-write",
                    status="running", parent_task_id="turn-cap-1", parent_stream=STREAM, **bad_fields))
        with pytest.raises(host_admission.Refused, match=r"orchestrator_child_cap_unknown:"):
            host_admission.check_turn_child_cap(linkage)
        bad_path.unlink()

    _write(dict(schema_version=2, task_id="c-3", agent="claude", mode="workspace-write",
                status="running", run_nonce="cn-3", **linkage))
    with pytest.raises(host_admission.Refused, match=r"orchestrator_child_cap_reached:.*4/4"):
        host_admission.check_turn_child_cap(linkage)
    ro_decision = host_admission.evaluate("read-only", sweep=False)
    with (
        pytest.raises(host_admission.Refused, match=r"orchestrator_child_cap_reached:.*4/4"),
        host_admission.reserve("c-5", "read-only", ro_decision, agent="claude", disk_guard=lambda: False,
                               force_reason="try override", linkage=linkage),
    ):
        pass
    monkeypatch.setenv("DISPATCH_MAX_CHILDREN_PER_TURN", "5")
    with pytest.raises(host_admission.Refused, match="orchestrator_child_cap_unknown"):
        host_admission.check_turn_child_cap(linkage)
    monkeypatch.delenv("DISPATCH_MAX_CHILDREN_PER_TURN")


@pytest.mark.parametrize(
    "case_name,record_overrides,expected_cap_error",
    [
        ("spawning", {"status": "spawning"}, "orchestrator_child_cap_unknown:"),
        ("active", {"status": "running"}, "orchestrator_child_cap_unknown:"),
        ("invalid_status", {"status": "bogus"}, "orchestrator_child_cap_unknown:"),
        ("terminal_non_refusal", {"status": "done"}, "orchestrator_child_cap_unknown:"),
        ("launch_fields_present", {"status": "failed", "launch_mode": "scope"}, r"orchestrator_child_cap_reached:.*4/4"),
    ],
)
def test_turn_children_count_contradictory_never_started_reason(
    task_store, monkeypatch, case_name, record_overrides, expected_cap_error,
):
    """PR1306-F5 defect class: count_turn_children or _terminal_child_unsettled_state excludes a child
    with never_started_reason despite a non-refusal/invalid status or launch evidence present."""
    parent_nonce = f"nonce-f5-{case_name}"
    linkage = {"parent_task_id": "turn-f5", "parent_run_nonce": parent_nonce, "parent_stream": STREAM}
    for i in range(3):
        _write(dict(schema_version=2, task_id=f"c-ok-{i}", agent="claude", mode="workspace-write",
                    status="done", run_nonce=f"cn-ok-{i}", **linkage))
    child = dict(
        schema_version=2, task_id="c-contra", agent="claude", mode="workspace-write",
        run_nonce="cn-contra", never_started_reason="admission_capacity", **linkage,
        **record_overrides,
    )
    _write(child)

    with pytest.raises(host_admission.Refused, match=expected_cap_error):
        host_admission.check_turn_child_cap(linkage)
    assert worker._terminal_child_unsettled_state(child) == "unknown"

    if case_name == "invalid_status":
        for non_str_status in (None, 123, True):
            bad = {**child, "status": non_str_status}
            _write(bad)
            with pytest.raises(host_admission.Refused, match="orchestrator_child_cap_unknown:"):
                host_admission.check_turn_child_cap(linkage)
            assert worker._terminal_child_unsettled_state(bad) == "unknown"
    elif case_name == "terminal_non_refusal":
        turn = _orchestrator_record("turn-f5", run_nonce=parent_nonce)
        scan = worker._scan_turn_children(turn)
        assert "c-contra" in scan["unknown"]
    elif case_name == "launch_fields_present":
        for field, value in (
            ("pid", _DEAD_PID),
            ("process_start", "boot:1"),
            ("provider_pgid", _DEAD_PID),
            ("launch_unit", "lu-worker-c-contra"),
            ("launch_cgroup", "/user.slice/lu-worker-c-contra.scope"),
        ):
            other = dict(
                schema_version=2, task_id="c-contra", agent="claude", mode="workspace-write",
                status="rate_limited", run_nonce="cn-contra", never_started_reason="headroom",
                **linkage, **{field: value},
            )
            _write(other)
            assert dispatch_admission.count_turn_children(task_store, "turn-f5", parent_nonce) == 4
            with pytest.raises(host_admission.Refused, match=r"orchestrator_child_cap_reached:.*4/4"):
                host_admission.check_turn_child_cap(linkage)
        settled_headroom = dict(
            schema_version=2, task_id="c-contra", agent="claude", mode="workspace-write",
            status="rate_limited", run_nonce="cn-contra", never_started_reason="headroom",
            pid=_DEAD_PID, process_start="boot:1", launch_mode="scope",
            launch_unit="lu-worker-c-contra", worktree_disposition="removed", **linkage,
        )
        with monkeypatch.context() as mp:
            mp.setattr(worker, "_provider_alive", lambda _rec: False)
            assert worker._terminal_child_unsettled_state(settled_headroom) is None
        for refusal_status in ("failed", "rate_limited"):
            valid_refusal = dict(
                schema_version=2, task_id="c-contra", agent="claude", mode="workspace-write",
                status=refusal_status, run_nonce="cn-contra", never_started_reason="admission_capacity",
                **linkage,
            )
            _write(valid_refusal)
            assert dispatch_admission.count_turn_children(task_store, "turn-f5", parent_nonce) == 3
            host_admission.check_turn_child_cap(linkage)
            assert worker._terminal_child_unsettled_state(valid_refusal) is None
        # PR1310-F1: ambiguous LaunchRefused(never_started=False) records launch_mode="scope" so it counts
        # toward the cap and stays unsettled ("unknown"), whereas LaunchRefused(never_started=True) is excluded.
        from lu_machinery.dispatcher import admission
        from lu_machinery.dispatcher.types import TaskSpec

        spec = TaskSpec("c-contra", "claude", "claude-sonnet-5-5", "high", "workspace-write",
                        task_store, task_store / "p", task_store / "r")
        rec_path = store.record_path("c-contra")
        for err_code in ("dispatch_isolation_failed", "dispatch_fallback_refused"):
            spawning = dict(
                schema_version=2, task_id="c-contra", agent="claude", mode="workspace-write",
                status="spawning", pid=None, run_nonce="cn-contra", **linkage,
            )
            _write(spawning)
            worker._record_failed_launch(
                rec_path, spec, spawning,
                admission.LaunchRefused("ambiguous", {"status": "failed", "error": f"{err_code}: ambiguous"}, never_started=False),
            )
            ambig = store.read("c-contra")
            assert ambig["launch_mode"] == "scope"
            assert dispatch_admission.classify_never_started_refusal(ambig) == (False, None)
            assert dispatch_admission.count_turn_children(task_store, "turn-f5", parent_nonce) == 4
            with pytest.raises(host_admission.Refused, match=r"orchestrator_child_cap_reached:.*4/4"):
                host_admission.check_turn_child_cap(linkage)
            assert worker._terminal_child_unsettled_state(ambig) == "unknown"
        # dispatch_isolation_failed without never_started_reason is no longer cleared by _PRE_LAUNCH_TERMINAL_REASONS.
        assert worker._terminal_child_unsettled_state(dict(
            schema_version=2, task_id="c-contra", agent="claude", mode="workspace-write",
            status="failed", pid=None, run_nonce="cn-contra",
            returncode_reason="dispatch_isolation_failed", **linkage,
        )) == "unknown"
        spawning_unstarted = dict(
            schema_version=2, task_id="c-contra", agent="claude", mode="workspace-write",
            status="spawning", pid=None, run_nonce="cn-contra", **linkage,
        )
        _write(spawning_unstarted)
        worker._record_failed_launch(
            rec_path, spec, spawning_unstarted,
            admission.LaunchRefused("not started", {"status": "failed", "error": "dispatch_isolation_failed: nothing started"}, never_started=True),
        )
        unstarted = store.read("c-contra")
        assert unstarted.get("launch_mode") is None
        assert dispatch_admission.classify_never_started_refusal(unstarted) == (True, None)
        assert dispatch_admission.count_turn_children(task_store, "turn-f5", parent_nonce) == 3
        host_admission.check_turn_child_cap(linkage)
        assert worker._terminal_child_unsettled_state(unstarted) is None


def test_unreadable_cgroup_in_fallback_is_never_started(task_store, tmp_path, monkeypatch, driver_scope_detection):
    """PR1310-F2 defect class: pre-launch fallback refusals in _fallback(..., never_started=True) escape as plain
    DispatchIsolationError (or raw UnicodeDecodeError / stderr write error), stamping launch_mode='scope' and
    burning a per-turn child cap slot on a launch that never started."""
    from lu_machinery.dispatcher import admission
    from lu_machinery.dispatcher.types import TaskSpec
    from lu_machinery.orchestration import dispatch_isolation

    parent_nonce = "nonce-pr1310-f2"
    linkage = {"parent_task_id": "turn-f2", "parent_run_nonce": parent_nonce, "parent_stream": STREAM}
    for idx in range(3):
        _write(dict(schema_version=2, task_id=f"c-ok-{idx}", agent="claude", mode="workspace-write",
                    status="done", run_nonce=f"cn-ok-{idx}", **linkage))

    monkeypatch.setattr(
        dispatch_isolation,
        "probe_isolation",
        lambda *_a, **_k: dispatch_isolation.ProbeResult(False, "bus unavailable"),
    )
    original_read_text = Path.read_text

    def _run_child(task_id: str) -> tuple[admission.LaunchRefused, dict, dict]:
        path = store.record_path(task_id)
        spec = TaskSpec(task_id, "claude", "claude-sonnet-5-5", "high", "workspace-write",
                        tmp_path, path.with_suffix(".prompt"), path.with_suffix(".result"))
        record = dict(
            schema_version=2, task_id=task_id, agent="claude", model="claude-sonnet-5-5",
            mode="workspace-write", status="spawning", pid=None, run_nonce=f"cn-{task_id}",
            prompt_file=str(spec.prompt_file), result_file=str(spec.final_file), **linkage,
        )
        progress = {"record": False, "started": False}
        with pytest.raises(admission.LaunchRefused) as raised:
            worker.start(spec, record, b"prompt", 60, progress=progress)
        return raised.value, progress, store.read(task_id)

    # 1-3: unreadable, empty, and non-UTF-8 /proc/self/cgroup through the real detector.
    monkeypatch.setattr(dispatch_isolation, "_caller_in_driver_scope", driver_scope_detection)
    for task_id, outcome, expected_fragment in (
        ("c-cg-perm", PermissionError("unavailable"), "caller-cgroup-unavailable"),
        ("c-cg-empty", "", "caller-cgroup-unverifiable"),
        ("c-cg-utf8", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), "caller-cgroup-unverifiable"),
    ):
        def _fake_read(self: Path, *args, _outcome=outcome, **kwargs):
            if str(self) == "/proc/self/cgroup":
                if isinstance(_outcome, BaseException):
                    raise _outcome
                return _outcome
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _fake_read)
        exc, progress, saved = _run_child(task_id)
        assert exc.never_started is True, (task_id, exc)
        assert progress.get("refused_before_start") is True, (task_id, progress)
        assert saved["status"] == "failed" and saved["pid"] is None
        assert saved["returncode_reason"] == "dispatch_fallback_refused"
        assert saved["never_started_reason"] == "worker_placement"
        assert expected_fragment in saved["error"]
        assert saved.get("launch_mode") is None, saved
        assert dispatch_admission.classify_never_started_refusal(saved) == (True, None)
        assert dispatch_admission.count_turn_children(task_store, "turn-f2", parent_nonce) == 3
        host_admission.check_turn_child_cap(linkage)
        assert worker._terminal_child_unsettled_state(saved) is None
    monkeypatch.setattr(Path, "read_text", original_read_text)

    # 4: replaced _caller_in_driver_scope seam raising plain DispatchIsolationError.
    def _seam_raises() -> bool:
        raise dispatch_isolation.DispatchIsolationError("fallback-refused: custom-seam")

    monkeypatch.setattr(dispatch_isolation, "_caller_in_driver_scope", _seam_raises)
    exc, progress, saved = _run_child("c-cg-seam")
    assert exc.never_started is True and progress.get("refused_before_start") is True
    assert saved["returncode_reason"] == "dispatch_fallback_refused"
    assert "custom-seam" in saved["error"]
    assert saved.get("launch_mode") is None
    assert dispatch_admission.count_turn_children(task_store, "turn-f2", parent_nonce) == 3
    host_admission.check_turn_child_cap(linkage)
    assert worker._terminal_child_unsettled_state(saved) is None

    # 5: post-start _fallback(..., never_started=False) never upgrades to LaunchNotStarted (C-6991).
    for exc_cls in (dispatch_isolation.DispatchIsolationError, dispatch_isolation.LaunchNotStarted):
        def _post_start_seam(_cls=exc_cls) -> bool:
            raise _cls("fallback-refused: custom-seam")

        monkeypatch.setattr(dispatch_isolation, "_caller_in_driver_scope", _post_start_seam)
        with pytest.raises(dispatch_isolation.DispatchIsolationError, match="custom-seam") as post_start:
            dispatch_isolation._fallback(
                [sys.executable, "-c", "pass"],
                popen=lambda *_a, **_k: pytest.fail("popen must not run when fallback is refused"),
                env={}, stdin=None, stdout=None, stderr=None, reason="systemd-run exited 1",
                allow_fallback=True, user_bus=dispatch_isolation.UserBus(env={}), never_started=False,
            )
        assert not isinstance(post_start.value, dispatch_isolation.LaunchNotStarted)

    # 6: _warn failing on non-UTF-8 / broken sys.stderr never aborts a permitted fallback before popen.
    monkeypatch.setattr(dispatch_isolation, "_caller_in_driver_scope", lambda: False)

    class _BrokenStderr:
        def write(self, text: str) -> int:
            if not text.isascii():
                raise UnicodeEncodeError("ascii", text, 0, 1, "ordinal not in range(128)")
            raise BrokenPipeError("closed")

    monkeypatch.setattr(sys, "stderr", _BrokenStderr())
    seen: list[list[str]] = []
    fake_proc = SimpleNamespace(pid=42, stdin=None)
    proc, launch = dispatch_isolation._fallback(
        [sys.executable, "-c", "pass"],
        popen=lambda argv, **_k: (seen.append(list(argv)), fake_proc)[1],
        env={}, stdin=None, stdout=None, stderr=None, reason="bus unavailable",
        allow_fallback=True, user_bus=dispatch_isolation.UserBus(env={}), never_started=True,
    )
    assert proc is fake_proc and launch.mode == dispatch_isolation.LAUNCH_FALLBACK and len(seen) == 1


def test_scope_runtime_max_stop_labels_timeout_not_client_cancel(task_store, tmp_path, monkeypatch):
    """Item 7 defect class: systemd RuntimeMaxSec SIGTERM on a turn scope is mislabelled as status=cancelled
    'cancelled by client' instead of status=timeout."""
    import signal as _signal

    from lu_machinery.orchestration import dispatch_isolation
    from tests.dispatcher import stub_provider

    monkeypatch.setattr(worker.providers, "load", lambda _agent: stub_provider)
    monkeypatch.setattr(worker, "_reap_settled", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_release_stream", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_turn_tree_unproven", lambda *_a, **_k: False)
    real_scope_runtime_max_expired = dispatch_isolation.scope_runtime_max_expired
    (task_store / "logs").mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir()

    def _run_with_sigterm(
        task_id: str,
        runtime_max_expired: bool,
        *,
        scope_runtime_max_s: int | None = 1200,
        mark_client_cancel: bool = False,
    ):
        def trigger_sigterm(*_args, **_kwargs):
            if mark_client_cancel:
                cur = store.read(task_id)
                worker._mark_client_cancel(task_id, cur["run_nonce"])
            _signal.raise_signal(_signal.SIGTERM)

        monkeypatch.setattr(worker.headroom, "check_runtime", trigger_sigterm)
        monkeypatch.setattr(
            dispatch_isolation,
            "scope_runtime_max_expired",
            lambda _unit, *, runtime_max_s: runtime_max_expired and type(runtime_max_s) is int and runtime_max_s > 0,
        )
        spec = worker.TaskSpec(
            task_id, "codex", "echo", None, "read-only", work,
            work / f"{task_id}.prompt", task_store / f"{task_id}.result",
        )
        spec.prompt_file.write_bytes(b"prompt")
        record = store.new_record(spec, {"key": "public", "github": "example/public"}, "main",
                                  "a" * 40, {"research_owned_path": []})
        record["prompt_file"] = str(task_store / f"{task_id}.prompt")
        record.update(launch_mode="scope", launch_unit=f"lu-worker-{task_id}-abcd1234")
        if scope_runtime_max_s is not None:
            record["scope_runtime_max_s"] = scope_runtime_max_s
        path = store.create(record, b"prompt")
        worker.supervise(path, {}, 60)
        return json.loads(path.read_text(encoding="utf-8"))

    timed_out = _run_with_sigterm("turn-rmax-timeout", True)
    assert timed_out["status"] == "timeout"
    assert timed_out["kill_reason"] == "scope_runtime_max_timeout"
    assert timed_out["error"] == "scope RuntimeMaxSec exceeded"

    # Client SIGTERM without RuntimeMaxSec confirmation stays cancelled (PR1299-F3).
    client_cancel = _run_with_sigterm("turn-rmax-cancel", False, scope_runtime_max_s=1)
    assert (client_cancel["status"], client_cancel["error"]) == ("cancelled", "cancelled by client")
    assert client_cancel.get("kill_reason") != "scope_runtime_max_timeout"

    # PR1304-F5: ordinary worker scope without scope_runtime_max_s never labels RuntimeMaxSec, and an explicit
    # per-run client cancel marker preserves status=cancelled even if systemd reports timeout.
    ordinary_scope = _run_with_sigterm("worker-no-rmax", True, scope_runtime_max_s=None)
    assert (ordinary_scope["status"], ordinary_scope["error"]) == ("cancelled", "cancelled by client")
    assert ordinary_scope.get("kill_reason") != "scope_runtime_max_timeout"
    explicit_cancel = _run_with_sigterm("turn-explicit-cancel", True, scope_runtime_max_s=1200, mark_client_cancel=True)
    assert (explicit_cancel["status"], explicit_cancel["error"]) == ("cancelled", "cancelled by client")
    assert explicit_cancel.get("kill_reason") != "scope_runtime_max_timeout"

    # PR1304-F5: real scope_runtime_max_expired parser requires Result=timeout, finite RuntimeMaxUSec,
    # SubState=stop-sigterm, and elapsed >= runtime_max_s (rejecting TimeoutStopSec stop-sigkill and short stops).
    import subprocess as _subprocess

    def _probe_show(stdout: str, *, runtime_max_s: int = 1200) -> bool:
        monkeypatch.setattr(
            dispatch_isolation,
            "_run",
            lambda *_a, **_k: _subprocess.CompletedProcess([], 0, stdout, ""),
        )
        return real_scope_runtime_max_expired("lu-worker-x-abcd1234", runtime_max_s=runtime_max_s)

    assert _probe_show(
        "Result=timeout\nRuntimeMaxUSec=20min\nSubState=stop-sigterm\n"
        "ActiveEnterTimestampMonotonic=1000000000\nStateChangeTimestampMonotonic=2200000000\n"
    ) is True
    assert _probe_show(  # TimeoutStopSec escalation to stop-sigkill
        "Result=timeout\nRuntimeMaxUSec=20min\nSubState=stop-sigkill\n"
        "ActiveEnterTimestampMonotonic=1000000000\nStateChangeTimestampMonotonic=2200000000\n"
    ) is False
    assert _probe_show(  # Stopped before RuntimeMaxSec elapsed
        "Result=timeout\nRuntimeMaxUSec=20min\nSubState=stop-sigterm\n"
        "ActiveEnterTimestampMonotonic=1000000000\nStateChangeTimestampMonotonic=1500000000\n"
    ) is False
    assert _probe_show(  # No RuntimeMaxSec configured in systemd
        "Result=timeout\nRuntimeMaxUSec=infinity\nSubState=stop-sigterm\n"
        "ActiveEnterTimestampMonotonic=1000000000\nStateChangeTimestampMonotonic=2200000000\n"
    ) is False

    # Confirmed scope timeout with an active child preserves exact timeout error while recording child diagnostics (PR1299-F3).
    _write(dict(schema_version=2, task_id="child-during-rmax", agent="claude", mode="workspace-write",
                status="running", run_nonce="nonce-child-rmax", parent_task_id="turn-rmax-child"))
    timed_out_child = _run_with_sigterm("turn-rmax-child", True)
    assert timed_out_child["status"] == "timeout"
    assert timed_out_child["kill_reason"] == "scope_runtime_max_timeout"
    assert timed_out_child["error"] == "scope RuntimeMaxSec exceeded"
    assert timed_out_child["orphaned_child_tasks"] == ["child-during-rmax"]

    # PR1304-F5 (R2): if RuntimeMaxSec was confirmed first and a late client cancel marks the exact run before
    # final publication, kill_reason=scope_runtime_max_timeout is cleared and orphaned-child diagnostics apply.
    _write(dict(schema_version=2, task_id="child-during-late-cancel", agent="claude", mode="workspace-write",
                status="running", run_nonce="nonce-child-late-cancel", parent_task_id="turn-late-cancel-wins"))
    real_begin_settlement_r2 = store.begin_settlement

    def settle_with_late_client_cancel(record_arg):
        real_begin_settlement_r2(record_arg)
        worker._mark_client_cancel(record_arg["task_id"], record_arg["run_nonce"])

    monkeypatch.setattr(store, "begin_settlement", settle_with_late_client_cancel)
    late_cancel_wins = _run_with_sigterm("turn-late-cancel-wins", True, scope_runtime_max_s=1200)
    monkeypatch.setattr(store, "begin_settlement", real_begin_settlement_r2)
    assert late_cancel_wins["status"] == "cancelled"
    assert late_cancel_wins.get("kill_reason") is None
    assert late_cancel_wins["error"] == "orphaned_child_tasks: child-during-late-cancel; cancelled by client"

    # Late RuntimeMaxSec SIGTERM after provider watchdog already set status=timeout still records scope cause (F10).
    from lu_machinery.dispatcher.watchdog import DeadlineExpired

    real_begin_settlement = store.begin_settlement

    def raise_watchdog_timeout(*_args, **_kwargs):
        raise DeadlineExpired(["codex"], "stdout_silence_timeout", 60.0)

    def settle_with_late_sigterm(record_arg):
        real_begin_settlement(record_arg)
        _signal.raise_signal(_signal.SIGTERM)

    monkeypatch.setattr(worker.headroom, "check_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "communicate", raise_watchdog_timeout)
    monkeypatch.setattr(store, "begin_settlement", settle_with_late_sigterm)
    monkeypatch.setattr(dispatch_isolation, "scope_runtime_max_expired", lambda _unit, *, runtime_max_s: True)
    spec = worker.TaskSpec(
        "turn-rmax-late", "codex", "echo", None, "read-only", work,
        work / "turn-rmax-late.prompt", task_store / "turn-rmax-late.result",
    )
    spec.prompt_file.write_bytes(b"prompt")
    rec = store.new_record(spec, {"key": "public", "github": "example/public"}, "main",
                           "a" * 40, {"research_owned_path": []})
    rec["prompt_file"] = str(task_store / "turn-rmax-late.prompt")
    rec.update(launch_mode="scope", launch_unit="lu-worker-turn-rmax-late-abcd1234", scope_runtime_max_s=1200)
    path = store.create(rec, b"prompt")
    worker.supervise(path, {}, 60)
    late = json.loads(path.read_text(encoding="utf-8"))
    assert late["status"] == "timeout" and late["kill_reason"] == "scope_runtime_max_timeout"
    assert "RuntimeMaxSec" in late["error"]


def test_scan_turn_children_skips_foreign_json_and_fails_closed_on_damaged_records(
    task_store, tmp_path, monkeypatch
):
    """Defect class: foreign non-record JSON objects (such as *-ac-policy.json, receipts, or []) in hot or
    archived task stores are counted as unreadable by _scan_turn_children, blocking turn completion and lease
    release; damaged, non-strict, non-object or invalid-identity files must still fail closed."""
    cli = _fake_driver_state_cli(tmp_path, {"n1": _fresh_note("n1")})
    monkeypatch.setenv(store.DRIVER_STATE_CLI_ENV, str(cli))
    envelope = {"monitor": "http://127.0.0.1:8765", "lease": {"stream_id": STREAM}}
    releases: list[tuple] = []
    monkeypatch.setattr(host_admission, "release_turn", lambda lease, **kwargs: releases.append((lease, kwargs)))
    monkeypatch.setattr(worker, "_provider_alive", lambda _record: False)

    turn = _settled(**{**_LAUNCHED, "launch_unit": "lu-worker-turn", "turn_lease": envelope})
    _write(turn)
    full_nonce = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    _write(dict(
        schema_version=2, task_id="child-settled", agent="claude", mode="workspace-write",
        status="done", run_nonce=full_nonce, parent_task_id=turn["task_id"],
        parent_run_nonce=turn["run_nonce"], parent_stream=STREAM,
        launch_mode="scope", pid=_DEAD_PID, process_start="boot:1",
        launch_unit="lu-worker-child-settled", worktree_disposition="removed",
    ))

    # Foreign non-record JSON files in hot and archive stores must be skipped, not counted unreadable.
    (task_store / "child-ac-policy.json").write_text(
        json.dumps({"schema": "ac-policy.v1", "rules": []}), encoding="utf-8"
    )
    (task_store / "turn.review-state.json").write_text(
        json.dumps({"round": 1, "pr": 42}), encoding="utf-8"
    )
    (task_store / "other-receipt.json").write_text(
        json.dumps({"schema": "proof.v1", "task_id": "unrelated-task"}), encoding="utf-8"
    )
    (task_store / "review-findings.json").write_text(
        json.dumps({"schema_version": "code-review-findings.v1", "findings": []}), encoding="utf-8"
    )
    (task_store / "empty-list.json").write_text("[]", encoding="utf-8")
    arch_dir = task_store / "archive"
    arch_dir.mkdir()
    (arch_dir / "archived-ac-policy.json").write_text(
        json.dumps({"schema": "ac-policy.v1", "archived": True}), encoding="utf-8"
    )
    (arch_dir / "archived-empty-list.json").write_text("[]", encoding="utf-8")

    scan_clean = worker._scan_turn_children(turn)
    assert scan_clean == {
        "children": [{"task_id": "child-settled", "run_nonce": full_nonce, "status": "done"}],
        "live": [],
        "unknown": [],
        "unreadable": [],
    }
    worker._enforce_delivery(turn)
    store.replace(turn)
    worker._release_stream(turn, evidence_lost=False)
    assert turn["status"] == "done"
    assert "unreadable_child_records" not in turn
    assert len(releases) == 1

    # Damaged, non-strict, non-object, symlinked, and invalid-identity/invalid-schema files fail closed.
    (task_store / "bad-truncated.json").write_text("{", encoding="utf-8")
    (task_store / "bad-blank.json").write_text("   \n", encoding="utf-8")
    (task_store / "bad-array.json").write_text("[1, 2]", encoding="utf-8")
    (task_store / "bad-scalar.json").write_text("123", encoding="utf-8")
    (task_store / "bad-foreign-nan.json").write_text('{"x": NaN}', encoding="utf-8")
    (task_store / "bad-legacy-no-id.json").write_text(
        json.dumps({"agent": "claude", "status": "running"}), encoding="utf-8"
    )
    # R1-F1 defect class: a .json symlink to a regular foreign JSON file must still fail closed.
    foreign_target = tmp_path / "external-foreign.json"
    foreign_target.write_text(json.dumps({"schema": "ac-policy.v1", "rules": []}), encoding="utf-8")
    (task_store / "bad-symlink-foreign.json").symlink_to(foreign_target)
    (arch_dir / "arch-symlink-foreign.json").symlink_to(foreign_target)
    # R1-F2 defect class: a partial child record with an invalid string schema_version and lifecycle fields
    # (missing task_id) must fail closed rather than being classified foreign.
    partial_bad_schema = json.dumps({
        "schema_version": "2", "parent_task_id": turn["task_id"], "status": "running", "run_nonce": "rn-partial",
    })
    (task_store / "bad-partial-schema.json").write_text(partial_bad_schema, encoding="utf-8")
    (arch_dir / "arch-partial-schema.json").write_text(partial_bad_schema, encoding="utf-8")
    (arch_dir / "arch-array-record.json").write_text('[{"task_id": "c1"}]', encoding="utf-8")
    (arch_dir / "arch-foreign-inf.json").write_text('{"x": Infinity}', encoding="utf-8")
    (arch_dir / "arch-legacy-no-id.json").write_text(
        json.dumps({"agent": "claude", "status": "done"}), encoding="utf-8"
    )

    scan_bad = worker._scan_turn_children(turn)
    assert scan_bad["unreadable"] == sorted([
        "bad-truncated.json",
        "bad-blank.json",
        "bad-array.json",
        "bad-scalar.json",
        "bad-foreign-nan.json",
        "bad-legacy-no-id.json",
        "bad-symlink-foreign.json",
        "bad-partial-schema.json",
        "archive/arch-symlink-foreign.json",
        "archive/arch-partial-schema.json",
        "archive/arch-array-record.json",
        "archive/arch-foreign-inf.json",
        "archive/arch-legacy-no-id.json",
    ])
    refusal = worker._child_state_refusal(scan_bad)
    assert refusal is not None and refusal.startswith("orchestrator_orphaned_children:")


def _release(box, task_id):
    (box.tasks / f"{task_id}.release").write_text("go")


pytestmark = pytest.mark.usefixtures("sources_product_root")
