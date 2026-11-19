"""Delivery evidence on pinned branches, exact-commit reviews, sibling repositories and side-effect-free refusals.

Restored from learn-ukrainian ``tests/dispatcher/test_core_delivery_heads.py`` at 5aba2adb (machinery #904).
The product kept that file because it imported the recorder, which ships here now
(``lu_machinery.review.record_cf_verdict``). Muse's contributor-mode forwarding case is adapted, not
restored as written: machinery #558 forces contributor mode off, and ``muse-spark-1.3`` is still dispatchable
(``lifecycle: hold``).

Defect classes, one per case: a delivery is counted from a prefixed, unrelated or dirty head, or from a
crashed supervisor; a review runs at a branch tip that moved past its pinned commit, or on a reused branch;
a sibling repository's records leave the shared store; a dry run or an invalid flag creates a record,
worktree or branch; a contributor-mode request reaches an agent that cannot honour it; and a Muse dispatch
forwards contributor mode or loses the request from its record.
"""

import json
import os
import shlex
import signal
from pathlib import Path

import pytest

from lu_machinery.dispatcher import git, worker
from lu_machinery.dispatcher.cli import ALIASES
from lu_machinery.review.record_cf_verdict import _review_branch
from tests.dispatcher.conftest import await_file, run_git
from tests.helpers.product_tree import configured_roots  # noqa: F401


