"""Approved #12192 admission and mid-run record-damage defect classes."""

import hashlib
import json
import re
import signal
from pathlib import Path

import pytest

from lu_machinery.dispatcher import cli, ownership, store, worker
from lu_machinery.dispatcher.types import ReviewAttempt, TaskSpec
from lu_machinery.orchestration.task_record_store import read_task_record
from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review import review_lineage
from tests.dispatcher.conftest import run_git
from tests.helpers import current_sol
from tests.helpers.product_tree import configured_roots  # noqa: F401
from tests.test_fleet_repos import transferred_catalog

CURRENT_SOL = current_sol()





HEAD = "a" * 40


def admission_readers(root):
    return [
        lambda: review_lineage.review_records(root, repository="org/repo"),
        lambda: store.list_tasks(include_archive=True, strict=True),
        lambda: recorder.admitted_unknown_history(
            repository="org/repo", branch="topic", head_sha=HEAD, task_root=root, repo_root=root
        ),
        lambda: recorder.require_no_outside_review_in_flight(root, "org/repo", HEAD),
    ]


def test_strict_attempt_scan_keeps_a_moved_record_and_its_reply_once(tmp_path, monkeypatch):
    """Defect class: archive relocation refuses attempt checks or counts one run twice."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    hot = tmp_path / "review.json"
    archived = tmp_path / "archive" / hot.name
    archived.parent.mkdir()
    reply = hot.with_suffix(".result")
    archived_reply = archived.with_suffix(".result")
    reply.write_text("VERDICT: APPROVE")
    binding = ReviewAttempt(review_id="review-id", attempt_id="attempt-id", manifest_sha256="b" * 64)
    hot.write_text(json.dumps({
        "task_id": "review", "schema_version": 2, "status": "done",
        "review_attempt": binding, "result_file": str(reply),
    }))
    read_bytes = Path.read_bytes

    def archive_before_read(path):
        if path == hot and hot.exists():
            hot.rename(archived)
            reply.rename(archived_reply)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", archive_before_read)

    records = store.list_tasks(include_archive=True, strict=True)
    assert [(record["task_id"], record["result_file"]) for record in records] == [("review", str(archived_reply))]
    with pytest.raises(ValueError, match="review attempt already spent"):
        store.check_review_attempt(binding)


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize(("foreign", "refused"), [
    (b"[curriculum_manifest_canary] Manifest written successfully: 3901 files\n", True),
    (b'{"schema":"agy-read-only.v1","task_id":"worker"}', False),
    (b'{"schema_version":"code-review-findings.v1","findings":[]}', False),
    (b'["a proof list"]', True),
])
def test_foreign_sidecar_blocks_review_admission_only_when_it_may_hold_a_record(
    tmp_path, monkeypatch, caplog, foreign, refused, archived
):
    """A foreign JSON object must not deny an unrelated review or spend its attempt.

    Text that is not JSON and a non-empty array are not foreign: either may hold a record, so
    every admission reader refuses them rather than skipping a possible live run (#1440).
    """
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    archive = tmp_path / "archive"
    archive.mkdir()
    valid = dict(task_id="unrelated", schema_version=2, repository="org/repo", status="done", review_profile="infra")
    (tmp_path / "unrelated.json").write_text(json.dumps(valid))
    sidecar = (archive if archived else tmp_path) / "worker.manifest-canary.json"
    sidecar.write_bytes(foreign)
    if refused:
        readers = admission_readers(tmp_path)
        for reader in readers[:3]:
            with pytest.raises((ValueError, recorder.RecordError), match=re.escape("worker.manifest-canary.json")):
                reader()
        # The in-flight guard reads the active (hot) ledger only; an archived record is terminal.
        if archived:
            assert readers[3]() is None
        else:
            with pytest.raises(recorder.RecordError, match="RED_TEAM_REFUSED: task ledger unavailable"):
                readers[3]()
        with pytest.raises(ValueError, match=re.escape("worker.manifest-canary.json")):
            store.check_review_attempt(dict(review_id="unused", attempt_id="unused", manifest_sha256="b" * 64))
        return
    assert [record["task_id"] for _, record in admission_readers(tmp_path)[0]()] == ["unrelated"]
    assert [record["task_id"] for record in admission_readers(tmp_path)[1]()] == ["unrelated"]
    assert admission_readers(tmp_path)[2]() == ()
    assert admission_readers(tmp_path)[3]() is None
    store.check_review_attempt(dict(review_id="unused", attempt_id="unused", manifest_sha256="b" * 64))
    assert str(sidecar) in caplog.text


def _plant_record_and_copy(root, copy_name, **copy_updates):
    """Plant ``worker.json`` and a misnamed copy; a None update omits the field from the copy."""
    record = dict(task_id="worker", schema_version=2, run_nonce="n1", repository="org/repo", status="done",
                  review_mode="independent", review=True, retried=False)
    (root / "worker.json").write_text(json.dumps(record))
    copy = {**record, **copy_updates}
    (root / copy_name).write_text(json.dumps({key: value for key, value in copy.items() if value != "__omit__"}))


def test_exact_copy_of_a_named_record_does_not_block_admission(tmp_path, monkeypatch):
    """Defect class: a worker's saved copy of a task record refused every review admission read."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    _plant_record_and_copy(tmp_path, "worker.copy.json")
    readers = admission_readers(tmp_path)
    assert [path.name for path, _ in readers[0]()] in ([], ["worker.json"])
    assert [record["task_id"] for record in readers[1]()] == ["worker"]
    assert readers[2]() == ()
    assert readers[3]() is None


_READERS = [
    (0, "task record worker.COPY.json has invalid task identity"),
    (1, "unreadable task record .*worker.COPY"),
    (2, "task record .*worker.COPY.* unreadable"),
    (3, "RED_TEAM_REFUSED: task ledger unavailable"),
]
_READER_IDS = ["review_records", "list_tasks_strict", "admitted_unknown_history", "in_flight_guard"]
_MOVE_OUT = r"move it out of the task directory \(for example into <task-id>\.artifacts/\)"


@pytest.mark.parametrize("reader, message", _READERS, ids=_READER_IDS)
@pytest.mark.parametrize("copy_name, updates", [
    ("forged", {"run_nonce": "n2"}),
    ("claims", {"worktree_path": "/virtual/checkout", "review_profile": "infra", "review_head": HEAD,
                "review_attempt": 1}),
    ("status_nulled", {"status": None}),
    ("review_mode_omitted", {"review_mode": "__omit__"}),
    ("mid_run", {"status": "running"}),
    ("true_as_one", {"review": 1}),
    ("false_as_zero", {"retried": 0}),
])
def test_divergent_copy_still_blocks_admission(tmp_path, monkeypatch, reader, message, copy_name, updates):
    """Defect class: a copy differing from its named record in any field is skipped as if it were identical."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    errors = (ValueError, recorder.BranchFactsError, recorder.RecordError)
    _plant_record_and_copy(tmp_path, f"worker.{copy_name}.json", **updates)
    with pytest.raises(errors, match=message.replace("COPY", copy_name)) as caught:
        admission_readers(tmp_path)[reader]()
    assert re.search(_MOVE_OUT, str(caught.value))


@pytest.mark.parametrize("reader, message", _READERS, ids=_READER_IDS)
def test_orphaned_or_renamed_record_still_blocks_admission(tmp_path, monkeypatch, reader, message):
    """Defect class: a record with no correctly named twin (or an archived-only one) is skipped as a copy."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    errors = (ValueError, recorder.BranchFactsError, recorder.RecordError)
    _plant_record_and_copy(tmp_path, "worker.copy.json")
    (tmp_path / "worker.json").rename(tmp_path / "worker.renamed.json")
    with pytest.raises(errors, match=message.replace("COPY", "(copy|renamed)")):
        admission_readers(tmp_path)[reader]()
    (tmp_path / "worker.renamed.json").unlink()
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "worker.json").write_text((tmp_path / "worker.copy.json").read_text())
    with pytest.raises(errors, match=message.replace("COPY", "copy")):
        admission_readers(tmp_path)[reader]()


