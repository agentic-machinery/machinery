"""Merged-PR delivery, branch-start attribution, stale refs and origin poisoning."""



import json
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from lu_machinery.dispatcher import cli, git, worker
from tests.dispatcher.conftest import MERGED as MERGED
from tests.dispatcher.conftest import await_file as await_file
from tests.dispatcher.conftest import in_process_repo as in_process_repo
from tests.dispatcher.conftest import local_branches as local_branches
from tests.dispatcher.conftest import prior_worktree as prior_worktree
from tests.dispatcher.conftest import run_git as run_git
from tests.helpers.product_tree import configured_roots as configured_roots


@pytest.mark.parametrize("unpublished_ancestor", [False, True], ids=["unchanged-start", "unpublished-ancestor"])
def test_accepted_merged_pr_without_new_commits_is_delivered(merged_delivery_world, unpublished_ancestor):
    """An accepted PR can credit an unchanged task, but a zero count does not prove a different HEAD."""
    world = merged_delivery_world
    world.accept(77)
    world.prs[77], world.files[77] = MERGED, []
    world.published = (None, False)
    if not unpublished_ancestor:
        world.record["head_sha"] = world.record["worktree_base_sha"]
    worker._enforce_delivery(world.record)
    if unpublished_ancestor:
        assert world.record["status"] == "no_deliverable"
        assert "delivery_pr" not in world.record
    else:
        assert (world.record["status"], world.record["error"]) == ("done", None)
        assert world.record["delivery_pr"] == "org/repo#77"


def test_branch_reset_or_deleted_after_merge_is_delivered(merged_delivery_world):
    """Defect class: the task's own PR merged, then the branch was reset or deleted, so the check read a git error."""
    world = merged_delivery_world
    world.search_listed = []  # a just-merged PR is not yet visible in the issue-search index
    world.record["commits_ahead"] = 2
    world.listed = [{"number": 78, "isCrossRepository": False, "headRefName": "claude/merge-probe"}]
    world.prs[78], world.files[78] = {**MERGED, "headRefOid": world.record["head_sha"]}, ["docs/owned.md"]
    worker._enforce_delivery(world.record)
    assert (world.record["status"], world.record["error"]) == ("done", None)
    assert world.record["delivery_pr"] == "org/repo#78"


def test_exact_head_search_never_credits_a_task_with_no_commits_of_its_own(merged_delivery_world):
    """Defect class: a task that added nothing (its head is where it started, such as a fix round's branch tip)
    was credited with whichever merged pull request ended on that same head (#100 R93-a)."""
    world = merged_delivery_world
    world.record["commits_ahead"] = 0
    world.listed = [{"number": 90, "isCrossRepository": False, "headRefName": "other/task",
                     "headRefOid": world.record["head_sha"]}]
    world.prs[90], world.files[90] = {**MERGED, "headRefOid": world.record["head_sha"]}, ["docs/owned.md"]
    worker._enforce_delivery(world.record)
    assert "delivery_pr" not in world.record
    assert world.record["status"] == "no_deliverable"


def test_a_recorded_pr_with_another_head_does_not_hide_a_later_matching_candidate(merged_delivery_world):
    """Defect class: an accepted merged PR whose head differs from the snapshot ended the search, so the
    exact-head candidate after it was never tried (#100 R93-b)."""
    world = merged_delivery_world
    head = world.record["head_sha"]
    world.record["commits_ahead"] = 2
    world.published = (None, False)
    world.accept(81)
    world.listed = [{"number": 82, "isCrossRepository": False, "headRefName": "other/task", "headRefOid": head}]
    world.prs[81], world.files[81] = {**MERGED, "headRefOid": "b" * 40}, ["docs/owned.md"]
    world.prs[82], world.files[82] = {**MERGED, "headRefOid": head}, ["docs/owned.md"]
    worker._enforce_delivery(world.record)
    assert (world.record["status"], world.record["error"]) == ("done", None)
    assert world.record["delivery_pr"] == "org/repo#82"


def test_merged_pr_outside_owned_paths_is_still_refused(merged_delivery_world):
    """Control: a merged PR is no licence to change paths outside the task's ownership."""
    world = merged_delivery_world
    world.accept(79)
    world.prs[79] = {**MERGED, "headRefOid": world.record["head_sha"]}
    world.files[79] = ["docs/owned.md", "scripts/elsewhere.py"]
    worker._enforce_delivery(world.record)
    assert (world.record["status"], world.record["error"]) == (
        "no_deliverable", "outside_owned_paths: scripts/elsewhere.py")
    # A name that only differs from an owned path by a trailing newline is not that path.
    world.record.update(status="done", error=None)
    world.files[79] = ["docs/owned.md\n"]
    worker._enforce_delivery(world.record)
    assert world.record["status"] == "no_deliverable"
    assert world.record["error"].startswith("outside_owned_paths: docs/owned.md")


@pytest.mark.parametrize("fault", [
    "open-pr", "merged-before-start", "foreign-base", "unpublished-head", "github-timeout", "ledger-unreadable",
    "missing-field",
])
def test_unpushed_change_without_a_provable_merged_delivery_is_still_refused(merged_delivery_world, fault):
    """Control: unpushed work, even beside a merged, stale or foreign PR or a failed lookup, keeps its exact refusal."""
    from lu_machinery.orchestration import pr_ownership

    world = merged_delivery_world
    world.accept(80)
    world.prs[80], world.files[80] = dict(MERGED), ["docs/owned.md"]
    world.record["commits_ahead"] = 1
    expected = "work_not_pushed_or_unknown"
    if fault == "open-pr":
        world.prs[80]["state"] = "OPEN"
    elif fault == "merged-before-start":
        world.prs[80]["mergedAt"] = "2026-10-29T07:00:00Z"
    elif fault == "foreign-base":
        world.prs[80]["baseRefName"] = "other"
    elif fault == "unpublished-head":
        # Merged work must not hide a further local commit that was never pushed.
        world.published = (None, False)
    elif fault == "github-timeout":
        world.lookup_error = subprocess.TimeoutExpired(["gh"], 60)
    elif fault == "ledger-unreadable":
        pr_ownership.state_path().write_text("{")
    else:
        del world.record["commits_ahead"]
        world.published = (None, False)
        expected = "no_commits_or_unknown"
    worker._enforce_delivery(world.record)
    assert (world.record["status"], world.record["error"]) == ("no_deliverable", expected)
    assert "delivery_pr" not in world.record
    if fault == "open-pr":
        world.record.update(status="done", owned_paths=None)
        worker._enforce_delivery(world.record)
        assert world.record["error"] == "work_not_pushed_or_unknown"


@pytest.mark.parametrize(("commits", "closeout_code"), [(0, 0), (3, 0), (3, 1)])
def test_additional_task_prs_recover_split_branch_delivery(
    merged_delivery_world, monkeypatch, tmp_path, commits, closeout_code,
):
    """Defect class: accepted split PRs must be recovered and closed out without paginating closed PRs.

    Unrecorded prefix-split branches require ``pr_ownership accept`` (D-SETTLE-FRESH).
    """
    world = merged_delivery_world
    world.record["commits_ahead"] = commits
    world.listed = [
        {"number": 78, "isCrossRepository": False, "headRefName": "claude/merge-probe-layout"},
        {"number": 79, "isCrossRepository": False, "headRefName": "claude/merge-probe-alerts"},
    ]
    world.prs = {78: dict(MERGED), 79: {**MERGED, "headRefOid": "e" * 40,
                                     "mergeCommit": {"oid": world.record["head_sha"]}}}
    world.files = {78: ["docs/owned.md"], 79: ["docs/second.md"]}
    world.record["owned_paths"].append("docs/second.md")
    if commits == 0:
        # Unrecorded prefix-split branches are never discovered via closed-PR pagination.
        assert git.merged_delivery(world.record) is None
    world.accept(78, 79)
    worker._enforce_delivery(world.record)
    assert (world.record["status"], world.record["error"]) == ("done", None)
    assert world.record["delivery_prs"] == ["org/repo#78", "org/repo#79"]
    assert "no_deliverable_reason" not in world.record

    # The existing closeout chain owns every merged split, after worktree removal.
    root = tmp_path / "checkout"
    root.mkdir()
    world.record["repo"] = "fixture"
    monkeypatch.setattr(git, "resolve_repo", lambda _: (root, {}))
    monkeypatch.setattr(worker, "_stop_task_services", lambda _: None)
    monkeypatch.setattr(worker, "_settle_task_scratch", lambda _record, **_kwargs: None)
    monkeypatch.setattr(worker, "_reap_worktree", lambda record, **_: record.update(worktree_disposition="removed"))
    calls = []

    def closeout(request):
        assert world.record["worktree_disposition"] == "removed"
        calls.append(request["pr"])
        return {"pr": request["pr"], "returncode": closeout_code if request["pr"] == 79 else 0}

    monkeypatch.setattr(worker, "_run_merge_closeout", closeout)
    worker._reap_settled(world.record)
    assert calls == [78, 79]
    assert world.record["closeout"]["returncode"] == closeout_code
    assert world.record["status"] == ("failed" if closeout_code else "done")
    worker._reap_settled(world.record)
    assert calls == [78, 79]


