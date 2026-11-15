"""Approved coverage for retention-hold clearing and cleanup inventory agreement."""



import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from lu_machinery.dispatcher import cli, git, store
from lu_machinery.orchestration import scheduled_worktree_cleanup as cleanup
from tests.helpers.product_tree import configured_roots as configured_roots
import concurrent.futures
import os
import shutil
import signal
import subprocess
import time
from lu_machinery.dispatcher import worker
from tests.dispatcher.conftest import await_file as await_file, in_process_repo as in_process_repo, local_branches as local_branches, run_git as run_git

def test_clear_invalid_retention_hold_removes_disposition(tmp_path, monkeypatch, capsys):
    """A cleared malformed hold must not leave a non-retry invalid disposition."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    path = tmp_path / "held.json"
    base = {"schema_version": 2, "task_id": "held", "run_nonce": "same-run"}
    path.write_text(json.dumps(base))
    assert cli.main(["retain-worktree", "held", "--clear"]) == 0
    never_held = json.loads(path.read_text())
    capsys.readouterr()
    path.write_text(json.dumps({
        **base,
        "worktree_retention_hold": "false",
        "worktree_disposition": "retained",
        "worktree_disposition_reason": "retention_hold_invalid",
        "worktree_disposition_detail": "worktree_retention_hold must be a boolean; got 'false'",
    }))

    assert cli.main(["retain-worktree", "held", "--clear"]) == 0

    assert json.loads(capsys.readouterr().out) == {"task_id": "held", "worktree_retention_hold": False}
    assert json.loads(path.read_text()) == never_held


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("case", ["absent", "false", "true", "null", "string", "zero", "unreadable", "stale"])
def test_inventory_uses_dispatcher_retention_classification(tmp_path, monkeypatch, archived, case):
    """Malformed, unreadable and stale holds must protect the same inventory trees."""
    root = tmp_path / "repo"
    tree = root / ".worktrees" / "held"
    tree.mkdir(parents=True)
    tasks = root / "batch_state" / "tasks"
    directory = tasks / "archive" if archived else tasks
    directory.mkdir(parents=True)
    # Inventory must use its control plane, rather than this unrelated default.
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "unrelated"))
    monkeypatch.setattr(cleanup.worktree_git, "control_plane_root", lambda _root: root)
    path = directory / "held.json"
    record = {
        "schema_version": 2, "task_id": "held", "status": "failed",
        "finished_at": (datetime.now(UTC) - timedelta(hours=7)).isoformat(),
        "worktree_path": str(tree),
    }
    values = {"false": False, "true": True, "null": None, "string": "false", "zero": 0, "stale": True}
    if case in values:
        record["worktree_retention_hold"] = values[case]
    path.write_text(json.dumps(record))
    before = path.read_bytes()
    stale = dict(record)
    if case == "stale":
        stale.pop("worktree_retention_hold")
        monkeypatch.setattr(cleanup, "read_task_record", lambda _path: dict(stale))
    if case == "unreadable":
        real_read = Path.read_text

        def read_text(self, *args, **kwargs):
            if self == path:
                raise PermissionError("synthetic record read refusal")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
    inspected = []

    def inspect(_tree, *args):
        inspected.append(args[0])
        return {"status": " M tracked.txt", "rev-list": "0", "rev-parse": "test/held"}[args[0]]

    monkeypatch.setattr(cleanup, "_inspect_terminal_git", inspect)
    report = cleanup.report_terminal_worktrees(root)
    expected_reason = {
        "absent": None, "false": None, "true": "retention_hold", "null": "retention_hold_invalid",
        "string": "retention_hold_invalid", "zero": "retention_hold_invalid",
        "unreadable": "task_record_damaged", "stale": "retention_hold",
    }[case]
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    if expected_reason is None:
        assert git._retention_held(stale) is False
        assert report["summary"] == {"candidate": 1}
        assert report["tasks"][0]["action"] == "report"
        assert inspected == ["status", "rev-list", "rev-parse"]
    else:
        disposition = git.settle_worktree(stale)
        assert disposition["worktree_disposition"] == "retained"
        assert disposition["worktree_disposition_reason"] == expected_reason
        assert report["summary"] == {"candidate": 0}
        if case == "unreadable":
            assert report["tasks"][0]["action"] == "error"
        else:
            assert report["tasks"] == []
        assert inspected == []
    assert path.read_bytes() == before
    assert tree.is_dir()
    assert cleanup.retention_hold_disposition is store.retention_hold_disposition


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("unique_output", [False, True])
def test_salvage_release_pins_remove_published_tracked_work_but_retain_unique_output(
        sandbox, monkeypatch, synthetic_opsec, unique_output):
    """Defect class: salvage ignores committed release pins and retains published tracked work."""
    from lu_machinery.common import github_client
    from lu_machinery.fleet.regenerable_output import committed_release_pins
    from tests.orchestration.test_worktree_output_chokepoint import pinned_checkout

    pinned_checkout((sandbox.repo, sandbox.repo, sandbox.tasks))
    run_git(sandbox.repo, "push", "origin", "main")
    assets = (sandbox.repo.parent / "release-store.json").read_text()
    monkeypatch.setattr(github_client, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, json.dumps({"assets": json.loads(assets)})))
    monkeypatch.setattr(git, "SALVAGE_IGNORED_LIMIT", 1)
    record = sandbox.settle(sandbox.dispatch("pushed-dirty", "--mode", "workspace-write",
                                            "--sparse-include", "site/src/data"), expected=1)
    tree = Path(record["worktree_path"])
    # Copy only the ignored release outputs; pointers and hydrators came from the committed base.
    for name in ("site/src/data/lexicon-manifest.json", "site/public/lexicon/practice-index.A1.json"):
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sandbox.repo / name, tree / name)
    run_git(tree, "add", "delivered.txt")
    published = run_git(tree, "commit-tree", run_git(tree, "write-tree"), "-p", "HEAD", "-m", "published PR head")
    run_git(tree, "push", "origin", f"{published}:refs/pull/12424/head")
    assert run_git(sandbox.remote, "show", f"{published}:delivered.txt") == "uncommitted delivery"
    assert run_git(tree, "status", "--porcelain") == "M  delivered.txt"
    if unique_output:
        (tree / "ignored").mkdir()
        (tree / "ignored/evidence.bin").write_bytes(b"unpublished evidence")

    in_process_repo(sandbox, monkeypatch)
    assert set(committed_release_pins(tree, set(run_git(tree, "ls-files").splitlines()))) == {
        "site/src/data/lexicon-manifest.json", "site/public/lexicon/practice-index.A1.json"}
    worker.settle_left_worktrees(salvage=True)
    settled = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    if unique_output:
        assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == (
            "retained", "large_ignored_output")
        assert (tree / "ignored/evidence.bin").read_bytes() == b"unpublished evidence"
        assert run_git(sandbox.remote, "for-each-ref", f"refs/heads/salvage/{record['task_id']}") == ""
    else:
        assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("removed", "salvaged")
        assert not tree.exists()


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_hourly_salvage_removes_a_dirty_tree_only_after_its_work_is_on_a_new_salvage_ref(
        sandbox, monkeypatch, synthetic_opsec):
    """#12043 AC-06a: a finished task's dirty tree stayed forever; a failed salvage push must still keep it.

    The synthetic matcher lets the push scan run where the private install is absent, so a refused
    push is still salvage_failed rather than a scan that could not start.
    """
    record = sandbox.settle(sandbox.dispatch("pushed-dirty", "--mode", "workspace-write"), expected=1)
    assert record["worktree_disposition_reason"] == "dirty"
    tree, ref = Path(record["worktree_path"]), f"refs/heads/salvage/{record['task_id']}"
    (tree / "notes.txt").write_text("untracked note\n")
    in_process_repo(sandbox, monkeypatch)
    verification_git = git._verification_git

    def refused_push(path, *args, **kwargs):
        if args[0] == "push":
            raise subprocess.CalledProcessError(1, ["git", "push"])
        return verification_git(path, *args, **kwargs)

    def settled():
        return json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())

    monkeypatch.setattr(git, "_verification_git", refused_push)
    worker.settle_left_worktrees(salvage=True)
    assert (settled()["worktree_disposition"], settled()["worktree_disposition_reason"]) == (
        "retained", "salvage_failed")
    assert tree.is_dir() and run_git(sandbox.remote, "for-each-ref", ref) == ""

    monkeypatch.setattr(git, "_verification_git", verification_git)
    worker.settle_left_worktrees(salvage=True)
    assert (settled()["worktree_disposition"], settled()["worktree_disposition_reason"]) == ("removed", "salvaged")
    assert not tree.exists()
    salvaged = run_git(sandbox.remote, "rev-parse", ref)
    assert run_git(sandbox.remote, "rev-parse", f"{salvaged}^") == record["head_sha"]
    assert run_git(sandbox.remote, "show", f"{salvaged}:delivered.txt") == "uncommitted delivery"
    assert run_git(sandbox.remote, "show", f"{salvaged}:notes.txt") == "untracked note"


def _salvage_ref(record):
    return f"refs/heads/salvage/{record['task_id']}"


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_salvage_keeps_a_tree_when_the_push_scan_refuses(sandbox, monkeypatch, synthetic_opsec):
    """Defect class: salvage pushes a range the OPSEC push scan would refuse because hooksPath is /dev/null."""
    from lu_machinery.orchestration.scheduled_worktree_cleanup import SAFETY_STOP_REASONS
    from tests.opsec_fixtures import TOKEN

    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    assert record["worktree_disposition_reason"] == "unpublished"
    tree, ref = Path(record["worktree_path"]), _salvage_ref(record)
    run_git(tree, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", f"note {TOKEN}")
    in_process_repo(sandbox, monkeypatch)

    def settled():
        return json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())

    worker.settle_left_worktrees(salvage=True)
    first = settled()
    assert (first["worktree_disposition"], first["worktree_disposition_reason"]) == ("retained", "opsec_refused")
    assert first["worktree_disposition_reason"] not in worker.RETRY_REASONS
    assert first["worktree_disposition_reason"] in SAFETY_STOP_REASONS
    assert first["worktree_disposition_detail"] == git.OPSEC_REFUSAL_DETAIL
    assert TOKEN not in json.dumps(first)
    assert tree.is_dir() and run_git(sandbox.remote, "for-each-ref", ref) == ""
    worker.settle_left_worktrees(salvage=True)
    assert settled()["worktree_disposition_reason"] == "opsec_refused"
    assert tree.is_dir() and run_git(sandbox.remote, "for-each-ref", ref) == ""


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_salvage_still_publishes_a_range_the_push_scan_accepts(sandbox, monkeypatch, synthetic_opsec):
    """Defect class: regression of clean public salvage. A range the push scan accepts is still published."""
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    assert record["worktree_disposition_reason"] == "unpublished"
    tree, ref = Path(record["worktree_path"]), _salvage_ref(record)
    head = run_git(tree, "rev-parse", "HEAD")
    in_process_repo(sandbox, monkeypatch)
    worker.settle_left_worktrees(salvage=True)
    settled = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("removed", "salvaged")
    assert not tree.exists()
    assert run_git(sandbox.remote, "rev-parse", ref) == head


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("salvage", [False, True])
@pytest.mark.parametrize("damage", ["missing", "unreadable", "json", "non-object", "encoding"])
def test_retention_refuses_unreadable_durable_record(tmp_path, monkeypatch, damage, salvage):
    """F3: a damaged durable record must not fall back to a stale unheld copy."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    tree = tmp_path / "tree"
    tree.mkdir()
    record = {"task_id": "retention-refusal", "worktree_path": str(tree), "worktree_retention_hold": False}
    path = tmp_path / "retention-refusal.json"
    if damage != "missing":
        path.write_bytes({"json": b"{", "non-object": b"[]", "encoding": b"\xff"}.get(
            damage, json.dumps(record).encode()))
    original_read = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == path and damage == "unreadable":
            raise PermissionError("record cannot be read")
        return original_read(self, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("retention refusal reached Git inspection, push or removal")

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(git, "_tree_changes", forbidden)
    monkeypatch.setattr(git, "_git", forbidden)
    monkeypatch.setattr(git, "_salvage", forbidden)
    result = git.settle_worktree(record, salvage=salvage)
    assert result == {
        "worktree_disposition": "retained",
        "worktree_disposition_reason": "task_record_damaged",
        "worktree_disposition_detail": "retention decision requires a readable on-disk task record",
    }
    assert tree.is_dir()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("salvage", [False, True])
@pytest.mark.parametrize("source", ["disk", "memory"])
@pytest.mark.parametrize("value", [None, "true", "false", 1, 0, [], {}, 1.0])
def test_retention_refuses_non_boolean_hold(tmp_path, monkeypatch, value, source, salvage):
    """F4: malformed holds must retain the tree and survive stale record writes."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    tree = tmp_path / "tree"
    tree.mkdir()
    record = {"task_id": "retention-refusal", "run_nonce": "same-run", "worktree_path": str(tree)}
    disk = dict(record)
    (disk if source == "disk" else record)["worktree_retention_hold"] = value
    path = tmp_path / "retention-refusal.json"
    path.write_text(json.dumps(disk))
    if source == "disk":
        store.replace(dict(record), path)
        assert json.loads(path.read_text())["worktree_retention_hold"] == value

    def forbidden(*args, **kwargs):
        pytest.fail("retention refusal reached Git inspection, push or removal")

    monkeypatch.setattr(git, "_tree_changes", forbidden)
    monkeypatch.setattr(git, "_git", forbidden)
    monkeypatch.setattr(git, "_salvage", forbidden)
    result = git.settle_worktree(record, salvage=salvage)
    assert result == {
        "worktree_disposition": "retained",
        "worktree_disposition_reason": "retention_hold_invalid",
        "worktree_disposition_detail": f"worktree_retention_hold must be a boolean; got {value!r}",
    }
    assert tree.is_dir()


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_retention_hold_blocks_settlement_salvage_and_cleanup(sandbox, monkeypatch, synthetic_opsec):
    """Defect class: an unpublished tree has no owner hold, so salvage retries it."""
    from lu_machinery.orchestration import scheduled_worktree_cleanup as cleanup

    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    assert record["worktree_disposition_reason"] == "unpublished"
    tree, ref = Path(record["worktree_path"]), _salvage_ref(record)
    sandbox.cli("retain-worktree", record["task_id"], "--set")
    held = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert held["worktree_retention_hold"] is True
    assert held["worktree_disposition_reason"] == "retention_hold"
    assert "retention_hold" not in worker.RETRY_REASONS
    in_process_repo(sandbox, monkeypatch)
    assert git.settle_worktree(held) == {
        "worktree_disposition": "retained", "worktree_disposition_reason": "retention_hold",
    }
    assert tree.is_dir() and run_git(sandbox.remote, "for-each-ref", ref) == ""
    worker.settle_left_worktrees(salvage=True)
    assert tree.is_dir() and run_git(sandbox.remote, "for-each-ref", ref) == ""
    (sandbox.repo / "batch_state").mkdir(exist_ok=True)
    (sandbox.repo / "batch_state/tasks").symlink_to(sandbox.tasks, target_is_directory=True)
    report = cleanup.settle_terminal_worktrees(sandbox.repo, apply=True, github=cleanup.GitHubReads())
    assert report["complete"] and report["error"] is None
    assert tree.is_dir() and run_git(sandbox.remote, "for-each-ref", ref) == ""
    listed = report["trees"][0]
    assert listed["reason"] == "retention_hold" and listed["safety_stop"] is False
    sandbox.cli("retain-worktree", record["task_id"], "--clear")
    cleared = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert "worktree_retention_hold" not in cleared
    assert "worktree_disposition" not in cleared
    cleared["worktree_retention_hold"] = False
    (sandbox.tasks / f"{record['task_id']}.json").write_text(json.dumps(cleared))
    worker.settle_left_worktrees(salvage=True)
    settled = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("removed", "salvaged")
    assert not tree.exists()
    assert run_git(sandbox.remote, "rev-parse", ref)


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_salvage_uses_the_trusted_linter_when_the_worktree_copy_exits_clean(sandbox, monkeypatch, synthetic_opsec):
    """Defect: a worktree can replace its blob linter with a script that exits 0 and salvage will push."""
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    tree, ref = Path(record["worktree_path"]), _salvage_ref(record)
    (tree / "scripts/audit/lint_opsec_leaks.py").write_text("raise SystemExit(0)\n")
    (tree / "notes.txt").write_text(".".join(("10", "1", "2", "3")) + "\n")
    in_process_repo(sandbox, monkeypatch)
    worker.settle_left_worktrees(salvage=True)
    settled = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("retained", "opsec_refused")
    assert tree.is_dir() and run_git(sandbox.remote, "for-each-ref", ref) == ""


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_retention_hold_survives_a_stale_record_write_and_skips_inspection(sandbox, monkeypatch):
    """Defect: a supervisor write from an older copy drops the hold, and the old-tree report still reads Git."""
    from datetime import UTC, datetime, timedelta

    from lu_machinery.orchestration import scheduled_worktree_cleanup as cleanup

    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    tree = Path(record["worktree_path"])
    sandbox.cli("retain-worktree", record["task_id"], "--set")
    path = sandbox.tasks / f"{record['task_id']}.json"
    stale = json.loads(path.read_text())
    stale.pop("worktree_retention_hold")
    stale["worktree_disposition"] = "removed"
    stale["worktree_disposition_reason"] = "clean"
    in_process_repo(sandbox, monkeypatch)
    store.replace(stale, path)
    kept = json.loads(path.read_text())
    assert kept["worktree_retention_hold"] is True
    assert kept["worktree_disposition_reason"] == "retention_hold"
    naked = dict(record)
    naked.pop("worktree_retention_hold", None)
    assert git.settle_worktree(naked)["worktree_disposition_reason"] == "retention_hold"
    assert tree.is_dir()
    kept["finished_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    path.write_text(json.dumps(kept))
    (sandbox.repo / "batch_state").mkdir(exist_ok=True)
    (sandbox.repo / "batch_state/tasks").symlink_to(sandbox.tasks, target_is_directory=True)
    monkeypatch.setattr(cleanup.worktree_git, "control_plane_root", lambda root: sandbox.repo)

    def refused(*args, **kwargs):
        raise AssertionError("held tree was inspected")

    monkeypatch.setattr(cleanup, "_inspect_terminal_git", refused)
    report = cleanup.report_terminal_worktrees(sandbox.repo)
    assert report["tasks"] == []
    sandbox.cli("retain-worktree", record["task_id"], "--clear")
    cleared = json.loads(path.read_text())
    assert "worktree_retention_hold" not in cleared
    cleared["worktree_disposition"] = "removed"
    cleared["worktree_disposition_reason"] = "clean"
    store.replace(cleared, path)
    assert "worktree_retention_hold" not in json.loads(path.read_text())


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("pull_ref,disposition", [(True, ("removed", "clean")), (False, ("retained", "unpublished"))])
def test_recovery_removes_squash_merged_write_task_only_when_a_pull_ref_holds_its_head(sandbox, pull_ref, disposition):
    """CTO residual: after a squash merge and branch deletion, D3 never fired, so the worktree stayed forever."""
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    assert record["worktree_disposition_reason"] == "unpublished"
    tree, branch = Path(record["worktree_path"]), record["worktree_branch"]
    squash = run_git(tree, "commit-tree", "HEAD^{tree}", "-p", record["worktree_base_sha"], "-m", "squash (#7)")
    run_git(tree, "push", str(sandbox.remote), f"HEAD:refs/heads/{branch}", f"{squash}:refs/heads/main")
    if pull_ref:
        run_git(tree, "push", str(sandbox.remote), "HEAD:refs/pull/7/head")
    run_git(tree, "push", str(sandbox.remote), f":refs/heads/{branch}")
    sandbox.sweep()
    settled = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == disposition
    assert tree.exists() is not pull_ref
    assert (branch in local_branches(sandbox.repo)) is not pull_ref


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_continuation_records_sharing_a_landed_tree_resolve_to_the_creator_and_the_tree_is_removed(sandbox):
    """#12063 AC-06: continuations beside their creator left no single owner, so a landed tree stayed forever."""
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    tree, branch = Path(record["worktree_path"]), record["worktree_branch"]
    (tree / "unknown-output").mkdir()
    (tree / "unknown-output" / "artifact").write_text("ignored output")
    (sandbox.repo / ".git/info/exclude").write_text("/unknown-output/\n")
    squash = run_git(tree, "commit-tree", "HEAD^{tree}", "-p", record["worktree_base_sha"], "-m", "squash (#7)")
    run_git(tree, "push", str(sandbox.remote), f"HEAD:refs/heads/{branch}", f"{squash}:refs/heads/main")
    run_git(tree, "push", str(sandbox.remote), "HEAD:refs/pull/7/head")
    run_git(tree, "push", str(sandbox.remote), f":refs/heads/{branch}")
    # Dispatcher records written before the reuse flag and creation inventory existed.
    creator_file = sandbox.tasks / f"{record['task_id']}.json"
    creator = json.loads(creator_file.read_text())
    creator.pop("worktree_reused", None)
    creator.pop("ignored_output_baseline", None)
    creator_file.write_text(json.dumps(creator))
    for suffix in ("c", "d"):
        continuation = dict(creator, task_id=f"{record['task_id']}{suffix}", run_nonce=f"{suffix}" * 32,
                            worktree_disposition="retained", worktree_disposition_reason="borrowed_worktree",
                            options=dict(creator["options"], cwd=str(tree), task_id=f"{record['task_id']}{suffix}"))
        (sandbox.tasks / f"{continuation['task_id']}.json").write_text(json.dumps(continuation))
    sandbox.sweep()
    settled = json.loads(creator_file.read_text())
    assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("removed", "clean")
    assert not tree.exists()


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_crash_recovery_keeps_worktree_while_the_orphaned_provider_runs(sandbox):
    """Defect (F2): recovery removed a crashed task's worktree without proving its provider had exited."""
    task = sandbox.dispatch("blocked")
    provider = int(await_file(sandbox.tasks / f"{task[0]}.started"))
    # The provider can write its marker before Popen returns and the supervisor records its group; a
    # supervisor killed before that record leaves liveness unknown, which this test does not cover.
    deadline = time.monotonic() + 10
    while "provider_pgid" not in (record := json.loads((sandbox.tasks / f"{task[0]}.json").read_text())):
        assert time.monotonic() < deadline, "supervisor never recorded its provider"
        time.sleep(0.02)
    os.kill(record["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 10
    while worker._identity(record["pid"]) == record["process_start"]:
        assert time.monotonic() < deadline, "supervisor survived SIGKILL"
        time.sleep(0.02)
    sandbox.sweep()
    crashed = json.loads((sandbox.tasks / f"{task[0]}.json").read_text())
    assert crashed["status"] == "crashed"
    assert (crashed["worktree_disposition"], crashed["worktree_disposition_reason"]) == (
        "retained", "provider_processes_alive")
    assert Path(record["worktree_path"]).is_dir()
    os.killpg(provider, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while worker._provider_alive(crashed):
        assert time.monotonic() < deadline, "provider survived SIGKILL"
        time.sleep(0.02)
    sandbox.sweep()
    settled = json.loads((sandbox.tasks / f"{task[0]}.json").read_text())
    assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("removed", "clean")
    assert not Path(record["worktree_path"]).exists()


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("borrower_state", ["running", "crashed", "aliased"])
def test_settlement_keeps_a_worktree_that_a_live_task_borrowed(sandbox, borrower_state):
    """Defect (F3): a finished task removed its worktree while another task's provider worked there via --cwd.

    crashed: the borrower's supervisor died, so its record is terminal while its provider still runs.
    aliased: the borrower named the tree by another spelling (TREE/../TREE_NAME).
    """
    owner = sandbox.dispatch("blocked")
    await_file(sandbox.tasks / f"{owner[0]}.started")
    tree = Path(json.loads((sandbox.tasks / f"{owner[0]}.json").read_text())["worktree_path"])
    cwd = tree / ".." / tree.name if borrower_state == "aliased" else tree
    borrower = sandbox.dispatch("blocked", "--cwd", str(cwd))
    provider = int(await_file(sandbox.tasks / f"{borrower[0]}.started"))
    if borrower_state == "crashed":
        # The provider writes its marker before the supervisor records its group. Killing in that
        # window leaves liveness unknown, and the sweep keeps a borrowed tree until the group is
        # proven gone (_provider_alive is False, not merely falsy).
        deadline = time.monotonic() + 10
        while "provider_pgid" not in (
            supervisor := json.loads((sandbox.tasks / f"{borrower[0]}.json").read_text())
        ):
            assert time.monotonic() < deadline, "supervisor never recorded its provider"
            time.sleep(0.02)
        os.kill(supervisor["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while worker._identity(supervisor["pid"]) == supervisor["process_start"]:
            assert time.monotonic() < deadline, "supervisor survived SIGKILL"
            time.sleep(0.02)
        sandbox.sweep()  # scheduled recovery records the borrower as crashed
        assert json.loads((sandbox.tasks / f"{borrower[0]}.json").read_text())["status"] == "crashed"
    (sandbox.tasks / f"{owner[0]}.release").touch()
    record = sandbox.settle(owner)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == (
        "retained", "borrowed_by_live_task")
    assert tree.is_dir()
    if borrower_state == "crashed":
        os.killpg(provider, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while worker._provider_alive(json.loads((sandbox.tasks / f"{borrower[0]}.json").read_text())) is not False:
            assert time.monotonic() < deadline, "provider survived SIGKILL"
            time.sleep(0.02)
    else:
        (sandbox.tasks / f"{borrower[0]}.release").touch()
        assert sandbox.settle(borrower)["worktree_disposition_reason"] == "borrowed_worktree"
    sandbox.sweep()
    assert json.loads((sandbox.tasks / f"{owner[0]}.json").read_text())["worktree_disposition"] == "removed"
    assert not tree.exists()


@pytest.mark.usefixtures("salvage_product_linter")
@pytest.mark.usefixtures('configured_roots')
def test_wait_does_not_report_a_task_while_its_settlement_holds_the_lock(sandbox, monkeypatch):
    """Defect (F4): wait returned a terminal task while crash recovery was still removing its worktree."""
    task = sandbox.dispatch()
    sandbox.settle(task)
    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        with store.task_state_lock(store.record_path(task[0])):
            waiting = pool.submit(worker.wait, task[0], task[1], 15, 0.02)
            with pytest.raises(TimeoutError):
                waiting.result(timeout=0.5)
        assert waiting.result(timeout=15)[1] == 0


_SALVAGE_LINTER = '#!/usr/bin/env python3\n"""Lint repository files and git diffs for Operational Security (OPSEC) leaks.\n\nScans tracked files, staged files, or specified git ranges for sensitive\ninfrastructure metadata that MUST NOT appear in the public repository\n(learn-ukrainian/learn-ukrainian.github.io), public PRs, or public docs.\n\nForbidden items:\n1. Raw IPv4 addresses (excluding loopback 127.0.0.1, 0.0.0.0, RFC 5737 doc IPs, public DNS, section headers)\n2. Private Key headers (PKCS#1, PKCS#8, OPENSSH, RSA, EC, DSA, ENCRYPTED)\n3. Raw SSH password-less auth method disclosure\n\nSafe exclusions:\n- Loopback / Localhost (127.0.0.1, 0.0.0.0)\n- Standard Public DNS Resolvers (1.1.1.1, 1.0.0.1, 8.8.8.8, 8.8.4.4, 9.9.9.9, 4.2.2.2)\n- Standard RFC 5737 Documentation IPs (e.g. 203.0.113.7, 198.51.100.4)\n"""\n\nfrom __future__ import annotations\n\nimport argparse\nimport json\nimport os\nimport re\nimport subprocess\nimport sys\nimport unicodedata\nfrom contextlib import contextmanager\nfrom pathlib import Path\n\nREPO_ROOT = Path(__file__).resolve().parent.parent.parent\n\nDEFAULT_GIT_TIMEOUT_SECONDS: float = 30.0\n\n# Regex to match IPv4 addresses: X.X.X.X where each X is 1-3 digits\n_IPV4_RE = re.compile(r"\\b(?P<ip>(?:[0-9]{1,3}\\.){3}[0-9]{1,3})\\b")\n\n# Keywords indicating section / document hierarchy citations rather than network IP addresses (exact whole-word tokens)\n_SECTION_CITATION_KEYWORDS = {"§", "section", "standard_ref", "textbook_ref", "chapter", "paragraph", "ref:", "title:", "pages:", "version", "semver"}\n\n# Line heading context pattern (e.g. "### 4.1.3.1", "- 4.2.4.1", "1.2.3.4 Section Title", "| 4.1.3.1 |")\n_SECTION_LINE_RE = re.compile(r"^\\s*(?:[§#\\*|-]|\\d+\\.)\\s*(?:\\d+\\.){3}\\d+|\\|\\s*(?:\\d+\\.){3}\\d+\\s*\\|")\n\n# Safe allowlist of four-octet IPs (loopback, public DNS, standard testing)\n_SAFE_IP_ALLOWLIST = {\n    "127.0.0.1",\n    "127.0.1.1",\n    "0.0.0.0",\n    "1.1.1.1",\n    "1.0.0.1",\n    "8.8.8.8",\n    "8.8.4.4",\n    "9.9.9.9",\n    "4.2.2.2",\n}\n\n# Forbidden Private Key & SSH / Auth / Credential patterns (dynamically built to avoid self-triggering)\n_BEGIN_PRIV = "-----BEGIN " + "PRIVATE KEY-----"\n_PWLESS_SSH = "passwordless " + "SSH"\n\n_FORBIDDEN_PATTERNS = [\n    (re.compile(r"-----BEGIN\\s+(?:[A-Z0-9_-]+\\s+)?PRIVATE\\s+KEY-----", re.IGNORECASE), "Private Key Header (PKCS#1/PKCS#8/OpenSSH/RSA/EC/DSA/Encrypted)"),\n    (re.compile(r"passwordless\\s+SSH", re.IGNORECASE), "Raw SSH auth method disclosure"),\n]\n\n# Personal identifiers scrubbed from public learner-facing content.  Match the\n# name stems rather than only their nominative forms: Ukrainian declension can\n# produce Альони, Альоні, Альону, Альоною, or Альоно.  The leading word boundary\n# prevents an embedded substring such as батальона from being treated as a name.\n_SCRUBBED_PERSONAL_IDENTIFIER_TOKENS = ("alona", "альона")\n_PERSONAL_IDENTIFIER_PATTERNS = tuple(\n    (\n        token,\n        re.compile(\n            rf"(?<!\\w){re.escape(stem)}\\w*\\b",\n            re.IGNORECASE,\n        ),\n    )\n    for token, stem in (("alona", "alona"), ("альона", "альон"))\n)\n\n# This is deliberately exact rather than a broad directory exemption.  Tests\n# exercise the detector directly, so no public learner-facing file is allowed\n# to carry a regression marker.\n_PERSONAL_IDENTIFIER_PATH_ALLOWLIST: frozenset[str] = frozenset()\n\n_PUBLIC_PERSONAL_IDENTIFIER_ROOTS = (\n    # All files that may be shipped by the static site, including pages, their\n    # supporting source, and already-built public assets.\n    Path("site/src"),\n    Path("site/public"),\n    Path("docs"),\n)\n\n# Category D: Private tracker citations in public documentation and agent rules\n_PRIVATE_TRACKER_PATTERN = re.compile(\n    r"\\b(?:private|infra[-_]private)(?:[_\\s]+(?:operational|infrastructure|infra|dataset|task|qa|tracking|coordination|hramatka))*(?:[_\\s]+(?:issue|issues|tracker|repo|repository|ticket|tickets|board|hramatka)){0,2}[_\\s:=(\\[]*#[0-9]+\\b",\n    re.IGNORECASE,\n)\n\n_PUBLIC_AGENT_DOC_ROOTS = (\n    Path("agents_extensions"),\n    Path("docs/runbooks"),\n    Path("docs/best-practices"),\n    Path("docs/audits"),\n    Path(".claude"),\n    Path(".agent"),\n    Path(".gemini"),\n    Path(".codex"),\n)\n\n# File extensions to skip (strictly binary / generated / virtual environment files)\n_SKIP_EXTENSIONS = {\n    ".db", ".db-journal", ".sqlite", ".sqlite3", ".pyc", ".png", ".jpg", ".jpeg",\n    ".gif", ".webp", ".zip", ".tar", ".gz", ".7z", ".pdf", ".mp3", ".wav", ".ogg",\n    ".lock"\n}\n\n# Public content trees and test files scanned (F001 fix: preserve scripts/audit/ via filter_rel_paths)\n_SKIP_PATH_SUBSTRINGS = [\n    ".git/",\n    ".venv/",\n    "node_modules/",\n    "data/sources.db",\n    "data/vesum.db",\n    "wiki/.state/",\n    "docs/references/external/",\n    "requirements-lock.txt",\n    "audit/",\n    "_archive/",\n]\n\n\ndef is_rfc1918(p0: int, p1: int) -> bool:\n    """Return True if IP is in RFC 1918 private address ranges."""\n    if p0 == 10:\n        return True\n    if p0 == 172 and 16 <= p1 <= 31:\n        return True\n    return p0 == 192 and p1 == 168\n\n\ndef is_section_citation(line: str, filename: str, p0: int, p1: int, p2: int, p3: int) -> bool:\n    """Return True if a 4-part number is a document section citation, network base address, or semver string."""\n    # Network base address or major version block (e.g. 120.0.0.0, 131.0.0.0)\n    if p1 == 0 and p2 == 0 and p3 == 0:\n        return True\n\n    # State Standard section codes / semver strings / outline numbering (e.g. 4.1.3.1, 1.4.1.2)\n    if p0 <= 15 and p0 != 10 and p1 <= 30 and p2 <= 30 and p3 <= 30:\n        line_lower = line.lower()\n        if any(kw in line_lower for kw in _SECTION_CITATION_KEYWORDS):\n            return True\n        if _SECTION_LINE_RE.search(line):\n            return True\n        if "#" in line or \'"""\' in line or "\'\'\'" in line or "//" in line:\n            return True\n        if \'"\' in line or "\'" in line or "`" in line or ":" in line or "-" in line or "(" in line or "[" in line:\n            return True\n        if filename.endswith(".md") or filename.endswith(".mdx") or filename.endswith(".yaml") or filename.endswith(".yml") or filename.endswith(".txt"):\n            return True\n\n    return False\n\n\ndef is_public_personal_identifier_path(filename: str) -> bool:\n    """Return whether a path is a learner-facing tree protected from name leaks."""\n    normalized = Path(filename.replace("\\\\", "/"))\n    return any(normalized.is_relative_to(root) for root in _PUBLIC_PERSONAL_IDENTIFIER_ROOTS)\n\n\ndef check_personal_identifiers(content: str, filename: str) -> list[tuple[int, str, str]]:\n    """Return scrubbed personal-name findings in public learner-facing content only."""\n    normalized_filename = filename.replace("\\\\", "/")\n    if (\n        normalized_filename in _PERSONAL_IDENTIFIER_PATH_ALLOWLIST\n        or not is_public_personal_identifier_path(normalized_filename)\n    ):\n        return []\n\n    findings: list[tuple[int, str, str]] = []\n    for line_number, line in enumerate(unicodedata.normalize("NFC", content).splitlines(), 1):\n        for token, pattern in _PERSONAL_IDENTIFIER_PATTERNS:\n            if pattern.search(line):\n                findings.append((line_number, token, "Scrubbed personal identifier"))\n    return findings\n\n\ndef is_public_agent_doc_path(filename: str) -> bool:\n    """Return whether a path is public agent documentation or rule content."""\n    normalized = Path(filename.replace("\\\\", "/"))\n    return any(normalized.is_relative_to(root) for root in _PUBLIC_AGENT_DOC_ROOTS)\n\n\ndef check_private_tracker_citations(content: str, filename: str) -> list[tuple[int, str, str]]:\n    """Return private tracker citation leaks in public agent documentation and rules."""\n    normalized_filename = filename.replace("\\\\", "/")\n    if not is_public_agent_doc_path(normalized_filename):\n        return []\n\n    findings: list[tuple[int, str, str]] = []\n    lines = content.splitlines()\n    for idx, line in enumerate(lines, 1):\n        for match in _PRIVATE_TRACKER_PATTERN.finditer(line):\n            findings.append((idx, match.group(0), "Private tracker citation disclosure (Category D OPSEC leak)"))\n    return findings\n\n\ndef check_content(\n    content: str,\n    filename: str,\n    *,\n    include_infrastructure: bool = True,\n) -> list[tuple[int, str, str]]:\n    """Scan string content line by line for OPSEC leaks.\n\n    Returns list of (line_num, match_str, description).\n    """\n    findings: list[tuple[int, str, str]] = []\n    if not include_infrastructure:\n        return check_personal_identifiers(content, filename)\n\n    lines = content.splitlines()\n\n    is_linter_itself = filename.replace("\\\\", "/").endswith("lint_opsec_leaks.py")\n\n    for idx, line in enumerate(lines, 1):\n        # Sol F001 / Fable F003: Precise self-scanning exemption on linter\'s own pattern definition constants\n        if is_linter_itself and ("_FORBIDDEN_PATTERNS" in line or "_BEGIN_PRIV" in line or "_PWLESS_SSH" in line or "is_rfc1918" in line or "_SAFE_IP_ALLOWLIST" in line):\n            continue\n\n        # 1. Check for IPv4 addresses\n        for match in _IPV4_RE.finditer(line):\n            ip_str = match.group("ip")\n            if ip_str in _SAFE_IP_ALLOWLIST:\n                continue\n            parts = ip_str.split(".")\n            if all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):\n                p0, p1, p2, p3 = (int(x) for x in parts)\n\n                # RFC 1918 private IPs are ALWAYS flagged\n                if is_rfc1918(p0, p1):\n                    findings.append((idx, ip_str, "RFC 1918 Private IPv4 address"))\n                    continue\n\n                # Skip RFC 5737 documentation blocks (192.0.2.x, 198.51.100.x, 203.0.113.x)\n                if (p0 == 192 and p1 == 0 and p2 == 2) or \\\n                   (p0 == 198 and p1 == 51 and p2 == 100) or \\\n                   (p0 == 203 and p1 == 0 and p2 == 113):\n                    continue\n\n                # Document section hierarchy / paragraph citation / semver check\n                if is_section_citation(line, filename, p0, p1, p2, p3):\n                    continue\n\n                findings.append((idx, ip_str, "Raw IPv4 address"))\n\n        # 2. Check forbidden key / credential patterns\n        for pattern, desc in _FORBIDDEN_PATTERNS:\n            if pattern.search(line):\n                findings.append((idx, line.strip(), desc))\n\n    return (\n        findings\n        + check_personal_identifiers(content, filename)\n        + check_private_tracker_citations(content, filename)\n    )\n\n\ndef get_git_content(rel_path: str, rev: str = "") -> str | None:\n    """Read exact blob content from git index or revision using \'git show <rev>:<path>\'."""\n    try:\n        git_target = f":{rel_path}" if rev == "" else f"{rev}:{rel_path}"\n        res = subprocess.run(\n            ["git", "show", git_target],\n            capture_output=True,\n            cwd=REPO_ROOT,\n            check=True,\n            timeout=DEFAULT_GIT_TIMEOUT_SECONDS,\n        )\n        return res.stdout.decode("utf-8", errors="ignore")\n    except (subprocess.CalledProcessError, UnicodeDecodeError, subprocess.TimeoutExpired):\n        return None\n\n\ndef run_git_nul_separated(cmd: list[str]) -> list[str]:\n    """Sol F003: Run git command returning NUL-separated path bytes to handle quoted Unicode paths cleanly."""\n    res = subprocess.run(cmd, capture_output=True, cwd=REPO_ROOT, check=True, timeout=DEFAULT_GIT_TIMEOUT_SECONDS)\n    raw_paths = res.stdout.split(b"\\x00")\n    decoded: list[str] = []\n    for raw in raw_paths:\n        if not raw:\n            continue\n        try:\n            decoded.append(raw.decode("utf-8"))\n        except UnicodeDecodeError:\n            decoded.append(raw.decode("utf-8", errors="ignore"))\n    return decoded\n\n\ndef run_git_lines(cmd: list[str]) -> list[str]:\n    """Run a git command whose output is one record per line."""\n    res = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, check=True, timeout=DEFAULT_GIT_TIMEOUT_SECONDS)\n    return [line for line in res.stdout.splitlines() if line]\n\n\ndef get_diff_range_head(diff_range: str) -> str:\n    """Return the revision containing files at the right side of a git range."""\n    for separator in ("...", ".."):\n        if separator in diff_range:\n            return diff_range.rsplit(separator, 1)[1]\n    return "HEAD"\n\n\ndef get_files_changed_in_commit_range(\n    commit_range: str,\n    *,\n    apply_infrastructure_skips: bool = True,\n) -> tuple[list[tuple[str, str]], str]:\n    """Select each touched path at the commit that contains its published bytes."""\n    commits = run_git_lines(["git", "rev-list", commit_range])\n    selected: list[tuple[str, str]] = []\n    for commit in commits:\n        paths = run_git_nul_separated(\n            [\n                "git",\n                "diff-tree",\n                "--root",\n                "-r",\n                "-m",\n                "--no-commit-id",\n                "--no-renames",\n                "--name-only",\n                "--diff-filter=ACMRT",\n                "-z",\n                commit,\n            ]\n        )\n        # A merge can delete a path relative to one parent while changing it\n        # relative to another. Only blobs present in this commit can be read.\n        commit_paths = set(run_git_nul_separated(["git", "ls-tree", "-rz", "--name-only", commit]))\n        selected.extend(\n            (path, commit)\n            for path in filter_rel_paths(\n                sorted(set(paths) & commit_paths),\n                apply_infrastructure_skips=apply_infrastructure_skips,\n            )\n        )\n    return selected, f"git commit range ({commit_range})"\n\n\ndef get_files_to_check(\n    diff_range: str | None = None,\n    staged_only: bool = False,\n    scan_all: bool = False,\n    *,\n    apply_infrastructure_skips: bool = True,\n) -> tuple[list[str], str, str]:\n    """Return tuple of (list_of_relative_path_strings, mode_description, rev_target)."""\n    if scan_all:\n        cmd = ["git", "ls-files", "-z"]\n        paths = run_git_nul_separated(cmd)\n        return filter_rel_paths(paths, apply_infrastructure_skips=apply_infrastructure_skips), "all tracked files (--all)", "HEAD"\n\n    if staged_only:\n        cmd = ["git", "diff", "--cached", "-z", "--name-only", "--diff-filter=ACMRT"]\n        paths = run_git_nul_separated(cmd)\n        return filter_rel_paths(paths, apply_infrastructure_skips=apply_infrastructure_skips), "staged git index (:path)", ""\n\n    if diff_range:\n        rev_target = get_diff_range_head(diff_range)\n        cmd = ["git", "diff", "-z", "--name-only", "--diff-filter=ACMRT", diff_range]\n        paths = run_git_nul_separated(cmd)\n        return filter_rel_paths(paths, apply_infrastructure_skips=apply_infrastructure_skips), f"git diff ({diff_range})", rev_target\n\n    # Sol F004 / Fable F007: Explicit pre-push ref environment check\n    pre_push_local = os.environ.get("PRE_PUSH_LOCAL_REF") or os.environ.get("PRE_COMMIT_TO_REF", "")\n    pre_push_remote = os.environ.get("PRE_PUSH_REMOTE_REF") or os.environ.get("PRE_COMMIT_FROM_REF", "")\n    if pre_push_local and pre_push_remote and pre_push_remote != "0" * 40:\n        range_spec = f"{pre_push_remote}..{pre_push_local}"\n        cmd = ["git", "diff", "-z", "--name-only", "--diff-filter=ACMRT", range_spec]\n        paths = run_git_nul_separated(cmd)\n        return filter_rel_paths(paths, apply_infrastructure_skips=apply_infrastructure_skips), f"pre-push range ({range_spec})", pre_push_local\n\n    # Default logic: staged > feature branch diff > HEAD~1..HEAD\n    cmd_staged = ["git", "diff", "--cached", "-z", "--name-only", "--diff-filter=ACMRT"]\n    staged_paths = run_git_nul_separated(cmd_staged)\n    if staged_paths:\n        return filter_rel_paths(staged_paths, apply_infrastructure_skips=apply_infrastructure_skips), "staged git index (:path)", ""\n\n    # Feature branch diff mode\n    try:\n        branch_res = subprocess.run(\n            ["git", "rev-parse", "--abbrev-ref", "HEAD"],\n            capture_output=True,\n            text=True,\n            cwd=REPO_ROOT,\n            check=True,\n            timeout=DEFAULT_GIT_TIMEOUT_SECONDS,\n        )\n        curr_branch = branch_res.stdout.strip()\n        if curr_branch and curr_branch != "main":\n            cmd = ["git", "diff", "-z", "--name-only", "--diff-filter=ACMRT", "origin/main..HEAD"]\n            paths = run_git_nul_separated(cmd)\n            if paths:\n                return filter_rel_paths(paths, apply_infrastructure_skips=apply_infrastructure_skips), "git diff (origin/main..HEAD)", "HEAD"\n    except Exception:\n        pass\n\n    # Default commit range check for push/local\n    cmd = ["git", "diff", "-z", "--name-only", "--diff-filter=ACMRT", "HEAD~1..HEAD"]\n    paths = run_git_nul_separated(cmd)\n    return filter_rel_paths(paths, apply_infrastructure_skips=apply_infrastructure_skips), "recent commit (HEAD~1..HEAD)", "HEAD"\n\n\ndef filter_rel_paths(\n    paths: list[str], *, apply_infrastructure_skips: bool = True\n) -> list[str]:\n    """Filter paths by extension and, for infrastructure scans, skip patterns."""\n    valid_paths: list[str] = []\n    for rel_str in paths:\n        path_obj = Path(rel_str)\n        if path_obj.suffix.lower() in _SKIP_EXTENSIONS:\n            continue\n        normalized = rel_str.replace("\\\\", "/")\n        if (\n            apply_infrastructure_skips\n            and not normalized.startswith("scripts/audit/")\n            and any(sub in normalized for sub in _SKIP_PATH_SUBSTRINGS)\n        ):\n            continue\n        valid_paths.append(rel_str)\n    return valid_paths\n\n\ndef get_public_personal_identifier_paths(revision: str = "") -> list[str]:\n    """Return all tracked learner-facing files that must be free of scrubbed names."""\n    command = ["git", "ls-files", "-z"]\n    if revision:\n        command = ["git", "ls-tree", "-rz", "--name-only", revision]\n    paths = run_git_nul_separated(command)\n    return [path for path in paths if is_public_personal_identifier_path(path)]\n\n\n_PUBLICATION_INPUT_ERRORS = (\n    KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError, RuntimeError,\n)\n\n\nclass PublicationInputError(RuntimeError):\n    """Name the failed stage, with a redacted cause for live metadata failures."""\n\n    def __init__(self, stage: str, error: Exception):\n        cause = ""\n        if stage == "live PR fetch":\n            # Keep direct, stdlib-only script execution working too.\n            if str(REPO_ROOT) not in sys.path:\n                sys.path.insert(0, str(REPO_ROOT))\n            from lu_machinery.secret_redactor import REDACTION, URL_PATTERN, redact_text, redact_value\n\n            message = str(error)\n            # Also remove held secrets whose shape the pattern redactor cannot know.\n            redacted_env = redact_value(dict(os.environ))\n            for name, value in os.environ.items():\n                if value and redacted_env[name] == REDACTION:\n                    message = message.replace(value, REDACTION)\n            # A query or an arbitrary header may carry secrets without a token name.\n            message = URL_PATTERN.sub("[REDACTED_URL]", message)\n            message = re.sub(\n                r"(?im)(?:\\b(?:authorization|proxy-authorization|cookie|set-cookie|x-[\\w-]+)|^(?!gh:)[a-z][\\w-]*)\\s*:[^\\r\\n]*",\n                "[REDACTED_HEADER]", message,\n            )\n            message = redact_text(message) or ""\n            cause = ": " + " ".join(message.split()) if message else ""\n        super().__init__(f"{stage} ({type(error).__name__}{cause})")\n\n\n@contextmanager\ndef publication_stage(stage: str):\n    try:\n        yield\n    except PublicationInputError:\n        raise\n    except _PUBLICATION_INPUT_ERRORS as error:\n        raise PublicationInputError(stage, error) from None\n\n\ndef publication_git(*args: str) -> str:\n    """Capture Git output so failures never print submitted messages or refs."""\n    return subprocess.run(\n        ["git", *args], cwd=REPO_ROOT, capture_output=True, check=True,\n        timeout=DEFAULT_GIT_TIMEOUT_SECONDS,\n    ).stdout.decode("utf-8")\n\n\ndef publication_sha(value: object) -> str:\n    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):\n        raise ValueError("invalid publication SHA")\n    return value\n\n\ndef publication_messages(base: object, head: object, *, new_branch: bool = False) -> str:\n    base, head = publication_sha(base), publication_sha(head)\n    if head == "0" * 40 or (base == head and not new_branch):\n        raise ValueError("empty publication range")\n    if publication_git("rev-parse", "--is-shallow-repository").strip() != "false":\n        raise ValueError("incomplete publication history")\n    with publication_stage("git verify head"):\n        publication_git("cat-file", "-e", f"{head}^{{commit}}")\n    if base == "0" * 40 and new_branch:\n        span = head\n    else:\n        with publication_stage("git verify base"):\n            publication_git("cat-file", "-e", f"{base}^{{commit}}")\n            publication_git("merge-base", base, head)\n        span = f"{base}..{head}"\n    with publication_stage("commit-message read"):\n        return publication_git("log", "--format=%B%x00", span)\n\n\ndef current_publication_pr(repository: str, number: int) -> dict:\n    # Direct script execution also works in a clean stdlib-only checkout.\n    if str(REPO_ROOT) not in sys.path:\n        sys.path.insert(0, str(REPO_ROOT))\n    from lu_machinery.common import github_client\n\n    result = github_client.run(\n        ["gh", "api", "-X", "GET", f"repos/{repository}/pulls/{number}"],\n        fresh=True, capture_output=True, text=True, timeout=30,\n    )\n    if result.returncode:\n        refusal = github_client.refusal_of(result)\n        if refusal is not None:\n            raise refusal\n        detail = (result.stderr or "").strip() or "no error message"\n        raise ValueError(f"current publication metadata unavailable (exit {result.returncode}): {detail}")\n    pr = json.loads(result.stdout)\n    if not isinstance(pr, dict) or pr.get("number") != number:\n        raise ValueError("invalid current publication metadata")\n    return pr\n\n\ndef publication_pr_texts(pr: dict, *, live: bool = False) -> list[tuple[str, str]]:\n    title, body, branch = pr["title"], pr["body"], pr["head"]["ref"]\n    if not isinstance(title, str) or not title or not isinstance(branch, str) or not branch:\n        raise ValueError("missing publication field")\n    if body is not None and not isinstance(body, str):\n        raise ValueError("invalid publication body")\n    base, head = publication_sha(pr["base"]["sha"]), publication_sha(pr["head"]["sha"])\n    if live:\n        for role, sha in (("base", base), ("head", head)):\n            with publication_stage(f"git verify {role}"):\n                try:\n                    publication_git("cat-file", "-e", f"{sha}^{{commit}}")\n                except subprocess.CalledProcessError:\n                    with publication_stage(f"git fetch {role}"):\n                        publication_git("fetch", "--no-tags", "--quiet", "origin", sha)\n    return [("title", title), ("body", body or ""), ("branch", branch),\n            ("commit", publication_messages(base, head))]\n\n\ndef publication_texts(payload: dict, event: str) -> list[tuple[str, str]]:\n    """Read all required event and live fields without replacing event evidence."""\n    if event == "push":\n        ref = payload["ref"]\n        if not isinstance(ref, str) or not ref.startswith(("refs/heads/", "refs/tags/")):\n            raise ValueError("invalid push ref")\n        publication_git("check-ref-format", ref)\n        before, after = publication_sha(payload["before"]), publication_sha(payload["after"])\n        if after == "0" * 40 and payload.get("deleted") is True:\n            with publication_stage("git verify base"):\n                publication_git("cat-file", "-e", f"{before}^{{commit}}")\n            return [("branch", ref)]\n        new_branch = before == "0" * 40\n        # A force push withdraws `before`, so no fetched ref carries it; the branch\n        # is then scanned from the same base a new branch uses.\n        rebase = new_branch\n        if not new_branch:\n            try:\n                publication_git("cat-file", "-e", f"{before}^{{commit}}")\n            except subprocess.CalledProcessError:\n                rebase = True\n        if rebase:\n            default = payload["repository"]["default_branch"]\n            if not isinstance(default, str) or not default:\n                raise ValueError("invalid default branch")\n            publication_git("check-ref-format", f"refs/heads/{default}")\n            if ref != f"refs/heads/{default}":\n                with publication_stage("git verify base"):\n                    bases = publication_git("merge-base", "--all", f"refs/remotes/origin/{default}", after).split()\n                    if len(bases) != 1:\n                        raise ValueError("new branch range unavailable")\n                    before = publication_sha(bases[0])\n            elif not new_branch:\n                raise ValueError("default branch base unavailable")\n        return [("branch", ref), ("commit", publication_messages(before, after, new_branch=rebase))]\n    if event not in {"pull_request", "merge_group"}:\n        raise ValueError("unsupported publication event")\n    repository = payload["repository"]["full_name"]\n    if not isinstance(repository, str) or not re.fullmatch(r"[\\w.-]+/[\\w.-]+", repository, re.ASCII):\n        raise ValueError("invalid publication repository")\n    if event == "pull_request":\n        if payload["action"] not in {"opened", "synchronize", "reopened", "edited"}:\n            raise ValueError("unsupported publication action")\n        pr = payload["pull_request"]\n        number = pr["number"]\n        if type(number) is not int or number < 1:\n            raise ValueError("invalid publication PR")\n        texts = publication_pr_texts(pr)\n        with publication_stage("live PR fetch"):\n            live_pr = current_publication_pr(repository, number)\n            live_texts = publication_pr_texts(live_pr, live=True)\n        return texts + live_texts\n    if payload["action"] != "checks_requested":\n        raise ValueError("unsupported queue action")\n    group = payload["merge_group"]\n    base, head = publication_sha(group["base_sha"]), publication_sha(group["head_sha"])\n    texts = [("commit", publication_messages(base, head))]\n    with publication_stage("queue-chain read"):\n        publication_git("merge-base", "--is-ancestor", base, head)\n        chain = publication_git("rev-list", "--first-parent", f"{base}..{head}").split()\n        if not chain:\n            raise ValueError("empty queue chain")\n    # Same squash-chain convention as check_agent_attribution, without its\n    # Markdown dependency. Every queue member, not just the ref\'s PR, is read.\n    for sha in chain:\n        with publication_stage("queue-chain read"):\n            if len(publication_git("rev-list", "--parents", "-n", "1", sha).split()) != 2:\n                raise ValueError("unsupported queue commit")\n            subject = publication_git("log", "-1", "--format=%s", sha).strip()\n            match = re.search(r"\\(#([1-9][0-9]*)\\)$", subject)\n            if not match:\n                raise ValueError("queue PR unavailable")\n        with publication_stage("live PR fetch"):\n            texts.extend(publication_pr_texts(current_publication_pr(repository, int(match[1])), live=True))\n    ref = group["head_ref"]\n    if not isinstance(ref, str) or not ref.startswith("refs/heads/gh-readonly-queue/"):\n        raise ValueError("invalid queue ref")\n    publication_git("check-ref-format", ref)\n    texts.append(("branch", ref))\n    return texts\n\n\ndef scan_publication_event(event_file: str) -> int:\n    try:\n        payload = json.loads(Path(event_file).read_text(encoding="utf-8"))\n        if not isinstance(payload, dict):\n            raise ValueError("invalid event")\n        texts = publication_texts(payload, os.environ.get("GITHUB_EVENT_NAME", ""))\n        failed = False\n        for field, text in texts:\n            for line, _matched, rule in check_content(text, "docs/runbooks/publication.txt"):\n                print(f"OPSEC publication: {field} line={line} rule={rule}")\n                failed = True\n        return int(failed)\n    except _PUBLICATION_INPUT_ERRORS as error:\n        failure = error if isinstance(error, PublicationInputError) else PublicationInputError("event parse", error)\n        print(f"OPSEC publication: required input unavailable or invalid: {failure}", file=sys.stderr)\n        return 1\n\n\ndef main(argv: list[str] | None = None) -> int:\n    parser = argparse.ArgumentParser(\n        description="Lint committed or staged files for OPSEC leaks.\\nUse commit ranges to check historical content before publication.",\n        formatter_class=argparse.RawDescriptionHelpFormatter,\n        epilog="Examples:\\n  lint_opsec_leaks.py --staged\\n  lint_opsec_leaks.py --commit-range origin/main..HEAD\\n"\n        "Outputs: redacted findings on stdout; no files written.\\n"\n        "Exit codes: 0 clean; 1 leaks or unreadable Git data; 2 invalid arguments.\\n"\n        "Event scanning: --event-file event.json reads GITHUB_EVENT_NAME and current PR metadata; requires gh.\\n"\n        "Related: docs/runbooks/pre-push-gate.md; #10579; #10524.",\n    )\n    parser.add_argument("diff_range", nargs="?", help="Git diff range (e.g. origin/main...HEAD)")\n    parser.add_argument("--staged", action="store_true", help="Inspect staged git index blobs")\n    parser.add_argument("--all", action="store_true", help="Explicitly scan all tracked files in repo")\n    parser.add_argument("--event-file", help="Scan publication fields from a GitHub event JSON file; default: file scanning")\n    parser.add_argument(\n        "--commit-range",\n        help="Scan each commit\'s touched blobs, e.g. origin/main..HEAD; default: push refs when available",\n    )\n    parser.add_argument(\n        "--public-identifiers",\n        action="store_true",\n        help="Scan learner-facing files for scrubbed personal identifiers only",\n    )\n    parser.add_argument(\n        "--revision",\n        help="Read --public-identifiers files from this git revision instead of HEAD",\n    )\n    args = parser.parse_args(argv)\n    if args.event_file is not None:\n        if any((args.diff_range, args.commit_range, args.staged, args.all, args.public_identifiers, args.revision)):\n            parser.error("--event-file cannot be combined with file-scanning options")\n        return scan_publication_event(args.event_file)\n\n    if args.public_identifiers and (args.staged or args.all):\n        parser.error("--public-identifiers cannot be combined with --staged or --all")\n    if args.commit_range and (args.diff_range or args.staged or args.all):\n        parser.error("--commit-range cannot be combined with a diff range, --staged, or --all")\n    if args.revision and not args.public_identifiers:\n        parser.error("--revision requires --public-identifiers")\n    if args.revision and (args.diff_range or args.commit_range):\n        parser.error("--revision cannot be combined with a diff range or --commit-range")\n\n    commit_range = args.commit_range\n    if not any((commit_range, args.diff_range, args.public_identifiers, args.staged, args.all)):\n        local = os.environ.get("PRE_PUSH_LOCAL_REF") or os.environ.get("PRE_COMMIT_TO_REF", "")\n        remote = os.environ.get("PRE_PUSH_REMOTE_REF") or os.environ.get("PRE_COMMIT_FROM_REF", "")\n        if local and remote and remote != "0" * 40:\n            commit_range = f"{remote}..{local}"\n\n    try:\n        if commit_range:\n            selected, mode_str = get_files_changed_in_commit_range(\n                commit_range, apply_infrastructure_skips=not args.public_identifiers\n            )\n            if args.public_identifiers:\n                selected = [(path, rev) for path, rev in selected if is_public_personal_identifier_path(path)]\n        elif args.public_identifiers:\n            if args.diff_range:\n                changed_paths, diff_mode, rev_target = get_files_to_check(\n                    args.diff_range, apply_infrastructure_skips=False\n                )\n                rel_paths = [\n                    path for path in changed_paths if is_public_personal_identifier_path(path)\n                ]\n                mode_str = f"changed learner-facing files (--public-identifiers; {diff_mode})"\n            else:\n                rel_paths = get_public_personal_identifier_paths(args.revision or "HEAD")\n                mode_str = "all tracked learner-facing files (--public-identifiers)"\n                rev_target = args.revision or "HEAD"\n        else:\n            rel_paths, mode_str, rev_target = get_files_to_check(args.diff_range, staged_only=args.staged, scan_all=args.all)\n        if not commit_range:\n            selected = [(path, rev_target) for path in rel_paths]\n    except Exception as exc:\n        print(f"💥 Fail-closed git error: {exc}", file=sys.stderr)\n        return 1\n\n    total_findings = 0\n\n    print(f"🔒 Checking {len(selected)} file versions for OPSEC leaks (mode: {mode_str})...")\n\n    for rel_path, rev_target in selected:\n        content = get_git_content(rel_path, rev=rev_target)\n\n        if content is None:\n            if commit_range:\n                print(f"💥 Fail-closed unreadable selected blob: {rev_target}:{rel_path}", file=sys.stderr)\n                total_findings += 1\n                continue\n            disk_path = REPO_ROOT / rel_path\n            if not disk_path.is_file():\n                print(f"💥 Fail-closed unreadable selected file: {rel_path}", file=sys.stderr)\n                total_findings += 1\n                continue\n            try:\n                content = disk_path.read_text(encoding="utf-8", errors="ignore")\n            except OSError as exc:\n                print(f"💥 Fail-closed unreadable selected file: {rel_path}: {exc}", file=sys.stderr)\n                total_findings += 1\n                continue\n\n        findings = check_content(\n            content,\n            rel_path,\n            include_infrastructure=not args.public_identifiers,\n        )\n\n        if findings:\n            total_findings += len(findings)\n            print(f"\\n❌ OPSEC LEAK DETECTED in [{rel_path}] at {rev_target or \'index\'}:")\n            for line_num, _match_str, desc in findings:\n                print(f"   Line {line_num}: [{desc}] (value redacted)")\n\n    if total_findings > 0:\n        print(f"\\n💥 Total OPSEC leaks found: {total_findings}")\n        print("Please remove OPSEC-sensitive infrastructure details and scrubbed identifiers before submitting!")\n        return 1\n\n    print("✅ OPSEC check passed. Zero leaks detected.")\n    return 0\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n'


@pytest.fixture
def salvage_product_linter(sandbox):
    path = sandbox.repo / "scripts/audit/lint_opsec_leaks.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SALVAGE_LINTER, encoding="utf-8")
    run_git(sandbox.repo, "add", "scripts/audit/lint_opsec_leaks.py")
    run_git(sandbox.repo, "commit", "-m", "product linter fixture")
    run_git(sandbox.repo, "push", "origin", "main")

pytestmark = [pytest.mark.usefixtures('configured_roots')]