def test_receipt_marker_cannot_hide_legacy_review_claim(tmp_path, monkeypatch):
    """A receipt marker cannot remove a lifecycle claim from admission's inventory."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    record = dict(schema="agy-read-only.v1", task_id="legacy-review", agent="claude", status="done",
                  review_profile="infra", review_mode="cross_family", repository="org/repo", worktree_branch="topic")
    (tmp_path / "legacy-review.receipt.json").write_text(json.dumps(record))

    assert [row["task_id"] for _, row in review_lineage.review_records(tmp_path, repository="org/repo")] == [
        "legacy-review"
    ]
    assert [row["task_id"] for row in store.list_tasks(include_archive=True, strict=True)] == ["legacy-review"]


@pytest.mark.parametrize(("marker", "value"), [
    ("pr", 12192),
    ("review_attempt", {"review_id": "legacy"}),
    ("require_review_verdict", True),
    ("review_profile", "infra"),
    ("review_author_model", CURRENT_SOL),
    ("review_risk", "high"),
    ("review_mode", "cross_family"),
    ("type", "review"),
])
def test_legacy_review_flags_remain_visible_to_lineage(tmp_path, marker, value):
    """Every established review flag retains a legacy row in lineage inventory."""
    (tmp_path / "legacy-review.json").write_text(json.dumps({"repository": "org/repo", marker: value}))

    assert [path.name for path, _ in review_lineage.review_records(tmp_path, repository="org/repo")] == [
        "legacy-review.json"
    ]


@pytest.mark.parametrize("damaged", [
    b'{"schema_version":2,"task_id":"damaged",',
    b'{"schema_version":3,"task_id":"damaged"}',
    b'{"schema_version":"2","task_id":"damaged"}',
    b'{"schema_version":2,"task_id":"forged"}',
    '{"schema_version":2,"task_id":"damaged"}'.encode("utf-16"),
    b'\xef\xbb\xbf{"schema_version":2,"task_id":"damaged"}',
    b'\xef\xbb\xbf\n{"schema_version":2,"task_id":"damaged"}',
    b'\xef\xbb\xbf\t  {"schema_version":2,"task_id":"damaged"}',
    '\ufeff\u00a0{"schema_version":2,"task_id":"damaged"}'.encode("utf-8"),
    b'\x00\x00{"schema_version":2,"task_id":"damaged"}',
])
def test_damaged_record_still_refuses_admission(tmp_path, monkeypatch, damaged):
    """Corruption or forged identity must not disappear from admission scans."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    archive = tmp_path / "archive"
    archive.mkdir()
    legacy = archive / "legacy.20261030T000000Z.1.archived.json"
    legacy.write_text(json.dumps(dict(task_id="legacy", status="done", review_profile="infra", repository="org/repo")))
    assert read_task_record(legacy)["task_id"] == "legacy"
    assert [record["task_id"] for _, record in admission_readers(tmp_path)[0]()] == ["legacy"]
    (tmp_path / "damaged.json").write_bytes(damaged)
    for reader in admission_readers(tmp_path):
        with pytest.raises((ValueError, recorder.RecordError)):
            reader()