@pytest.mark.parametrize("fault", ["outside-path", "reverted-outside-path", "foreign-trailer", "branch-collision", "invalid-branch-page", "mixed-trailers", "own-exact-with-mixed-successor"])
def test_split_delivery_checks_every_owned_path(merged_delivery_world, monkeypatch, fault):
    """Defect class: accepting the first merged PR hides an unowned path or credits another task's PR."""
    world = merged_delivery_world
    world.record["commits_ahead"] = 2
    world.listed = [
        {"number": 78, "isCrossRepository": False, "headRefName": "claude/merge-probe-layout"},
        {"number": 79, "isCrossRepository": False, "headRefName": "claude/merge-probe-alerts"},
    ]
    world.prs = {78: dict(MERGED), 79: dict(MERGED)}
    world.files = {78: ["docs/owned.md"], 79: ["outside.txt"]}
    if fault in {"outside-path", "reverted-outside-path"}:
        world.accept(78, 79)
        world.prs[79]["headRefOid"] = world.record["head_sha"]
    if fault == "reverted-outside-path":
        world.files[79] = ["docs/owned.md"]
        monkeypatch.setattr(worker.ownership, "delivered_paths", lambda *_args, **_kwargs: ["outside.txt"])
    elif fault == "foreign-trailer":
        world.listed[0]["headRefOid"] = world.listed[1]["headRefOid"] = "d" * 40
        world.messages[78] = world.messages[79] = "X-Agent: claude/other-task"
    elif fault == "branch-collision":
        world.listed[0]["headRefName"] = world.listed[1]["headRefName"] = "claude/merge-probeother"
    elif fault == "invalid-branch-page":
        world.branch_pages = [{"number": "not-a-page"}]
    elif fault in {"mixed-trailers", "own-exact-with-mixed-successor"}:
        world.listed[1]["headRefOid"] = "d" * 40
        world.files[79] = ["docs/owned.md"]
        world.messages[79] = "X-Agent: claude/merge-probe\nX-Agent: claude/successor-task"
        if fault == "own-exact-with-mixed-successor":
            world.listed[0]["headRefName"] = "claude/merge-probe"
            world.prs[78]["headRefOid"] = world.record["head_sha"]
            world.files[79] = ["foreign-task.txt"]
    worker._enforce_delivery(world.record)
    if fault == "own-exact-with-mixed-successor":
        assert (world.record["status"], world.record["error"]) == ("done", None)
        assert world.record["delivery_prs"] == ["org/repo#78"]
        assert world.record["merged_delivery_exclusions"] == [
            {"repo": "org/repo", "number": 79, "reason": "ambiguous_task_attribution"}]
        proof = git.merged_delivery(world.record)
        assert [pr["number"] for pr in proof["pull_requests"]] == [78]
        return
    assert world.record["status"] == "no_deliverable"
    if fault in {"outside-path", "reverted-outside-path"}:
        assert world.record["error"] == "outside_owned_paths: outside.txt"
    else:
        assert "delivery_pr" not in world.record
    if fault == "mixed-trailers":
        assert world.record["merged_delivery_lookup"] == "ambiguous_task_attribution"


@pytest.mark.parametrize("dirty", [False, True])
def test_split_delivery_rejects_unpublished_snapshot(merged_delivery_world, dirty):
    """Defect class: a merged split must never conceal a later unpushed commit or tracked edit."""
    world = merged_delivery_world
    world.record.update(commits_ahead=2, worktree_dirty_on_exit=dirty)
    world.published = (None, False)
    world.accept(78)
    world.listed = [{"number": 78, "isCrossRepository": False, "headRefName": "claude/merge-probe-layout"}]
    world.prs[78], world.files[78] = dict(MERGED), ["docs/owned.md"]
    worker._enforce_delivery(world.record)
    assert world.record["status"] == "no_deliverable"
    assert "delivery_pr" not in world.record


@pytest.mark.parametrize("merged", [False, True])
def test_split_delivery_preserves_provider_failure_evidence(merged_delivery_world, merged):
    """Defect class: delivery recovery must retain the provider's original failure and require merged proof."""
    world = merged_delivery_world
    world.record.update(status="failed", error="provider exited unsuccessfully", provider_terminal_failure=True,
                        commits_ahead=2, returncode=17)
    world.accept(78)
    world.listed = [{"number": 78, "isCrossRepository": False, "headRefName": "claude/merge-probe-layout"}]
    world.prs[78] = {**MERGED, "state": "MERGED" if merged else "OPEN", "headRefOid": world.record["head_sha"]}
    world.files[78] = ["docs/owned.md"]
    worker._enforce_delivery(world.record)
    assert world.record["status"] == ("done" if merged else "failed")
    assert world.record["provider_terminal_error"] == "provider exited unsuccessfully"
    assert world.record["returncode"] == 17
    assert world.record["error"] == (None if merged else "provider exited unsuccessfully")


@pytest.mark.parametrize("reuse_cwd", [False, True])
def test_preexisting_branch_commits_are_not_attributed_to_new_task(sandbox, reuse_cwd, monkeypatch):
    if reuse_cwd:
        tree = prior_worktree(sandbox)
        flags = ["--cwd", str(tree)]
    else:
        tree = sandbox.repo
        run_git(tree, "checkout", "-b", "feature-x")
        flags = ["--branch", "feature-x"]
    (tree / "outside.txt").write_text("previous task\n")
    run_git(tree, "add", "outside.txt")
    run_git(tree, "commit", "-m", "preexisting outside scope")
    starting_head = run_git(tree, "rev-parse", "HEAD")
    if not reuse_cwd:
        run_git(tree, "push", "origin", "feature-x")
    result = sandbox.settle(sandbox.dispatch("pushed", "--mode", "workspace-write",
                                            "--owned-path", "delivered.txt", *flags))
    assert result["worktree_base_sha"] == starting_head
    assert (result["status"], result["error"]) == ("done", None)
    # Settlement can now remove this published tree; restore the exact head for the older-Git proof.
    if not Path(result["worktree_path"]).exists():
        run_git(sandbox.repo, "worktree", "add", "--detach", result["worktree_path"], result["head_sha"])
    original = git._verification_git
    monkeypatch.setattr(git, "_verification_git", lambda path, command, *args, **kw:
                        "git version 2.35.0" if command == "version" else original(path, command, *args, **kw))
    worker._enforce_delivery(result)
    assert (result["status"], result["error"]) == ("done", None)


