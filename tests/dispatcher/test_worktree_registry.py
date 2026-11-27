"""A repository that declares a worktree registry gets every dispatched worktree registered and archived (#12540)."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from tests.dispatcher.conftest import run_git
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.host_context.conftest import host_layout as host_layout

REGISTRY = ".agent/workspace-hygiene.json"


def declared_repo(sandbox):
    """A sibling checkout whose fleet entry declares a registry that already holds other records."""
    config = sandbox.repo / "lu_machinery/config/fleet_repos.yaml"
    catalog = yaml.safe_load(config.read_text())
    catalog["repos"]["declared"] = dict(local_name="declared", github="example/declared", role="public-content",
                                        worktree_registry=REGISTRY)
    config.write_text(yaml.safe_dump(catalog))
    run_git(sandbox.repo, "commit", "-am", "declare a registry")
    primary = sandbox.repo.parent / "declared"
    primary.mkdir()
    run_git(primary, "init", "-b", "main")
    run_git(primary, "config", "user.name", "Stub")
    run_git(primary, "config", "user.email", "stub@example.invalid")
    run_git(primary, "commit", "--allow-empty", "-m", "fixture")
    registry = primary / REGISTRY
    registry.parent.mkdir()
    other = dict(owner="claude-cto", purpose="hand written", branch="b", head="0" * 40, state="active",
                 next_action="x", pr=None, extra="kept")
    registry.write_text(json.dumps(dict(worktrees={"/elsewhere": other}, artifacts={"/scratch/a": {"keep": 1}}, note="kept")))
    return primary, registry, other


def test_dispatched_worktree_is_registered_before_creation_and_archived_at_removal(sandbox):
    primary, registry, other = declared_repo(sandbox)
    # A non-empty directory at the path makes creation fail after registration, which must not leave a phantom.
    (primary / ".worktrees/dispatch/codex/doomed").mkdir(parents=True)
    (primary / ".worktrees/dispatch/codex/doomed/occupant").write_text("x")
    sandbox.cli("dispatch", "--agent", "sol", "--model", "echo", "--prompt", "never created", "--repo", "declared",
                "--base", "main", "--task-id", "doomed", "--initiator", "claude-cto", expected=2)
    data = json.loads(registry.read_text())
    doomed = data["worktrees"][str(primary / ".worktrees/dispatch/codex/doomed")]
    assert (doomed["state"], doomed["pr"]) == ("archived", None)
    assert "not created" in doomed["next_action"]
    assert "doomed" in doomed["owner"]
    assert "claude-cto" in doomed["owner"]

    task = sandbox.dispatch("echo", "--repo", "declared", "--base", "main", "--initiator", "claude-cto",
                            "--task-id", "registered")
    record = sandbox.settle(task)
    tree = str(primary / ".worktrees/dispatch/codex/registered")
    assert record["worktree_path"] == tree
    assert (record["worktree_disposition"], record["worktree_registry"]) == ("removed", "archived")
    data = json.loads(registry.read_text())
    entry = data["worktrees"][tree]
    assert entry["owner"] == "codex dispatch task registered; initiator claude-cto"
    assert entry["purpose"] == "literal final response"
    assert (entry["branch"], entry["pr"], entry["state"]) == ("codex/registered", None, "archived")
    assert entry["head"] == run_git(primary, "rev-parse", "main")
    assert "removed the worktree" in entry["next_action"]
    # Other records and unknown keys survive untouched; nothing is ever removed.
    assert data["worktrees"]["/elsewhere"] == other
    assert data["artifacts"] == {"/scratch/a": {"keep": 1}} and data["note"] == "kept"
    assert not Path(tree).exists()


def test_register_confirms_branch_and_head_and_a_malformed_registry_is_refused(sandbox):
    from lu_machinery.dispatcher import git

    primary, registry, _ = declared_repo(sandbox)
    tree = primary / ".worktrees/dispatch/codex/direct"
    sha = run_git(primary, "rev-parse", "HEAD")
    git.register_worktree(registry, tree, agent="codex", task_id="direct", initiator=None, purpose="p\nq",
                          branch="codex/direct", sha=sha)
    entry = json.loads(registry.read_text())["worktrees"][str(tree.resolve())]
    assert (entry["state"], entry["pr"], entry["purpose"], entry["head"]) == ("active", None, "p q", sha)
    git.create_worktree(primary, tree, "codex/direct", sha)
    git.confirm_worktree(registry, tree)
    assert git.archive_worktree(registry, tree, "done") is True
    assert git.archive_worktree(registry, tree / "absent", "done") is False
    registry.write_text("[]")
    try:
        git.register_worktree(registry, tree / "next", agent="codex", task_id="n", initiator=None, purpose="p",
                              branch="b", sha=sha)
    except ValueError as exc:
        assert "invalid shape" in str(exc)
    else:
        raise AssertionError("a malformed registry must be refused")


def test_a_creation_that_fails_after_git_made_the_tree_leaves_the_record_active(sandbox, monkeypatch):
    from lu_machinery.dispatcher import git

    primary, registry, _ = declared_repo(sandbox)
    sha = run_git(primary, "rev-parse", "HEAD")
    tree = primary / ".worktrees/dispatch/codex/survivor"
    registration = dict(registry=registry, agent="codex", task_id="survivor", initiator=None, purpose="p")
    real_git = git._git

    def reset_fails(where, *args, **kwargs):
        if args[:2] == ("reset", "--hard"):
            raise OSError("disk full")
        return real_git(where, *args, **kwargs)

    monkeypatch.setattr(git, "_git", reset_fails)
    try:
        git.create_worktree(primary, tree, "codex/survivor", sha, registration=registration)
    except OSError:
        pass
    else:
        raise AssertionError("the failure must propagate")
    assert tree.is_dir()
    entry = json.loads(registry.read_text())["worktrees"][str(tree.resolve())]
    assert (entry["state"], entry["branch"], entry["head"]) == ("active", "codex/survivor", sha)


def test_a_failed_registry_reconcile_keeps_the_creation_error(sandbox, monkeypatch, caplog):
    """Defect class: a registry reconcile error raised while handling a failed creation replaced the creation error."""
    import logging

    from lu_machinery.dispatcher import git

    primary, registry, _ = declared_repo(sandbox)
    sha = run_git(primary, "rev-parse", "HEAD")
    tree = primary / ".worktrees/dispatch/codex/masked"
    registration = dict(registry=registry, agent="codex", task_id="masked", initiator=None, purpose="p")
    real_git = git._git

    def reset_fails(where, *args, **kwargs):
        if args[:2] == ("reset", "--hard"):
            raise OSError("disk full")
        return real_git(where, *args, **kwargs)

    def reconcile_fails(*_args, **_kwargs):
        raise RuntimeError("injected reconcile failure")

    monkeypatch.setattr(git, "_git", reset_fails)
    monkeypatch.setattr(git, "confirm_worktree", reconcile_fails)
    with caplog.at_level(logging.WARNING, logger="lu_machinery.dispatcher.git"), pytest.raises(OSError) as raised:
        git.create_worktree(primary, tree, "codex/masked", sha, registration=registration)
    assert str(raised.value) == "disk full"
    assert any("RuntimeError: injected reconcile failure" in note for note in raised.value.__notes__)
    assert "worktree registry reconcile failed: RuntimeError: injected reconcile failure" in caplog.text


def test_noncanonical_spelling_and_live_path(sandbox):
    from lu_machinery.dispatcher import git

    primary, registry, _ = declared_repo(sandbox)
    sha = run_git(primary, "rev-parse", "HEAD")
    spelled = primary / ".worktrees/dispatch/codex/mid/../canon"
    registration = dict(registry=registry, agent="codex", task_id="canon", initiator=None, purpose="p")
    (primary / ".worktrees/dispatch/codex/mid").mkdir(parents=True)
    git.create_worktree(primary, spelled, "codex/canon", sha, registration=registration)
    canonical = str((primary / ".worktrees/dispatch/codex/canon").resolve())
    worktrees = json.loads(registry.read_text())["worktrees"]
    assert worktrees[canonical]["state"] == "active" and worktrees[canonical]["head"] == sha
    # A second dispatch to the live path is refused and leaves the first owner's record untouched.
    before = worktrees[canonical]
    try:
        git.create_worktree(primary, spelled, "codex/other", sha, registration={**registration, "task_id": "other"})
    except ValueError as exc:
        assert "already holds a worktree" in str(exc)
    else:
        raise AssertionError("a live path must not be re-registered")
    assert json.loads(registry.read_text())["worktrees"][canonical] == before


def test_a_commit_refreshes_the_record_and_a_retained_tree_keeps_its_final_head(sandbox, monkeypatch):
    from lu_machinery.dispatcher import git, store

    monkeypatch.setattr(store, "retention_hold_disposition", lambda _record: None)  # no on-disk task record here

    primary, registry, other = declared_repo(sandbox)
    sha = run_git(primary, "rev-parse", "HEAD")
    tree = primary / ".worktrees/dispatch/codex/live"
    registration = dict(registry=registry, agent="codex", task_id="live", initiator=None, purpose="p")
    # The repository's own hook keeps running beside the refresh hooks.
    own = primary / ".git/hooks/post-commit"
    own.write_text(f"#!/bin/sh\ntouch {primary}/own-hook-ran\n")
    own.chmod(0o755)
    monkeypatch.setattr(git, "resolve_repo", lambda _key=None: (primary, dict(worktree_registry=REGISTRY)))
    git.create_worktree(primary, tree, "codex/live", sha, registration=registration)

    def entry():
        return json.loads(registry.read_text())["worktrees"][str(tree.resolve())]
    assert entry()["head"] == sha
    # Plain git commits, so the hooks really fire; the second is an amend (post-rewrite).
    run_git(tree, "-c", "user.name=S", "-c", "user.email=s@example.invalid", "commit", "--allow-empty", "-m", "one")
    first = run_git(tree, "rev-parse", "HEAD")
    assert entry()["head"] == first != sha
    run_git(tree, "-c", "user.name=S", "-c", "user.email=s@example.invalid", "commit", "--amend", "--allow-empty",
            "-m", "one again")
    second = run_git(tree, "rev-parse", "HEAD")
    assert entry()["head"] == second != first and entry()["branch"] == "codex/live"
    assert (primary / "own-hook-ran").exists()

    # Settlement of a tree it keeps records that final head; the other records stay as they were.
    (tree / "README").write_text("uncommitted\n")
    run_git(tree, "add", "README")
    record = dict(task_id="live", repo="declared", worktree_path=str(tree), worktree_branch="codex/live",
                  worktree_base_sha=sha, mode="workspace-write", options={})
    registry_before = json.loads(registry.read_text())
    stale = {**registry_before["worktrees"][str(tree.resolve())], "head": sha}
    registry.write_text(json.dumps({**registry_before, "worktrees": {**registry_before["worktrees"],
                                                                        str(tree.resolve()): stale}}))
    outcome = git.settle_worktree(record)
    assert (outcome["worktree_disposition"], outcome["worktree_disposition_reason"]) == ("retained", "dirty")
    assert outcome["worktree_registry"] == "refreshed"
    data = json.loads(registry.read_text())
    assert (data["worktrees"][str(tree.resolve())]["head"], data["worktrees"][str(tree.resolve())]["state"]) == (second, "active")
    assert data["worktrees"]["/elsewhere"] == other and data["artifacts"] == {"/scratch/a": {"keep": 1}}


def test_refresh_keeps_configured_hooks_and_contains_registry_trouble(sandbox, monkeypatch):
    import fcntl

    from lu_machinery.dispatcher import git, store

    primary, registry, _ = declared_repo(sandbox)
    monkeypatch.setattr(store, "retention_hold_disposition", lambda _record: None)
    monkeypatch.setattr(git, "resolve_repo", lambda _key=None: (primary, dict(worktree_registry=REGISTRY)))
    sha = run_git(primary, "rev-parse", "HEAD")
    # A relative core.hooksPath is resolved against the worktree, so its guards survive; a failing post hook
    # of the repository's own does not stop the refresh.
    (primary / ".githooks").mkdir()
    guard = primary / ".githooks/pre-commit"
    guard.write_text("#!/bin/sh\nexit 1\n")
    failing = primary / ".githooks/post-commit"
    failing.write_text("#!/bin/sh\nexit 1\n")
    for hook in (guard, failing):
        hook.chmod(0o755)
    run_git(primary, "config", "core.hooksPath", ".githooks")
    run_git(primary, "add", ".githooks")
    run_git(primary, "-c", "core.hooksPath=/dev/null", "commit", "-m", "tracked hooks")
    sha = run_git(primary, "rev-parse", "HEAD")
    tree = primary / ".worktrees/dispatch/codex/guarded"
    git.create_worktree(primary, tree, "codex/guarded", sha,
                        registration=dict(registry=registry, agent="codex", task_id="guarded", initiator=None, purpose="p"))
    refused = git.subprocess.run(["git", "-C", str(tree), "-c", "user.name=S", "-c", "user.email=s@example.invalid",
                                  "commit", "--allow-empty", "-m", "x"], capture_output=True)
    assert refused.returncode != 0, "the repository's pre-commit guard must still run"
    key = str(tree.resolve())
    # A malformed entry and a held lock are contained at settlement; the disposition is unchanged.
    data = json.loads(registry.read_text())
    data["worktrees"][key] = "garbage"
    registry.write_text(json.dumps(data))
    (tree / "README").write_text("dirty\n")
    run_git(tree, "add", "README")
    record = dict(task_id="guarded", repo="declared", worktree_path=str(tree), worktree_branch="codex/guarded",
                  worktree_base_sha=sha, mode="workspace-write", options={})
    assert git.settle_worktree(record)["worktree_disposition_reason"] == "dirty"
    monkeypatch.setattr(git, "REFRESH_LOCK_WAIT", 0.2)
    data["worktrees"][key] = dict(owner="o", purpose="p", branch="codex/guarded", head=sha, state="active",
                                  next_action="n", pr=None)
    registry.write_text(json.dumps(data))
    with open(registry.with_name(registry.name + ".lock"), "ab") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        outcome = git.settle_worktree(record)
    assert outcome["worktree_disposition_reason"] == "dirty" and outcome["worktree_registry"].startswith("failed: TimeoutError")


def test_a_home_relative_hooks_path_keeps_its_guards(sandbox, monkeypatch, tmp_path):
    from lu_machinery.dispatcher import git

    primary, registry, _ = declared_repo(sandbox)
    home = tmp_path / "home"
    (home / "hooks").mkdir(parents=True)
    guard = home / "hooks/pre-commit"
    guard.write_text("#!/bin/sh\nexit 1\n")
    guard.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    run_git(primary, "config", "core.hooksPath", "~/hooks")
    sha = run_git(primary, "rev-parse", "HEAD")
    tree = primary / ".worktrees/dispatch/codex/tilde"
    git.create_worktree(primary, tree, "codex/tilde", sha,
                        registration=dict(registry=registry, agent="codex", task_id="tilde", initiator=None, purpose="p"))
    refused = git.subprocess.run(["git", "-C", str(tree), "-c", "user.name=S", "-c", "user.email=s@example.invalid",
                                  "commit", "--allow-empty", "-m", "x"], capture_output=True)
    assert refused.returncode != 0, "the guard behind ~/hooks must still run"



pytestmark = pytest.mark.usefixtures("configured_roots")


def enroll(primary, *, ignored="/.claude/\n/.deploy-state/\n", descriptor=None, extra=None):
    """Enroll the checkout in the installed agent deployment, with the Claude hooks its release makes mandatory.

    The Claude source lives under a tree dispatch worktrees exclude by default (wiki), as a product may declare.
    ``descriptor`` replaces the descriptor's bytes; the value ``"symlink"`` commits it as a dangling symlink.
    ``extra`` maps further paths to the text committed there.
    """
    import os
    from importlib import resources

    inventory = json.loads(resources.files("lu_machinery.agent_deploy").joinpath("release.v1.json").read_bytes())
    settings = {}
    for requirement in inventory["required_hooks"]["claude"][".claude/settings.json"]:
        *parents, event = requirement["path"]
        node = settings
        for key in parents:
            node = node.setdefault(key, {})
        node.setdefault(event, []).append({**requirement["group"], "hooks": requirement["hooks"]})
    (primary / "agents_extensions/shared").mkdir(parents=True)
    (primary / "agents_extensions/shared/README.md").write_text("shared\n")
    claude = primary / "wiki/agents-claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps(settings))
    target = primary / "agents_extensions/deployment.v1.json"
    if descriptor == "symlink":
        os.symlink("missing.json", target)
    else:
        target.write_text(descriptor or json.dumps({
            "providers": ["claude"], "preserve": [],
            "sources": {"shared": {"path": "agents_extensions/shared", "required": True},
                        "claude": {"path": "wiki/agents-claude", "required": True}}}))
    (primary / ".gitignore").write_text(ignored)
    for name, text in (extra or {}).items():
        (primary / name).parent.mkdir(parents=True, exist_ok=True)
        (primary / name).write_text(text)
    run_git(primary, "add", ".")
    run_git(primary, "commit", "-m", "enroll")
    return run_git(primary, "rev-parse", "HEAD"), settings


def deployment_binding(host_layout, sandbox, python):
    """Bind the fixture dispatcher and its deployer to the same runtime and detached state."""
    state = Path(host_layout.data["state_root"])
    batch = state / "batch_state"
    host_layout.data["repos"] = {"machinery": str(sandbox.repo)}
    host_layout.data["default_product"] = "public"
    host_layout.data["runtime"]["python"] = python
    host_layout.data["products"] = {"public": {"repo": "machinery", "python": python}}
    host_layout.data["monitor_url"] = sandbox.env["LU_MONITOR_LOOPBACK"]
    host_layout.write()
    return {
        **host_layout.env,
        "LU_STATE_ROOT": str(state),
        "LEARN_UKRAINIAN_STATE_ROOT": str(state),
        "LEARN_UKRAINIAN_BATCH_STATE_DIR": str(batch),
        "LEARN_UKRAINIAN_OWNERSHIP_LEDGER": str(batch / "tasks/write-ownership.sqlite3"),
        "LU_PRIMARY_REPO_ROOT": str(sandbox.repo),
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(sandbox.repo),
        "LU_PRODUCT_ROOT": str(sandbox.repo),
        "LU_PYTHON": python,
        "LEARN_UKRAINIAN_PYTHON": python,
        "LU_TASKS_DIR": str(batch / "tasks"),
        "LU_GITHUB_CACHE_DIR": str(batch / "github-client"),
        "FLEET_COMMS_ROOT": str(batch / "fleet-comms/v1"),
    }


@pytest.mark.parametrize("deploy_agents", [True, False])
def test_an_enrolled_write_worktree_gets_its_product_guards_and_no_other_worktree_does(
        sandbox, monkeypatch, host_layout, deploy_agents):
    """Defect classes: a write worker of an enrolled checkout launched without the product's guards, because
    Claude Code loads project settings only from the worktree it runs in and the checkout tracks none of them,
    or because the sparse checkout hid a source the descriptor declares; a worker's edit to a source, even one
    Git status cannot see, reaching the guards; a deployment that does not match its start commit; an enrolled
    tree, which a write task must not borrow (--cwd) because another task there can commit, enroll or
    redeploy at any moment, taken for one that is not; a deployed output the checkout tracks, which could
    enter the worker's commit; and a read-only (review) worktree widened by settings the dispatch did not ask
    for."""
    import sys

    from lu_machinery.dispatcher import git

    primary, _, _ = declared_repo(sandbox)
    for name, value in deployment_binding(host_layout, sandbox, sys.executable).items():
        monkeypatch.setenv(name, value)
    # A tree that is not enrolled is left as it is, and a write task may still borrow it.
    plain = primary / ".worktrees/dispatch/codex/plain"
    run_git(primary, "worktree", "add", "--detach", str(plain), "HEAD")
    start = run_git(plain, "rev-parse", "HEAD")
    assert git.agent_deploy_enrolled(plain, start) is False
    assert git.deploy_agent_extensions(plain, start) is False and not (plain / ".claude").exists()
    sha, settings = enroll(primary)
    assert git.agent_deploy_enrolled(plain, start) is False
    # Enrollment follows the start or HEAD commit, whatever the working tree holds.
    run_git(plain, "checkout", "--detach", sha)
    assert git.agent_deploy_enrolled(plain, start) is True and git.agent_deploy_enrolled(plain, sha) is True
    tree = primary / ".worktrees/dispatch/codex/enrolled"
    git.create_worktree(primary, tree, "codex/enrolled", sha, deploy_agents=deploy_agents)
    deployed = tree / ".claude/settings.json"
    assert not (tree / "wiki").exists()  # the source stays hidden: the deployment reads the commit, not the tree
    if not deploy_agents:
        assert not (tree / ".claude").exists() and not (tree / ".deploy-state").exists()
        return
    from lu_machinery.agent_runtime.agy_hook_config import hook_mode_for_source, native_hook_command

    expected = deepcopy(settings["hooks"]["PreToolUse"])
    for group in expected:
        for hook in group["hooks"]:
            mode = hook_mode_for_source(hook["command"])
            if mode is not None:
                hook["command"] = native_hook_command(mode, python=Path(sys.executable))
    assert json.loads(deployed.read_text())["hooks"]["PreToolUse"] == expected
    assert run_git(tree, "status", "--porcelain", "--untracked-files=all") == ""
    # Guards come from the commit: a worker's edit to a source never reaches them, even one status hides.
    before = {name: (tree / name).read_bytes() for name in (".claude/settings.json", ".claude/README.md",
                                                             ".deploy-state/agent-deploy.v1.json")}
    run_git(tree, "update-index", "--assume-unchanged", "agents_extensions/shared/README.md")
    (tree / "agents_extensions/shared/README.md").write_text("worker edit\n")
    assert run_git(tree, "status", "--porcelain", "--untracked-files=all") == ""
    assert git.deploy_agent_extensions(tree, sha) is True
    assert {name: (tree / name).read_bytes() for name in before} == before
    # The deployment is bound to the task's start: a checkout whose HEAD moved refuses before anything runs.
    deployed.unlink()
    run_git(tree, "commit", "--allow-empty", "-m", "worker commit")
    with pytest.raises(ValueError, match=f"^agent_deploy_failed: the checkout's HEAD [0-9a-f]{{12}} is not the "
                                         f"task's start {sha[:12]}$"):
        git.deploy_agent_extensions(tree, sha)
    assert not deployed.exists()
    sha = run_git(tree, "rev-parse", "HEAD")
    # A tracked output refuses, however clean it is.
    run_git(tree, "add", "--force", ".claude/README.md")
    with pytest.raises(ValueError, match="^agent_deploy_outputs_not_ignored: the checkout does not ignore "
                                         r"\.claude/README\.md$"):
        git.deploy_agent_extensions(tree, sha)


@pytest.mark.parametrize("broken, global_excludes, reason", [
    ({"descriptor": "{"}, False, "agent_deploy_failed: exit 2: invalid deployment: invalid JSON"),
    ({"descriptor": "symlink"}, False, "agent_deploy_failed: exit 2: "),
    ({"ignored": "/.deploy-state/\n"}, False, "agent_deploy_outputs_not_ignored: the checkout does not ignore .claude/"),
    ({"ignored": "/.deploy-state/\n"}, True, "agent_deploy_outputs_not_ignored: the checkout does not ignore .claude/"),
    # The product marker that selects prompt lint lies in a tree the sparse checkout hides.
    ({"extra": {"curriculum/l2-uk-en/curriculum.yaml": "levels: {}\n",
                "agents_extensions/shared/phases/research.md": "Write in the persona voice.\n"}}, False,
     "agent_deploy_failed: exit 2: invalid deployment: prompt lint: PERSONA_IN_RESEARCH"),
])
def test_a_deployment_that_cannot_be_applied_refuses_the_launch_with_its_reason(
        sandbox, tmp_path, host_layout, broken, global_excludes, reason):
    """Defect class: a write worker launched unguarded, or with generated outputs it could commit, after its
    checkout's agent deployment failed: a broken or dangling descriptor skipped, or outputs only the
    dispatcher's own user-level excludes file hides, which the worker does not share, or a validation the
    commit selects but the sparse checkout hid from the apply; or a write task borrowing (--cwd) an enrolled
    tree launched, or deployed into. A read-only task of the same checkout is never deployed, so the same
    breakage cannot refuse it."""
    bound = deployment_binding(host_layout, sandbox, sandbox.python)
    sandbox.env.update(bound)
    sandbox.tasks = Path(bound["LU_TASKS_DIR"])
    if global_excludes:
        (tmp_path / "excludes").write_text("/.claude/\n")
        (tmp_path / "gitconfig").write_text(f"[core]\n\texcludesFile = {tmp_path / 'excludes'}\n")
        sandbox.env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    primary, _, _ = declared_repo(sandbox)
    enroll(primary, **broken)
    # A write task starts from an origin base that settlement can verify.
    run_git(primary, "init", "--bare", "--initial-branch=main", str(tmp_path / "declared.git"))
    run_git(primary, "remote", "add", "origin", str(tmp_path / "declared.git"))
    run_git(primary, "push", "-u", "origin", "main")
    refused = sandbox.cli("dispatch", "--agent", "sol", "--model", "echo", "--prompt", "never launched",
                          "--repo", "declared", "--base", "origin/main", "--mode", "workspace-write",
                          "--task-id", "refused", expected=2)
    assert refused.stderr.splitlines()[-1].startswith("dispatcher: " + reason), refused.stderr
    # Never launched: the hold becomes a terminal setup refusal that keeps the surviving tree attributed (#490).
    hold = json.loads((sandbox.tasks / "refused.json").read_text())
    assert (hold["status"], hold["pid"], hold["never_started_reason"]) == ("failed", None, "setup_refusal")
    record = sandbox.settle(sandbox.dispatch("echo", "--repo", "declared", "--base", "origin/main", "--task-id", "read"))
    assert (record["status"], record["mode"]) == ("done", "read-only")
    # Settlement hands the refused tree to the guarded remover, which removes it and its branch, or keeps it with
    # its disposition recorded (here only when a user-level excludes file the settlement shares hides outputs).
    sandbox.sweep()
    settled = json.loads((sandbox.tasks / "refused.json").read_text())
    assert settled["status"] == "failed"
    assert (primary / ".worktrees/dispatch/codex/refused").exists() == (settled["worktree_disposition"] != "removed")
    if not global_excludes:
        assert settled["worktree_disposition"] == "removed"
        assert run_git(primary, "for-each-ref", "--format=%(refname:short)", "refs/heads") == "main"
    # A task borrowing (--cwd) an enrolled tree: read-only runs as it is; a write task refuses before it
    # starts, recorded as a setup refusal under its own id, and nothing is written into the tree.
    tree = primary / ".worktrees/dispatch/codex/attached"
    run_git(primary, "worktree", "add", "-b", "attached", str(tree), "main")
    record = sandbox.settle(sandbox.dispatch("echo", "--repo", "declared", "--cwd", str(tree), "--base", "origin/main",
                                             "--task-id", "read-attached"))
    assert (record["status"], record["mode"], (tree / ".claude").exists()) == ("done", "read-only", False)
    attached = sandbox.cli("dispatch", "--agent", "sol", "--model", "echo", "--prompt", "never launched",
                           "--repo", "declared", "--base", "origin/main", "--mode", "workspace-write",
                           "--cwd", str(tree), "--task-id", "attached", expected=2)
    assert attached.stderr.splitlines()[-1] == ("dispatcher: agent_deploy_attach_refused: a write task cannot borrow "
                                                "(--cwd) a tree enrolled in the agent deployment; dispatch it without "
                                                "--cwd"), attached.stderr
    refusals = [entry for entry in (json.loads(path.read_text()) for path in sandbox.tasks.glob("*.json"))
                if entry.get("requested_task_id") == "attached"]
    assert [(entry["status"], entry["never_started_reason"]) for entry in refusals] == [("failed", "setup_refusal")]
    assert not (sandbox.tasks / "attached.json").exists() and not (tree / ".claude").exists()