@pytest.mark.parametrize("phase", ["provider_exit", "settlement_cutoff", "terminal_write"])
@pytest.mark.parametrize("damage", ["deleted", "truncated", "non_object", "missing_nonce", "replaced_nonce", "recursion"])
def test_mid_run_record_damage_publishes_typed_terminal_failure(sandbox, monkeypatch, phase, damage):
    """Record loss/corruption cannot strand a supervisor or overwrite a replacement run."""
    from tests.dispatcher import stub_provider

    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    monkeypatch.setattr(worker.providers, "load", lambda _agent: stub_provider)
    spec = TaskSpec("damage-probe", "codex", "echo", None, "read-only", sandbox.repo,
                    sandbox.repo / "damage-probe.prompt", sandbox.tasks / "damage-probe.result")
    spec.prompt_file.write_bytes(b"literal final response")
    record = store.new_record(spec, {"key": "public", "github": str(sandbox.remote)}, "main",
                              run_git(sandbox.repo, "rev-parse", "HEAD"), {"research_owned_path": []})
    record["prompt_file"] = str(sandbox.tasks / "damage-probe.prompt")
    path = store.create(record, b"literal final response")
    (sandbox.tasks / "logs").mkdir()
    original = worker.communicate if phase == "provider_exit" else getattr(
        store, "begin_settlement" if phase == "settlement_cutoff" else "_write"
    )
    damaged = False

    def damage_record():
        nonlocal damaged
        if damaged:
            return
        damaged = True
        if damage == "deleted":
            path.unlink()
        elif damage == "truncated":
            path.write_text('{"schema_version":2,')
        elif damage == "non_object":
            path.write_text("[]")
        elif damage == "recursion":
            path.write_text("[" * 100000)
        elif damage == "missing_nonce":
            path.write_text("{}")
        else:
            path.write_text(json.dumps(dict(record, run_nonce="replacement")))

    def interrupted(*args, **kwargs):
        if phase == "terminal_write":
            if args[1].get("status") not in store.ACTIVE:
                damage_record()
            return original(*args, **kwargs)
        result = original(*args, **kwargs)
        damage_record()
        return result

    monkeypatch.setattr(worker if phase == "provider_exit" else store,
                        "communicate" if phase == "provider_exit" else (
                            "begin_settlement" if phase == "settlement_cutoff" else "_write"), interrupted)
    reaped = []
    monkeypatch.setattr(worker, "_reap_settled", lambda *_args, **_kwargs: reaped.append(True))
    previous_term, previous_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    if damage == "replaced_nonce":
        with pytest.raises(store.TaskRecordError, match="TASK_RECORD_REPLACED"):
            worker.supervise(path, {}, 10)
        assert json.loads(path.read_text())["run_nonce"] == "replacement"
    else:
        worker.supervise(path, {}, 10)
        terminal = store.read("damage-probe", record["run_nonce"])
        assert terminal["status"] == "crashed"
        assert terminal["error"] == "TASK_RECORD_DAMAGED"
        assert terminal["finished_at"]
        assert terminal["worktree_disposition_reason"] == "task_record_damaged"
        assert worker._identity(terminal["provider_pgid"]) is None
    assert damaged
    assert reaped == []
    assert signal.getsignal(signal.SIGTERM) is previous_term
    assert signal.getsignal(signal.SIGINT) is previous_int


def test_failed_record_recovery_types_os_errors_and_keeps_a_removed_tree(tmp_path, monkeypatch):
    """Defect class: an OSError subclass escaping without a typed outcome, or a removed disposition overwritten."""
    record = dict(schema_version=2, task_id="probe", run_nonce="n1", status="running")
    unwritable = tmp_path / "unwritable.json"
    unwritable.mkdir()
    with pytest.raises(store.TaskRecordError, match=r"^TASK_RECORD_IO_ERROR: IsADirectoryError$"):
        store.publish_failed_record(dict(record), unwritable)
    unreadable = tmp_path / "unreadable.json"
    unreadable.write_text("{")
    read_text = type(unreadable).read_text

    def denied(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("test denial")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(unreadable), "read_text", denied)
    with pytest.raises(store.TaskRecordError, match=r"^TASK_RECORD_IO_ERROR: PermissionError$"):
        store.publish_failed_record(dict(record), unreadable)
    assert read_text(unreadable) == "{"
    reaped = tmp_path / "probe.json"
    reaped.write_text('{"schema_version":2,')
    store.publish_failed_record(dict(record, worktree_disposition="removed", worktree_disposition_reason="merged"), reaped)
    terminal = json.loads(reaped.read_text())
    assert (terminal["status"], terminal["error"]) == ("crashed", "TASK_RECORD_DAMAGED")
    assert (terminal["worktree_disposition"], terminal["worktree_disposition_reason"]) == ("removed", "merged")