@pytest.mark.parametrize("attach", ["branch", "pinned-head", "cwd"])
@pytest.mark.parametrize("new_path", ["delivered.txt", "new-outside.txt"])
def test_branch_fix_round_ownership_starts_after_preexisting_commits(sandbox, attach, new_path):
    tree = prior_worktree(sandbox)
    base = run_git(sandbox.repo, "rev-parse", "origin/main")
    (tree / "earlier-outside.txt").write_text("previous round\n")
    run_git(tree, "add", "earlier-outside.txt")
    run_git(tree, "commit", "-m", "previous round outside current scope")
    starting_head = run_git(tree, "rev-parse", "HEAD")
    branch = "codex/prior-round"
    run_git(tree, "push", "origin", "HEAD")
    flags = ["--cwd", str(tree)] if attach == "cwd" else ["--branch", branch]
    if attach == "pinned-head":
        flags += ["--pinned-head", starting_head]
    task = sandbox.dispatch("blocked", "--mode", "workspace-write", "--owned-path", "delivered.txt", *flags)
    path = sandbox.tasks / f"{task[0]}.json"
    record = json.loads(path.read_text())
    await_file(Path(record["prompt_file"]).with_suffix(".started"))
    tree = Path(record["cwd"])
    (tree / new_path).write_text("current round\n")
    run_git(tree, "add", new_path)
    run_git(tree, "commit", "-m", "current round delivery")
    run_git(tree, "push", "origin", f"HEAD:{branch}")
    Path(record["prompt_file"]).with_suffix(".release").touch()
    outside = new_path != "delivered.txt"
    result = sandbox.settle(task, expected=1 if outside else 0)
    if attach != "cwd":
        # Settlement removed the published worktree; re-attach it at its head for the recheck below.
        assert (result["worktree_disposition"], result.get("worktree_disposition_reason")) == ("removed", "clean")
        run_git(sandbox.repo, "worktree", "add", "-b", result["worktree_branch"], result["cwd"], result["head_sha"])
    # Recheck the reported defect's record shape: base is main, start is the branch tip.
    # Keep the actual start captured at dispatch, independent of the configured base.
    result.update(worktree_base_sha=base, status="done", error=None)
    result.update(git.snapshot_delivery(worker._spec(result, {}), result))
    worker._enforce_delivery(result)
    assert (result["status"], result["error"]) == (
        ("no_deliverable", "outside_owned_paths: new-outside.txt") if outside else ("done", None)
    )
    assert result["worktree_start_sha"] == starting_head
    assert result["worktree_base_sha"] == base
    assert result["commits_ahead"] == 1
    assert result["push_state"] == "pushed"
    assert result["remote_head_sha"] == result["head_sha"]


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("new_path", ["delivered.txt", "new-outside.txt"])
def test_branch_fix_round_uses_remote_start_when_local_branch_is_stale(sandbox, pinned, new_path, monkeypatch):
    branch = "codex/existing-pr"
    stale_head = run_git(sandbox.repo, "rev-parse", "HEAD")
    run_git(sandbox.repo, "branch", branch)
    previous_tree = prior_worktree(sandbox)
    (previous_tree / "earlier-outside.txt").write_text("previous author round\n")
    run_git(previous_tree, "add", "earlier-outside.txt")
    run_git(previous_tree, "commit", "-m", "earlier outside-scope delivery")
    remote_start = run_git(previous_tree, "rev-parse", "HEAD")
    run_git(previous_tree, "push", "origin", f"HEAD:{branch}")
    assert run_git(sandbox.repo, "rev-parse", branch) == stale_head
    if not pinned:
        # Change shared refs and URL rewrites after origin validation. Neither may
        # replace the object advertised through the private canonical origin.
        run_git(previous_tree, "commit", "--allow-empty", "-m", "unpublished local tip")
        planted = run_git(previous_tree, "rev-parse", "HEAD")
        alternate = sandbox.repo.parent / "alternate.git"
        run_git(sandbox.repo, "init", "--bare", str(alternate))
        run_git(previous_tree, "push", str(alternate), f"HEAD:{branch}")
        original = git._verification_git
        rewrite = f"url.{alternate}.insteadOf"
        def race(path, command, *args, **kwargs):
            if command == "init":
                run_git(sandbox.repo, "config", rewrite, str(sandbox.remote))
                run_git(sandbox.repo, "config", "remote.origin.url", str(alternate))
            result = original(path, command, *args, **kwargs)
            if command in {"ls-remote", "fetch"}:
                run_git(sandbox.repo, "update-ref", f"refs/remotes/origin/{branch}", planted)
            return result
        with monkeypatch.context() as injected:
            injected.setattr(git, "_verification_git", race)
            resolved, remote = git.publish_start(
                sandbox.repo, {"github": str(sandbox.remote), "transport": "file"}, branch, "private-start",
            )
        assert (resolved, remote) == (remote_start, str(sandbox.remote))
        assert run_git(sandbox.repo, "rev-parse", f"refs/remotes/origin/{branch}") == planted
        run_git(sandbox.repo, "config", "--unset", rewrite)
        run_git(sandbox.repo, "config", "remote.origin.url", str(sandbox.remote))
    flags = ["--branch", branch]
    if pinned:
        flags += ["--pinned-head", remote_start]
    task = sandbox.dispatch("blocked", "--mode", "workspace-write", "--owned-path", "delivered.txt", *flags)
    record = json.loads((sandbox.tasks / f"{task[0]}.json").read_text())
    await_file(Path(record["prompt_file"]).with_suffix(".started"))
    tree = Path(record["cwd"])
    # A fix worker synchronizes the already-published PR before adding its own commit.
    run_git(tree, "fetch", "origin", branch)
    run_git(tree, "merge", "--ff-only", f"origin/{branch}")
    (tree / new_path).write_text("fix round\n")
    run_git(tree, "add", new_path)
    run_git(tree, "commit", "-m", "current fix round")
    run_git(tree, "push", "origin", f"HEAD:{branch}")
    Path(record["prompt_file"]).with_suffix(".release").touch()
    outside = new_path != "delivered.txt"
    result = sandbox.settle(task, expected=1 if outside else 0)
    assert (result["status"], result["error"]) == (
        ("no_deliverable", "outside_owned_paths: new-outside.txt") if outside else ("done", None)
    )
    assert result["worktree_start_sha"] == remote_start
    assert result["commits_ahead"] == 1
    assert result["push_state"] == "pushed"
    assert result["remote_head_sha"] == result["head_sha"]
    assert run_git(sandbox.repo, "rev-parse", branch) == stale_head


def test_branch_write_starts_at_advertised_object_despite_shared_replace_ref(sandbox):
    """A shared replace ref must not change the published start's checkout or record."""
    branch = "codex/existing-pr"
    advertised = run_git(sandbox.repo, "rev-parse", "HEAD")
    run_git(sandbox.repo, "push", "origin", f"HEAD:refs/heads/{branch}")
    run_git(sandbox.repo, "checkout", "-b", "planted")
    (sandbox.repo / "outside.txt").write_text("outside current ownership\n")
    run_git(sandbox.repo, "add", "outside.txt")
    run_git(sandbox.repo, "commit", "-m", "local outside-scope commit")
    run_git(sandbox.repo, "replace", advertised, run_git(sandbox.repo, "rev-parse", "HEAD"))
    run_git(sandbox.repo, "checkout", "main")
    task = sandbox.dispatch("pushed", "--mode", "workspace-write", "--owned-path", "delivered.txt", "--branch", branch)
    result = sandbox.settle(task)
    assert not (Path(result["cwd"]) / "outside.txt").exists()
    assert result["worktree_start_sha"] == advertised
    assert (result["status"], result["error"], result["commits_ahead"]) == ("done", None, 1)


def test_branch_write_refuses_local_outside_commit_after_remote_branch_deleted(sandbox):
    """A deleted remote must not let a planted local commit escape ownership."""
    branch = "codex/existing-pr"
    run_git(sandbox.repo, "branch", branch)
    run_git(sandbox.repo, "push", "origin", branch)
    (sandbox.repo / "outside.txt").write_text("outside current ownership\n")
    run_git(sandbox.repo, "add", "outside.txt")
    run_git(sandbox.repo, "commit", "-m", "local outside-scope commit")
    planted = run_git(sandbox.repo, "rev-parse", "HEAD")
    run_git(sandbox.repo, "update-ref", f"refs/heads/{branch}", planted)
    run_git(sandbox.repo, "push", "origin", f":refs/heads/{branch}")
    result = subprocess.run(
        [sandbox.python, "lu_machinery/dispatch.py", "dispatch", "--agent", "sol", "--model", "pushed",
         "--prompt", "literal final response", "--task-id", "missing-remote", "--mode", "workspace-write",
         "--owned-path", "delivered.txt", "--branch", branch],
        cwd=sandbox.repo, env=sandbox.env, text=True, capture_output=True, timeout=30,
    )
    if result.returncode == 0:
        accepted = sandbox.settle(tuple(result.stdout.splitlines()))
        assert accepted["worktree_start_sha"] == planted
        assert (Path(accepted["cwd"]) / "outside.txt").read_text() == "outside current ownership\n"
        print(f"Deleted-remote delivery accepted: status={accepted['status']}, error={accepted['error']}")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "publish branch is absent from the canonical remote" in result.stderr
    assert not (sandbox.tasks / "missing-remote.json").exists()
    assert not (sandbox.repo / ".worktrees/dispatch/codex/missing-remote").exists()


