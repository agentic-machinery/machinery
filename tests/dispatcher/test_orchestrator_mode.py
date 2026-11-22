"""Orchestrator dispatch mode: a CTO-started stream turn that writes no product change (DRIVERS-1122).

The tests own their data: a private task store, a scripted Monitor (an in-process fake or a loopback stub
server for the subprocess dispatcher) and the dispatcher sandbox's stub provider.
"""

import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lu_machinery.dispatcher import git, host_admission, ownership, store, worker
from lu_machinery.orchestration import dispatch_admission
from lu_machinery.session_supervisor import remote
from tests.dispatcher.conftest import await_file, run_git
from tests.helpers.product_tree import configured_roots  # noqa: F401

STREAM = "epic:7"
TASK_ENV = ownership.TASK_ID_ENV


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
    ],
)
def test_entry_refuses_anything_but_the_cto_seat_on_a_canonical_stream(task_store, cto_seat, monkeypatch, capsys,
                                                                       flags, reason):
    """Defect class: a caller other than the CTO, or a non-canonical stream or product scope, enters the mode."""
    from lu_machinery.dispatcher import cli

    for target, name in ((git, "create_worktree"), (worker, "start"), (host_admission, "reserve")):
        monkeypatch.setattr(target, name, lambda *_a, _name=name, **_k: pytest.fail(f"{_name} ran before refusal"))
    code = cli.main(["dispatch", "--agent", "claude", "--model", "claude-sonnet-5-5", "--task-id", "turn",
                     "--prompt", "drive", *flags])
    assert code == 2
    assert reason in capsys.readouterr().err
    assert not store.read("turn", missing_ok=True)


def test_entry_refuses_a_caller_inside_a_dispatched_task(task_store, cto_seat, monkeypatch, capsys):
    """Defect class: a running task (a turn or a worker copying --initiator claude-cto) starts a turn."""
    from lu_machinery.dispatcher import cli

    monkeypatch.setenv(TASK_ENV, "some-worker")
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


def test_concurrent_turns_on_one_stream_get_one_reservation(task_store):
    """Defect class: two turns admitted at once on one stream (check-then-act race), or a stream never released."""
    decision = host_admission.evaluate("workspace-write", sweep=False, orchestrator=True)
    contenders = 6
    start, release = threading.Barrier(contenders + 1), threading.Event()
    holds, refusals, errors = [], [], []

    def contend(task_id, stream):
        try:
            start.wait()
            with host_admission.reserve(task_id, "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                        orchestrator={"stream": stream}) as hold:
                holds.append(hold)
                release.wait(30)
        except host_admission.Refused as exc:
            refusals.append((task_id, exc.code, str(exc)))
        except Exception as exc:  # surfaced below; a thread must not swallow it
            errors.append(exc)

    threads = [threading.Thread(target=contend, args=(f"turn-{n}", STREAM)) for n in range(contenders - 1)]
    threads.append(threading.Thread(target=contend, args=("other-stream", "epic:8")))
    for thread in threads:
        thread.start()
    start.wait()
    deadline = time.monotonic() + 30
    while len(holds) + len(refusals) + len(errors) < contenders and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert not errors
        assert sorted(hold[dispatch_admission.ORCHESTRATOR_KEY]["stream"] for hold in holds) == ["epic:7", "epic:8"]
        assert len(refusals) == contenders - 2
        assert all(code == 3 and "orchestrator_stream_busy: epic:7" in text for _, code, text in refusals)
        [winner] = [hold for hold in holds if hold["orchestrator"]["stream"] == STREAM]
        assert store.read(winner["task_id"])["orchestrator"] == {"stream": STREAM}  # visible to every dispatcher
    finally:
        release.set()
        for thread in threads:
            thread.join(30)
    # The holds ended unused, so the stream is free again.
    with host_admission.reserve("turn-next", "workspace-write", decision, agent="claude", disk_guard=lambda: False,
                                orchestrator={"stream": STREAM}) as hold:
        assert hold["orchestrator"] == {"stream": STREAM}


@pytest.mark.parametrize(
    ("fields", "held"),
    [
        (dict(status="done"), False),
        (dict(status="timeout"), False),
        (dict(status="crashed", provider_pgid=None), False),
        (dict(status="crashed", provider_pgid=os.getpgid(0)), True),  # its provider still runs
        (dict(status="crashed"), True),  # provider liveness unknown
        (dict(status="mystery"), True),
        (dict(status="spawning"), True),
        (dict(orchestrator={"stream": STREAM.upper()}), False),
        (dict(orchestrator="epic:7"), True),  # malformed: holds every stream
    ],
)
def test_a_stream_stays_reserved_until_its_turn_is_proven_finished(task_store, fields, held):
    """Defect class: a stream is released while a crashed turn's provider may still dispatch."""
    _write(_orchestrator_record("prior", **fields))
    holders = host_admission.stream_holders(STREAM, "turn")
    assert bool(holders) is held, holders


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
        ({**_lease(), "expires_at": "soon"}, "orchestrator_lease_unknown"),
        ("lease", "orchestrator_lease_unknown"),
    ],
)
def test_a_live_legacy_lease_refuses_the_turn(monkeypatch, lease, refused):
    """Defect class: a turn starts beside a live tmux driver lease, or an unreadable lease reads as none."""
    monkeypatch.setattr(remote.RemoteEpicClient, "stream", lambda self, stream_id, digest_limit=20: {
        "stream_id": stream_id, "lease": lease})
    reason = host_admission.legacy_lease_refusal(STREAM)
    if refused is None:
        assert reason is None
    else:
        assert reason.startswith(refused), reason