REPLY = '{"verdict": "REQUEST_CHANGES", "findings": [{"id": "F1"}]}\n'


def _review_of_takeover(tmp_path, monkeypatch, *, review_branch="codex/inherited", review_pr=None):
    """A running takeover author (publish branch unlike its worktree branch) and its done review at its head."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.delenv(worker.write_boundary.BROKER_ENV, raising=False)
    (tmp_path / "tasks").mkdir()
    tree = tmp_path / "author-tree"
    tree.mkdir()
    run_git(tree, "init", "-b", "codex/author")
    run_git(tree, "-c", "user.email=s@example.invalid", "-c", "user.name=S", "commit", "--allow-empty", "-m", "head")
    head = run_git(tree, "rev-parse", "HEAD")
    common = dict(schema_version=2, repository_id="org/repo")
    for task_id, record in {
        "author": dict(status="running", run_nonce="p1", worktree_branch="codex/author",
                       publish_branch="codex/inherited", worktree_path=str(tree)),
        "stranger": dict(status="running", run_nonce="s1", worktree_branch="codex/inherited",
                         worktree_path=str(tree)),
        "author-review": dict(status="done", run_nonce="r1", parent_task_id="author", mode="read-only",
                              review_branch=review_branch, pinned_head=head, pr=review_pr,
                              result_file=str(tmp_path / "tasks/author-review.result"),
                              result_sha256=hashlib.sha256(REPLY.encode()).hexdigest()),
    }.items():
        (tmp_path / "tasks" / f"{task_id}.json").write_text(json.dumps(dict(common, task_id=task_id, **record)))
    (tmp_path / "tasks/author-review.result").write_text(REPLY)
    return tree


@pytest.mark.parametrize("review_branch", ["codex/inherited", "codex/author"])
def test_takeover_author_reads_its_own_exact_head_review_reply(tmp_path, monkeypatch, review_branch):
    """Defect: a takeover worker is refused its sealed reply when the review is on its new branch."""
    _review_of_takeover(tmp_path, monkeypatch, review_branch=review_branch)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "author")
    record, code = worker.wait("author-review", "r1", reply=True)
    assert (code, record.get("result"), record.get("result_unavailable")) == (0, REPLY, None)
    # Without --reply the output stays an exact copy of the record (saved wait output, task_record_store).
    assert worker.wait("author-review", "r1")[0] == json.loads((tmp_path / "tasks/author-review.json").read_text())


def test_takeover_author_gets_no_review_reply_for_another_branch(tmp_path, monkeypatch):
    """A matching head cannot authorize a reply on a branch outside the parent's recorded branches."""
    _review_of_takeover(tmp_path, monkeypatch, review_branch="codex/other")
    monkeypatch.setenv(ownership.TASK_ID_ENV, "author")
    record, _code = worker.wait("author-review", "r1", reply=True)
    assert ("result" in record, record["result_unavailable"]) == (False, "review_not_of_parent_branch")


def test_unrelated_worker_gets_no_review_reply(tmp_path, monkeypatch):
    """Control: a running task that did not dispatch the review is refused its reply, even on the same branch."""
    _review_of_takeover(tmp_path, monkeypatch)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "stranger")
    record, _code = worker.wait("author-review", "r1", reply=True)
    assert ("result" in record, record["result_unavailable"]) == (False, "caller_not_parent")


def test_stale_nonce_or_moved_head_gets_no_review_reply(tmp_path, monkeypatch):
    """Control: the parent's wait under another run's nonce, or after its head moved, is refused the reply."""
    tree = _review_of_takeover(tmp_path, monkeypatch)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "author")
    with pytest.raises(ValueError, match="run nonce mismatch"):
        worker.wait("author-review", "stale", reply=True)
    run_git(tree, "-c", "user.email=s@example.invalid", "-c", "user.name=S", "commit", "--allow-empty", "-m", "fix")
    record, _code = worker.wait("author-review", "r1", reply=True)
    assert ("result" in record, record["result_unavailable"]) == (False, "head_moved")


def test_unborn_head_with_revision_named_path_gets_no_review_reply(tmp_path, monkeypatch):
    """Defect: an unborn HEAD with a path named HEAD^{commit} is reported as a moved head."""
    tree = _review_of_takeover(tmp_path, monkeypatch)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "author")
    run_git(tree, "update-ref", "-d", "HEAD")
    (tree / "HEAD^{commit}").touch()

    record, _code = worker.wait("author-review", "r1", reply=True)

    assert ("result" in record, record["result_unavailable"]) == (False, "parent_head_unavailable")


def _write_ownership_state(tmp_path: Path, handoffs: dict[str, dict]) -> Path:
    state_file = tmp_path / "pr_ownership" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "schema": 1,
        "last_tick": None,
        "episodes": {},
        "handoffs": handoffs,
        "history": [],
        "outbox": [],
        "heal_attempts": {},
    }))
    return state_file