def test_takeover_uses_recorded_start_after_history_rewrite(sandbox, delivery_settle):
    """The old published start may be detached from both tips after a takeover rewrites its branch."""
    record, settle = delivery_settle
    tree = Path(record["cwd"])
    (tree / "predecessor.txt").write_text("first predecessor commit\n")
    run_git(tree, "add", "predecessor.txt")
    run_git(tree, "commit", "-m", "predecessor")
    predecessor = run_git(tree, "rev-parse", "HEAD")
    run_git(tree, "commit", "--allow-empty", "-m", "superseded predecessor tip")
    record["worktree_start_sha"] = run_git(tree, "rev-parse", "HEAD")
    run_git(tree, "push", "origin", f"HEAD:{record['worktree_branch']}")
    run_git(tree, "reset", "--hard", predecessor)
    run_git(tree, "push", "--force-with-lease", "origin", f"HEAD:{record['worktree_branch']}")

    delivered = settle("pushed")
    assert (delivered["status"], delivered["error"]) == ("done", None)


def test_workspace_write_sha_base_is_refused_at_dispatch(sandbox):
    """A commit SHA is not an origin base, so settlement would record no_deliverable."""
    sha = run_git(sandbox.repo, "rev-parse", "HEAD")
    result = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "literal final response",
        "--task-id", "sha-base-write", "--mode", "workspace-write", "--base", sha,
        expected=2,
    )
    assert "workspace_write_base_unverified:" in result.stderr
    assert "--base origin/main --branch <PR head branch>" in result.stderr
    assert "with the PR's files as owned paths" in result.stderr
    assert not (sandbox.tasks / "sha-base-write.json").exists()
    assert not (sandbox.repo / ".worktrees/dispatch/codex/sha-base-write").exists()


def test_workspace_write_base_equal_to_branch_is_refused_at_dispatch(sandbox):
    """A publish branch used as its own base is refused before a task or worktree exists (#12667)."""
    branch = "codex/existing-pr"
    remote_head = _published_branch(sandbox, branch)
    refused = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "takeover",
        "--task-id", "base-equals-branch", "--mode", "workspace-write",
        "--owned-path", "delivered.txt", "--base", f"origin/{branch}", "--branch", branch,
        expected=2,
    )
    assert "workspace_write_base_equals_branch:" in refused.stderr
    assert "--base origin/main --branch <PR head branch>" in refused.stderr
    assert "with the PR's files as owned paths" in refused.stderr
    assert not (sandbox.tasks / "base-equals-branch.json").exists()
    assert not (sandbox.repo / ".worktrees/dispatch/codex/base-equals-branch").exists()
    # refs/remotes/origin/<branch> is the same base, and it is refused the same way.
    prefixed = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "takeover",
        "--task-id", "base-equals-branch-prefixed", "--mode", "workspace-write",
        "--owned-path", "delivered.txt", "--base", f"refs/remotes/origin/{branch}", "--branch", branch,
        expected=2,
    )
    assert "workspace_write_base_equals_branch:" in prefixed.stderr
    assert not (sandbox.tasks / "base-equals-branch-prefixed.json").exists()
    # The allowed form still dispatches: origin/main with a different publish branch.
    allowed = sandbox.settle(sandbox.dispatch(
        "pushed", "--task-id", "base-is-main", "--mode", "workspace-write",
        "--owned-path", "delivered.txt", "--base", "origin/main", "--branch", branch,
    ))
    assert (allowed["status"], allowed["error"]) == ("done", None)
    assert allowed["worktree_base_ref"] == "origin/main"
    assert allowed["publish_branch"] == branch
    assert allowed["worktree_start_sha"] == remote_head


def test_read_only_sha_base_is_still_admitted(sandbox):
    """Read-only reviews keep a commit SHA as --base; settlement does not verify it."""
    sha = run_git(sandbox.repo, "rev-parse", "HEAD")
    record = sandbox.settle(sandbox.dispatch("echo", "--task-id", "sha-base-read", "--mode", "read-only", "--base", sha))
    assert record["status"] == "done"
    assert record["mode"] == "read-only"
    assert record["worktree_base_ref"] == sha


def test_stale_local_ref_never_yields_read_only_checkout_at_stale_commit(sandbox):
    """Defect (#12441): a branch-pinned read-only dispatch ran at a stale local ref, ignoring --base."""
    sandbox.review_models("echo")
    stale = run_git(sandbox.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m",
                    "stale authored change\n\nX-Agent: claude/claude-opus-5-5")
    run_git(sandbox.repo, "branch", "review-stale", stale)
    run_git(sandbox.repo, "push", "origin", "review-stale")
    remote_head = run_git(sandbox.repo, "commit-tree", "HEAD^{tree}", "-p", stale, "-m",
                          "newer authored change\n\nX-Agent: claude/claude-opus-5-5")
    run_git(sandbox.repo, "push", "origin", f"{remote_head}:refs/heads/review-stale")
    assert run_git(sandbox.repo, "rev-parse", "refs/heads/review-stale") == stale
    record = sandbox.settle(sandbox.dispatch(
        "echo", "--task-id", "stale-local-ref", "--mode", "read-only",
        "--branch", "review-stale", "--base", remote_head,
    ))
    assert record["status"] == "done"
    assert record["worktree_start_sha"] == remote_head
    assert record["pinned_head"] == remote_head
    assert record["head_sha"] == remote_head
    # The fix never moves the primary checkout's local branch ref.
    assert run_git(sandbox.repo, "rev-parse", "refs/heads/review-stale") == stale
    refused = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "review",
        "--task-id", "stale-local-ref-mismatch", "--mode", "read-only",
        "--branch", "review-stale", "--base", stale, expected=2,
    )
    assert "read_only_branch_base_mismatch:" in refused.stderr
    assert not (sandbox.tasks / "stale-local-ref-mismatch.json").exists()
    # Git accepts unique abbreviations from four characters; they refuse the same way.
    short = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "review",
        "--task-id", "stale-local-ref-short", "--mode", "read-only",
        "--branch", "review-stale", "--base", stale[:6], expected=2,
    )
    assert "read_only_branch_base_mismatch:" in short.stderr
    assert not (sandbox.tasks / "stale-local-ref-short.json").exists()


def _published_branch(sandbox, name):
    """Push ``name`` to origin at a commit that is not the local ``name`` (if any)."""
    remote_head = run_git(sandbox.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "remote pr head")
    run_git(sandbox.repo, "push", "origin", f"{remote_head}:refs/heads/{name}")
    return remote_head


def _write_branch_plan(sandbox, branch, *extra):
    result = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "takeover", "--mode", "workspace-write",
        "--owned-path", "delivered.txt", "--branch", branch, "--dry-run", *extra,
    )
    assert not sandbox.tasks.exists()
    return json.loads(result.stdout)


def test_write_branch_dry_run_without_local_ref_starts_at_remote_head(sandbox):
    """Defect (#12468): a write --branch preview rev-parsed a missing local ref and died as CalledProcessError."""
    remote_head = _published_branch(sandbox, "takeover-no-local")
    assert not run_git(sandbox.repo, "branch", "--list", "takeover-no-local")
    assert _write_branch_plan(sandbox, "takeover-no-local")["start_sha"] == remote_head
    assert not run_git(sandbox.repo, "branch", "--list", "takeover-no-local")


def test_write_branch_dry_run_ignores_stale_local_ref(sandbox):
    """Defect (#12468): a stale local refs/heads/<branch> must never set the start of a takeover."""
    stale = run_git(sandbox.repo, "rev-parse", "HEAD")
    run_git(sandbox.repo, "branch", "takeover-stale", stale)
    remote_head = _published_branch(sandbox, "takeover-stale")
    assert _write_branch_plan(sandbox, "takeover-stale")["start_sha"] == remote_head
    assert run_git(sandbox.repo, "rev-parse", "refs/heads/takeover-stale") == stale
    refused = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "takeover", "--mode", "workspace-write",
        "--owned-path", "delivered.txt", "--branch", "takeover-missing", "--dry-run", expected=2,
    )
    assert "branch_absent_on_remote:" in refused.stderr and "takeover-missing" in refused.stderr


