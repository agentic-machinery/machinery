"""The provider environment carries the sense-proof directory and nothing else that is ambient."""



import contextlib
import json
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from lu_machinery.common.git_credentials import SELECTOR_ENV
from lu_machinery.dispatcher import agy_sandbox, environment, git, store, worker, write_boundary
from lu_machinery.dispatcher.types import Invocation, ParsedResult, TaskSpec
from tests.dispatcher.conftest import await_file as await_file
from tests.dispatcher.conftest import prior_worktree as prior_worktree
from tests.dispatcher.conftest import run_git as run_git
from tests.helpers.product_tree import configured_roots as configured_roots

PROOF_DIR = "LU_SENSE_PROOF_DIR"
SENTINEL = "/fixture/sense-proofs"
LOGIN_NAMES = sorted({name for names in environment._AUTH.values() for name in names} | {"CODEX_ACCESS_TOKEN"})
PROVIDERS = [(agent, None) for agent in environment._AUTH] + [("opencode", "openrouter")]


@pytest.mark.parametrize(("agent", "provider"), PROVIDERS)
def test_child_env_carries_sense_proof_dir_and_strips_logins(tmp_path, monkeypatch, agent, provider):
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    for name in (*LOGIN_NAMES, "GH_TOKEN"):
        monkeypatch.setenv(name, "fixture-credential")
    spec = TaskSpec("fixture", agent, "stub", None, "read-only", tmp_path, tmp_path / "prompt", tmp_path / "final",
                    provider=provider)

    monkeypatch.setenv(PROOF_DIR, SENTINEL)
    env = environment.child_env(spec)
    assert env[PROOF_DIR] == SENTINEL
    own = set(environment._AUTH[agent]) if provider is None else {"OPENROUTER_API_KEY"}
    assert set(LOGIN_NAMES) & env.keys() <= own
    assert "GH_TOKEN" not in env

    monkeypatch.delenv(PROOF_DIR)
    assert PROOF_DIR not in environment.child_env(spec)

    # Every worker's hooks refuse without the configured shared-state anchor (#100), so it
    # reaches each provider in either mode.
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(tmp_path / "product"))
    monkeypatch.setenv("LEARN_UKRAINIAN_STATE_ROOT", str(tmp_path))
    for mode in ("read-only", "workspace-write"):
        monkeypatch.setenv(write_boundary.BROKER_ENV, "untrusted-inherited-marker")
        child = worker._provider_env(replace(spec, mode=mode))
        assert child.get(write_boundary.BROKER_ENV) == (
            write_boundary._BROKER_INSIDE if agent == "claude" and mode == "read-only" else None)
        assert child["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"] == str(tmp_path)
        if mode == "workspace-write":
            assert child["LEARN_UKRAINIAN_PYTHON"] == sys.executable
            assert child["LU_PRODUCT_ROOT"] == str(tmp_path / "product")
            assert child["LEARN_UKRAINIAN_STATE_ROOT"] == str(tmp_path)
            assert "GH_TOKEN" not in child
            assert set(LOGIN_NAMES) & child.keys() <= own
            # The launcher must not mark hook configuration as host-only, which would strip it
            # before the contained provider starts. No sandbox or credential store is needed here.
            monkeypatch.setattr(write_boundary, "credential_files", lambda *_: [])
            argv = write_boundary.launcher(spec, Invocation(["/bin/sh"], None, child, None), tmp_path / "task.json")
            for name in ("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LEARN_UKRAINIAN_PYTHON", "LU_PRODUCT_ROOT",
                         "LEARN_UKRAINIAN_STATE_ROOT"):
                assert f"{name}={child[name]}" not in argv
            # Workspace-write workers configure Sources outside the shared boundary.
            write_argv = write_boundary.launcher(replace(spec, mode=mode), Invocation(["/bin/sh"], None, child, None),
                                                 tmp_path / "task.json")
            assert "--sources-mcp" not in write_argv[:write_argv.index("--")]
            read_only = argv[:argv.index("--")]
            projected = read_only[read_only.index("--sources-mcp") + 1] if "--sources-mcp" in read_only else None
            assert projected == (agent if agent in ("claude", "codex", "cursor", "opencode") else None)
            # Defect class: the launcher's host GitHub reads inherit a provider's private config root (the Savant
            # route's), so gh reads anonymously and the boundary refuses. They keep the dispatcher's login.
            monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
            monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
            monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
            private = {**child, "XDG_CONFIG_HOME": str(tmp_path / "private")}
            host = write_boundary.launcher(spec, Invocation(["/bin/sh"], None, private, None), tmp_path / "task.json")
            host = host[:host.index("--")]
            assert f"GH_CONFIG_DIR={tmp_path / 'host-home' / '.config' / 'gh'}" in host
            assert "XDG_CONFIG_HOME=" in host

    # A missing anchor stays missing; worker Git metadata must never fill it in.
    monkeypatch.delenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT")
    assert "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT" not in environment.child_env(replace(spec, mode="workspace-write"))