def test_running_parent_reads_replacement_pr_reply_with_accepted_ownership(tmp_path, monkeypatch, capsys):
    """Defect class: PR branch replacement denies the running owner's sealed review reply."""
    tree = _review_of_takeover(tmp_path, monkeypatch, review_branch="codex/replacement", review_pr=1277)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "author")
    argv = ["wait", "author-review", "--run-nonce", "r1", "--reply"]

    assert cli.main(argv) == 0
    refused = json.loads(capsys.readouterr().out)
    assert ("result" in refused, refused["result_unavailable"]) == (False, "review_not_of_parent_branch")

    _write_ownership_state(tmp_path, {"org/repo#1277": {
        "successor": "author",
        "predecessor": None,
        "accepted_at": "2026-11-23T15:00:00+00:00",
        "head": run_git(tree, "rev-parse", "HEAD"),
        "custody": [],
        "rerun_allowance": "unspent",
    }})
    assert cli.main(argv) == 0
    record = json.loads(capsys.readouterr().out)
    assert (record.get("result"), record.get("result_unavailable")) == (REPLY, None)


def _review_with_pr_successor(
    tmp_path,
    monkeypatch,
    *,
    parent_status="timeout",
    archive_parent=False,
    review_pr=1277,
    review_branch="release/start-0.1.63",
    review_repo="org/repo",
    successor_status="running",
    successor_repo="org/repo",
    successor_publish_branch="release/start-0.1.63",
    successor_worktree_branch="codex/successor",
    write_handoff=True,
    handoff_repo="org/repo",
    handoff_pr=1277,
    handoff_entry=None,
):
    """A settled (or active) parent's PR review and a running takeover successor at its head."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.delenv(worker.write_boundary.BROKER_ENV, raising=False)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    tree = tmp_path / "successor-tree"
    tree.mkdir()
    run_git(tree, "init", "-b", successor_worktree_branch)
    run_git(tree, "-c", "user.email=s@example.invalid", "-c", "user.name=S", "commit", "--allow-empty", "-m", "head")
    head = run_git(tree, "rev-parse", "HEAD")

    parent_record = {
        "schema_version": 2,
        "task_id": "parent",
        "status": parent_status,
        "run_nonce": "p1",
        "repository_id": review_repo,
        "worktree_branch": "codex/parent",
        "worktree_path": str(tmp_path / "parent-tree"),
    }
    parent_dir = tasks / "archive" if archive_parent else tasks
    parent_dir.mkdir(exist_ok=True)
    (parent_dir / "parent.json").write_text(json.dumps(parent_record))

    successor_record: dict = {
        "schema_version": 2,
        "task_id": "successor",
        "status": successor_status,
        "run_nonce": "s1",
        "repository_id": successor_repo,
        "worktree_branch": successor_worktree_branch,
        "worktree_path": str(tree),
    }
    if successor_publish_branch is not None:
        successor_record["publish_branch"] = successor_publish_branch
    (tasks / "successor.json").write_text(json.dumps(successor_record))

    review_record: dict = {
        "schema_version": 2,
        "task_id": "pr-review",
        "status": "done",
        "run_nonce": "r1",
        "parent_task_id": "parent",
        "mode": "read-only",
        "repository_id": review_repo,
        "review_branch": review_branch,
        "pinned_head": head,
        "result_file": str(tasks / "pr-review.result"),
        "result_sha256": hashlib.sha256(REPLY.encode()).hexdigest(),
    }
    if review_pr is not None:
        review_record["pr"] = review_pr
    (tasks / "pr-review.json").write_text(json.dumps(review_record))
    (tasks / "pr-review.result").write_text(REPLY)

    if write_handoff:
        entry = handoff_entry if handoff_entry is not None else {
            "successor": "successor",
            "predecessor": None,
            "accepted_at": "2026-11-23T15:00:00+00:00",
            "head": head,
            "custody": [],
            "rerun_allowance": "unspent",
        }
        _write_ownership_state(tmp_path, {f"{handoff_repo.casefold()}#{handoff_pr}": entry})
    return tree, head


@pytest.mark.parametrize(("parent_status", "archive_parent", "successor_repo"), [
    ("timeout", False, "org/repo"),
    ("done", True, "Org/Repo"),
    ("no_deliverable", False, "org/repo"),
])
def test_settled_parent_pr_successor_reads_exact_head_review_reply(
    tmp_path, monkeypatch, parent_status, archive_parent, successor_repo
):
    """Defect: a settled parent's accepted PR successor is refused the sealed review reply."""
    _review_with_pr_successor(
        tmp_path, monkeypatch, parent_status=parent_status, archive_parent=archive_parent,
        successor_repo=successor_repo,
    )
    monkeypatch.setenv(ownership.TASK_ID_ENV, "successor")

    record, code = worker.wait("pr-review", "r1", reply=True)
    assert (code, record.get("result"), record.get("result_unavailable")) == (0, REPLY, None)
    # Without --reply the output stays an exact copy of the record.
    assert worker.wait("pr-review", "r1")[0] == json.loads((tmp_path / "tasks/pr-review.json").read_text())

    # Digest verification still applies on the successor path.
    (tmp_path / "tasks/pr-review.result").write_text("tampered\n")
    tampered, _code = worker.wait("pr-review", "r1", reply=True)
    assert "result" not in tampered
    assert tampered["result_unavailable"].startswith("result_unreadable: terminal_result_digest_mismatch")


