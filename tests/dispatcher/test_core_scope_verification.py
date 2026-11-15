"""Scope-verification failure, private ownership git and merge-resolution containment."""



import json
import subprocess
from pathlib import Path
import pytest
from lu_machinery.dispatcher import git, worker
from tests.dispatcher.conftest import run_git as run_git
from tests.helpers.product_tree import configured_roots as configured_roots
import concurrent.futures
import copy
import sys
import time
from lu_machinery.dispatcher import cli
from lu_machinery.dispatcher import ownership
from lu_machinery.dispatcher import store
from tests.dispatcher.conftest import MERGED as MERGED

@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "timeout", "crashed"])
@pytest.mark.parametrize("command", ["fetch", "merge-base", "version", "rev-list", "log"])
def test_scope_verification_failure_only_rejects_success(delivery_settle, monkeypatch, status, command):
    record, settle = delivery_settle
    record = settle("pushed")
    record.update(status=status, error="original cause")
    original = git._verification_git
    def broken(path, *args, **kwargs):
        if args[0] == command:
            raise subprocess.CalledProcessError(1, ["git", command])
        return original(path, *args, **kwargs)
    monkeypatch.setattr(git, "_verification_git", broken)
    failures = []
    original_cause = worker._verification_cause

    def diagnostic_cause(exc):
        origins = []
        frame = exc.__traceback__
        while frame is not None:
            origins.append((Path(frame.tb_frame.f_code.co_filename).name,
                            frame.tb_frame.f_code.co_name, frame.tb_lineno))
            frame = frame.tb_next
        failures.append({"type": type(exc).__name__, "origins": origins,
                         "filename": getattr(exc, "filename", None),
                         "filename2": getattr(exc, "filename2", None)})
        return original_cause(exc)

    monkeypatch.setattr(worker, "_verification_cause", diagnostic_cause)
    worker._enforce_delivery(record)
    assert record["status"] == ("no_deliverable" if status == "done" else status), failures
    assert record["error"] == (f"owned_paths_verification_failed: git {command} failed" if status == "done" else "original cause"), failures


def test_private_ownership_git_does_not_start_background_maintenance(delivery_settle, monkeypatch, tmp_path):
    """Defect class: a disposable Git namespace starts writers that outlive its fetch."""
    _, settle = delivery_settle
    record = settle("pushed")
    trace = tmp_path / "verification-trace.json"
    original_run = subprocess.run

    def traced_run(argv, *args, **kwargs):
        if argv[0] == git.GIT_BINARY and any(str(arg).startswith("--git-dir=") for arg in argv):
            kwargs["env"] = {**kwargs["env"], "GIT_TRACE2_EVENT": str(trace)}
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", traced_run)
    with git.ownership_git(record) as run:
        assert run("rev-parse", "refs/heads/delivery") == record["head_sha"]
    complete, _, _ = trace.read_text().rpartition("\n")
    events = [json.loads(line) for line in complete.splitlines()]
    assert any(event.get("event") == "start" and "fetch" in event.get("argv", []) for event in events)
    maintenance = [event["argv"] for event in events
                   if event.get("event") == "child_start" and "maintenance" in event.get("argv", [])]
    assert maintenance == []


@pytest.mark.parametrize("history", ["conflict", "no-commit", "unrelated", "parentless", "clean"])
def test_merge_resolution_and_root_paths_cannot_escape_scope(sandbox, delivery_settle, history, monkeypatch):
    record, settle = delivery_settle
    tree = Path(record["cwd"])
    branch = record["worktree_branch"]
    if history in {"unrelated", "parentless"}:
        run_git(tree, "checkout", "--orphan", "new-root")
        run_git(tree, "read-tree", "--empty")
        (tree / "outside.txt").write_text("root addition\n")
        run_git(tree, "add", "outside.txt")
        run_git(tree, "commit", "-m", "parentless outside addition")
        if history == "unrelated":
            run_git(tree, "checkout", branch)
            run_git(tree, "merge", "--allow-unrelated-histories", "--no-edit", "new-root")
        else:
            # Keep the parentless commit as HEAD's first-parent ancestor.
            run_git(tree, "merge", "--allow-unrelated-histories", "--no-edit", branch)
            run_git(tree, "branch", "-f", branch, "HEAD")
            run_git(tree, "checkout", branch)
    else:
        (sandbox.repo / "outside.txt").write_text("upstream\n")
        run_git(sandbox.repo, "add", "outside.txt")
        run_git(sandbox.repo, "commit", "-m", "upstream outside path")
        if history == "conflict":
            (tree / "outside.txt").write_text("worker conflict\n")
            run_git(tree, "add", "outside.txt")
            run_git(tree, "commit", "-m", "worker side")
            # This models a commit already present at dispatch. Only the merge is new.
            record["worktree_base_sha"] = run_git(tree, "rev-parse", "HEAD")
        else:
            run_git(tree, "commit", "--allow-empty", "-m", "worker side")
        merge = subprocess.run(["git", "-C", str(tree), "merge", "--no-commit", "main"],
                               capture_output=True, text=True, timeout=30)
        assert merge.returncode == (1 if history == "conflict" else 0)
        if history != "clean":
            (tree / "outside.txt").write_text("worker merge edit\n")
            run_git(tree, "add", "outside.txt")
        run_git(tree, "commit", "--no-edit")
    run_git(sandbox.repo, "push", "origin", "main:release")
    # A new owned commit makes the supervisor exercise delivery after the merge.
    result = settle("pushed")
    assert result["status"] == ("done" if history == "clean" else "no_deliverable")
    assert result["error"] == (None if history == "clean" else "outside_owned_paths: outside.txt")
    original = git._verification_git
    monkeypatch.setattr(git, "_verification_git", lambda path, command, *args, **kw:
                        "git version 2.35.0" if command == "version" else original(path, command, *args, **kw))
    result.update(status="done", error=None)
    worker._enforce_delivery(result)
    assert (result["status"], result["error"]) == ("no_deliverable", "owned_paths_verification_failed: merge resolution paths cannot be verified")