def test_failed_git_call_reports_its_operation_and_a_constant_summary_without_secrets(capsys, monkeypatch):
    """Defect (#12468): a failed git call printed only the exception class name.

    Nothing user-controlled is printed: the subcommand and a summary chosen from the first stderr line, both
    constants. Every disclosure probe from the review of the first attempt must end in them.
    """
    def fail(command, stderr):
        error = subprocess.CalledProcessError(128, [command[0], "-C", "/home/ops/private", *command[1:]], stderr=stderr)
        monkeypatch.setattr(cli, "_dispatch", Mock(side_effect=error))
        assert cli.main(["dispatch", "--agent", "sol", "--model", "echo", "--prompt", "x"]) == 2
        return capsys.readouterr().err

    assert fail(["git", "rev-parse", "--verify", "topic^{commit}"], "fatal: Needed a single revision") == (
        "dispatcher: CalledProcessError: git rev-parse exited 128: needed a single revision\n")
    assert fail(["git", "fetch", "--", ('https' + '://' + 'user' + ':' + 'token' + '@' + 'example.com/org/repo.git'), "abc"],
                ("fatal: could not read from " + "https" + "://" + "user" + ":" + "token" + "@" + "example.com/org/repo.git\nsecond line\n")) == (
        "dispatcher: CalledProcessError: git fetch exited 128: remote not reachable\n")
    assert fail(["curl", "https://example.com"], "secret") == "dispatcher: CalledProcessError: external command exited 128\n"
    assert fail(["/usr/bin/git", "fetch"], "fatal: could not read from x") == (
        "dispatcher: CalledProcessError: git fetch exited 128: remote not reachable\n")
    assert fail(["git", "--git-dir", "/synthetic/x", "fetch"], "fatal: denied") == (
        "dispatcher: CalledProcessError: git fetch exited 128: unrecognized git error\n")
    summaries = {text for _pattern, text in cli._GIT_FAILURES} | {cli.UNRECOGNIZED_GIT_FAILURE}
    for command, stderr, leaked in (
        (["git", "fetch"], "remote: Authorization: Bearer SYNTHETIC_SECRET", "SYNTHETIC_SECRET"),
        (["git", "fetch"], "remote: Password: shallow branch denied object", "shallow"),
        (["git", "fetch"], "fatal: cannot open '/synthetic/Acme Open Access Client/repo': denied", "Access"),
        (["git", "fetch"], "fatal: Unable to create '/synthetic/private directory/worktree/index.lock': denied", "worktree"),
        (["git", "worktree", "add", "--no-checkout", "worktree", "status"], "remote: secret", "worktree status"),
        (["git", "fetch", "user:syntheticsecret"], "fatal: denied", "syntheticsecret"),
    ):
        message = fail(command, stderr)
        assert message.rstrip("\n").rsplit(": ", 1)[1] in summaries
        assert leaked not in message and "synthetic" not in message.lower() and "/home/ops/private" not in message


def test_scope_error_is_bounded_and_local_base_fails_closed(delivery_settle, monkeypatch):
    record, settle = delivery_settle
    record = settle("pushed")
    original = git._verification_git
    monkeypatch.setattr(git, "_verification_git", lambda path, command, *args, **kw:
                        "\0".join(f"outside-{n:04d}.txt" for n in range(200)) if command == "log"
                        else original(path, command, *args, **kw))
    worker._enforce_delivery(record)
    assert record["status"] == "no_deliverable"
    assert record["error"].startswith("outside_owned_paths: outside-0000.txt")
    assert len(record["error"]) <= 1024
    assert record["error"].endswith("... (truncated)")
    record.update(status="done", worktree_base_ref="main")
    worker._enforce_delivery(record)
    assert (record["status"], record["error"]) == ("no_deliverable", "owned_paths_verification_failed: ownership verification requires an origin base")


@pytest.mark.parametrize("transport", ["ssh", "https"])
def test_canonical_origins_does_not_reject_same_identity_when_clone_transport_differs(transport):
    """Clone transport mismatches must not reject the registered repository identity."""
    origins = git.canonical_origins({"github": "org/repo", "transport": transport})
    assert "https://github.com/org/repo.git" in origins
    accepted = {
        "https://github.com/org/repo.git", "https://github.com/org/repo",
        "git@github.com:org/repo.git", "git@github.com:org/repo",
        "ssh://git@github.com/org/repo.git", "ssh://git@github.com/org/repo",
    }
    assert set(origins) == accepted
    assert len(origins) == len(accepted)
    refused = [
        "https://github.com/org/other.git", "git@github.com:org/other.git", "https://github.com/org/rep.git",
        "https://github.com/org/repo-fork.git", "https://github.com/someone/repo.git", "git@github.com:someone/repo.git",
        "https://gitlab.com/org/repo.git", "git@gitlab.com:org/repo.git", "ssh://git@example.com/org/repo.git",
        "/srv/org/repo.git", "file:///srv/org/repo.git", ('https' + '://' + 'user' + ':' + 'token' + '@' + 'github.com/org/repo.git'),
        "https://token@github.com/org/repo.git", "ssh://user:token@github.com/org/repo.git",
        "https://github.com/Org/repo.git", "https://GitHub.com/org/repo.git", "git@github.com:org/Repo.git",
        "https://github.com/org/repo/", "https://github.com/org/repo.git/", "git@github.com:org/repo.git/",
        "https://github.com/org/repo.git.bak", "git@github.com:org/repo.gitx", "https://github.com/org/repo.git/extra",
        " https://github.com/org/repo.git", "\tgit@github.com:org/repo.git", "https://github.com/org/repo.git\n",
    ]
    for origin in refused:
        assert origin not in origins


def test_prior_worker_origin_poisoning_refuses_dispatch_and_fails_closed(sandbox):
    """Shared config poisoned before dispatch cannot certify a forged empty range."""
    earlier = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    # Expire the previous reservation so the next dispatch reaches the origin check.
    earlier["started_at"] = "2000-01-01T00:00:00Z"
    (sandbox.tasks / f"{earlier['task_id']}.json").write_text(json.dumps(earlier), encoding="utf-8")
    fake = sandbox.repo.parent / "forged.git"
    tree = Path(earlier["cwd"])
    run_git(tree, "clone", "--bare", str(sandbox.remote), str(fake))
    run_git(tree, "remote", "set-url", "origin", str(fake))
    result = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "pushed", "--prompt", "deliver",
        "--task-id", "poisoned", "--mode", "workspace-write", "--owned-path", "delivered.txt", expected=2,
    )
    assert "origin must match the registered canonical repository URL" in result.stderr
    assert not (sandbox.tasks / "poisoned.json").exists()
    assert not (sandbox.tasks / "poisoned.started").exists()
    # Read-only admission still records missing trust; even a later write settle fails closed.
    record = sandbox.settle(sandbox.dispatch())
    assert record["worktree_remote_url"] is None
    # Its own worktree was removed at settlement; forge the delivery from an earlier round's worktree.
    tree = prior_worktree(sandbox)
    for name in ("outside.txt", "delivered.txt"):
        (tree / name).write_text("forged delivery\n")
    run_git(tree, "add", "outside.txt", "delivered.txt")
    run_git(tree, "commit", "-m", "forged delivery")
    run_git(tree, "push", "origin", f"HEAD:refs/heads/{record['worktree_branch']}", "HEAD:main")
    record.update(
        worktree_path=str(tree), cwd=str(tree), mode="workspace-write", owned_paths=["delivered.txt"], head_sha=run_git(tree, "rev-parse", "HEAD"),
        push_state="pushed", commits_ahead=1, worktree_dirty_on_exit=False,
    )
    assert run_git(fake, "rev-parse", "main") == record["head_sha"]
    assert run_git(sandbox.remote, "rev-parse", "main") == record["worktree_base_sha"]
    worker._enforce_delivery(record)
    assert record["status"] == "no_deliverable"
    assert record["error"].startswith("owned_paths_verification_failed: ")