@pytest.mark.parametrize("case", [
    "no_state",
    "other_pr",
    "other_repo",
    "other_successor",
    "decision_custody",
    "keeper_custody",
    "review_without_pr",
    "review_bool_pr",
])
def test_pr_successor_without_accepted_ownership_gets_no_review_reply(tmp_path, monkeypatch, case):
    """Control: a caller without accepted task ownership of the review's PR stays refused."""
    from lu_machinery.orchestration import pr_ownership

    kwargs: dict = {}
    if case == "no_state":
        kwargs["write_handoff"] = False
    elif case == "other_pr":
        kwargs["handoff_pr"] = 9999
    elif case == "other_repo":
        kwargs["handoff_repo"] = "org/other"
    elif case == "other_successor":
        kwargs["handoff_entry"] = {
            "successor": "someone-else",
            "predecessor": None,
            "accepted_at": "2026-11-23T15:00:00+00:00",
            "head": HEAD,
            "custody": [],
            "rerun_allowance": "unspent",
        }
    elif case == "decision_custody":
        kwargs["handoff_entry"] = {
            "successor": "successor",
            "predecessor": None,
            "accepted_at": "2026-11-23T15:00:00+00:00",
            "head": HEAD,
            "custody": [],
            "rerun_allowance": "unspent",
            "reason": "held for decision",
            "decision_owner": "successor",
            "pr_state": "OPEN",
        }
    elif case == "keeper_custody":
        digest = pr_ownership._keeper_custody_digest(
            repository="org/repo", pr=1277, base="main", head=HEAD, worker_task="parent",
            worker_nonce="p1", review_classification="trivial", verdict_receipt=None,
        )
        kwargs["handoff_entry"] = {
            "successor": pr_ownership.SENDER,
            "predecessor": "parent",
            "accepted_at": "2026-11-23T15:00:00+00:00",
            "acknowledged_at": "2026-11-23T15:00:00+00:00",
            "custody": list(pr_ownership.KEEPER_CUSTODY_COVERS),
            "custody_mode": "keeper",
            "keeper_state": "active",
            "repository": "org/repo",
            "pr": 1277,
            "base": "main",
            "head": HEAD,
            "worker_task": "parent",
            "worker_nonce": "p1",
            "review_classification": "trivial",
            "verdict_receipt": None,
            "custody_sha256": digest,
            "rerun_allowance": "unspent",
        }
    elif case == "review_without_pr":
        kwargs["review_pr"] = None
    elif case == "review_bool_pr":
        kwargs["review_pr"] = True
    _review_with_pr_successor(tmp_path, monkeypatch, **kwargs)
    monkeypatch.setenv(ownership.TASK_ID_ENV, pr_ownership.SENDER if case == "keeper_custody" else "successor")

    record, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in record, record["result_unavailable"]) == (False, "caller_not_parent")


def test_corrupt_ownership_state_fails_closed_for_pr_successor(tmp_path, monkeypatch):
    """Defect class: unreadable or malformed ownership state must fail closed with a typed reason."""
    _review_with_pr_successor(tmp_path, monkeypatch, write_handoff=False)
    state_file = tmp_path / "pr_ownership" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{corrupt")
    monkeypatch.setenv(ownership.TASK_ID_ENV, "successor")

    record, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in record, record["result_unavailable"]) == (False, "ownership_state_unavailable")


@pytest.mark.parametrize(("parent_status", "expected"), [
    ("running", "parent_still_running"),
    ("spawning", "parent_still_running"),
    ("corrupt_status", "parent_record_unavailable"),
])
def test_pr_successor_refused_while_parent_is_not_settled(tmp_path, monkeypatch, parent_status, expected):
    """Control: accepted PR ownership cannot read a review whose parent has not settled."""
    _review_with_pr_successor(tmp_path, monkeypatch, parent_status=parent_status)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "successor")

    record, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in record, record["result_unavailable"]) == (False, expected)


def test_pr_successor_refused_on_moved_head_branch_mismatch_or_bad_caller(tmp_path, monkeypatch):
    """Control: successor reply requires exact head, matching PR branch/repo, active caller and run nonce."""
    tree, _head = _review_with_pr_successor(tmp_path, monkeypatch)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "successor")

    # Omitted nonce refuses.
    no_nonce, _code = worker.wait("pr-review", None, reply=True)
    assert ("result" in no_nonce, no_nonce["result_unavailable"]) == (False, "run_nonce_required")

    # Wrong nonce raises before reply.
    with pytest.raises(ValueError, match="run nonce mismatch"):
        worker.wait("pr-review", "stale", reply=True)

    # Head moved refuses.
    run_git(tree, "-c", "user.email=s@example.invalid", "-c", "user.name=S", "commit", "--allow-empty", "-m", "fix")
    moved, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in moved, moved["result_unavailable"]) == (False, "head_moved")

    # Unborn HEAD refuses with successor_head_unavailable.
    run_git(tree, "update-ref", "-d", "HEAD")
    unborn, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in unborn, unborn["result_unavailable"]) == (False, "successor_head_unavailable")


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({"review_branch": "codex/successor"}, "review_not_of_successor_branch"),
    ({"successor_publish_branch": "", "successor_worktree_branch": "release/start-0.1.63"},
     "review_not_of_successor_branch"),
    ({"successor_repo": "org/other"}, "review_not_of_successor_branch"),
    ({"successor_status": "done"}, "successor_not_running"),
])
def test_pr_successor_refused_on_branch_repo_or_settled_caller(tmp_path, monkeypatch, kwargs, expected):
    """Control: review branch must match the successor's PR head branch, repo and active status."""
    _review_with_pr_successor(tmp_path, monkeypatch, **kwargs)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "successor")

    record, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in record, record["result_unavailable"]) == (False, expected)