@pytest.fixture(autouse=True)
def git_commands_name_their_repository(monkeypatch):
    """Drop the repository-selection names Git reads before the sandbox builds its repositories.

    ``run_git`` is ``git -C``. A caller's ``GIT_DIR`` or injected ``core.worktree`` would select
    another repository, including while the sandbox copies the process environment to its dispatcher.
    """
    from lu_machinery.common.git_context import GIT_REDIRECT_ENV_KEYS

    for name in (*GIT_REDIRECT_ENV_KEYS, "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    # Home and system Git config are also inputs. Point both at an empty file so a
    # caller's core.worktree cannot select a repository after the names above are gone.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in list(os.environ):
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(name, raising=False)
    # Git is the first ``git`` on PATH. An agent-runtime shim scans every push and refuses
    # this fixture repository when its private-matcher rules are not available. These tests
    # run Git itself.
    monkeypatch.setenv("PATH", os.pathsep.join(
        entry for entry in os.environ.get("PATH", "").split(os.pathsep)
        if not entry.rstrip("/").endswith(f"{os.sep}agent_runtime{os.sep}shims")
    ))


@pytest.mark.parametrize(
    ("target", "commits", "dirty", "expected", "reason", "crash"),
    [
        ("pinned", 1, False, "pushed", "remote_head_matches", False),
        ("pinned", 1, False, "pushed", "remote_head_matches", True),
        ("own", 1, False, "pushed", "remote_head_matches", False),
        ("unrelated", 1, False, "unpushed", "remote_head_differs", False),
        ("prefix", 1, False, "unpushed", "remote_branch_missing", False),
        ("pinned", 1, True, "unpushed", "dirty_worktree", False),
        (None, 0, False, "unpushed", "remote_branch_missing", False),
        ("pinned", 0, False, "pushed", "remote_head_matches", False),
    ],
)
@pytest.mark.parametrize("preexisting", [False, True])
def test_delivery_only_counts_exact_heads_on_authorized_branches(
    sandbox, target, commits, dirty, expected, reason, crash, preexisting,
):
    """Prefix tricks and supervisor crashes cannot falsify pinned-branch delivery evidence."""
    pinned = "codex/existing-pr"
    run_git(sandbox.repo, "checkout", "-b", pinned, "main")
    if preexisting:
        (sandbox.repo / "earlier.txt").write_text("previous round\n")
        run_git(sandbox.repo, "add", "earlier.txt")
        run_git(sandbox.repo, "commit", "-m", "preexisting branch commit")
    starting_head = run_git(sandbox.repo, "rev-parse", "HEAD")
    run_git(sandbox.repo, "checkout", "main")
    run_git(sandbox.repo, "push", "origin", pinned)
    task = sandbox.dispatch("blocked", "--mode", "workspace-write", "--branch", pinned)
    record_path = sandbox.tasks / f"{task[0]}.json"
    record = json.loads(record_path.read_text())
    assert record["publish_branch"] == pinned
    child = int(await_file(Path(record["prompt_file"]).with_suffix(".started")))
    if target in {None, "prefix"}:
        run_git(sandbox.repo, "push", "origin", f":refs/heads/{pinned}")
    worktree = Path(record["worktree_path"])
    branch = record["worktree_branch"]
    if commits:
        (worktree / "fix.txt").write_text("fix round\n")
        run_git(worktree, "add", "fix.txt")
        run_git(worktree, "commit", "-m", "fix round")
    remote_branch = {
        "pinned": pinned, "own": branch, "unrelated": "codex/unrelated", "prefix": f"x/refs/heads/{pinned}",
    }.get(target)
    if remote_branch:
        run_git(worktree, "push", "origin", f"HEAD:refs/heads/{remote_branch}")
    if dirty:
        (worktree / "uncommitted.txt").write_text("still local\n")
    head = run_git(worktree, "rev-parse", "HEAD")
    if crash:
        before_crash = git.snapshot_delivery(worker._spec(record, {}), record)
        assert before_crash["push_state"] == "pushed"
        assert before_crash["remote_ref"] == f"refs/heads/{pinned}"
        os.kill(record["pid"], signal.SIGKILL)
        os.killpg(child, signal.SIGKILL)
    else:
        Path(record["prompt_file"]).with_suffix(".release").touch()
    delivery_error = (
        "worktree_dirty_or_unknown" if dirty else "no_commits_or_unknown" if not commits
        else "work_not_pushed_or_unknown" if expected != "pushed" else None
    )
    observed = sandbox.settle(task, expected=1 if crash or delivery_error else 0)
    assert observed["status"] == ("crashed" if crash else "no_deliverable" if delivery_error else "done")
    if not crash:
        assert observed.get("no_deliverable_reason") == delivery_error
    matched = remote_branch if target in {"pinned", "own"} else None
    assert observed["push_state"] == expected
    assert observed["push_state_reason"] == reason
    assert observed["publish_branch"] == pinned
    assert observed["remote_ref"] == (f"refs/heads/{matched}" if matched else None)
    if crash:
        assert observed["remote_ref"] == before_crash["remote_ref"]
    assert observed["head_sha"] == head
    assert observed["commits_ahead"] == commits
    assert observed["worktree_start_sha"] == starting_head
    assert observed["worktree_dirty_on_exit"] is dirty
    if matched:
        assert observed["remote_head_sha"] == head
        assert run_git(sandbox.remote, "rev-parse", f"refs/heads/{matched}") == head
    else:
        assert observed["remote_head_sha"] == (starting_head if target not in {None, "prefix"} else None)


def test_review_uses_exact_commit_on_fresh_branch_and_cwd_reuses(sandbox):
    task = sandbox.dispatch("unpushed", "--mode", "workspace-write")
    first = sandbox.settle(task, expected=1)
    # A read-only --branch dispatch is a review (C-2902 F10): attribute the stub delivery to a family
    # outside the stub reviewer's, keeping its tree and parent.
    sandbox.review_models("echo")
    head = run_git(sandbox.repo, "commit-tree", f"{first['head_sha']}^{{tree}}", "-p", f"{first['head_sha']}^",
                   "-m", "stub delivery\n\nX-Agent: claude/claude-opus-5-5")
    # The branch then moves past the pinned commit, so a review started from the branch tip runs another commit.
    newer = run_git(sandbox.repo, "commit-tree", f"{head}^{{tree}}", "-p", head,
                    "-m", "later round\n\nX-Agent: claude/claude-opus-5-5")
    run_git(sandbox.repo, "update-ref", f"refs/heads/{first['worktree_branch']}", newer)
    assert newer != head
    result = sandbox.cli(
        "dispatch",
        "--agent",
        "sol",
        "--model",
        "echo",
        "--prompt",
        "review",
        "--branch",
        first["worktree_branch"],
        "--pinned-head",
        head,
    )
    review = sandbox.settle(result.stdout.splitlines())
    assert review["worktree_base_sha"] == head
    assert review["pinned_head"] == head
    assert review["review_branch"] == first["worktree_branch"]
    assert _review_branch(review) == first["worktree_branch"]
    assert review["worktree_branch"] != first["worktree_branch"]
    assert review["push_state"] == "not_applicable"
    assert not Path(review["cwd"]).exists()
    reused = sandbox.settle(sandbox.dispatch("echo", "--cwd", first["cwd"]))
    assert (reused["cwd"], reused["worktree_disposition_reason"]) == (first["cwd"], "borrowed_worktree")
    assert Path(first["cwd"]).is_dir()
    assert run_git(sandbox.repo, "branch", "--show-current") == "main"


def test_selected_sibling_repo_keeps_records_in_shared_store(sandbox):
    private = sandbox.repo.parent / "private"
    private.mkdir()
    run_git(private, "init", "-b", "main")
    run_git(private, "config", "user.name", "Stub")
    run_git(private, "config", "user.email", "stub@example.invalid")
    run_git(private, "commit", "--allow-empty", "-m", "private fixture")
    task = sandbox.dispatch("echo", "--repo", "private", "--base", "main")
    record = sandbox.settle(task)
    assert record["repo"] == "private"
    assert record["repository_id"] == record["repository"] == "example/private"
    assert Path(record["cwd"]).is_relative_to(private / ".worktrees/dispatch/codex")
    assert Path(record["prompt_file"]).parent == sandbox.tasks
    assert run_git(private, "branch", "--show-current") == "main"


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_dry_run_and_invalid_flags_create_nothing(sandbox, mode):
    before = run_git(sandbox.repo, "branch", "--list")
    result = sandbox.cli("dispatch", "--agent", "sol", "--model", "echo", "--prompt", "preview", "--dry-run")
    plan = json.loads(result.stdout)
    assert plan["argv"][2] == "echo"
    assert not Path(plan["worktree"]).exists()
    assert not sandbox.tasks.exists()
    assert run_git(sandbox.repo, "branch", "--list") == before
    sandbox.cli("dispatch", "--agent", "sol", "--model", "echo", "--prompt", "x", "--force-new", expected=2)
    sandbox.cli("dispatch", "--agent", "sol", "--model", "echo", "--prompt", "x", "--prompt-file", "y", expected=2)
    for include in ("../curriculum", "site/src", "unknown", ""):
        sandbox.cli("dispatch", "--agent", "sol", "--model", "echo", "--prompt", "x", "--sparse-include", include, expected=2)
    for branch in ("unsafe..branch", "origin/feature", "refs/heads/feature", "bad:name", ""):
        sandbox.cli(
            "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "x",
            "--mode", mode, "--branch", branch,
            "--pinned-head", run_git(sandbox.repo, "rev-parse", "HEAD"), expected=2,
        )
    assert run_git(sandbox.repo, "branch", "--list") == before
    assert not sandbox.tasks.exists()


@pytest.mark.parametrize("agent", sorted((set(ALIASES) | set(ALIASES.values()) | {"claude"}) - {"muse"}))
@pytest.mark.parametrize("contributor", ["on", "off"])
def test_contributor_mode_refuses_non_muse_agents_without_side_effects(sandbox, agent, contributor):
    before = run_git(sandbox.repo, "branch", "--list")
    result = sandbox.cli(
        "dispatch",
        "--agent",
        agent,
        "--model",
        "echo",
        "--prompt",
        "public input",
        "--contributor-mode",
        contributor,
        expected=2,
    )
    assert result.stderr == "dispatcher: --contributor-mode is only supported with --agent muse\n"
    assert not sandbox.tasks.exists()
    assert run_git(sandbox.repo, "branch", "--list") == before


@pytest.mark.parametrize("contributor", ["on", "off", None])
def test_muse_cli_forces_contributor_mode_off_and_keeps_the_request_auditable(sandbox, contributor):
    """A Muse dispatch always runs with contributor mode off and records what was asked for.

    Adapted from ``test_muse_cli_forwards_contributor_mode_and_keeps_auditable_record``: machinery #558 reversed
    forwarding (Muse confinement), so the request is kept only as ``requested_contributor_mode``. The model is
    ``muse-spark-1.3``, which the catalog holds (``lifecycle: hold``) rather than retires.
    Defect classes: an explicit contributor request reaches the provider; the request leaves no audit record.
    """
    args = ["dispatch", "--agent", "muse", "--model", "muse-spark-1.3", "--prompt", "public input"]
    if contributor is not None:
        args += ["--contributor-mode", contributor]
    plan = json.loads(sandbox.cli(*args, "--dry-run").stdout)
    argv = plan["argv"]
    assert argv[argv.index("--contributor-mode") + 1] == "off"
    assert not sandbox.tasks.exists()
    assert not Path(plan["worktree"]).exists()

    # Keep the real file transport, sandbox and parser; only the paid executable is replaced. The sandbox runs
    # the versioned native binary behind the ``muse`` wrapper and permits no host writes, so the stub reports
    # through its exit status: 9 when the provider sees contributor mode on, 8 when the prompt lacks the core.
    binary_dir = sandbox.repo.parent / "bin"
    binary_dir.mkdir(exist_ok=True)
    version = "0.0.0-R1"
    (binary_dir / ".muse-version").write_text(version)
    executable = binary_dir / "muse"
    executable.write_text(f'#!/bin/sh\nexec "$(dirname "$0")/muse-bin-{version}" "$@"\n')
    native = binary_dir / f"muse-bin-{version}"
    event = json.dumps({"payload_type": "run.terminal.completed", "payload": {"text": "Finished."}})
    native.write_text(r'''#!/bin/sh
set -eu
mode=""
prompt=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --contributor-mode) shift; mode="$1" ;;
        --prompt-file) shift; prompt=$(cat "$1") ;;
    esac
    shift
done
[ "$mode" = off ] || exit 9
case "$prompt" in
    '<rules-core seat="core"'*'</rules-core>'*'public input') ;;
    *) exit 8 ;;
esac
''' + f"printf '%s\\n' {shlex.quote(event)}\n")
    for path in (executable, native):
        path.chmod(0o755)
    home = sandbox.repo.parent / "synthetic-home"
    home.mkdir(exist_ok=True)
    sandbox.env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    sandbox.env["PATH"] = str(binary_dir) + os.pathsep + os.defpath
    task = sandbox.cli(*args).stdout.splitlines()
    record = sandbox.settle(task)
    assert record["status"] == "done", record.get("error")
    assert (record["contributor_mode"], record["requested_contributor_mode"]) == ("off", contributor or "off")
