"""Delivery observation and write-settle scope checks."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from lu_machinery.dispatcher import git, worker
from lu_machinery.dispatcher.types import TaskSpec
from tests.dispatcher.conftest import run_git
from tests.dispatcher.conftest import MERGED
from tests.dispatcher.stub_provider import LEFTOVER_NAMES
from tests.helpers.product_tree import configured_roots  # noqa: F401






@pytest.mark.parametrize(
    ("model", "push_state", "ahead", "dirty"),
    [
        ("pushed", "pushed", 1, False),
        ("unpushed", "unpushed", 1, False),
        ("dirty", "unpushed", 0, True),
    ],
)
def test_delivery_observes_actual_remote_and_dirty_worktree(sandbox, model, push_state, ahead, dirty):
    record = sandbox.settle(sandbox.dispatch(model, "--mode", "workspace-write"), expected=0 if model == "pushed" else 1)
    assert record["push_state"] == push_state
    assert record["commits_ahead"] == ahead
    assert record["worktree_dirty_on_exit"] is dirty
    assert "closeout" not in record  # An actual unmerged dispatch must never trigger merge cleanup.
    assert (record["worktree_disposition"] == "removed") is (push_state == "pushed" or dirty)
    if record["worktree_disposition"] == "removed":
        # Settlement removed the published or scratch-only worktree; re-attach it at its head for the probes below.
        run_git(sandbox.repo, "worktree", "add", "-b", record["worktree_branch"], record["cwd"], record["head_sha"])
    assert record["head_sha"] == run_git(Path(record["cwd"]), "rev-parse", "HEAD")
    if push_state == "pushed":
        assert (
            record["remote_head_sha"]
            == run_git(sandbox.repo, "ls-remote", "origin", f"refs/heads/{record['worktree_branch']}").split()[0]
        )
        # A stale tracking ref must not be mistaken for the actual remote branch.
        run_git(sandbox.remote, "update-ref", "-d", f"refs/heads/{record['worktree_branch']}")
        probe = TaskSpec(
            record["task_id"],
            "codex",
            "echo",
            None,
            "workspace-write",
            Path(record["cwd"]),
            Path(record["prompt_file"]),
            Path(record["result_file"]),
        )
        observed = git.snapshot_delivery(probe, record)
        assert observed["push_state"] == "unpushed"
        run_git(probe.worktree, "remote", "set-url", "origin", str(sandbox.tasks / "missing-remote"))
        assert (
            git.snapshot_delivery(probe, record)["push_state"]
            == "unknown"
        )


def test_delivery_snapshot_does_not_execute_worker_git_config(sandbox):
    """The supervisor must not execute worker-controlled Git configuration."""
    worktree = sandbox.repo.parent / "worker"
    head = run_git(sandbox.repo, "rev-parse", "HEAD")
    run_git(sandbox.repo, "worktree", "add", "-b", "delivery", str(worktree), head)
    run_git(worktree, "push", "origin", "delivery")
    run_git(sandbox.repo, "config", "extensions.worktreeConfig", "true")
    marker = sandbox.repo.parent / "fsmonitor-executed"
    run_git(worktree, "config", "--worktree", "core.fsmonitor", f'touch "{marker}"')
    probe = TaskSpec("snapshot", "codex", "echo", None, "workspace-write", worktree,
                     worktree / "prompt", worktree / "result")
    observed = git.snapshot_delivery(probe, {
        "task_id": "snapshot", "worktree_base_sha": head, "worktree_branch": "delivery",
        "worktree_remote_url": str(sandbox.remote),
    })
    assert not marker.exists(), "the supervisor executed the worker's fsmonitor command"
    assert observed["push_state"] == "pushed"
    assert observed["head_sha"] == observed["remote_head_sha"] == head
    assert observed["commits_ahead"] == 0
    assert observed["worktree_dirty_on_exit"] is False


@pytest.mark.parametrize("model,mode,owned,state,cause", [
    ("pushed", "workspace-write", ["other/"], "no_deliverable", "outside_owned_paths: delivered.txt"),
    ("pushed", "workspace-write", ["delivered.txt"], "done", None),
    ("pushed-nested", "workspace-write", ["src/*.txt"], "done", None),
    ("pushed", "workspace-write", ["delivered"], "no_deliverable", "outside_owned_paths: delivered.txt"),
    ("pushed-nested", "workspace-write", ["src/"], "done", None),
    ("pushed-dirty", "workspace-write", [], "no_deliverable", "worktree_dirty_or_unknown"),
    # Untracked output outside the scope is generated or unrelated state; inside it, or unscoped, it is task work.
    ("pushed-scratch", "workspace-write", ["delivered.txt"], "done", None),
    ("pushed-scratch", "workspace-write", ["delivered.txt", "leftover/"], "no_deliverable", "worktree_dirty_or_unknown"),
    ("pushed-scratch", "workspace-write", [], "no_deliverable", "worktree_dirty_or_unknown"),
    # A tracked edit stays task work outside the scope too: the worker may have been unable to commit it.
    ("pushed-dirty-outside", "workspace-write", ["delivered.txt"], "no_deliverable", "worktree_dirty_or_unknown"),
    ("echo", "read-only", [], "done", None),
    ("dirty", "read-only", [], "done", None),
    ("pushed", "read-only", [], "done", None),
])
def test_settle_rejects_out_of_scope_or_unpublished_write_delivery(sandbox, model, mode, owned, state, cause):
    """A provider's successful response cannot hide scope violations or undelivered work."""
    flags = [flag for path in owned for flag in ("--owned-path", path)]
    record = sandbox.settle(sandbox.dispatch(model, "--mode", mode, *flags), expected=0 if state == "done" else 1)
    assert record["status"] == state
    assert record["owned_paths"] == owned
    assert record["research_owned_paths"] == []
    assert record["error"] == cause
    assert record.get("no_deliverable_reason") == cause
    assert record["returncode"] == 0
    assert Path(record["result_file"]).read_text() == "literal final response"
    if model == "pushed-scratch":
        # Delivery verdicts never discard the leftover file: settlement removed the tree only after preserving it.
        assert record["worktree_dirty_on_exit"] is True
        assert record["worktree_unrelated_only_on_exit"] is (state == "done")
        assert record["push_state"] == ("pushed" if state == "done" else "unpushed")
        assert record["worktree_disposition"] == "removed"
        archive = record["branch_holder_archive"]
        assert archive["retention_disposition"] == "retrieved"
        names = [f"leftover/{name}" for name in LEFTOVER_NAMES]
        assert sorted(entry["path"] for entry in archive["paths"]) == sorted(names)
        for name in LEFTOVER_NAMES:
            assert (sandbox.repo / archive["location"] / "leftover" / name).read_text() == f"leftover {name!r}\n"