@pytest.mark.parametrize(("agent", "provider"), PROVIDERS)
def test_child_env_forwards_fleet_repo_selector_when_set_and_omits_when_absent(tmp_path, monkeypatch, agent, provider):
    """Defect class: dropped LU_FLEET_REPO_SELECTOR_FILE breaks active-slug resolution in dispatched workers."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    selector = "/fixture/fleet-repo-selector/selector.yaml"
    spec = TaskSpec("fixture", agent, "stub", None, "workspace-write", tmp_path, tmp_path / "prompt", tmp_path / "final",
                    provider=provider)

    monkeypatch.setenv(SELECTOR_ENV, selector)
    for mode in ("workspace-write", "read-only"):
        mode_spec = replace(spec, mode=mode)
        assert environment.child_env(mode_spec)[SELECTOR_ENV] == selector
        assert environment.probe_env(mode_spec)[SELECTOR_ENV] == selector
        assert worker._provider_env(mode_spec)[SELECTOR_ENV] == selector

    monkeypatch.delenv(SELECTOR_ENV, raising=False)
    for mode in ("workspace-write", "read-only"):
        assert SELECTOR_ENV not in environment.child_env(replace(spec, mode=mode))


@pytest.mark.usefixtures('configured_roots')
def test_supervise_restores_signal_handlers_after_cleanup(sandbox, monkeypatch):
    """Defect class: leaked signal disposition after in-process supervision."""
    from tests.dispatcher import stub_provider

    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    monkeypatch.setattr(worker.providers, "load", lambda _agent: stub_provider)
    monkeypatch.setattr(worker, "_reap_settled", lambda *_args, **_kwargs: None)
    spec = TaskSpec("signal-cleanup", "codex", "echo", None, "read-only", sandbox.repo,
                    sandbox.repo / "signal-cleanup.prompt", sandbox.tasks / "signal-cleanup.result")
    spec.prompt_file.write_bytes(b"literal final response")
    record = store.new_record(spec, {"key": "public", "github": str(sandbox.remote)}, "main",
                              run_git(sandbox.repo, "rev-parse", "HEAD"), {"research_owned_path": []})
    record["prompt_file"] = str(sandbox.tasks / "signal-cleanup.prompt")
    path = store.create(record, b"literal final response")
    (sandbox.tasks / "logs").mkdir()
    original_term, original_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)

    def previous_term(_signum, _frame):
        pass

    try:
        signal.signal(signal.SIGTERM, previous_term)
        worker.supervise(path, {}, 10)
        assert json.loads(path.read_text())["status"] == "done"
        assert signal.getsignal(signal.SIGTERM) is previous_term
        assert signal.getsignal(signal.SIGINT) is original_int
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)


@pytest.mark.usefixtures('configured_roots')
def test_cancel_during_a_callee_that_absorbs_exceptions_still_cancels_before_launch(sandbox, monkeypatch):
    """#11994: the handler raised at an arbitrary bytecode, so a callee's broad handler absorbed the cancel."""
    from tests.dispatcher import stub_provider

    def absorbing_check(*_args, **_kwargs):
        with contextlib.suppress(Exception):  # a callee's broad handler
            signal.raise_signal(signal.SIGTERM)  # the handler runs before this call returns

    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    monkeypatch.setattr(worker.providers, "load", lambda _agent: stub_provider)
    monkeypatch.setattr(worker, "_reap_settled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.headroom, "check_runtime", absorbing_check)
    spec = TaskSpec("absorbed-cancel", "codex", "echo", None, "read-only", sandbox.repo,
                    sandbox.repo / "absorbed-cancel.prompt", sandbox.tasks / "absorbed-cancel.result")
    spec.prompt_file.write_bytes(b"literal final response")
    record = store.new_record(spec, {"key": "public", "github": str(sandbox.remote)}, "main",
                              run_git(sandbox.repo, "rev-parse", "HEAD"), {"research_owned_path": []})
    record["prompt_file"] = str(sandbox.tasks / "absorbed-cancel.prompt")
    path = store.create(record, b"literal final response")
    (sandbox.tasks / "logs").mkdir()
    original_term, original_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        worker.supervise(path, {}, 10)
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)
    result = json.loads(path.read_text())
    assert (result["status"], result["error"], result["provider_pgid"]) == ("cancelled", "cancelled by client", None)
    assert not (sandbox.tasks / "absorbed-cancel.started").exists()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("receipt", [None, "refused", "ran"])
