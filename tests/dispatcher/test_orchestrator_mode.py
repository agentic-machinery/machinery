"""Orchestrator dispatch mode: a CTO-started stream turn that writes no product change (DRIVERS-1122).

The tests own their data: a private task store, a scripted Monitor (an in-process fake or a loopback stub
server for the subprocess dispatcher) and the dispatcher sandbox's stub provider.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lu_machinery.dispatcher import environment, git, host_admission, ownership, store, worker
from lu_machinery.orchestration import dispatch_admission, task_record_store
from lu_machinery.session_supervisor import remote
from tests.dispatcher.conftest import await_file, run_git
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
        (["--mode", "orchestrator", "--stream", STREAM, "--initiator", "claude-cto", "--task-id", "turn.sandbox"],
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
    task_id = [] if "--task-id" in flags else ["--task-id", "turn"]
    code = cli.main(["dispatch", "--agent", "claude", "--model", "claude-sonnet-5-5", *task_id,
                     "--prompt", "drive", *flags])
    assert code == 2
    assert reason in capsys.readouterr().err
    assert not store.read("turn", missing_ok=True)


@pytest.mark.parametrize(("marker", "value"), [(TASK_ENV, "some-worker"), (TASK_ENV, ""),
                                               ("LU_WRITE_BOUNDARY_BROKER", "/broker.sock"),
                                               ("LU_WRITE_BOUNDARY_BROKER", "")])
def test_entry_refuses_a_caller_inside_a_dispatched_task(task_store, cto_seat, monkeypatch, capsys, marker, value):
    """Defect class: a running task (a turn or a worker copying --initiator claude-cto) starts a turn."""
    from lu_machinery.dispatcher import cli

    monkeypatch.setenv(marker, value)
    monkeypatch.setattr(worker, "start", lambda *_a, **_k: pytest.fail("spawned"))
    assert cli.main(["dispatch", "--agent", "claude", "--model", "claude-sonnet-5-5", "--task-id", "turn",
                     "--prompt", "drive", "--mode", "orchestrator", "--stream", STREAM,
                     "--initiator", "claude-cto"]) == 2
    assert "only the CTO seat" in capsys.readouterr().err


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


# A dispatcher process that reserves a stream on "go", reports, and holds the reservation until "done" (stdin).
_CONTENDER = """
import sys
from lu_machinery.dispatcher import host_admission
task_id, stream = sys.argv[1:3]
decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
print("ready", flush=True)
sys.stdin.readline()
try:
    with host_admission.reserve(task_id, "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                orchestrator={"stream": stream}, tree="/trees/" + task_id, launch={"started": False}):
        print("held", flush=True)
        sys.stdin.readline()
except host_admission.Refused as exc:
    print("refused", exc.code, exc, flush=True)
"""


def _reservation(stream=STREAM):
    return host_admission.reservations_dir() / f"{stream}{host_admission.RESERVATION_SUFFIX}"


def _reserve(task_id, stream=STREAM, *, force_reason=None, launch=None):
    """Reserve ``stream`` for ``task_id`` as dispatch does, then hand the record on as to a supervisor."""
    decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    with host_admission.reserve(task_id, "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                force_reason=force_reason, orchestrator={"stream": stream},
                                tree=f"/trees/{task_id}", launch=launch) as hold:
        store.replace({**hold, "prompt_file": "/prompts/turn.md"}, store.record_path(task_id))
    return hold


def test_dispatch_processes_contending_for_one_stream_get_one_reservation(task_store):
    """Defect class: two dispatcher processes both admit a turn on one stream (check-then-act across processes),
    one stream's reservation blocks another stream, or a hold that ends unused keeps its stream."""
    contenders = [(f"turn-{n}", STREAM) for n in range(4)] + [("other", "epic:8")]
    procs = [subprocess.Popen([sys.executable, "-c", _CONTENDER, task_id, stream], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for task_id, stream in contenders]
    # Bounds every pipe read: a contender that hangs is killed, which ends its output.
    watchdog = threading.Timer(120, lambda: [proc.kill() for proc in procs])
    watchdog.start()
    try:
        for proc in procs:
            assert proc.stdout.readline() == "ready\n", proc.stderr.read()
        for proc in procs:  # every process has its admission decision; now they all reserve at once
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = [proc.stdout.readline() for proc in procs]
        held = [task_id for (task_id, _stream), line in zip(contenders, results, strict=True) if line == "held\n"]
        assert len(held) == 2 and "other" in held, results
        [winner] = [task_id for task_id in held if task_id != "other"]
        refused = [line for line in results if line.startswith("refused")]
        assert len(refused) == 3
        assert all(line.startswith(f"refused 3 ❌ orchestrator_stream_busy: {STREAM} is reserved by turn {winner} ")
                   for line in refused), refused
        assert json.loads(_reservation().read_text())["task_id"] == winner
        assert json.loads(_reservation("epic:8").read_text())["task_id"] == "other"
    finally:
        for proc in procs:
            with contextlib.suppress(BrokenPipeError, ValueError):
                proc.stdin.write("done\n")
                proc.stdin.close()
            proc.wait(timeout=60)
        watchdog.cancel()
    # Each hold ended before any supervisor could start, so both streams are free again.
    assert not _reservation().exists() and not _reservation("epic:8").exists()
    assert _reserve("turn-next")["orchestrator"] == {"stream": STREAM}


def test_a_crash_between_the_reservation_and_the_hold_leaves_the_stream_held(task_store):
    """Defect class: a dispatcher that dies after reserving and before publishing its hold frees the stream."""
    crash = ("import os, sys\nfrom lu_machinery.dispatcher import host_admission, store\n"
             "store._write = lambda *_a, **_k: os._exit(9)\n" + _CONTENDER.split("print(\"ready\"", 1)[0]
             + "with host_admission.reserve(task_id, 'workspace-write', decision, agent='claude', disk_guard=lambda: False,"
             " orchestrator={'stream': stream}, tree='/trees/' + task_id):\n    pass\n")
    result = subprocess.run([sys.executable, "-c", crash, "turn", STREAM], capture_output=True, text=True, timeout=120)
    assert result.returncode == 9, result.stderr
    assert store.read("turn", missing_ok=True) is None  # no hold, no record
    assert json.loads(_reservation().read_text())["task_id"] == "turn"
    for task_id in ("turn", "turn-2"):  # neither the same task id nor another one gets the stream
        with pytest.raises(host_admission.Refused, match="orchestrator_stream_busy: epic:7 is reserved by turn turn "):
            _reserve(task_id)
    assert host_admission.stream_refusal(STREAM).startswith("orchestrator_stream_busy")


def test_release_needs_the_reserving_task_and_run(task_store):
    """Defect class: a stale run (or another task) releases a stream that a newer turn reserved."""
    hold = _reserve("turn")
    for task_id, nonce in (("turn", "f" * 32), ("other", hold["run_nonce"]), ("turn", hold["run_nonce"].upper())):
        assert host_admission.release_stream(STREAM, task_id, nonce).startswith("orchestrator_reservation_foreign")
        assert _reservation().exists()
    assert host_admission.release_stream(STREAM, "turn", hold["run_nonce"]) is None
    assert not _reservation().exists()
    assert host_admission.release_stream(STREAM, "turn", hold["run_nonce"]) == "orchestrator_reservation_missing"


@pytest.mark.parametrize(("progress", "record", "released"), [
    ({"started": False}, "kept", True),  # this dispatch never started a supervisor
    ({"started": False}, "deleted", True),
    ({"started": True}, "kept", False),  # a supervisor may run, whatever the record says
    ({"started": True}, "deleted", False),
    ({"started": True}, "replaced", False),
    ({"started": None}, "kept", False),  # damaged progress is not proof
    (None, "kept", False),  # no progress given
])
def test_a_pre_launch_abort_follows_the_dispatchers_own_launch_progress(task_store, progress, record, released):
    """Defect class: a lost, replaced or stale hold record reads as 'never launched' and frees the stream while a
    supervisor this dispatch started may still run."""
    decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    with host_admission.reserve("turn", "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                orchestrator={"stream": STREAM}, tree="/trees/turn", launch=progress) as hold:
        path = store.record_path("turn")
        if record == "deleted":
            path.unlink()
        elif record == "replaced":
            path.write_text(json.dumps({**hold, "run_nonce": "f" * 32}))
    assert _reservation().exists() is not released


@pytest.mark.parametrize("damage", ["deleted", "archived", "truncated", "key-lost", "foreign", "fifo"])
def test_occupancy_is_the_reservation_alone_whatever_the_task_records_say(task_store, damage):
    """Defect class: a deleted, archived, damaged or identity-less turn record (or a non-regular file in the store)
    frees, or blocks, a stream; a force reason or a reused task id bypasses a reservation."""
    _reserve("turn")
    path = store.record_path("turn")
    if damage == "deleted":
        path.unlink()
    elif damage == "archived":
        archive = task_store / task_record_store.ARCHIVE_DIR_NAME
        archive.mkdir()
        path.rename(archive / path.name)
    elif damage == "truncated":
        path.write_text(path.read_text()[:20])
    elif damage == "key-lost":
        path.write_text(json.dumps({key: value for key, value in json.loads(path.read_text()).items()
                                    if key != "orchestrator"}))
    elif damage == "foreign":
        path.write_text(json.dumps({"orchestrator": {"stream": STREAM}}))
    else:
        path.unlink()
        os.mkfifo(path)
    for task_id in ("turn", "turn-2"):
        with pytest.raises(host_admission.Refused, match="orchestrator_stream_busy: epic:7 is reserved by turn turn "):
            _reserve(task_id, force_reason="operator says go")
    # Another stream is untouched by this one's records, damaged or not.
    assert _reserve("turn-8", stream="epic:8")["orchestrator"] == {"stream": "epic:8"}


def _plant(kind, entry):
    if kind == "symlink":
        target = entry.with_name("elsewhere.json")
        target.write_text("{}")
        entry.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(entry)
    elif kind == "directory":
        entry.mkdir()
    elif kind == "oversized":
        entry.write_bytes(b" " * (64 * 1024))
    elif kind == "empty":
        entry.write_bytes(b"")
    else:
        hold = dict(schema=host_admission.RESERVATION_SCHEMA, stream=STREAM, task_id="turn", run_nonce="a" * 32,
                    worktree_path="/trees/turn", reserved_at=store.now())
        text = {
            "not-json": "{",
            "not-utf8": b"\xff\xfe".decode("latin-1"),
            "a-list": "[]",
            "duplicate-key": json.dumps(hold)[:-1] + ', "task_id": "other"}',
            "extra-field": json.dumps({**hold, "expires_at": store.now()}),
            "missing-field": json.dumps({key: value for key, value in hold.items() if key != "run_nonce"}),
            "other-stream": json.dumps({**hold, "stream": "epic:8"}),
            "bad-nonce": json.dumps({**hold, "run_nonce": "nonce"}),
            "bad-task-id": json.dumps({**hold, "task_id": "../turn"}),
            "relative-tree": json.dumps({**hold, "worktree_path": "trees/turn"}),
            "nul-tree": json.dumps({**hold, "worktree_path": "/trees/turn\x00x"}),
            "surrogate-tree": json.dumps({**hold, "worktree_path": "/trees/\ud800"}),
            "bad-time": json.dumps({**hold, "reserved_at": "soon"}),
            "other-schema": json.dumps({**hold, "schema": "v0"}),
        }[kind]
        entry.write_text(text, encoding="latin-1" if kind == "not-utf8" else "utf-8")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "oversized", "empty", "not-json", "not-utf8",
                                  "a-list", "duplicate-key", "extra-field", "missing-field", "other-stream",
                                  "bad-nonce", "bad-task-id", "relative-tree", "nul-tree", "surrogate-tree",
                                  "bad-time", "other-schema"])
def test_an_unsafe_or_malformed_reservation_holds_its_stream(task_store, kind):
    """Defect class: a symlinked, non-regular (a FIFO must not block), oversized or malformed reservation file reads
    as a free stream, or its damage frees or blocks another stream."""
    directory = host_admission.reservations_dir()
    directory.mkdir(mode=0o700)
    _plant(kind, _reservation())
    reason = host_admission.stream_refusal(STREAM)
    assert reason is not None and reason.startswith("orchestrator_reservation_"), reason
    with pytest.raises(host_admission.Refused, match="orchestrator_reservation_"):
        _reserve("turn")
    assert store.read("turn", missing_ok=True) is None  # refused before any hold
    assert host_admission.release_stream(STREAM, "turn", "a" * 32) is not None  # never unlinked
    assert os.path.lexists(_reservation())
    assert _reserve("turn-8", stream="epic:8")["orchestrator"] == {"stream": "epic:8"}


@pytest.mark.parametrize("kind", ["symlink", "file", "group-writable"])
def test_an_unsafe_reservation_directory_holds_every_stream(task_store, tmp_path, kind):
    """Defect class: a planted reservation directory (a symlink, a file, writable by others) is trusted or replaced."""
    directory = host_admission.reservations_dir()
    if kind == "symlink":
        (tmp_path / "planted").mkdir()
        directory.symlink_to(tmp_path / "planted")
    elif kind == "file":
        directory.write_text("")
    else:
        directory.mkdir()
        directory.chmod(0o770)
    for stream in (STREAM, "epic:8"):
        assert host_admission.stream_refusal(stream).startswith("orchestrator_reservation_dir_unsafe")
        with pytest.raises(host_admission.Refused, match="orchestrator_reservation_dir_unsafe"):
            _reserve("turn", stream=stream)
    assert host_admission.turn_writer_refusal(task_ids={"worker"}).startswith("orchestrator_reservation_dir_unsafe")
    if kind == "symlink":
        assert not list((tmp_path / "planted").iterdir())  # nothing was written through the link


def test_a_dry_run_reads_the_stream_without_writing(task_store):
    """Defect class: a dry run creates the reservation directory or file, or misses a reserved stream."""
    assert host_admission.stream_refusal(STREAM) is None
    assert not host_admission.reservations_dir().exists()
    _reserve("turn")
    assert host_admission.stream_refusal(STREAM).startswith("orchestrator_stream_busy: epic:7 is reserved by turn turn ")
    assert host_admission.stream_refusal("epic:8") is None


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
def test_settlement_releases_the_stream_only_on_proof_the_run_is_gone(task_store, monkeypatch, change, released):
    """Defect class: settlement frees a stream while a process or service of the turn may still write, or from a
    record that is not this run's terminal one."""
    hold = _reserve("turn")
    record = _orchestrator_record("turn", **{**_LAUNCHED, "status": "done", "run_nonce": hold["run_nonce"],
                                             "launch_unit": "lu-worker-turn"})
    published = {**record, **{key: value for key, value in change.items() if key != "probe"}}
    path = store.record_path("turn")
    path.write_text(json.dumps(published))  # raw: the store's writer refuses some of these states

    def probe(seen):
        assert seen["run_nonce"] == published["run_nonce"]
        if change.get("probe") == "raise":
            raise ValueError("a damaged liveness field")
        return change.get("probe", False)

    monkeypatch.setattr(worker, "_provider_alive", probe)
    worker._release_stream(record, evidence_lost=True)  # the record had to be rebuilt at publication
    assert _reservation().exists()
    worker._release_stream(record, evidence_lost=False)
    assert _reservation().exists() is not released


def test_a_turn_tree_takes_no_other_writer(task_store, tmp_path, monkeypatch):
    """Defect class: a --cwd task or a task service is added to an orchestrator turn while it runs or drains, also
    when the turn's record lost its orchestrator key, so its tree changes after it was judged."""
    from lu_machinery.orchestration import dispatch_isolation as iso

    tree = tmp_path / "turn-tree"
    tree.mkdir()
    decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    with host_admission.reserve("turn", "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                orchestrator={"stream": STREAM}, tree=str(tree)) as hold:
        store.replace({**hold, "prompt_file": "/prompts/turn.md"}, store.record_path("turn"))
    # The record lost its orchestrator key: the reservation still attributes the turn and its tree.
    store.record_path("turn").write_text(json.dumps({**hold, "prompt_file": "/prompts/turn.md", "orchestrator": None}))
    refused = host_admission.turn_writer_refusal(tree=str(tree))
    assert refused.startswith("orchestrator_turn_writer_refused: turn is the orchestrator turn on epic:7")
    assert host_admission.turn_writer_refusal(tree=str(tmp_path)) is None  # any other tree
    nonce = hold["run_nonce"]
    scope = iso.scope_unit_name("turn", nonce)
    base = tmp_path / "var-tmp"
    base.mkdir()
    monkeypatch.setattr(environment, "TASK_SCRATCH_BASE", base)
    monkeypatch.setattr(environment, "TASK_SCRATCH_PATH_MAX", 4096)
    record = {"task_id": "turn", "launch_mode": "scope", "launch_unit": scope, "run_nonce": nonce}

    def systemd(_argv):
        pytest.fail("systemd was asked to start a turn's service")

    for copy, inherited in (({**record, "orchestrator": {"stream": STREAM}}, None), (record, None),
                            # a child process of the turn passing another task's record copy
                            ({**record, "task_id": "worker", "launch_unit": iso.scope_unit_name("worker", nonce)},
                             "turn")):
        if inherited:
            monkeypatch.setenv(TASK_ENV, inherited)
        with pytest.raises(iso.DispatchIsolationError, match="orchestrator_turn_writer_refused"):
            iso.start_task_service(copy, ["sleep", "30"], run=systemd)
        assert "task_services" not in copy
    assert not list(base.iterdir())  # no scratch side effect
    # An ordinary task still starts its service.
    monkeypatch.delenv(TASK_ENV)
    worker_record = {**record, "task_id": "worker", "launch_unit": iso.scope_unit_name("worker", nonce)}
    started = iso.start_task_service(worker_record, ["sleep", "30"],
                                     run=lambda argv: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))
    assert worker_record["task_services"] == [started]
    # A reservation that cannot be read is anyone's: both refuse.
    _plant("not-json", _reservation("epic:9"))
    assert host_admission.turn_writer_refusal(tree=str(tmp_path)).startswith("orchestrator_reservation_malformed")
    with pytest.raises(iso.DispatchIsolationError, match="orchestrator_reservation_malformed"):
        iso.start_task_service(dict(worker_record), ["sleep", "30"], run=systemd)


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


# --- legacy lease --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lease", "refused"),
    [
        (None, None),
        (_lease(), "orchestrator_stream_leased"),
        (_lease(session_state="rolling"), "orchestrator_stream_leased"),
        (_lease(expires_in=-5), None),
        (_lease(state="released"), None),
        (_lease(session_state="closed"), None),
        (_lease(state="expired"), None),
        ({**_lease(), "expires_at": "soon"}, "orchestrator_lease_unknown"),
        ("lease", "orchestrator_lease_unknown"),
        (_MISSING, "orchestrator_lease_unknown"),  # no lease field is not the projection's explicit null
        ({key: value for key, value in _lease().items() if key != "session_state"}, "orchestrator_lease_unknown"),
        (_lease(state="held"), "orchestrator_lease_unknown"),
        (_lease(session_state=["open"]), "orchestrator_lease_unknown"),
        # Every part of a lease is checked, whatever its state decides.
        ({**_lease(state="released"), "expires_at": "soon"}, "orchestrator_lease_unknown"),
        ({**_lease(state="expired"), "expires_at": None}, "orchestrator_lease_unknown"),
        ({**_lease(session_state="closed"), "expires_at": "2026-11-22T20:00:00\x00+00:00"}, "orchestrator_lease_unknown"),
        ({**_lease(), "expires_at": "2026-11-22 20:00:00Z"}, "orchestrator_lease_unknown"),
    ],
)
def test_a_live_legacy_lease_refuses_the_turn(monkeypatch, lease, refused):
    """Defect class: a turn starts beside a live tmux driver lease, or an unreadable lease reads as none."""
    monkeypatch.setattr(remote.RemoteEpicClient, "stream", lambda self, stream_id, digest_limit=20: {
        "stream_id": stream_id, **({} if lease is _MISSING else {"lease": lease})})
    reason = host_admission.legacy_lease_refusal(STREAM)
    if refused is None:
        assert reason is None
    else:
        assert reason.startswith(refused), reason