def test_v2_record_without_owned_paths_remains_readable(sandbox):
    record = sandbox.settle(sandbox.dispatch())
    record.pop("owned_paths", None)
    (sandbox.tasks / f"{record['task_id']}.json").write_text(json.dumps(record))
    assert json.loads(sandbox.cli("status", record["task_id"]).stdout)["status"] == "done"


@pytest.mark.parametrize("owned", [None, []])
@pytest.mark.parametrize("model,cause", [
    ("pushed", None), ("dirty", "worktree_dirty_or_unknown"),
    ("unpushed", "work_not_pushed_or_unknown"), ("echo", "no_commits_or_unknown"),
])
def test_write_settle_without_ownership(delivery_settle, owned, model, cause):
    _, settle = delivery_settle
    result = settle(model, owned)
    assert result["status"] == ("no_deliverable" if cause else "done")
    assert result["error"] == cause
    assert ("owned_paths" in result) is (owned is not None)


LINE_BREAK_NAMES = ["scratch/lf\nname", "scratch/cr\rname", "scratch/crlf\r\nname"]


@pytest.mark.parametrize(("case", "grants", "unrelated"), [
    ("outside", None, True),
    ("line-breaks", None, True),  # distinct names a text read would fold into one
    ("ignored", None, False),  # ignored output is not uncommitted state at all
    ("outside", ["scratch"], False),  # a grant bound to this run widens the scope
    ("outside", "unbound", False),  # an unreadable scope owns every untracked file
    ("nested", None, False),  # a nested checkout's files are not listed, so it may hold task work
    ("bare", None, False),  # a bare repository's commits may hold work, even beside a .git link to the tree's own
    ("bare-symlink-head", None, False),  # Git still opens a bare repository whose HEAD is a symbolic link
    ("bare-trailing-blank", None, False),  # a repository name ending in a blank is still that repository
    ("marker-output", None, False),  # any HEAD or .git entry, even one Git would not open, may mark a repository
    ("nested-listed", None, False),  # a checkout under a path the tree tracks: status lists its files one by one
    ("nested-gitfile", None, False),  # the same checkout, its .git a file naming a separate Git directory
    ("nested-all-tracked", None, False),  # a checkout whose files the tree tracks: status lists none of them
    ("nested-ignored", None, False),  # a checkout under an ignored path: status never lists it
    ("stat-hidden", None, False),  # a tracked edit the worker's stat settings would hide from status
    ("attributes-file", None, True),  # a configured attributes file keeps a clean checkout clean
])
def test_snapshot_separates_unrelated_untracked_state(tmp_path, case, grants, unrelated):
    """Defect class: settlement failed a pushed delivery for scratch the task does not own (#524).

    Untracked files outside the effective owned paths are generated or unrelated state; anything the
    scope covers, or cannot be shown not to cover, stays uncommitted task work. The snapshot reads the
    tree as removal does, byte for byte and without trusting the worker's stat settings.
    """
    work = tmp_path / "work"
    run_git(tmp_path, "init", "-q", "-b", "main", str(work))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        run_git(work, "config", key, value)
    (work / ".gitignore").write_text("*.log\nignored/\n")
    (work / "delivered.txt").write_text("delivery\n")
    nested = work / ("ignored/nested" if case == "nested-ignored" else "vendor/nested")
    if case in {"nested-listed", "nested-gitfile", "nested-all-tracked"}:
        nested.mkdir(parents=True)
        (nested / "shared.txt").write_text("shared\n")
    if case == "attributes-file":  # checked out as CRLF only through the configured attributes file
        (tmp_path / "attributes").write_text("*.txt text eol=crlf\n")
        run_git(work, "config", "core.attributesFile", str(tmp_path / "attributes"))
        (work / "delivered.txt").write_bytes(b"delivery\r\n")
    os.utime(work / "delivered.txt", (1_000_000_000, 1_000_000_000))  # older than the index: not racily clean
    run_git(work, "add", ".")
    run_git(work, "commit", "-q", "-m", "delivery")
    leftovers ={"line-breaks": LINE_BREAK_NAMES, "ignored": ["scratch/out.log"]}.get(case, ["scratch/notes.txt"])
    (work / "scratch").mkdir()
    for name in leftovers:
        (work / name).write_text(f"left over {name!r}\n")
    if case == "nested":
        run_git(work, "init", "-q", "scratch/nested")
        (work / "scratch/nested/inner.txt").write_text("nested\n")
    elif case in {"bare", "bare-symlink-head", "bare-trailing-blank"}:
        run_git(work, "init", "-q", "--bare", "scratch/nested.git" + (" " if case == "bare-trailing-blank" else ""))
        if case == "bare-symlink-head":  # the legacy form; its branch may exist only as a packed ref
            (work / "scratch/nested.git/HEAD").unlink()
            (work / "scratch/nested.git/HEAD").symlink_to("refs/heads/main")
        elif case == "bare":  # Git's discovery from inside would stop at this link and find the tree's repository
            (work / "scratch/nested.git/.git").symlink_to(work / ".git")
    elif case == "attributes-file":
        os.utime(work / "delivered.txt")  # new timestamps, same bytes: status re-reads it through the attributes
    elif case.startswith("nested-"):
        separate = ["--separate-git-dir", str(tmp_path / "separate.git")] if case == "nested-gitfile" else []
        run_git(tmp_path, "init", "-q", "-b", "main", *separate, str(nested))
        committed = "shared.txt" if case == "nested-all-tracked" else "local.txt"
        (nested / committed).write_text("shared\n" if committed == "shared.txt" else "local\n")
        run_git(nested, "add", committed)
        run_git(nested, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                "commit", "-q", "-m", "work the delivery does not hold")
    elif case == "marker-output":
        (work / "scratch/output/objects").mkdir(parents=True)
        (work / "scratch/output/HEAD").write_text("generated report\n")
        (work / "scratch/output/objects/artifact.json").write_text("{}\n")
        for name, text in (("empty", ""), ("invalid", "generated\n"), ("missing", f"gitdir: {tmp_path}/none\n")):
            (work / "scratch/output" / name).mkdir()
            (work / "scratch/output" / name / ".git").write_text(text)
    elif case == "stat-hidden":
        # Same size and timestamps on a new inode: only the stat checks the worker switches off can see it.
        before, rewrite = (work / "delivered.txt").stat(), work / "rewrite.tmp"
        rewrite.write_text("DELIVERY\n")
        os.utime(rewrite, ns=(before.st_atime_ns, before.st_mtime_ns))
        rewrite.replace(work / "delivered.txt")
        run_git(work, "config", "core.trustctime", "false")
        run_git(work, "config", "core.checkStat", "minimal")
        # Hidden from a plain status; --no-optional-locks keeps that probe from refreshing the index.
        assert "delivered.txt" not in run_git(work, "--no-optional-locks", "status", "--porcelain")
    record = {"task_id": "probe", "run_nonce": "nonce", "owned_paths": ["delivered.txt"],
              "worktree_base_sha": run_git(work, "rev-parse", "HEAD")}
    if grants is not None:
        record["owned_path_grants"] = [{"task_id": "probe", "run_nonce": "other" if grants == "unbound" else "nonce",
                                        "grant_id": "g1", "owned_paths": ["scratch"]}]
    observed = git.snapshot_delivery(SimpleNamespace(worktree=work, mode="read-only"), record)
    assert observed["worktree_dirty_on_exit"] is (case != "ignored")
    # The stat-hidden edit is a tracked change to the snapshot itself, with or without scratch beside it.
    assert observed["worktree_untracked_only_on_exit"] is (case not in {"ignored", "stat-hidden"})
    assert observed["worktree_unrelated_only_on_exit"] is unrelated
    # Removal's inventory is the same read: the same exact names and the same tracked changes.
    changed, untracked = git._tree_changes(work)
    assert bool(changed) is (case == "stat-hidden")
    if case in {"outside", "line-breaks"}:
        assert sorted(untracked) == sorted(leftovers)