@pytest.mark.usefixtures('configured_roots')
def test_read_only_task_removes_its_worktree_at_settle(sandbox):
    """Defect: the dispatcher left every task worktree it created for a separate reaper."""
    record = sandbox.settle(sandbox.dispatch())
    assert record["status"] == "done"
    assert not Path(record["worktree_path"]).exists()
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("removed", "clean")
    assert record["worktree_branch"] not in local_branches(sandbox.repo)
    assert Path(record["result_file"]).read_text() == "literal final response"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("model,flags,status", [
    ("pushed", [], "done"),
    # A fix round starts at its publish branch's tip; the task branch never exists on origin.
    ("echo", ["--branch", "release"], "no_deliverable"),
])
def test_pushed_write_task_removes_its_worktree_at_settle(sandbox, model, flags, status):
    """Defect: published write work kept its worktree and local branch after the task ended."""
    run_git(sandbox.repo, "push", "origin", "main:release")
    record = sandbox.settle(sandbox.dispatch(model, "--mode", "workspace-write", *flags),
                            expected=0 if status == "done" else 1)
    assert record["status"] == status
    assert not Path(record["worktree_path"]).exists()
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("removed", "clean")
    assert record["worktree_branch"] not in local_branches(sandbox.repo)
    published = record.get("publish_branch") or record["worktree_branch"]
    assert run_git(sandbox.remote, "rev-parse", f"refs/heads/{published}") == record["head_sha"]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("model,flags,reason", [
    ("pushed-dirty", [], "dirty"),
    ("unpushed", [], "unpublished"),
    ("unpushed", ["--branch", "release"], "remote_mismatch"),
])
def test_dirty_or_unpublished_write_task_keeps_worktree_with_reason(sandbox, model, flags, reason):
    """Defect: settlement must never discard uncommitted or unpublished work, and must say why it kept it.

    Defect (CLEANUP-WORKFILES-1118): a write worker's temp files went to the host temp area. Its TMPDIR is now the
    task scratch. These stub workers run as fallback launches, whose tree is never proven gone, so settlement keeps
    the scratch for tmp_sweep's settled-task rule instead of removing it.
    """
    run_git(sandbox.repo, "push", "origin", "main:release")
    task = sandbox.dispatch(model, "--mode", "workspace-write", *flags)
    record = sandbox.settle(task, expected=1)
    assert record["status"] == "no_deliverable"
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("retained", reason)
    tree = Path(record["worktree_path"])
    assert (tree / "delivered.txt").exists()
    assert record["worktree_branch"] in local_branches(sandbox.repo)
    assert run_git(tree, "rev-parse", "HEAD") == record["head_sha"]
    leftover = Path((sandbox.tasks / f"{task[0]}.tmpfile").read_text())
    assert leftover.parent == sandbox.scratch_base / task[0] and leftover.read_bytes() == b"t" * 4096
    assert (record["launch_mode"], record["task_scratch_disposition"]) == ("popen-fallback", "kept")
    assert record["task_scratch_disposition_reason"].startswith("a fallback launch's process tree is never proven")


@pytest.mark.usefixtures('configured_roots')
def test_dirty_worktree_is_retried_and_removed_once_its_changes_are_gone(sandbox):
    """Defect: a tree retained as dirty was never looked at again, even after only ignored files were left."""
    record = sandbox.settle(sandbox.dispatch("pushed-dirty", "--mode", "workspace-write"), expected=1)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("retained", "dirty")
    assert record["worktree_disposition_detail"]
    tree = Path(record["worktree_path"])
    sandbox.sweep()  # scheduled recovery finds the same change and keeps the tree
    kept = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert (kept["worktree_disposition"], kept["worktree_disposition_reason"]) == ("retained", "dirty")
    assert tree.is_dir()
    run_git(tree, "reset", "--hard")
    run_git(tree, "clean", "-fd")
    (tree / ".pytest_cache").mkdir()
    (tree / ".pytest_cache/ignored.txt").write_text("ignored output\n")
    (sandbox.repo / ".git/info/exclude").write_text("/.pytest_cache/\n")
    sandbox.sweep()
    settled = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("removed", "clean")
    assert "worktree_disposition_detail" not in settled
    assert not tree.exists()


def preserved_copy(sandbox, record, relative):
    """The bytes settlement copied out of the task's worktree before removing it."""
    receipt = record["branch_holder_archive"]
    assert (receipt["owner"], receipt["retention_disposition"]) == (record["task_id"], "retrieved")
    return (sandbox.repo / receipt["location"] / relative).read_text()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("model,status", [("dirty", "no_deliverable"), ("capacity-dirty", "rate_limited")])
def test_untracked_scratch_is_preserved_and_the_worktree_removed_at_settle(sandbox, model, status):
    """Defect (FR-01): an untracked file a worker left kept its worktree forever; copy it out, then remove."""
    record = sandbox.settle(sandbox.dispatch(model, "--mode", "workspace-write"), expected=1)
    assert record["status"] == status
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("removed", "clean")
    assert not Path(record["worktree_path"]).exists()
    assert record["worktree_branch"] not in local_branches(sandbox.repo)
    assert preserved_copy(sandbox, record, "delivered.txt") == "literal delivered value\n"
    assert json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text()) == record


@pytest.mark.usefixtures('configured_roots')
def test_untracked_names_with_line_breaks_are_preserved_byte_exact_before_removal(sandbox):
    """Defect (#524 F2): removal read untracked names as text, which folds CR and CRLF into LF. Where each folded
    name is another file's, it preserved that file twice and removed the tree with the CR and CRLF files in it."""
    names = ["leftover/cr\rname", "leftover/cr\nname", "leftover/crlf\r\nname", "leftover/crlf\nname"]
    task = sandbox.dispatch("blocked", "--mode", "workspace-write")
    await_file(sandbox.tasks / f"{task[0]}.started")
    tree = Path(json.loads((sandbox.tasks / f"{task[0]}.json").read_text())["worktree_path"])
    (tree / "leftover").mkdir()
    for name in names:
        (tree / name).write_text(f"leftover {name!r}\n")
    (sandbox.tasks / f"{task[0]}.release").touch()
    record = sandbox.settle(task, expected=1)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("removed", "clean")
    assert sorted(entry["path"] for entry in record["branch_holder_archive"]["paths"]) == sorted(names)
    for name in names:
        assert preserved_copy(sandbox, record, name) == f"leftover {name!r}\n"


@pytest.mark.usefixtures('configured_roots')
def test_dirty_detail_naming_a_non_utf8_path_still_encodes_as_utf8(sandbox):
    """Defect (#807 round 1, Carbon F2): a byte-exact read names a non-UTF-8 path with surrogates, which the
    retained tree's detail carried into UTF-8 receipts (scheduled_worktree_cleanup.write_receipt), failing them."""
    task = sandbox.dispatch("blocked", "--mode", "workspace-write")
    await_file(sandbox.tasks / f"{task[0]}.started")
    tree = Path(json.loads((sandbox.tasks / f"{task[0]}.json").read_text())["worktree_path"])
    name = os.fsdecode(b"bad\xffname")
    (tree / name).write_text("staged, never committed\n")
    run_git(tree, "add", "--", name)
    (sandbox.tasks / f"{task[0]}.release").touch()
    record = sandbox.settle(task, expected=1)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("retained", "dirty")
    assert record["worktree_disposition_detail"] == "A  bad\\udcffname"
    json.dumps(record, ensure_ascii=False).encode("utf-8")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_tracked_edit_hidden_by_an_index_flag_keeps_the_worktree(sandbox, flag):
    """Review F1: status omits edits under these flags, so untracked scratch beside one forced a removal."""
    task = sandbox.dispatch("blocked", "--mode", "workspace-write")
    await_file(sandbox.tasks / f"{task[0]}.started")
    tree = Path(json.loads((sandbox.tasks / f"{task[0]}.json").read_text())["worktree_path"])
    run_git(tree, "update-index", flag, "lu_machinery/dispatch.py")
    (tree / "lu_machinery/dispatch.py").write_text("uncommitted tracked edit\n")
    (tree / "scratch.txt").write_text("scratch\n")
    (sandbox.tasks / f"{task[0]}.release").touch()
    record = sandbox.settle(task, expected=1)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("retained", "dirty")
    assert (tree / "lu_machinery/dispatch.py").read_text() == "uncommitted tracked edit\n"