def test_read_only_agy_run_needs_a_verified_boundary_receipt(sandbox, monkeypatch, receipt):
    """C-3027: the receipt, never the provider's output, decides a read-only AGY run and its Sources count."""
    from tests.dispatcher import stub_provider

    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    monkeypatch.setattr(worker.providers, "load", lambda _agent: stub_provider)
    monkeypatch.setattr(worker, "_reap_settled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agy_sandbox, "unavailable", lambda: None)
    monkeypatch.setattr(worker.headroom, "check_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.headroom, "check_runtime", lambda *_args, **_kwargs: None)
    discarded = []
    monkeypatch.setattr(agy_sandbox, "discard", discarded.append)
    spec = TaskSpec("agy-ro", "agy", "echo", None, "read-only", sandbox.repo,
                    sandbox.repo / "agy-ro.prompt", sandbox.tasks / "agy-ro.result")
    spec.prompt_file.write_bytes(b"VERDICT: APPROVE")
    head = run_git(sandbox.repo, "rev-parse", "HEAD")
    record = store.new_record(spec, {"key": "public", "github": str(sandbox.remote)}, "main", head,
                              {"research_owned_path": []})
    record["prompt_file"] = str(sandbox.tasks / "agy-ro.prompt")
    path = store.create(record, b"VERDICT: APPROVE")
    (sandbox.tasks / "logs").mkdir()
    if receipt is not None:
        state = {"head": head, "clean": True}
        agy_sandbox.receipt_file(spec.final_file).write_text(json.dumps({
            "schema": agy_sandbox.SCHEMA, "task_id": "agy-ro", "status": receipt, "reason": "the sandbox canary failed",
            "canary": {"passed": receipt == "ran"}, "input": {"start": state, "end": state},
            "sources": {"completed": 4},
        }))
    worker.supervise(path, {}, 10)
    result = json.loads(path.read_text())
    assert discarded == ["agy-ro"]
    if receipt == "ran":
        assert (result["status"], result["sources_mcp_call_count"]) == ("done", 4)
        assert result["read_only_boundary"]["canary"] == {"passed": True}
    else:
        assert result["status"] == "failed" and "sources_mcp_call_count" not in result
        assert result["error"].startswith("agy_read_only_boundary: ")
    assert result.get("never_started_reason") is None  # The provider ran, even when its boundary receipt was invalid.


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("agent", ["codex", "agy"])
@pytest.mark.parametrize("boundary_status", ["refused", "interrupted", None])
def test_boundary_exit_125_needs_a_pre_launch_receipt(tmp_path, monkeypatch, agent, boundary_status):
    """Exit 125 can mean a boundary refusal, a failed running boundary or the provider's own exit."""
    from types import SimpleNamespace

    tasks = tmp_path / "tasks"
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setattr(worker, "_reap_settled", lambda *_a, **_kw: None)
    monkeypatch.setattr(worker, "create_dispatch_lease", lambda *_a: None)
    monkeypatch.setattr(worker, "_discard_read_only_scratch", lambda *_a: None)
    monkeypatch.setattr(git, "pin_settlement_registry", lambda: None)
    monkeypatch.setattr(git, "snapshot_delivery", lambda *_a: {})
    monkeypatch.setattr(worker.headroom, "check_plan", lambda *_a, **_kw: None)
    monkeypatch.setattr(worker.headroom, "check_runtime", lambda *_a, **_kw: None)
    monkeypatch.setattr(agy_sandbox, "unavailable", lambda: None)
    marker = tasks / "provider-ran"
    argv = [sys.executable, "-c", "raise SystemExit(125)"]
    if boundary_status != "refused":
        argv = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch(); sys.exit(125)",
                str(marker)]
    provider = SimpleNamespace(parse=lambda *_a: ParsedResult(None, None, "fixture exit 125"))
    monkeypatch.setattr(worker.providers, "load", lambda _agent: provider)
    monkeypatch.setattr(worker.runtime_context, "build", lambda _s, env: Invocation(argv, None, env, None))
    monkeypatch.setattr(write_boundary, "launcher", lambda _spec, invocation, _task: invocation.argv)
    spec = TaskSpec("boundary-ro", agent, "echo", None, "read-only", tmp_path,
                    tmp_path / "prompt", tasks / "boundary-ro.result")
    spec.prompt_file.write_text("Fixture prompt")
    record = store.new_record(spec, {"key": "public", "github": "fixture/repo"}, "main", "1" * 40,
                              {"research_owned_path": []})
    record["prompt_file"] = str(tasks / "boundary-ro.prompt")
    path = store.create(record, b"Fixture prompt")
    (tasks / "logs").mkdir()
    if boundary_status:
        boundary = write_boundary if agent == "codex" else agy_sandbox
        receipt_base = path if agent == "codex" else spec.final_file
        boundary.receipt_file(receipt_base).write_text(json.dumps({
            "schema": boundary.SCHEMA, "task_id": spec.task_id, "status": boundary_status, "mode": "read-only",
            "reason": "fixture boundary", "canary": {"passed": False},
        }))

    worker.supervise(path, {}, 10)

    result = store.read(spec.task_id)
    assert (result["status"], result["returncode"], result["usage"]) == ("failed", 125, None)
    assert result.get("never_started_reason") == ("boundary_refusal" if boundary_status == "refused" else None)
    assert marker.exists() == (boundary_status != "refused")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("flags,restored", [
    ([], set()), (["--full-checkout"], {"curriculum", "site/src/data"}),
    (["--sparse-include", "/curriculum/"], {"curriculum"}),
    (["--sparse-include", "curriculum", "--sparse-include", "site/src/data"], {"curriculum", "site/src/data"}),
    (["--owned-path", "data/projects/fixture.txt"], {"data/projects"}),
    (["--research-owned-path", "./data//projects/./**/*.json"], {"data/projects"}),
    (["--owned-path", "tests/projects/open_model_data/test_mine.py"], {"data/projects"}),
    (["--research-owned-path", "scripts/projects/open_model_data/**"], {"data/projects"}),
    (["--owned-path", "site"], {"site/src/content", "site/src/data"}),
    (["--owned-path", "curriculum/**", "--sparse-include", "data/projects"],
     {"curriculum", "data/projects", "registry/lexicon/source-inventory", "site/src/content"}),
    (["--owned-path", "data/projects-other", "--research-owned-path", "scripts/projects/open_model_database"], set()),
])
def test_sparse_dispatch_never_turns_on_sparse_checkout_in_primary_or_sibling_worktrees(sandbox, flags, restored):
    """Owned paths must restore required trees without changing another checkout's sparse settings."""
    excluded = {
        "curriculum", "wiki", "data/projects", "data/lexicon", "data/datasets", "data/processed",
        "registry/projects", "registry/lexicon/source-inventory",
        "registry/lexicon/source-inventory-review-decisions", "site/src/content", "site/src/data",
    }
    for directory in excluded | {"site/src/components", "registry/lexicon/kept"}:
        path = sandbox.repo / directory / "fixture.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("tracked fixture")
    (sandbox.repo / "data/kept.txt").write_text("parent file")
    run_git(sandbox.repo, "add", "-f", *[f"{directory}/fixture.txt" for directory in excluded | {"site/src/components", "registry/lexicon/kept"}], "data/kept.txt")
    run_git(sandbox.repo, "commit", "-m", "heavy fixture trees")
    run_git(sandbox.repo, "push", "origin", "main")
    sibling = sandbox.repo.parent / "sibling"
    run_git(sandbox.repo, "worktree", "add", "-b", "sibling", str(sibling))
    untracked = sandbox.repo / "curriculum/untracked.txt"
    untracked.write_text("uncommitted primary file")
    mode = ["--mode", "workspace-write"] if "--owned-path" in flags else []
    task = sandbox.dispatch("blocked", *mode, *flags)
    worktree = Path(json.loads((sandbox.tasks / f"{task[0]}.json").read_text())["cwd"])
    await_file(sandbox.tasks / f"{task[0]}.started")
    for directory in excluded:
        assert (worktree / directory / "fixture.txt").exists() == ("--full-checkout" in flags or directory in restored)
    assert (worktree / "site/src/components/fixture.txt").exists()
    assert (worktree / "registry/lexicon/kept/fixture.txt").exists()
    assert (worktree / "data/kept.txt").exists()
    assert run_git(worktree, "status", "--porcelain") == ""
    assert untracked.read_text() == "uncommitted primary file"
    for checkout in (sandbox.repo, sibling):
        assert (checkout / "curriculum/fixture.txt").read_text() == "tracked fixture"
        config = run_git(checkout, "config", "--show-origin", "--list")
        assert "core.sparsecheckout=true" not in config.lower()
    assert run_git(sandbox.repo, "config", "--local", "--get", "extensions.worktreeConfig") == "true"
    if "--full-checkout" not in flags:
        origin = run_git(worktree, "config", "--show-origin", "--get", "core.sparseCheckout")
        assert origin.endswith("/config.worktree\ttrue")
    (sandbox.tasks / f"{task[0]}.release").touch()
    # A write task that commits nothing settles as undelivered; the sparse checkout is what this test covers.
    assert sandbox.settle(task, expected=1 if mode else 0)["worktree_disposition"] == "removed"


@pytest.mark.usefixtures('configured_roots')
def test_dispatch_without_a_curriculum_path_keeps_curriculum_inputs_excluded(sandbox):
    """A dispatch that owns no curriculum path does not gain the curriculum input trees."""
    owned = sandbox.repo / "docs/owned.md"
    owned.parent.mkdir(exist_ok=True)
    owned.write_text("owned")
    for directory in ("registry/lexicon/source-inventory", "site/src/content"):
        path = sandbox.repo / directory / "fixture.txt"
        path.parent.mkdir(parents=True)
        path.write_text("tracked fixture")
    run_git(sandbox.repo, "add", "-f", "docs/owned.md", "registry/lexicon/source-inventory/fixture.txt", "site/src/content/fixture.txt")
    run_git(sandbox.repo, "commit", "-m", "non-curriculum owned path")
    run_git(sandbox.repo, "push", "origin", "main")
    task = sandbox.dispatch("blocked", "--mode", "workspace-write", "--owned-path", "docs/owned.md")
    worktree = Path(json.loads((sandbox.tasks / f"{task[0]}.json").read_text())["cwd"])
    await_file(sandbox.tasks / f"{task[0]}.started")
    assert (worktree / "docs/owned.md").read_text() == "owned"
    assert not (worktree / "registry/lexicon/source-inventory/fixture.txt").exists()
    assert not (worktree / "site/src/content/fixture.txt").exists()
    (sandbox.tasks / f"{task[0]}.release").touch()
    assert sandbox.settle(task, expected=1)["worktree_disposition"] == "removed"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("owned,gains", [
    ("scripts/lexicon", True), ("scripts/lexicon/x.py", True), ("scripts/**", True),
    ("scripts/dispatcher/cli.py", False),
])
def test_owning_a_lexicon_path_also_owns_the_fingerprint_its_gate_regenerates(sandbox, owned, gains):
    """The fingerprint is added once, so the record, the sparse checkout and the advisory binding agree (#12169)."""
    fingerprint = "site/src/data/lexicon-manifest.fingerprint.json"
    for relative in ("scripts/lexicon/x.py", fingerprint):
        (sandbox.repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (sandbox.repo / relative).write_text("tracked fixture")
    run_git(sandbox.repo, "add", "-f", "scripts/lexicon/x.py", fingerprint)
    run_git(sandbox.repo, "commit", "-m", "lexicon fixture")
    run_git(sandbox.repo, "push", "origin", "main")

    def binding(*paths):
        flags = [flag for path in paths for flag in ("--owned-path", path)]
        return sandbox.cli("dispatch", "--agent", "sol", "--model", "echo", "--mode", "workspace-write",
                           "--task-id", "binding", "--prompt", "literal final response",
                           "--print-advisory-binding", *flags).stdout

    assert (binding(owned) == binding(owned, fingerprint)) is gains
    result = sandbox.cli("dispatch", "--agent", "sol", "--model", "blocked", "--prompt", "literal final response",
                         "--mode", "workspace-write", "--owned-path", owned)
    task = tuple(result.stdout.splitlines())
    assert (fingerprint in result.stderr) is gains
    record = json.loads((sandbox.tasks / f"{task[0]}.json").read_text())
    assert record["owned_paths"] == ([owned, fingerprint] if gains else [owned])
    await_file(sandbox.tasks / f"{task[0]}.started")
    assert (Path(record["cwd"]) / fingerprint).exists() is gains
    (sandbox.tasks / f"{task[0]}.release").touch()
    assert sandbox.settle(task, expected=1)["worktree_disposition"] == "removed"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("redirected", [False, True])
def test_sparse_dispatch_never_turns_on_sparse_checkout_in_primary_with_git_redirects(sandbox, monkeypatch, redirected):
    tracked = sandbox.repo / "curriculum/tracked.txt"
    tracked.parent.mkdir(exist_ok=True)
    tracked.write_text("tracked primary file")
    run_git(sandbox.repo, "add", "-f", "curriculum")
    run_git(sandbox.repo, "commit", "-m", "excluded tree")
    untracked = tracked.with_name("untracked.txt")
    untracked.write_text("untracked primary file")
    sha = run_git(sandbox.repo, "rev-parse", "HEAD")
    path = sandbox.repo / ".worktrees/dispatch/sol/redirects"
    if redirected:
        for key, value in {
            "GIT_DIR": sandbox.repo / ".git", "GIT_WORK_TREE": sandbox.repo,
            "GIT_COMMON_DIR": sandbox.repo / ".git", "GIT_INDEX_FILE": sandbox.repo / ".git/index",
        }.items():
            monkeypatch.setenv(key, str(value))
    with monkeypatch.context() as injected:
        injected.setenv("GIT_CONFIG_PARAMETERS", "'core.sparseCheckout=false'")
        injected.setenv("GIT_CONFIG_COUNT", "2")
        injected.setenv("GIT_CONFIG_KEY_0", "core.worktree")
        injected.setenv("GIT_CONFIG_VALUE_0", str(sandbox.repo))
        injected.setenv("GIT_CONFIG_KEY_1", "core.hooksPath")
        injected.setenv("GIT_CONFIG_VALUE_1", str(sandbox.repo / "missing-hooks"))
        git.create_worktree(sandbox.repo, path, "sol/redirects", sha)
    if redirected:
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
            monkeypatch.delenv(key)
    assert tracked.read_text() == "tracked primary file"
    assert untracked.read_text() == "untracked primary file"
    assert not (path / "curriculum").exists()
    primary = subprocess.run(
        ["git", "-C", str(sandbox.repo), "config", "--show-origin", "--get", "core.sparseCheckout"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert (primary.returncode, primary.stdout) == (1, "")
    assert run_git(path, "config", "--show-origin", "--get", "core.sparseCheckout").endswith("/config.worktree\ttrue")
    assert run_git(sandbox.repo, "diff", "--cached") == ""


@pytest.mark.usefixtures('configured_roots')
def test_sandbox_dispatch_never_starts_a_host_systemd_scope(sandbox):
    """Defect class: test sandbox escape. The dispatcher subprocess probed the host user manager and ran
    the stub worker in a real ``lu-worker-*`` scope, outside the in-process probe stub."""
    record = sandbox.settle(sandbox.dispatch())
    assert record["status"] == "done"
    assert record["launch_mode"] == "popen-fallback" and "launch_unit" not in record
    assert record["launch_fallback_reason"] == "forced: LU_DISPATCH_ISOLATION=fallback"

pytestmark = [pytest.mark.usefixtures('configured_roots')]