def test_pr_successor_refused_when_parent_or_successor_record_missing(tmp_path, monkeypatch):
    """Control: missing parent or successor task records fail closed with typed reasons."""
    _review_with_pr_successor(tmp_path, monkeypatch)
    monkeypatch.setenv(ownership.TASK_ID_ENV, "successor")
    (tmp_path / "tasks/successor.json").unlink()
    missing_successor, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in missing_successor, missing_successor["result_unavailable"]) == (
        False, "successor_record_unavailable"
    )
    (tmp_path / "tasks/parent.json").unlink()
    missing_parent, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in missing_parent, missing_parent["result_unavailable"]) == (False, "parent_record_unavailable")


def test_review_record_repository_fields_can_straddle_transfer(monkeypatch):
    """A historical repository_id and current repository field are consistent for one fleet row."""
    moved = transferred_catalog(monkeypatch)
    assert worker._record_repo_id({
        "repository_id": moved.github, "repository": moved.alternate_github,
    }) == moved.github


def test_review_ownership_finds_historical_acceptance_by_current_slug(tmp_path, monkeypatch):
    """The review's current slug must find the ledger key stored under the old slug."""
    moved = transferred_catalog(monkeypatch)
    _review_with_pr_successor(tmp_path, monkeypatch, handoff_repo=moved.github)

    assert worker._review_pr_ownership_refusal(
        {"pr": 1277}, moved.alternate_github, "successor", not_owner="caller_not_parent",
    ) is None


def test_running_author_reads_exact_head_review_reply_across_transfer(tmp_path, monkeypatch):
    """A running author's review can name the current slug while the author record names the old slug."""
    moved = transferred_catalog(monkeypatch)
    _review_of_takeover(tmp_path, monkeypatch)
    record_path = tmp_path / "tasks" / "author-review.json"
    review = json.loads(record_path.read_text())
    record_path.write_text(json.dumps({**review, "repository_id": moved.alternate_github}))
    monkeypatch.setenv(ownership.TASK_ID_ENV, "author")

    record, code = worker.wait("author-review", "r1", reply=True)
    assert (code, record.get("result"), record.get("result_unavailable")) == (0, REPLY, None)


def test_pr_successor_reads_exact_head_review_reply_across_transfer(tmp_path, monkeypatch):
    """An accepted successor on the current slug can read the original exact-head review."""
    moved = transferred_catalog(monkeypatch)
    _review_with_pr_successor(
        tmp_path, monkeypatch, review_repo=moved.github, handoff_repo=moved.github,
        successor_repo=moved.alternate_github,
    )
    monkeypatch.setenv(ownership.TASK_ID_ENV, "successor")

    record, code = worker.wait("pr-review", "r1", reply=True)
    assert (code, record.get("result"), record.get("result_unavailable")) == (0, REPLY, None)