@pytest.mark.usefixtures('configured_roots')
def test_lingering_provider_process_is_ended_at_settlement_before_the_worktree_goes(sandbox):
    """Defect (FR-45, #12520): a provider child alive past settlement kept the tree with no retry; now the
    settling supervisor ends it, so the tree is released only after the process is gone."""
    task = sandbox.dispatch("tree")
    grandchild = int(await_file(sandbox.tasks / f"{task[0]}.grandchild"))
    (sandbox.tasks / f"{task[0]}.release").touch()
    record = sandbox.settle(task)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("removed", "clean")
    assert worker._identity(grandchild) is None
    assert not Path(record["worktree_path"]).exists()


@pytest.mark.usefixtures('configured_roots')
def test_settlement_after_a_dispatcher_module_changes_on_disk_removes_the_worktree(sandbox):
    """Defect (#12634): a worker running while a dispatcher change landed settled with old in-memory and new
    on-disk modules, so removal failed with an ImportError and left a clean tree. The worker now loads its
    post-start code and registry at launch so an on-disk release replacement cannot reach settlement."""
    task = sandbox.dispatch("blocked")
    await_file(sandbox.tasks / f"{task[0]}.started")
    # The change a merge brings: store gains a function, and the module that settlement loads imports it.
    with (sandbox.repo / "lu_machinery/dispatcher/store.py").open("a") as stream:
        stream.write("\n\ndef added_after_the_worker_started():\n    return None\n")
    consumer = sandbox.repo / "lu_machinery/fleet/ignored_task_output.py"
    consumer.write_text("from scripts.dispatcher.store import added_after_the_worker_started\n" + consumer.read_text())
    (sandbox.repo / "lu_machinery/config/fleet_repos.yaml").unlink()
    (sandbox.tasks / f"{task[0]}.release").touch()
    record = sandbox.settle(task)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("removed", "clean"), (
        record.get("worktree_disposition_detail"))
    assert not Path(record["worktree_path"]).exists()


@pytest.mark.usefixtures('configured_roots')
def test_dispatch_and_worker_exit_leave_unrelated_retained_worktrees_unchanged(sandbox):
    """Defect: unrelated admission and exit repeatedly rewrite and retry retained worktrees."""
    record = sandbox.settle(sandbox.dispatch("pushed-dirty", "--mode", "workspace-write"), expected=1)
    record_file = sandbox.tasks / f"{record['task_id']}.json"
    tree = Path(record["worktree_path"])
    with record_file.open("rb") as retained:
        before = retained.read()
        inode = os.fstat(retained.fileno()).st_ino
        task = sandbox.dispatch()
        assert record_file.read_bytes() == before
        assert record_file.stat().st_ino == inode, "admission retried an unrelated retained worktree"
        own = sandbox.settle(task)
        assert record_file.read_bytes() == before
        assert record_file.stat().st_ino == inode, "worker exit retried an unrelated retained worktree"
        assert (tree / "delivered.txt").read_text() == "uncommitted delivery\n"
        assert own["worktree_disposition"] == "removed"
        assert not Path(own["worktree_path"]).exists()
        sandbox.sweep()
        assert record_file.stat().st_ino != inode, "scheduled settlement did not retry the retained worktree"
    retried = json.loads(record_file.read_text())
    assert (retried["worktree_disposition"], retried["worktree_disposition_reason"]) == ("retained", "dirty")
    assert tree.is_dir()
    assert (tree / "delivered.txt").read_text() == "uncommitted delivery\n"
    assert run_git(tree, "rev-parse", "HEAD") == record["head_sha"]


def settled_by_scheduled_sweep(sandbox, retained_id, before_release=lambda _kept: None):
    """Retry a retained tree explicitly, as scheduled cleanup does.

    ``before_release`` sees the first sweep's retained record. A second sweep retries it
    after the callback has had the opportunity to clear the cause.
    """
    record_file = sandbox.tasks / f"{retained_id}.json"
    sandbox.sweep()
    with record_file.open("rb") as retained:
        before = os.fstat(retained.fileno()).st_ino
        before_release(json.loads(retained.read()))
        sandbox.sweep()
        assert record_file.stat().st_ino != before, "scheduled settlement never retried the retained worktree"
    return json.loads(record_file.read_text())


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("model,reason", [("pushed-dirty", "dirty"), ("unpushed", "unpublished")])
def test_settlement_retry_keeps_a_worktree_that_still_holds_content(sandbox, model, reason):
    """The retry removes only with settlement's proofs: uncommitted or unpublished work stays."""
    record = sandbox.settle(sandbox.dispatch(model, "--mode", "workspace-write"), expected=1)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == ("retained", reason)
    retried = settled_by_scheduled_sweep(sandbox, record["task_id"])
    assert (retried["worktree_disposition"], retried["worktree_disposition_reason"]) == ("retained", reason)
    tree = Path(record["worktree_path"])
    assert (tree / "delivered.txt").exists()
    assert run_git(tree, "rev-parse", "HEAD") == record["head_sha"]