@pytest.mark.parametrize("model,status,error", [
    ("fail", "failed", "provider exited unsuccessfully"),
    ("cancel", "cancelled", "cancelled by client"),
    ("blocked", "timeout", "process deadline exceeded"),
    ("crash", "crashed", "provider exited unsuccessfully"),
])
def test_unsuccessful_settle_preserves_cause_with_outside_commit(delivery_settle, model, status, error):
    record, settle = delivery_settle
    tree = Path(record["cwd"])
    (tree / "outside.txt").write_text("outside\n")
    run_git(tree, "add", "outside.txt")
    run_git(tree, "commit", "-m", "outside scope")
    result = settle(model)
    assert (result["status"], result["error"]) == (status, error)
    assert "no_deliverable_reason" not in result


@pytest.mark.parametrize("history", ["merged", "restored", "rename-in", "rename-out", "deleted"])
def test_scope_checks_each_own_commit(sandbox, delivery_settle, history, monkeypatch):
    record, settle = delivery_settle
    tree = Path(record["cwd"])
    if history == "merged":
        run_git(tree, "commit", "--allow-empty", "-m", "worker before upstream")
    if history != "restored":
        source = "delivered.txt" if history == "rename-out" else "outside.txt"
        (sandbox.repo / source).write_text("upstream\n")
        run_git(sandbox.repo, "add", source)
        run_git(sandbox.repo, "commit", "-m", "upstream")
        run_git(sandbox.repo, "push", "origin", "main:release")
        run_git(tree, "merge", "--no-edit", "main")
    if history.startswith("rename"):
        run_git(tree, "mv", source, "delivered.txt" if history == "rename-in" else "outside.txt")
    elif history == "deleted":
        run_git(tree, "rm", "outside.txt")
    else:
        (tree / "delivered.txt").write_text("delivery\n")
        if history == "restored":
            (tree / "outside.txt").write_text("outside\n")
            run_git(tree, "add", ".")
            run_git(tree, "commit", "-m", "temporary outside path")
            run_git(tree, "rm", "outside.txt")
    run_git(tree, "add", ".")
    run_git(tree, "commit", "-m", "worker delivery")
    run_git(tree, "push", "origin", "HEAD")
    result = settle()
    if history == "rename-in":
        # Force rename detection before the guard's explicit --no-renames override.
        original = git._verification_git
        monkeypatch.setattr(git, "_verification_git", lambda path, command, *args, **kw:
                            original(path, command, *(("-M",) if command == "log" else ()), *args, **kw))
        result["status"] = "done"
        worker._enforce_delivery(result)
    assert result["status"] == ("done" if history == "merged" else "no_deliverable")
    assert result["error"] == (None if history == "merged" else "outside_owned_paths: outside.txt")