@pytest.fixture
def grant_task(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-b", "main")
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.delenv(ownership.TASK_ID_ENV, raising=False)
    monkeypatch.setenv("SESSION_HANDOFF_AGENT", "coordinator")
    record = dict(schema_version=2, task_id="worker", run_nonce="nonce", status="running",
                  mode="workspace-write", worktree_path=str(root), repository_id="org/repo",
                  owned_paths=["original.txt"], options={})
    store.record_path("worker").parent.mkdir()
    store.replace(record)
    return record


def grant(record, *paths, nonce=None):
    return store.grant_paths(record["task_id"], nonce or record["run_nonce"], list(paths),
                             "Approved fixture change", "message-approval")


class TestSupervisorWritePreservesGrant:
    @pytest.mark.usefixtures('configured_roots')
    def test_stale_writes_retries_and_nonce(self, grant_task):
        """A stale supervisor or a concurrent replacement must not wipe an issued grant."""
        stale = copy.deepcopy(grant_task)
        first = grant(grant_task, "extra.txt")
        assert first["grant"]["granted_by"] == "coordinator"
        assert first["grant"]["granted_by_authenticated"] is False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(store.replace, copy.deepcopy(stale)),
                       pool.submit(grant, grant_task, "second.txt")]
            for result in results:
                result.result(timeout=5)
        with store.task_state_lock(store.record_path("worker")):
            store.replace(stale)  # the already-locked settlement path must not deadlock
        assert grant(grant_task, "extra.txt")["grant_id"] == first["grant_id"]
        current = store.read("worker")
        assert current["owned_paths"] == ["original.txt"]
        assert len(current["owned_path_grants"]) == 2
        assert ownership.effective_scope(current) == ["original.txt", "extra.txt", "second.txt"]
        before = store.record_path("worker").read_bytes()
        with pytest.raises(ValueError, match="nonce"):
            grant(grant_task, "third.txt", nonce="stale")
        with pytest.raises(ValueError, match="nonce"):
            store.replace(dict(stale, run_nonce="stale"))
        assert store.record_path("worker").read_bytes() == before


class TestSettlementClosesGrants:
    @pytest.mark.usefixtures('configured_roots')
    @pytest.mark.parametrize("status", ["running", "done", "cancelled"])
    def test_closed_grant_has_no_partial_mutation(self, grant_task, status, capsys):
        """A grant arriving after provider exit or terminal publication is refused."""
        grant(grant_task, "extra.txt")
        store.begin_settlement(grant_task)
        grant_task["status"] = status
        store.replace(grant_task)
        before = store.record_path("worker").read_bytes()
        assert cli.main(["grant-paths", "worker", "--run-nonce", "nonce", "--owned-path", "late.txt",
                         "--reason", "approved", "--approval-ref", "message-id"]) == 2
        assert "not settling" in capsys.readouterr().err
        assert store.record_path("worker").read_bytes() == before