def test_an_unreachable_monitor_refuses_the_turn(hermetic_monitor):
    """Defect class: a Monitor outage reads as 'no lease' and admits a turn beside a live driver."""
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


def _settled(**fields):
    record = _orchestrator_record("turn", status="done", error=None, commits_ahead=0, worktree_dirty_on_exit=False)
    record[store.HANDOFF_RECEIPT_KEY] = dict(
        schema=store.HANDOFF_RECEIPT_SCHEMA, receipt_id=store.handoff_receipt_id(STREAM, "turn", record["run_nonce"], "n1"),
        stream=STREAM, task_id="turn", run_nonce=record["run_nonce"], handoff_ref="n1", recorded_at=store.now())
    record.update(fields)
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

    def run(*args, task_env=None, expected=0):
        env = {**sandbox.env, **({TASK_ENV: task_env} if task_env else {})}
        result = subprocess.run([sandbox.python, "lu_machinery/dispatch.py", *args], cwd=sandbox.repo, env=env,
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == expected, (result.stdout, result.stderr)
        return result

    def turn(task_id, expected=0):
        return run("dispatch", "--agent", "sol", "--model", "blocked", "--mode", "orchestrator", "--stream", STREAM,
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
    """Defect class: duplicate turns, unlinked children, late children of a settled turn, done without a handoff."""
    box = turn_box
    task_id, nonce = box.turn("turn-1").stdout.splitlines()
    record = store.read("turn-1", tasks_root=box.tasks)
    assert record["orchestrator"] == {"stream": STREAM} and record["mode"] == "workspace-write"
    assert "orchestrator_stream_busy: epic:7" in box.turn("turn-2", expected=3).stderr
    await_file(box.tasks / "turn-1.started")  # the turn's provider runs, as when it dispatches
    child = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "child-1",
                    "--prompt", "map the flags", task_env="turn-1").stdout.splitlines()
    linked = json.loads(box.run("wait", *child[:1], "--run-nonce", child[1], "--timeout", "15").stdout)
    assert (linked["parent_task_id"], linked["parent_run_nonce"], linked["parent_stream"]) == ("turn-1", nonce, STREAM)
    receipt = json.loads(box.run("handoff-receipt", "turn-1", "--run-nonce", nonce, "--stream", STREAM,
                                 "--handoff-ref", "note-41", task_env="turn-1").stdout)
    assert receipt["schema"] == store.HANDOFF_RECEIPT_SCHEMA and receipt["handoff_ref"] == "note-41"
    _release(box, "turn-1")
    settled = box.settle((task_id, nonce))
    assert settled["status"] == "done", settled
    assert settled[store.HANDOFF_RECEIPT_KEY] == receipt
    late = box.run("dispatch", "--agent", "sol", "--model", "echo", "--task-id", "child-late", "--prompt", "late",
                   task_env="turn-1", expected=2)
    assert "orchestrator_parent_not_running" in late.stderr
    # The settled turn released its stream; a turn without a receipt settles no_deliverable.
    second = box.turn("turn-3").stdout.splitlines()
    _release(box, "turn-3")
    missed = box.settle(second, expected=1)
    assert (missed["status"], missed["no_deliverable_reason"]) == ("no_deliverable", "orchestrator_handoff_receipt_missing")


pytestmark = pytest.mark.usefixtures("sources_product_root")