@pytest.mark.parametrize(
    ("repo_key", "mode", "pr_state", "closeout_code"),
    [
        pytest.param("public", "workspace-write", "MERGED", 0, id="merged-settlement-omits-closeout"),
        pytest.param("machinery", "workspace-write", "MERGED", 0, id="wrong-repository-root"),
        pytest.param("public", "workspace-write", "OPEN", 0, id="unmerged-triggers-closeout"),
        pytest.param("public", "read-only", "MERGED", 0, id="read-only-triggers-closeout"),
        pytest.param("public", "workspace-write", "MERGED", 1, id="nonzero-closeout-hidden"),
    ],
)
def test_settlement_closes_only_merged_delivery_after_worktree_disposition(
    merged_delivery_world, monkeypatch, tmp_path, repo_key, mode, pr_state, closeout_code,
):
    """Defect classes: omitted, misrouted, premature, hidden-failure and duplicate settlement closeout.

    New cases approved by claude-cto, message_f413e45fe3914a1fa0ae103efbb27d8b.
    """
    world = merged_delivery_world
    slug = "learn-ukrainian/machinery" if repo_key == "machinery" else "learn-ukrainian/learn-ukrainian.github.io"
    root = tmp_path / repo_key
    root.mkdir()
    record = world.record
    record.update(repo=repo_key, mode=mode, commits_ahead=1, worktree_remote_url=f"https://github.com/{slug}.git")
    world.listed = [{"number": 78, "isCrossRepository": False, "headRefName": record["worktree_branch"]}]
    world.prs[78] = {**MERGED, "state": pr_state, "headRefOid": record["head_sha"]}
    world.files[78] = ["docs/owned.md"]
    request = {"pr": 78, "repo": slug, "repo_root": str(root)}
    monkeypatch.setattr(git, "resolve_repo", lambda key: (root, {"github": slug}) if key == repo_key else None)
    monkeypatch.setattr(worker, "_stop_task_services", lambda _: None)
    monkeypatch.setattr(worker, "_settle_task_scratch", lambda _: None)
    events = []
    disposition = "retained"

    def reap(current, **_):
        assert "closeout" not in current
        events.append(disposition)
        current.update(worktree_disposition=disposition,
                       worktree_disposition_reason="clean" if disposition == "removed" else "provider_processes_alive")

    def closeout(actual):
        assert record["worktree_disposition"] == "removed"
        assert actual == request
        events.append("closeout")
        return {"pr": 78, "returncode": closeout_code, "stdout": "closeout receipt", "stderr": ""}

    monkeypatch.setattr(worker, "_reap_worktree", reap)
    runner = Mock(side_effect=closeout)
    monkeypatch.setattr(worker, "_run_merge_closeout", runner)
    # A temporarily retained tree must not poison the receipt before the existing reap pass removes it.
    worker._reap_settled(record)
    assert events == ["retained"]
    assert "closeout" not in record
    assert (record["status"], record["error"]) == ("done", None)
    runner.assert_not_called()
    events.clear()
    disposition = "removed"
    worker._reap_settled(record)
    if mode == "read-only" or pr_state != "MERGED":
        assert events == ["removed"]
        assert "closeout" not in record
        runner.assert_not_called()
    else:
        assert events == ["removed", "closeout"]
        runner.assert_called_once_with(request)
        assert record["closeout"] == {"pr": 78, "returncode": closeout_code, "stdout": "closeout receipt", "stderr": ""}
    assert record["status"] == ("failed" if closeout_code else "done")
    assert record["error"] == ("closeout_failed: PR 78 exit 1" if closeout_code else None)


pytestmark = pytest.mark.usefixtures("configured_roots")