class TestHooksAndSettlementShareScope:
    @pytest.mark.usefixtures('configured_roots')
    def test_paused_task_resumes_in_same_worktree_and_wait_reports_usage(self, sandbox):
        """The same running task accepts a grant at both hook stages and settlement only."""
        sandbox.env.pop(ownership.TASK_ID_ENV, None)
        sandbox.env["PYTHONDONTWRITEBYTECODE"] = "1"
        task_id, nonce = task = sandbox.dispatch("blocked", "--mode", "workspace-write",
                                               "--owned-path", "original.txt")
        path = sandbox.tasks / f"{task_id}.json"
        deadline = time.monotonic() + 10
        initial = json.loads(path.read_text())
        # Provider startup does not imply the supervisor has published its process group.
        while not initial.get("provider_pgid"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
            initial = json.loads(path.read_text())
        tree = Path(initial["worktree_path"])
        task_env = {**sandbox.env, ownership.TASK_ID_ENV: task_id}

        def hook(stage, updates=None):
            return subprocess.run([sys.executable, "-m", "lu_machinery.dispatcher.ownership", "--worktree", str(tree), stage],
                                  cwd=tree, env=task_env, input=updates, capture_output=True, text=True, timeout=30)

        (tree / "extra.txt").write_text("approved change\n")
        run_git(tree, "add", "extra.txt")
        assert hook("staged").returncode == 1
        args = ["grant-paths", task_id, "--run-nonce", nonce, "--owned-path", "extra.txt",
                "--reason", "Approved fixture change", "--approval-ref", "message-approval"]
        receipt = json.loads(sandbox.cli(*args).stdout)
        assert receipt["effective_scope"] == ["original.txt", "extra.txt"]
        assert json.loads(sandbox.cli(*args).stdout)["grant_id"] == receipt["grant_id"]
        assert hook("staged").returncode == 0
        (tree / "second.txt").write_text("unapproved change\n")
        run_git(tree, "add", "second.txt")
        refused = hook("staged")
        assert refused.returncode == 1 and "second.txt" in refused.stderr
        run_git(tree, "reset", "--", "second.txt")
        (tree / "second.txt").unlink()
        run_git(tree, "commit", "-m", "approved extra path")
        head = run_git(tree, "rev-parse", "HEAD")
        updates = f"refs/heads/task {head} refs/heads/task {'0' * 40}\n"
        assert hook("push", updates).returncode == 0
        run_git(tree, "push", "origin", "HEAD")
        (tree / "second.txt").write_text("unapproved change\n")
        run_git(tree, "add", "second.txt")
        run_git(tree, "commit", "-m", "second path")
        refused = hook("push", f"refs/heads/task {run_git(tree, 'rev-parse', 'HEAD')} refs/heads/task {'0' * 40}\n")
        assert refused.returncode == 1 and "second.txt" in refused.stderr
        run_git(tree, "push", "origin", "HEAD")
        record = dict(json.loads(path.read_text()), status="done", error=None,
                      worktree_dirty_on_exit=False, commits_ahead=2, push_state="pushed",
                      head_sha=run_git(tree, "rev-parse", "HEAD"))
        worker._enforce_delivery(record)
        assert (record["status"], record["error"]) == ("no_deliverable", "outside_owned_paths: second.txt")
        run_git(tree, "revert", "--no-edit", "HEAD")
        # A revert still counts as outside scope; remove the unapproved local history before release.
        run_git(tree, "reset", "--hard", head)
        # A disposable remote permits this test-only restoration; no production branch is involved.
        run_git(tree, "push", "--force-with-lease", "origin", "HEAD")
        assert run_git(tree, "status", "--porcelain") == ""
        Path(initial["prompt_file"]).with_suffix(".release").touch()
        result = sandbox.settle(task)
        assert result["task_id"] == task_id and result["run_nonce"] == nonce
        assert result["worktree_path"] == initial["worktree_path"] and result["provider_pgid"] == initial["provider_pgid"]
        assert result["owned_path_grants_used"] == [{"path": "extra.txt", "grant_id": receipt["grant_id"]}]
        assert result["owned_path_grants"] == [receipt["grant"]]
        assert result["settling_at"]


class TestSelfAndDescendantRefusal:
    @pytest.mark.usefixtures('configured_roots')
    @pytest.mark.parametrize("caller", ["worker", "child", "grandchild"])
    def test_ancestry_guard(self, grant_task, monkeypatch, caller):
        """A worker cannot approve its own scope, directly or through a child task."""
        store.replace(dict(grant_task, task_id="child", parent_task_id="worker"))
        store.replace(dict(grant_task, task_id="grandchild", parent_task_id="child"))
        monkeypatch.setenv(ownership.TASK_ID_ENV, caller)
        before = store.record_path("worker").read_bytes()
        with pytest.raises(ValueError, match="self or descendant"):
            grant(grant_task, "extra.txt")
        assert store.record_path("worker").read_bytes() == before


class TestSealedEnvelopeRefusal:
    @pytest.mark.usefixtures('configured_roots')
    @pytest.mark.parametrize("binding", [
        {"mode": "read-only"}, {"review_attempt": {}}, {"advisory_envelope": {}},
        {"advisory_binding_sha256": "bound"}, {"driver_brief": {}},
        {"research_task_family": "bounded_implementation"}, {"bounded": True},
        {"options": {"advisory_task": "advisor"}},
    ])
    def test_admission_cannot_be_widened(self, grant_task, binding):
        """A grant cannot mutate the scope sealed into an admission or review envelope."""
        store.replace(dict(grant_task, **binding))
        before = store.record_path("worker").read_bytes()
        with pytest.raises(ValueError, match="sealed"):
            grant(grant_task, "extra.txt")
        assert store.record_path("worker").read_bytes() == before


class TestGrantPathValidation:
    @pytest.mark.usefixtures('configured_roots')
    @pytest.mark.parametrize("bad", ["*", "tests/**", "tests/?.py", "../escape", "tests/../escape", ".", "/",
                                        "/absolute", "tests/./x", "tests/line\nfeed", "tests/zero\u200bwidth",
                                        "tests/line\u2028separator", "C:/escape", ":(top)escape", "escape/link"])
    def test_invalid_requests_are_atomic_and_brackets_are_literal(self, grant_task, bad, tmp_path):
        """Literal grant validation rejects escapes and sparse trees without interpreting brackets."""
        root = Path(grant_task["worktree_path"])
        (root / "escape").symlink_to(tmp_path, target_is_directory=True)
        before = store.record_path("worker").read_bytes()
        with pytest.raises(ValueError):
            grant(grant_task, "valid.txt", bad)
        assert store.record_path("worker").read_bytes() == before
        receipt = grant(grant_task, "tests/[id].py")
        assert ownership.outside_owned(["tests/[id].py", "tests/else.py"], receipt["effective_scope"]) == ["tests/else.py"]
        generated = grant(grant_task, "scripts/lexicon/fix.py")
        assert generated["owned_paths"] == ["scripts/lexicon/fix.py", "site/src/data/lexicon-manifest.fingerprint.json"]
        (root / "data").mkdir()
        (root / "data/excluded.txt").write_text("excluded\n")
        run_git(root, "add", "data")
        run_git(root, "-c", "user.name=Stub", "-c", "user.email=stub@example.invalid", "commit", "-m", "sparse input")
        run_git(root, "sparse-checkout", "set", "--no-cone", "/*", "!/data/")
        before = store.record_path("worker").read_bytes()
        with pytest.raises(ValueError, match="sparse-excluded"):
            grant(grant_task, "valid.txt", "data/new.txt")
        assert store.record_path("worker").read_bytes() == before


class TestMergedPRRecoveryUsesGrant:
    @pytest.mark.usefixtures('configured_roots')
    def test_merged_delivery_uses_effective_scope(self, merged_delivery_world):
        """Merged-PR recovery must honour a grant and still reject another ungranted path."""
        world = merged_delivery_world
        world.record["run_nonce"] = "nonce"
        world.record["owned_path_grants"] = [dict(task_id="merge-probe", run_nonce="nonce",
                                                      grant_id="grant", owned_paths=["extra.txt"])]
        world.accept(77)
        world.prs[77], world.files[77] = MERGED, ["extra.txt"]
        worker._enforce_delivery(world.record)
        assert (world.record["status"], world.record["error"]) == ("done", None)
        assert world.record["owned_path_grants_used"] == [{"path": "extra.txt", "grant_id": "grant"}]
        world.files[77] = ["extra.txt", "second.txt"]
        worker._enforce_delivery(world.record)
        assert (world.record["status"], world.record["error"]) == ("no_deliverable", "outside_owned_paths: second.txt")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("mutation", ["heartbeat", "settling", "terminal", "nonce"])
def test_validation_does_not_hold_lock_and_rechecks_settlement(grant_task, monkeypatch, mutation):
    """Slow grant validation cannot block supervision or grant after its cutoff."""
    import threading

    validating, release = threading.Event(), threading.Event()
    original = store._grant_paths_validated

    def validation(root, paths):
        validating.set()
        assert release.wait(5), "validation was never released"
        return original(root, paths)

    def supervisor_write():
        path = store.record_path("worker")
        with store.task_state_lock(path):
            current = store.read("worker")
            if mutation == "settling":
                store.begin_settlement(current)
            elif mutation == "nonce":
                current["run_nonce"] = "replacement"
                path.write_text(json.dumps(current))
            else:
                current.update(heartbeat="fresh")
                if mutation == "terminal":
                    current["status"] = "done"
                store.replace(current)

    monkeypatch.setattr(store, "_grant_paths_validated", validation)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(grant, grant_task, "extra.txt")
        try:
            assert validating.wait(5)
            pool.submit(supervisor_write).result(timeout=5)
        finally:
            release.set()
        if mutation == "heartbeat":
            assert request.result(timeout=5)["effective_scope"] == ["original.txt", "extra.txt"]
            assert store.read("worker")["heartbeat"] == "fresh"
        else:
            with pytest.raises(ValueError, match=r"nonce|not settling"):
                request.result(timeout=5)
            assert "owned_path_grants" not in store.read("worker")

pytestmark = [pytest.mark.usefixtures('configured_roots')]