def process_group_exists(pgid):
    """Settlement's own liveness probe (worker._provider_alive): a group with any member, zombies included."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.usefixtures('configured_roots')
def test_scheduled_recovery_settles_terminal_task_left_by_crash(sandbox):
    """Defect: a supervisor killed before settlement left its worktree behind for good."""
    task = sandbox.dispatch("blocked")
    # Recovery needs the process group recorded by the supervisor before we kill it.
    record = json.loads(await_file(
        sandbox.tasks / f"{task[0]}.json",
        ready=lambda content: json.loads(content).get("provider_pgid") is not None,
    ))
    provider = record["provider_pgid"]
    os.kill(record["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 10
    while worker._identity(record["pid"]) == record["process_start"]:
        assert time.monotonic() < deadline, "supervisor survived SIGKILL"
        time.sleep(0.02)
    os.killpg(provider, signal.SIGKILL)
    # The orphaned group exists, as zombies, until init reaps it; settlement rightly keeps the tree until then.
    deadline = time.monotonic() + 10
    while process_group_exists(provider):
        assert time.monotonic() < deadline, "provider process group survived SIGKILL"
        time.sleep(0.02)
    assert Path(record["worktree_path"]).is_dir()
    sandbox.sweep()
    crashed = json.loads((sandbox.tasks / f"{task[0]}.json").read_text())
    assert crashed["status"] == "crashed"
    assert not Path(record["worktree_path"]).exists()
    assert (crashed["worktree_disposition"], crashed["worktree_disposition_reason"]) == ("removed", "clean")
    assert record["worktree_branch"] not in local_branches(sandbox.repo)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("redirect", ["set-url", "insteadOf"])
def test_settlement_proves_publication_only_on_the_recorded_canonical_remote(sandbox, monkeypatch, redirect):
    """Defect (F1): settlement trusted the shared, mutable origin instead of the URL recorded at dispatch."""
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    assert record["worktree_disposition_reason"] == "unpublished"
    assert record["worktree_remote_url"] == str(sandbox.remote)
    tree = Path(record["worktree_path"])
    decoy = sandbox.repo.parent / "decoy.git"
    run_git(sandbox.repo, "init", "--bare", str(decoy))
    run_git(tree, "push", str(decoy), f"HEAD:refs/heads/{record['worktree_branch']}")
    if redirect == "set-url":
        run_git(sandbox.repo, "remote", "set-url", "origin", str(decoy))
    else:
        run_git(sandbox.repo, "config", f"url.{decoy}.insteadOf", str(sandbox.remote))
    in_process_repo(sandbox, monkeypatch)
    assert git.settle_worktree(record) == dict(worktree_disposition="retained", worktree_disposition_reason="unpublished")
    assert tree.is_dir()
    assert record["worktree_branch"] in local_branches(sandbox.repo)


@pytest.mark.usefixtures('configured_roots')
def test_write_task_merged_into_main_counts_as_published_after_its_branch_is_deleted(sandbox, monkeypatch):
    """CTO D3: work the canonical main already contains is published, though its branch left the remote."""
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    tree, branch = Path(record["worktree_path"]), record["worktree_branch"]
    run_git(tree, "push", str(sandbox.remote), f"HEAD:refs/heads/{branch}")
    run_git(tree, "push", str(sandbox.remote), "HEAD:refs/heads/main")
    run_git(tree, "push", str(sandbox.remote), f":refs/heads/{branch}")
    in_process_repo(sandbox, monkeypatch)
    assert git.settle_worktree(record) == dict(worktree_disposition="removed", worktree_disposition_reason="clean")
    assert not tree.exists()
    assert branch not in local_branches(sandbox.repo)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("head_moved", [False, True])
def test_reap_uses_verified_merged_snapshot_only_for_same_head(sandbox, monkeypatch, head_moved):
    """Removal uses the pinned registry and merge proof, with the proof bound to one HEAD."""
    from lu_machinery.common import git_credentials
    from lu_machinery.orchestration import fleet_repos, merge_closeout

    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    tree = Path(record["worktree_path"])
    if head_moved:
        run_git(tree, "commit", "--allow-empty", "-m", "later unpublished work")
    in_process_repo(sandbox, monkeypatch)
    registry = git.machinery_path("config/fleet_repos.yaml")
    monkeypatch.setattr(git_credentials, "CONFIG", registry)
    monkeypatch.setattr(fleet_repos, "_CONFIG_PATH", registry)
    git.pin_settlement_registry()
    registry.unlink()
    monkeypatch.setattr(git, "merged_delivery", lambda _: {"repo": "org/repo", "number": 7, "merge_sha": "a" * 40})

    def fetch_pr_info(repo_root, number, *, repo):
        assert (repo_root, number, repo) == (sandbox.repo, 7, "org/repo")
        assert fleet_repos.canonical_repository("public") == str(sandbox.remote).casefold()
        return merge_closeout.PullRequestInfo(number=number, state="MERGED", head_ref_name=None, head_sha=None)

    monkeypatch.setattr(merge_closeout, "fetch_pr_info", fetch_pr_info)

    worker._reap_settled(record)

    assert tree.exists() is head_moved, record.get("worktree_disposition_reason")
    if head_moved:
        assert record["worktree_disposition_reason"] == "unpublished"
        assert "closeout" not in record
    else:
        assert record["worktree_disposition"] == "removed"
        assert record["closeout"]["pr"] == 7
        assert record["closeout"]["returncode"] == 0, record["closeout"]
        assert json.loads(record["closeout"]["stdout"])["ok"] is True


@pytest.mark.usefixtures('configured_roots')
def test_publication_check_accepts_head_contained_in_successor_branch(sandbox, monkeypatch):
    """Defect class: successor-branch ancestry was missed by ordinary publication checks."""
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    tree = Path(record["worktree_path"])
    successor = run_git(tree, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "successor")
    run_git(tree, "push", str(sandbox.remote), f"{successor}:refs/heads/successor")
    in_process_repo(sandbox, monkeypatch)
    verification_git = git._verification_git
    fetches = []

    def counted(path, *args, **kwargs):
        if args[0] == "fetch":
            fetches.append(args)
        return verification_git(path, *args, **kwargs)

    monkeypatch.setattr(git, "_verification_git", counted)
    assert git.settle_worktree(record) == dict(worktree_disposition="removed", worktree_disposition_reason="clean")
    assert len(fetches) == 1
    assert not tree.exists()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("unknown", [False, True])
def test_settlement_ignores_setup_links_and_regenerable_caches_and_records_unknown_ignored_files(
        sandbox, monkeypatch, unknown):
    """Defect: a clean published write task kept its worktree for the links, caches and ignored output it held.

    Ignored output follows the removal chokepoint's rule: inventoried in the task record, then removed.
    """
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    tree, branch = Path(record["worktree_path"]), record["worktree_branch"]
    # As in the project, tracked files keep these directories from collapsing into one ignored entry.
    for tracked in ("data/README", "site/README", ".githooks/pre-commit"):
        (tree / tracked).parent.mkdir(exist_ok=True)
        (tree / tracked).write_text("tracked\n")
    run_git(tree, "add", ".")
    run_git(tree, "commit", "-m", "tracked directories")
    run_git(tree, "push", str(sandbox.remote), f"HEAD:refs/heads/{branch}")
    ignored = [*git.DATABASE_LINKS, *git.NODE_LINKS, "scripts/__pycache__/", ".pytest_cache/", ".ruff_cache/",
               ".mypy_cache/", ".pytest_breadcrumbs/", ".githooks/__pycache__/"]
    if unknown:
        ignored.append("unknown-output/")
    (sandbox.repo / ".git/info/exclude").write_text("".join(f"/{name}\n" for name in ignored))
    for relative in (*git.DATABASE_LINKS, *git.NODE_LINKS):
        target = sandbox.repo / relative  # provision_links links each entry to the primary's own path
        target.parent.mkdir(parents=True, exist_ok=True)
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".db"):
            target.write_text("database")
        else:
            target.mkdir()
        (tree / relative).symlink_to(target)
    for name in ignored:
        if name.endswith("/"):
            (tree / name).mkdir(parents=True)
            (tree / name / "artifact").write_text("regenerated")
    in_process_repo(sandbox, monkeypatch)
    assert git.settle_worktree(record) == dict(worktree_disposition="removed", worktree_disposition_reason="clean")
    assert not tree.exists()
    recorded = [entry["path"] for entry in record.get("preserved_artifacts", {}).get("paths", [])]
    assert recorded == (["unknown-output/artifact"] if unknown else [])


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("output", ["dependencies", "unique-output", "missing-lockfile"])
def test_scheduled_settlement_does_not_retain_regenerable_dependencies_as_unique_output(sandbox, monkeypatch, output):
    """Defect class: hourly settlement counts lockfile-backed dependencies as unique ignored output."""
    from lu_machinery.orchestration import scheduled_worktree_cleanup as cleanup

    (sandbox.repo / ".gitignore").write_text("site/node_modules/\n.cache/\n")
    if output != "missing-lockfile":
        (sandbox.repo / "site").mkdir()
        (sandbox.repo / "site/package-lock.json").write_text('{"lockfileVersion": 3}\n')
        run_git(sandbox.repo, "add", "site/package-lock.json")
    run_git(sandbox.repo, "add", ".gitignore")
    run_git(sandbox.repo, "commit", "-m", "dependency inputs")
    run_git(sandbox.repo, "push", "origin", "main")
    record = sandbox.settle(sandbox.dispatch("unpushed", "--mode", "workspace-write"), expected=1)
    assert record["worktree_disposition_reason"] == "unpublished"
    tree = Path(record["worktree_path"])
    dependency = tree / "site/node_modules/package/index.js"
    dependency.parent.mkdir(parents=True)
    with dependency.open("wb") as stream:
        stream.truncate(git.SALVAGE_IGNORED_LIMIT + 1)
    if output == "unique-output":
        unique = tree / ".cache/unique-evidence.bin"
        unique.parent.mkdir()
        with unique.open("wb") as stream:
            stream.write(b"unpublished evidence")
            stream.truncate(git.SALVAGE_IGNORED_LIMIT + 1)

    successor = run_git(tree, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "successor publication")
    run_git(tree, "push", str(sandbox.remote), f"{successor}:refs/heads/successor")
    assert run_git(sandbox.remote, "show", "successor:delivered.txt") == "literal delivered value"
    in_process_repo(sandbox, monkeypatch)
    (sandbox.repo / "batch_state").mkdir(exist_ok=True)
    (sandbox.repo / "batch_state/tasks").symlink_to(sandbox.tasks, target_is_directory=True)
    report = cleanup.settle_terminal_worktrees(sandbox.repo, apply=True, github=cleanup.GitHubReads())
    settled = json.loads((sandbox.tasks / f"{record['task_id']}.json").read_text())
    assert report["complete"] and report["error"] is None
    if output == "dependencies":
        assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("removed", "clean")
        assert not tree.exists() and report["trees"] == []
        assert run_git(sandbox.remote, "for-each-ref", f"refs/heads/salvage/{record['task_id']}") == ""
    else:
        assert (settled["worktree_disposition"], settled["worktree_disposition_reason"]) == ("retained", "large_ignored_output")
        assert dependency.stat().st_size == git.SALVAGE_IGNORED_LIMIT + 1
        assert report["trees"][0]["reason"] == "large_ignored_output"
        if output == "unique-output":
            with unique.open("rb") as stream:
                assert stream.read(20) == b"unpublished evidence"

pytestmark = [pytest.mark.usefixtures('configured_roots')]