def test_review_reply_slug_readers_reject_kelvin_sign_lookalikes(tmp_path, monkeypatch):
    """Defect class: non-ASCII Kelvin-sign (U+212A) look-alike slugs fold into real repository IDs."""
    real_slug = "learn-ukrainian/machinery"
    kelvin_slug = "learn-u\u212arainian/machinery"

    # _record_repo_id rejects non-ASCII repository_id or repository before casefold.
    assert worker._record_repo_id({"repository_id": kelvin_slug, "repository": real_slug}) is None
    assert worker._record_repo_id({"repository_id": real_slug, "repository": kelvin_slug}) is None
    assert worker._record_repo_id({"repository_id": kelvin_slug}) is None
    assert worker._record_repo_id({"repository_id": real_slug, "repository": "Learn-Ukrainian/Machinery"}) == real_slug

    # _review_pr_ownership_refusal rejects non-ASCII repo_id and non-ASCII PR digits in handoff keys.
    _review_with_pr_successor(
        tmp_path,
        monkeypatch,
        review_repo=real_slug,
        handoff_repo=real_slug,
        successor_repo=kelvin_slug,
    )
    assert worker._review_pr_ownership_refusal(
        {"pr": 1277}, kelvin_slug, "successor", not_owner="caller_not_parent",
    ) == "caller_not_parent"
    assert worker._review_pr_ownership_refusal(
        {"pr": 1277}, "Learn-Ukrainian/Machinery", "successor", not_owner="caller_not_parent",
    ) is None
    _write_ownership_state(tmp_path, {
        f"{real_slug}#1277": {
            "successor": "successor",
            "predecessor": None,
            "accepted_at": "2026-11-23T15:00:00+00:00",
            "head": "a" * 40,
            "custody": [],
            "rerun_allowance": "unspent",
        },
        f"{real_slug}#٤٢": {
            "successor": "successor",
            "predecessor": None,
            "accepted_at": "2026-11-23T15:00:00+00:00",
            "head": "a" * 40,
            "custody": [],
            "rerun_allowance": "unspent",
        },
    })
    assert worker._review_pr_ownership_refusal(
        {"pr": 42}, real_slug, "successor", not_owner="caller_not_parent",
    ) == "caller_not_parent"

    # End-to-end wait(..., reply=True) refuses a successor whose repository_id uses a Kelvin sign.
    monkeypatch.setenv(ownership.TASK_ID_ENV, "successor")
    record, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in record, record["result_unavailable"]) == (False, "review_not_of_successor_branch")

    # Direct running-parent path also refuses matching non-ASCII repository_id or conflicting repository claims.
    tasks = tmp_path / "tasks"
    parent_rec = json.loads((tasks / "parent.json").read_text())
    review_rec = json.loads((tasks / "pr-review.json").read_text())
    parent_rec.update(status="running", worktree_path=str(tmp_path / "successor-tree"),
                      worktree_branch=review_rec["review_branch"], repository_id=kelvin_slug)
    review_rec.update(repository_id=kelvin_slug)
    (tasks / "parent.json").write_text(json.dumps(parent_rec))
    (tasks / "pr-review.json").write_text(json.dumps(review_rec))
    monkeypatch.setenv(ownership.TASK_ID_ENV, "parent")
    direct_kelvin, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in direct_kelvin, direct_kelvin["result_unavailable"]) == (False, "review_not_of_parent_branch")
    parent_rec.update(repository_id=real_slug, repository="learn-ukrainian/other")
    review_rec.update(repository_id=real_slug)
    (tasks / "parent.json").write_text(json.dumps(parent_rec))
    (tasks / "pr-review.json").write_text(json.dumps(review_rec))
    direct_conflict, _code = worker.wait("pr-review", "r1", reply=True)
    assert ("result" in direct_conflict, direct_conflict["result_unavailable"]) == (False, "review_not_of_parent_branch")


@pytest.mark.parametrize(
    ("fields", "path"),
    [
        ({"hard_timeout": float("nan")}, '["hard_timeout"]'),
        ({"usage": {"cost_usd": [1.0, float("inf")]}}, '["usage", "cost_usd", 1]'),
        ({"options": {float("-inf"): "x"}}, '["options", "-inf"]'),
    ],
)
def test_task_record_write_refuses_non_finite_numbers_and_names_the_field(tmp_path, fields, path):
    """Defect class (#1440): a task record is written with NaN or Infinity, which strict JSON readers refuse,
    or settlement of such a record (provider ``usage``) publishes no terminal failure."""
    target = tmp_path / "worker.json"
    record = {"task_id": "worker", "run_nonce": "nonce", "status": "failed"}
    store.replace(dict(record), target)
    before = target.read_bytes()
    with pytest.raises(store.TaskRecordError) as refused:
        store.replace({**record, **fields}, target)
    assert refused.value.code == f"TASK_RECORD_NON_FINITE: {path}"
    assert target.read_bytes() == before
    # Settlement's fallback publishes the terminal failure with the field cleared and named.
    store.publish_failed_record({**record, **fields, "status": "running"}, target)
    published = json.loads(target.read_text())
    assert (published["status"], published["error"], published[next(iter(fields))]) == (
        "crashed", f"TASK_RECORD_NON_FINITE: {path}", None)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["worker.json", "worker.json.lock"]


@pytest.mark.parametrize(
    ("flag", "value"),
    [("hard-timeout", "nan"), ("hard-timeout", "inf"), ("hard-timeout", "-inf"), ("hard-timeout", "1e400"),
     ("max-tokens", "inf"), ("max-budget-usd", "nan")],
)
def test_dispatch_refuses_a_non_finite_numeric_option_at_parse_time(flag, value, capsys):
    """Defect class (#1440): ``--hard-timeout nan`` or ``inf`` reaches the task record as a non-finite number."""
    argv = ["dispatch", "--agent", "codex", "--model", "m", "--prompt", "p"]
    parsed = cli.build_parser().parse_args([*argv, "--hard-timeout=7200.5", "--max-tokens=100", "--max-budget-usd=abc"])
    assert (parsed.hard_timeout, parsed.max_tokens, parsed.max_budget_usd) == (7200.5, "100", "abc")
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args([*argv, f"--{flag}={value}"])
    assert exited.value.code == 2
    assert f"--{flag}: must be a finite number" in capsys.readouterr().err
    if flag == "max-budget-usd":  # the orchestrator wrapper parses the same flag before it dispatches
        from lu_machinery.orchestration import orchestrator_control

        with pytest.raises(SystemExit) as exited:
            orchestrator_control.build_parser().parse_args(["dispatch", "--task-id", "t", f"--{flag}={value}"])
        assert exited.value.code == 2
        assert f"--{flag}: must be a finite number" in capsys.readouterr().err


pytestmark = pytest.mark.usefixtures("configured_roots")