def test_an_unreachable_monitor_refuses_the_turn(hermetic_monitor, monkeypatch):
    """Defect class: a Monitor outage or a malformed error reply reads as 'no lease' or escapes as a crash."""
    assert host_admission.legacy_lease_refusal(STREAM).startswith("orchestrator_lease_unknown")

    def malformed_error_body(self, stream_id, digest_limit=20):
        raise AttributeError("'list' object has no attribute 'get'")  # what a JSON array error body raised

    monkeypatch.setattr(remote.RemoteEpicClient, "stream", malformed_error_body)
    assert host_admission.legacy_lease_refusal(STREAM).startswith("orchestrator_lease_unknown")


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


# --- the turn end to end: reservation, child linkage, receipt, settlement ------------------------------------------


class _Monitor(BaseHTTPRequestHandler):
    def do_GET(self):
        stream = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
        body = json.dumps({"stream_id": stream, "lease": None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture
def turn_box(sandbox):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in (TASK_ENV, "LU_WRITE_BOUNDARY_BROKER", "SESSION_SEAT", "SESSION_EPIC"):
        sandbox.env.pop(name, None)
    sandbox.env["LU_MONITOR_LOOPBACK"] = f"http://127.0.0.1:{server.server_address[1]}"
    # A turn takes no write slot: no write worker could start at this cap.
    sandbox.env["DISPATCH_MAX_LIVE_WRITE_WORKERS"] = "0"
    # A turn's launch paces its meter, which would defer the duplicate turn before its stream is checked.
    sandbox.env["DISPATCH_MIN_METER_LAUNCH_INTERVAL_S"] = "0"

    def run(*args, task_env=None, expected=0):
        env = {**sandbox.env, **({TASK_ENV: task_env} if task_env else {})}
        result = subprocess.run([sandbox.python, "lu_machinery/dispatch.py", *args], cwd=sandbox.repo, env=env,
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == expected, (result.stdout, result.stderr)
        return result

    def turn(task_id, expected=0, stream=STREAM):
        return run("dispatch", "--agent", "sol", "--model", "blocked", "--mode", "orchestrator", "--stream", stream,
                   "--initiator", "claude-cto", "--task-id", task_id, "--prompt", "drive one wave",
                   expected=expected)

    sandbox.run, sandbox.turn = run, turn
    try:
        yield sandbox
    finally:
        server.shutdown()
        server.server_close()


def _release(box, task_id):
    (box.tasks / f"{task_id}.release").write_text("go")


def test_a_turn_end_to_end_reserves_links_children_and_settles_on_its_receipt(turn_box):
    """Defect class: duplicate turns, unlinked children, late children of a settled turn, done without a handoff,
    and a fallback turn (unprovable process tree) judged done or freeing its stream."""
    box = turn_box
    task_id, nonce = box.turn("turn-1").stdout.splitlines()
    record = store.read("turn-1", tasks_root=box.tasks)
    assert record["orchestrator"] == {"stream": STREAM} and record["mode"] == "workspace-write"
    assert record["research_role"] == "driver"  # role policy sees a driving turn, not only authoring
    assert "orchestrator_stream_busy: epic:7" in box.turn("turn-2", expected=3).stderr
    await_file(box.tasks / "turn-1.started")  # the turn's provider runs, as when it dispatches
    attach = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "borrower", "--prompt", "edit",
                     "--cwd", record["worktree_path"], task_env="turn-1", expected=2)
    assert "orchestrator_turn_writer_refused: turn-1 is the orchestrator turn on epic:7" in attach.stderr
    child = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "child-1",
                    "--prompt", "map the flags", task_env="turn-1").stdout.splitlines()
    linked = json.loads(box.run("wait", *child[:1], "--run-nonce", child[1], "--timeout", "15").stdout)
    assert (linked["parent_task_id"], linked["parent_run_nonce"], linked["parent_stream"]) == ("turn-1", nonce, STREAM)
    receipt = json.loads(box.run("handoff-receipt", "turn-1", "--run-nonce", nonce, "--stream", STREAM,
                                 "--handoff-ref", "note-41", task_env="turn-1").stdout)
    assert receipt["schema"] == store.HANDOFF_RECEIPT_SCHEMA and receipt["handoff_ref"] == "note-41"
    _release(box, "turn-1")
    settled = box.settle((task_id, nonce), expected=1)
    # The sandbox forces a fallback launch, which cannot prove its processes gone before its tree is judged.
    assert (settled["status"], settled["no_deliverable_reason"].split(":")[0]) == (
        "no_deliverable", "orchestrator_tree_unproven"), settled
    assert settled[store.HANDOFF_RECEIPT_KEY] == receipt
    late = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "child-late", "--prompt", "late",
                   task_env="turn-1", expected=2)
    assert "orchestrator_parent_not_running" in late.stderr
    # Nor does it prove the process tree gone afterwards: the settled turn keeps its stream's reservation.
    assert f"orchestrator_stream_busy: {STREAM} is reserved by turn turn-1 (run {nonce})" in box.turn(
        "turn-3", expected=3).stderr
    # A turn without a receipt settles no_deliverable.
    second = box.turn("turn-4", stream="epic:8").stdout.splitlines()
    _release(box, "turn-4")
    missed = box.settle(second, expected=1)
    assert (missed["status"], missed["no_deliverable_reason"]) == ("no_deliverable", "orchestrator_handoff_receipt_missing")


pytestmark = pytest.mark.usefixtures("sources_product_root")
