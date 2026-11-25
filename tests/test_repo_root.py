"""Unit and integration tests for repository root resolver and primary checkout anchoring."""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common import profile
from lu_machinery.common import repo_root as rr
from lu_machinery.common.repo_root import main_checkout_root, project_interpreter
from lu_machinery.orchestration import worktree_claims

REPO = Path(__file__).resolve().parents[1]


def test_main_checkout_root_resolves_primary_checkout_from_worktree(tmp_path):
    """Verify that main_checkout_root correctly identifies primary repo root from a worktree."""
    main_repo = tmp_path / "main"
    worktree = main_repo / ".worktrees" / "dispatch" / "agy" / "task-123"
    git_dir = main_repo / ".git" / "worktrees" / "task-123"

    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    assert main_checkout_root(worktree) == main_repo


@pytest.fixture
def wt_layout(tmp_path) -> tuple[Path, Path]:
    """Sets up a mock primary repo and a mock worktree layout.

    Returns a tuple of (mock_primary_root, mock_worktree_root).
    """
    main_repo = tmp_path / "main"
    worktree = main_repo / ".worktrees" / "dispatch" / "agy" / "task-123"
    git_dir = main_repo / ".git" / "worktrees" / "task-123"

    git_dir.mkdir(parents=True)
    for entry in ("objects", "refs"):
        (main_repo / ".git" / entry).mkdir(parents=True, exist_ok=True)
    (main_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    # Copy the bridge and common module code files to the mock worktree
    src_root = rr.machinery_root().parent

    # Create empty init for the bridge package to avoid loading other bridge modules
    bridge_init = worktree / "lu_machinery" / "ai_agent_bridge" / "__init__.py"
    bridge_init.parent.mkdir(parents=True, exist_ok=True)
    bridge_init.write_text("", encoding="utf-8")

    files_to_copy = [
        "lu_machinery/common/__init__.py",
        "lu_machinery/common/repo_root.py",
        "lu_machinery/common/github_client.py",
        "lu_machinery/ai_agent_bridge/_config.py",
        "lu_machinery/ai_agent_bridge/_env.py",
        "lu_machinery/ai_agent_bridge/_dispatch_wrappers.py",
        "lu_machinery/ai_agent_bridge/_monitor_cache.py",
        "lu_machinery/review/__init__.py",
        "lu_machinery/review/verdict_parser.py",
    ]
    for f in files_to_copy:
        dest = worktree / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text((src_root / f).read_text(encoding="utf-8"), encoding="utf-8")

    return main_repo, worktree


def _write_venv_python(checkout: Path, name: str = "python") -> Path:
    interpreter = checkout / ".venv" / "bin" / name
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    interpreter.chmod(0o755)
    return interpreter


def _link_worktree(primary: Path, worktree: Path) -> None:
    git_dir = primary / ".git" / "worktrees" / "task"
    git_dir.mkdir(parents=True, exist_ok=True)
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")


def test_project_interpreter_uses_an_executable_override(tmp_path):
    """A valid LEARN_UKRAINIAN_PYTHON wins over the primary and the worktree."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    _write_venv_python(primary)
    _write_venv_python(worktree)
    override = tmp_path / "override-python"
    override.write_text("", encoding="utf-8")
    override.chmod(0o755)

    assert project_interpreter(worktree, env={"LEARN_UKRAINIAN_PYTHON": str(override)}) == override


def test_project_interpreter_refuses_an_invalid_override_without_fallback(tmp_path):
    """An invalid override is a refusal. The primary venv is not a fallback."""
    primary = tmp_path / "primary"
    primary.mkdir()
    primary_python = _write_venv_python(primary)
    missing = tmp_path / "missing-python"
    unexecutable = tmp_path / "plain-file"
    unexecutable.write_text("", encoding="utf-8")

    with pytest.raises(rr.MachineryConfigError, match="must be an absolute path"):
        project_interpreter(primary, env={"LEARN_UKRAINIAN_PYTHON": "python"})
    for value in (str(missing), str(unexecutable)):
        with pytest.raises(FileNotFoundError, match="is not an executable file"):
            project_interpreter(primary, env={"LEARN_UKRAINIAN_PYTHON": value})
    assert project_interpreter(primary, env={}) == primary_python


def test_project_interpreter_refuses_sys_executable_when_no_primary_venv_exists(tmp_path, monkeypatch):
    """The removed sys.executable fallback stays a refusal, including for a foreign venv."""
    foreign = _write_venv_python(tmp_path / "other-checkout")
    monkeypatch.setattr(sys, "executable", str(foreign))
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    with pytest.raises(FileNotFoundError, match="is not an executable file"):
        project_interpreter(checkout, env={})
    with pytest.raises(FileNotFoundError, match="is not an executable file"):
        project_interpreter(Path("/no-such-checkout"), env={})


def test_project_interpreter_refuses_a_worktree_venv_when_the_primary_has_none(tmp_path, monkeypatch):
    """A worktree-local .venv is not the interpreter when the primary has none."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    local = _write_venv_python(worktree)
    monkeypatch.setattr(sys, "executable", str(local))

    with pytest.raises(FileNotFoundError, match="is not an executable file"):
        project_interpreter(worktree, env={})


def test_project_interpreter_refuses_a_malformed_gitfile_venv(tmp_path, monkeypatch):
    """A gitfile that is not .git/worktrees/<name> does not select that directory's .venv."""
    checkout = tmp_path / "wt"
    local = _write_venv_python(checkout)
    (checkout / ".git").write_text("gitdir: /nonexistent/elsewhere\n", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(local))

    with pytest.raises(FileNotFoundError, match="is not used"):
        project_interpreter(checkout, env={})


def test_project_interpreter_prefers_the_primary_checkout_venv(tmp_path, monkeypatch):
    """The primary checkout's .venv/bin/python wins when the worktree also has one."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    primary_python = _write_venv_python(primary)
    local = _write_venv_python(worktree)
    monkeypatch.setattr(sys, "executable", str(local))

    assert project_interpreter(worktree, env={}) == primary_python


def test_project_interpreter_uses_the_primary_venv_when_the_checkout_has_none(tmp_path, monkeypatch):
    """A linked worktree with no .venv uses the primary checkout's interpreter."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    primary_python = _write_venv_python(primary)
    foreign = _write_venv_python(tmp_path / "other-checkout")
    monkeypatch.setattr(sys, "executable", str(foreign))

    assert project_interpreter(worktree, env={}) == primary_python


def test_project_interpreter_refuses_a_non_executable_primary_venv(tmp_path):
    """A primary .venv/bin/python that is not executable is a refusal."""
    primary = tmp_path / "primary"
    primary.mkdir()
    interpreter = primary / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="is not an executable file"):
        project_interpreter(primary, env={})


# --------------------------------------------------------------------------- C-4843 root contract, step 1
#
# Installed lu_machinery has no implicit product and no implicit state anchor; only the
# in-tree source copy keeps its checkout defaults. Explicit selection needs no curriculum
# marker, an invalid selection never falls through, decoys in the working directory are
# never consulted, and resolution creates nothing.

_ROOT_ENV = (*rr.MACHINERY_ENV_VARS, rr.PRODUCT_ROOT_ENV)
_copy_ids = itertools.count()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ROOT_ENV:
        monkeypatch.delenv(name, raising=False)


def _primary_checkout(path: Path) -> Path:
    """A primary checkout: a real Git directory (HEAD, objects/ and refs/), as Git requires."""
    for entry in ("objects", "refs"):
        (path / ".git" / entry).mkdir(parents=True, exist_ok=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return path


def _decoy_cwd(path: Path) -> Path:
    """A directory that looks like a product primary holding dispatch state."""
    marker = path / rr.PRODUCT_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("decoy: true\n", encoding="utf-8")
    (path / "batch_state" / "tasks").mkdir(parents=True)
    (path / "batch_state" / "fleet-comms" / "v1").mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def _tree(root: Path) -> list[tuple[str, str]]:
    """Every entry under ``root`` (not following symlinks), with its kind."""
    entries = []
    for directory, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = Path(directory, name)
            kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "file"
            entries.append((path.relative_to(root).as_posix(), kind))
    return sorted(entries)


def _load_copy(monkeypatch: pytest.MonkeyPatch, package_dir: Path):
    """Load a copy of this resolver from ``package_dir/common/repo_root.py`` (as hooks load it, by file).

    The copy is registered in ``sys.modules`` for the test only (dataclasses look the module up).
    """
    target = package_dir / "common" / "repo_root.py"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rr.__file__, target)
    spec = importlib.util.spec_from_file_location(f"_repo_root_copy_{next(_copy_ids)}", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_the_installed_package_refuses_product_and_state_without_anchors(unconfigured, tmp_path, monkeypatch):
    """Defect class: installed machinery inventing a product or a state anchor from its own location or cwd."""
    assert rr.in_tree_source() is False
    assert rr.machinery_root().name == rr.INSTALLED_PACKAGE_NAME
    decoy = _decoy_cwd(tmp_path / "decoy")
    monkeypatch.chdir(decoy)
    with pytest.raises(rr.ProductRootError, match=rr.PRODUCT_ROOT_ENV):
        rr.product_root()
    for resolve in (
        rr.state_root,
        rr.primary_root,
        rr.machinery_roots,
        lambda: rr.state_root(decoy),
        lambda: rr.primary_root(decoy),
        lambda: rr.state_root(REPO),
    ):
        with pytest.raises(rr.StateRootError, match=rr.PRIMARY_ROOT_ENV):
            resolve()
    # Consumers that catch the resolver's ValueError keep refusing.
    with pytest.raises(worktree_claims.ControlPlaneError, match=rr.PRIMARY_ROOT_ENV):
        worktree_claims.public_primary_root()
    # A state root alone satisfies state_root(), but does not stand in for the primary Git checkout anchor.
    primary_candidate = _primary_checkout(tmp_path / "primary")
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(primary_candidate))
    assert rr.state_root() == primary_candidate
    with pytest.raises(rr.StateRootError, match=rr.PRIMARY_ROOT_ENV):
        rr.primary_root()


def test_the_installed_package_resolves_its_configured_anchors(unconfigured, tmp_path, monkeypatch):
    """Defect class: a configured anchor ignored, or replaced by a cwd decoy, in the installed package."""
    monkeypatch.chdir(_decoy_cwd(tmp_path / "decoy"))
    primary = _primary_checkout(tmp_path / "primary")
    product = tmp_path / "kube-dojo"
    product.mkdir()
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    assert rr.product_root() == product
    assert rr.state_root() == rr.primary_root() == primary
    assert worktree_claims.public_primary_root() == primary
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(primary))
    assert rr.state_root() == primary
    dedicated_state = tmp_path / "dedicated-state"
    dedicated_state.mkdir()
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(dedicated_state))
    assert rr.state_root() == dedicated_state
    assert rr.primary_root() == primary
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(tmp_path / "decoy"))
    with pytest.raises(rr.MachineryConfigError, match="not a real Git directory"):
        rr.state_root()


@pytest.mark.parametrize("in_tree", [False, True], ids=["installed", "in-tree"])
def test_an_explicit_product_needs_no_curriculum_marker(unconfigured, tmp_path, monkeypatch, in_tree):
    """Defect class: a non-curriculum product (kube-dojo) refused because it lacks Ukrainian curriculum data."""
    monkeypatch.setattr(rr, "_IN_TREE_SOURCE", in_tree)
    product = tmp_path / "kube-dojo"
    product.mkdir()
    assert not (product / rr.PRODUCT_MARKER).exists()
    assert rr.product_root(product) == product
    assert rr.product_root(str(product)) == product
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    assert rr.product_root() == product
    # Returned as given: a release snapshot link stays the snapshot path.
    snapshot = tmp_path / "current"
    snapshot.symlink_to(product, target_is_directory=True)
    assert rr.product_root(snapshot) == snapshot


@pytest.mark.parametrize("bad", ["relative", "missing", "file", "nul", "empty", "bytes"])
def test_an_invalid_explicit_product_never_falls_through(unconfigured, tmp_path, monkeypatch, bad):
    """Defect class: a mistyped selection silently replaced by the environment or the checkout default."""
    fallback = tmp_path / "configured"
    fallback.mkdir()
    (tmp_path / "a-file").write_text("", encoding="utf-8")
    value = {
        "relative": "kube-dojo",
        "missing": str(tmp_path / "missing"),
        "file": str(tmp_path / "a-file"),
        "nul": f"{tmp_path}/x\x00y",
        "empty": "",
        "bytes": os.fsencode(fallback),
    }[bad]
    # An in-tree copy inside a real product checkout: the derived default would succeed.
    product = tmp_path / "product"
    (product / rr.PRODUCT_MARKER).parent.mkdir(parents=True)
    (product / rr.PRODUCT_MARKER).write_text("", encoding="utf-8")
    in_tree = _load_copy(monkeypatch, product / "scripts")
    assert in_tree.in_tree_source() is True
    assert in_tree.product_root() == product
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(fallback))
    for module in (rr, in_tree):
        with pytest.raises(module.ProductRootError):
            module.product_root(value)
    if bad not in ("empty", "bytes", "nul"):  # empty means unset; the OS refuses NUL and bytes in os.environ
        monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, value)
        for module in (rr, in_tree):
            with pytest.raises(module.ProductRootError, match=rr.PRODUCT_ROOT_ENV):
                module.product_root()


def test_the_mode_comes_from_the_package_directory_name_not_the_surroundings(unconfigured, tmp_path, monkeypatch):
    """Defect class: a copy inside a product checkout losing its default, or a decoy enabling one."""
    monkeypatch.chdir(_decoy_cwd(tmp_path / "decoy"))
    # Installed-name copy inside a directory that carries every product and state marker.
    staged = _decoy_cwd(tmp_path / "site-packages-ish")
    installed = _load_copy(monkeypatch, staged / rr.INSTALLED_PACKAGE_NAME)
    assert installed.in_tree_source() is False
    with pytest.raises(installed.ProductRootError):
        installed.product_root()
    with pytest.raises(installed.StateRootError):
        installed.state_root()
    # The in-tree copy keeps its established checkout defaults.
    product = _decoy_cwd(tmp_path / "product")
    in_tree = _load_copy(monkeypatch, product / "scripts")
    assert in_tree.in_tree_source() is True
    assert in_tree.product_root() == product
    assert in_tree.state_root() == in_tree.primary_root() == product
    # A package directory symlinked into the product keeps the in-tree name it is loaded under.
    linked = _decoy_cwd(tmp_path / "linked-product")
    (linked / "scripts").symlink_to(staged / rr.INSTALLED_PACKAGE_NAME, target_is_directory=True)
    assert _load_copy(monkeypatch, linked / "scripts").in_tree_source() is True
    # An in-tree copy outside any product still refuses rather than consulting cwd.
    stray = _load_copy(monkeypatch, tmp_path / "stray" / "scripts")
    with pytest.raises(stray.ProductRootError, match="not a product checkout"):
        stray.product_root()


def test_resolution_creates_nothing(unconfigured, tmp_path, monkeypatch):
    """Defect class: a resolver that creates state directories, files or databases as a side effect."""
    decoy = _decoy_cwd(tmp_path / "decoy")
    primary = _primary_checkout(tmp_path / "primary")
    product = tmp_path / "product"
    product.mkdir()
    # An in-tree product checkout without state yet: its defaults must not create any.
    in_tree_product = _primary_checkout(tmp_path / "in-tree-product")
    (in_tree_product / rr.PRODUCT_MARKER).parent.mkdir(parents=True)
    (in_tree_product / rr.PRODUCT_MARKER).write_text("", encoding="utf-8")
    in_tree = _load_copy(monkeypatch, in_tree_product / "scripts")
    # The plane owner's consumer profile is read too, and reading it creates nothing either.
    _write_profile(primary, _PROFILE)
    monkeypatch.chdir(decoy)
    watched = (tmp_path, REPO / rr.INSTALLED_PACKAGE_NAME / "common")
    before = [_tree(root) for root in watched]
    repo_top = sorted(os.listdir(REPO))

    def every_resolution() -> None:
        for module in (rr, in_tree):
            for resolve, args in (
                (module.product_root, ()),
                (module.product_root, (product,)),
                (module.product_root, (tmp_path / "missing",)),
                (module.state_root, ()),
                (module.primary_root, ()),
                (module.machinery_roots, ()),
                (module.machinery_path, ("config/model_catalog.yaml",)),
            ):
                with contextlib.suppress(ValueError, FileNotFoundError):
                    resolve(*args)
        for resolve, args in (
            (profile.load_profile, ()),
            (profile.config_path, ("fleet_repos",)),
            (rr.github_slug, ()),
        ):
            with contextlib.suppress(ValueError):
                resolve(*args)

    every_resolution()
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    every_resolution()
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(primary))
    every_resolution()
    assert [_tree(root) for root in watched] == before
    assert sorted(os.listdir(REPO)) == repo_top
    for root in (primary, product, in_tree_product):
        assert not (root / "batch_state").exists()


def test_package_imports_and_help_work_with_nothing_configured(tmp_path):
    """Defect class: a missing anchor breaking import or ``--help`` instead of the operation that needs it."""
    decoy = _decoy_cwd(tmp_path / "decoy")
    env = {key: value for key, value in os.environ.items() if key not in (*_ROOT_ENV, "LU_TASKS_DIR", "PYTHONPATH")}
    env["PYTHONPATH"] = str(REPO)
    before = _tree(decoy)
    probe = (
        "from lu_machinery.common import repo_root as rr\n"
        "for resolve in (rr.product_root, rr.state_root):\n"
        "    try:\n"
        "        resolve()\n"
        "    except rr.MachineryConfigError as exc:\n"
        "        print(type(exc).__name__)\n"
    )
    imported = subprocess.run(
        [sys.executable, "-P", "-c", probe], cwd=decoy, env=env, capture_output=True, text=True, timeout=120
    )
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.split() == ["ProductRootError", "StateRootError"]
    helped = subprocess.run(
        [sys.executable, "-P", "-m", "lu_machinery.dispatch", "--help"],
        cwd=decoy,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert helped.returncode == 0, helped.stderr
    assert "usage" in helped.stdout.lower()
    assert _tree(decoy) == before


@pytest.mark.parametrize("configured", [rr.PRODUCT_ROOT_ENV, "CLAUDE_PROJECT_DIR"])
@pytest.mark.parametrize("shape", ["symlinked-git", "malformed-gitfile", "newline-named"])
def test_a_configured_product_cannot_hide_a_bad_invocation_git(unconfigured, tmp_path, monkeypatch, shape, configured):
    """Defect class (C3): a configured product root hiding the invocation checkout's symlinked or malformed ``.git``.

    ``newline-named`` is the same symlinked ``.git`` in a directory whose name ends in a
    newline, beside a clean-named real primary that command substitution would strip it to.

    Configured precedence stays: a well-formed invocation directory still resolves to the
    configured product. Only the invocation's own Git metadata is refused, at the hook-root
    rule and at the AGY startup gate, before any interpreter or hook config is accepted.
    """
    from lu_machinery.agent_runtime import agy_hook_config as hooks

    primary = _primary_checkout(tmp_path / "primary")
    product = tmp_path / "product"
    product.mkdir()
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
    # Empty, not unset: the gate sets a missing product root, and this restores it.
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, "")
    monkeypatch.setenv(configured, str(product))
    bad = tmp_path / ("invocation\n" if shape == "newline-named" else "invocation")
    bad.mkdir()
    if shape == "newline-named":
        # The clean namesake is a valid primary: checking it instead of ``bad`` would accept.
        _primary_checkout(tmp_path / "invocation")
    if shape in ("symlinked-git", "newline-named"):
        (bad / ".git").symlink_to(primary / ".git")
    else:
        (bad / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere' / 'x'}\n", encoding="utf-8")
    good = tmp_path / "plain"
    good.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # Written literally, so the case does not depend on how the registration is built.
    command = {"type": "command", "command": hooks.native_hook_command(hooks.GUARD_MODE), "timeout": 10}
    registration = {hooks.GUARD_NAME: {"PreToolUse": [{"matcher": "run_command", "hooks": [command]}]}}
    for workspace in (bad, good):
        (workspace / ".agents").mkdir()
        (workspace / ".agents" / "hooks.json").write_text(json.dumps(registration), encoding="utf-8")

    with pytest.raises(rr.ProductRootError, match="no hook context resolves"):
        rr.native_hook_roots(bad)
    with pytest.raises(hooks.AgyHookConfigError, match="hook_context_unavailable"):
        hooks.require_hook_config(bad, home)
    assert rr.native_hook_roots(good) == (product, primary.resolve())
    hooks.require_hook_config(good, home)


def test_native_hook_roots_admit_a_dedicated_non_git_state_root_beside_the_git_anchor(unconfigured, tmp_path, monkeypatch):
    """Defect class: the hook boundary refusing the detached layout (a dedicated non-Git LU_STATE_ROOT
    beside a Git primary anchor) or admitting a malformed state root. Every Python hook re-runs this
    rule, so a refusal here refused every driver-state and audit hook from the CTO workspace."""
    primary = _primary_checkout(tmp_path / "primary")
    workspace, state = tmp_path / "cto-workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(workspace))
    monkeypatch.setenv(rr.LU_STATE_ROOT_ENV, str(state))
    assert rr.native_hook_roots(workspace) == (workspace, primary.resolve())
    # A state root that is a checkout or inside any checkout must still be refused; a stray .git still refuses.
    other = _primary_checkout(tmp_path / "other")
    other_sub = other / "nested-state"
    other_sub.mkdir()
    primary_sub = primary / "nested-state"
    primary_sub.mkdir()
    link_to_other_sub = tmp_path / "link-to-other-sub"
    link_to_other_sub.symlink_to(other_sub, target_is_directory=True)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    link_inside_other = other / "link-to-outside"
    link_inside_other.symlink_to(outside, target_is_directory=True)
    stray = tmp_path / "stray"
    (stray / ".git").mkdir(parents=True)
    stray_sub = stray / "nested-state"
    stray_sub.mkdir()
    for refused in (other, other_sub, primary_sub, link_to_other_sub, link_inside_other, stray, stray_sub):
        monkeypatch.setenv(rr.LU_STATE_ROOT_ENV, str(refused))
        with pytest.raises(rr.ProductRootError, match="no hook context resolves"):
            rr.native_hook_roots(workspace)
    # An unsearchable directory whose `.git` lookup fails with EACCES must fail closed, not appear non-Git.
    if os.geteuid() != 0:
        unsearchable = _primary_checkout(tmp_path / "unsearchable-other")
        unsearchable.chmod(0o600)
        try:
            monkeypatch.setenv(rr.LU_STATE_ROOT_ENV, str(unsearchable))
            with pytest.raises(rr.ProductRootError, match="no hook context resolves"):
                rr.native_hook_roots(workspace)
        finally:
            unsearchable.chmod(0o755)
    # The alias is the generated hook command's checkout state root: named non-Git, it refuses there and here.
    monkeypatch.delenv(rr.LU_STATE_ROOT_ENV)
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(state))
    with pytest.raises(rr.ProductRootError, match="no hook context resolves"):
        rr.native_hook_roots(workspace)


def test_the_native_hook_contract_bytes_match_the_product_contract():
    """Defect class: the installed hook resolver block or tail drifting from the product's coordinated contract.

    Product manifests carry these exact bytes and the installed validator compares them
    byte for byte, so a change on one side alone refuses every product hook registration.
    """
    import hashlib

    from lu_machinery.agent_runtime import agy_hook_config as hooks

    digest = {
        "resolvers": hashlib.sha256(hooks._hook_resolvers().encode()).hexdigest(),
        "tail": hashlib.sha256(hooks._HOOK_TAIL.encode()).hexdigest(),
        "audit_tail": hashlib.sha256(hooks._AUDIT_HOOK_TAIL.encode()).hexdigest(),
        "guard_tail": hashlib.sha256(hooks._GUARD_HOOK_TAIL.encode()).hexdigest(),
    }
    assert digest == {
        "resolvers": "14fd6464950653da6ec18ddc09445077532975999f1e54bdca8a6e109a34b0d5",
        "tail": "54569cfc49db1ec65191d713492a0e765cdc3b04b46ecba77b3dc8a39884c1a8",
        "audit_tail": "1924ef1b52e5de1eef216e8976048723e7a3ae58207c486dbde5db12447424a1",
        "guard_tail": "bd53ab0788a15ae62dcfc2bdb1ab6b918b94a9dadc02e17526462bcce067f3a2",
    }
    # Product PR 13005 retains this audit extension until machinery owns it.
    assert "claude-audit-hook" in hooks.NATIVE_HOOK_MODES


# --------------------------------------------------------------------------- A08.1 consumer profile and LU_* names
#
# One profile, bound to the control-plane owner (the validated state root), is the one
# authority for a consumer's GitHub slug and config files. The LU_* names are the primary
# configuration names and the LEARN_UKRAINIAN_* names stay readable aliases.

_PROFILE = {
    "schema": profile.PROFILE_SCHEMA,
    "version": profile.PROFILE_VERSION,
    "profile_id": "demo-consumer",
    "github_slug": "demo-org/demo-consumer",
    "config": {"fleet_repos": ".lu-machinery/fleet_repos.yaml"},
}


def _write_profile(owner: Path, document: dict | bytes) -> Path:
    """Commit-shaped profile files below ``owner``: the profile and the config file it names."""
    directory = owner / profile.PROFILE_DIR
    directory.mkdir(exist_ok=True)
    (directory / "fleet_repos.yaml").write_text("version: 1\n", encoding="utf-8")
    blob = document if isinstance(document, bytes) else json.dumps(document).encode()
    (directory / profile.PROFILE_NAME).write_bytes(blob)
    return directory / profile.PROFILE_NAME


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_lu_and_legacy_root_names_resolve_alike_and_a_conflict_refuses(unconfigured, tmp_path, monkeypatch):
    """Defect class: alias split, where LU_* and LEARN_UKRAINIAN_* silently select different roots.

    New-only, legacy-only and agreeing settings resolve alike; differing ones refuse naming both;
    messages name the variable read; state/primary equality and the primary .venv fallback hold.
    """
    primary = _primary_checkout(tmp_path / "primary")
    other = _primary_checkout(tmp_path / "other")
    venv_python = _executable(primary / ".venv" / "bin" / "python")
    python, other_python = _executable(tmp_path / "bin" / "python"), _executable(tmp_path / "bin" / "python2")
    anchored = {rr.LU_PRIMARY_ROOT_ENV: str(primary)}
    cases = {
        rr.LU_PRIMARY_ROOT_ENV: ({}, primary, other, lambda env: rr.primary_root(env=env)),
        rr.LU_STATE_ROOT_ENV: (anchored, primary, other, lambda env: rr.state_root(env=env)),
        rr.LU_INTERPRETER_ENV: (anchored, python, other_python, lambda env: project_interpreter(env=env)),
        rr.LU_GITHUB_SLUG_ENV: (anchored, "demo-org/a", "demo-org/b", lambda env: rr.github_slug(env)),
    }
    before = _tree(tmp_path)
    for new, (base, value, different, resolve) in cases.items():
        legacy = rr.ENV_ALIASES[new]
        expected = resolve({**base, legacy: str(value)})
        assert expected == value
        assert resolve({**base, new: str(value)}) == expected
        assert resolve({**base, new: str(value), legacy: str(value)}) == expected
        assert resolve({**base, new: "", legacy: str(value)}) == expected
        for pair in ({new: str(value), legacy: str(different)}, {new: str(different), legacy: str(value)}):
            with pytest.raises(rr.MachineryConfigError, match=f"{new} and its compatibility alias {legacy}"):
                resolve({**base, **pair})
        for name in (new, legacy):
            with pytest.raises(rr.MachineryConfigError, match=f"^{name} must be"):
                resolve({**base, name: "relative"})
    for name in (rr.LU_STATE_ROOT_ENV, rr.STATE_ROOT_ENV):
        assert rr.state_root(env={**anchored, name: str(other)}) == other
        missing_state = tmp_path / "missing-state"
        with pytest.raises(rr.MachineryConfigError, match=f"^{name} .* must be an existing directory"):
            rr.state_root(env={**anchored, name: str(missing_state)})
    for name in (rr.LU_PRIMARY_ROOT_ENV, rr.PRIMARY_ROOT_ENV):
        assert project_interpreter(env={name: str(primary)}) == venv_python
    with pytest.raises(rr.StateRootError, match=rr.LU_PRIMARY_ROOT_ENV):
        rr.state_root(env={})
    # A missing primary anchor never hides an invalid state setting from any resolver.
    for invalid in (
        {rr.LU_STATE_ROOT_ENV: str(primary), rr.STATE_ROOT_ENV: str(other)},
        {rr.LU_STATE_ROOT_ENV: "relative"},
        {rr.STATE_ROOT_ENV: "relative"},
    ):
        for call in (
            lambda e: rr.state_root(env=e),
            rr.github_slug,
            rr.configured_github_slug,
            lambda e: profile.load_profile(env=e),
            lambda e: profile.config_path("fleet_repos", env=e),
        ):
            with pytest.raises(rr.MachineryConfigError) as refused:
                call(invalid)
            assert not isinstance(refused.value, rr.StateRootError), refused.value
    # The native hook boundary refuses a conflict before it runs the shell rule.
    monkeypatch.setenv(rr.LU_PRIMARY_ROOT_ENV, str(primary))
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(other))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("the hook rule ran"))
    with pytest.raises(rr.ProductRootError, match="compatibility alias"):
        rr.native_hook_roots(tmp_path)
    assert _tree(tmp_path) == before


def test_an_absent_profile_keeps_the_compatibility_slug_and_refuses_consumer_config(unconfigured, tmp_path, monkeypatch):
    """Defect class: silent fallback to packaged consumer data, or a behaviour change without a profile.

    A plane owner with no profile (learn-ukrainian today, configured with the legacy names) keeps
    today's slug; every consumer config name refuses, naming the name and the profile path. A
    by-file copy of the resolver, which has no profile reader beside it, keeps today's slug for an
    owner without a profile directory and refuses for one that has a profile directory.
    """
    owner = _primary_checkout(tmp_path / "owner")
    (owner / profile.PROFILE_DIR).mkdir()  # a profile directory without the profile is no profile
    bare = _primary_checkout(tmp_path / "bare-owner")
    copy = _load_copy(monkeypatch, tmp_path / "copy-package")
    where = re.escape(str(owner / profile.PROFILE_REL))
    before = _tree(tmp_path)
    for name in (rr.PRIMARY_ROOT_ENV, rr.LU_PRIMARY_ROOT_ENV):
        assert copy.github_slug({name: str(bare)}) == rr.DEFAULT_GITHUB_SLUG
        assert copy.github_slug({name: str(bare), rr.GITHUB_SLUG_ENV: "demo-org/x"}) == "demo-org/x"
        with pytest.raises(copy.MachineryConfigError, match="profile reader beside this resolver is unavailable"):
            copy.github_slug({name: str(owner)})
    for env in ({rr.PRIMARY_ROOT_ENV: str(owner)}, {rr.LU_PRIMARY_ROOT_ENV: str(owner)}, {}):
        assert profile.load_profile(env=env) is None
        assert rr.configured_github_slug(env) is None
        assert rr.github_slug(env) == rr.DEFAULT_GITHUB_SLUG
        assert rr.github_slug({**env, rr.LU_GITHUB_SLUG_ENV: "demo-org/x"}) == "demo-org/x"
        for name in sorted(profile.CONSUMER_CONFIG_NAMES):
            expected = f"consumer config '{name}' needs a consumer profile, and " + (
                f"the plane owner has none at {where}" if env else "no plane owner is configured"
            )
            with pytest.raises(profile.ProfileError, match=expected):
                profile.config_path(name, env=env)
        with pytest.raises(profile.ProfileError, match="'model_catalog' is not a consumer config name"):
            profile.config_path("model_catalog", env=env)
    assert _tree(tmp_path) == before


def test_a_profile_binds_slug_and_config_paths_to_the_plane_owner(unconfigured, tmp_path, monkeypatch):
    """Defect class: a profile read from or resolved against a non-owner root, or a conflicting slug accepted.

    Profiles in the selected product and the working directory are never read; config paths
    resolve below the plane owner; an environment slug may only agree with the profile.
    """
    owner = _primary_checkout(tmp_path / "owner")
    nested = owner / "deploy" / "streams.yaml"
    nested.parent.mkdir()
    nested.write_text("streams: []\n", encoding="utf-8")
    path = _write_profile(owner, {**_PROFILE, "config": {**_PROFILE["config"], "issue_streams": "deploy/streams.yaml"}})
    for decoy, slug in ((tmp_path / "product", "decoy-org/product"), (tmp_path / "cwd", "decoy-org/cwd")):
        decoy.mkdir()
        _write_profile(decoy, {**_PROFILE, "github_slug": slug, "config": {"code_owners": ".lu-machinery/fleet_repos.yaml"}})
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(tmp_path / "product"))
    monkeypatch.chdir(tmp_path / "cwd")
    before = _tree(tmp_path)
    for env in (
        {rr.LU_PRIMARY_ROOT_ENV: str(owner)},
        {rr.PRIMARY_ROOT_ENV: str(owner)},
        {rr.PRIMARY_ROOT_ENV: str(owner), rr.LU_STATE_ROOT_ENV: str(owner)},
        # A relative object-directory override names a place below the working directory (which
        # has no objects/ here), not a property of the plane owner: it never decides the answer.
        {rr.PRIMARY_ROOT_ENV: str(owner), "GIT_OBJECT_DIRECTORY": "objects"},
    ):
        loaded = profile.load_profile(env=env)
        assert loaded is not None
        assert (loaded.path, loaded.owner_root, loaded.profile_id, loaded.github_slug, dict(loaded.config)) == (
            path,
            owner,
            "demo-consumer",
            "demo-org/demo-consumer",
            {"fleet_repos": ".lu-machinery/fleet_repos.yaml", "issue_streams": "deploy/streams.yaml"},
        )
        assert rr.github_slug(env) == rr.configured_github_slug(env) == "demo-org/demo-consumer"
        assert profile.config_path("fleet_repos", env=env) == owner / ".lu-machinery" / "fleet_repos.yaml"
        assert profile.config_path("issue_streams", env=env) == nested
        with pytest.raises(profile.ProfileError, match="names no 'code_owners' config"):
            profile.config_path("code_owners", env=env)
        for name in (rr.LU_GITHUB_SLUG_ENV, rr.GITHUB_SLUG_ENV):
            assert rr.github_slug({**env, name: "demo-org/demo-consumer"}) == "demo-org/demo-consumer"
            for call in (
                rr.github_slug,
                rr.configured_github_slug,
                lambda e: profile.load_profile(env=e),
                lambda e: profile.config_path("fleet_repos", env=e),
            ):
                with pytest.raises(profile.ProfileError, match="the profile is the one authority"):
                    call({**env, name: "demo-org/other"})
    assert _tree(tmp_path) == before


def _profile_text(**changes: object) -> bytes:
    return json.dumps({**_PROFILE, **changes}).encode()


def _symlinked_profile(owner: Path) -> None:
    target = owner.parent / "elsewhere.json"
    target.write_bytes(json.dumps(_PROFILE).encode())
    (owner / profile.PROFILE_REL).unlink()
    (owner / profile.PROFILE_REL).symlink_to(target)


def _symlinked_directory(owner: Path) -> None:
    shutil.move(owner / profile.PROFILE_DIR, owner.parent / "elsewhere")
    (owner / profile.PROFILE_DIR).symlink_to(owner.parent / "elsewhere", target_is_directory=True)


def _replace(owner: Path, kind: str, relative: str = profile.PROFILE_REL) -> None:
    entry = owner / relative
    entry.unlink()
    if kind == "fifo":
        os.mkfifo(entry)
    elif kind == "directory":
        entry.mkdir()
    else:
        entry.symlink_to(owner / "deploy-target.yaml")
        (owner / "deploy-target.yaml").write_text("version: 1\n", encoding="utf-8")


def _profile_dir_is_a_file(owner: Path) -> None:
    shutil.rmtree(owner / profile.PROFILE_DIR)
    (owner / profile.PROFILE_DIR).write_text("{}", encoding="utf-8")


def _symlinked_config_directory(owner: Path) -> None:
    (owner / "real").mkdir()
    (owner / "real" / "fleet_repos.yaml").write_text("version: 1\n", encoding="utf-8")
    (owner / "linked").symlink_to(owner / "real", target_is_directory=True)
    (owner / profile.PROFILE_REL).write_bytes(_profile_text(config={"fleet_repos": "linked/fleet_repos.yaml"}))


_VALID = json.dumps(_PROFILE)
_INVALID_PROFILES = {
    "duplicate-member": _VALID.replace('"version": 1', '"version": 1, "version": 1').encode(),
    "nested-duplicate-member": _VALID.replace('"config": {', '"config": {"fleet_repos": "x.yaml", ').encode(),
    "unknown-field": _profile_text(hooks={}),
    "missing-field": json.dumps({k: v for k, v in _PROFILE.items() if k != "github_slug"}).encode(),
    "oversize": _VALID.encode() + b" " * profile.MAX_PROFILE_BYTES,
    "nan": _VALID.replace('"version": 1', '"version": NaN').encode(),
    "infinity": _VALID.replace('"version": 1', '"version": -Infinity').encode(),
    "bool-version": _profile_text(version=True),
    "float-version": _profile_text(version=1.0),
    "wrong-schema": _profile_text(schema="lu-machinery.profile.v2"),
    "non-utf8": _VALID.encode().replace(b"demo-consumer", b"demo-\xff"),
    "utf8-bom": b"\xef\xbb\xbf" + _VALID.encode(),
    "not-an-object": b"[]",
    "empty-file": b"",
    "trailing-data": _VALID.encode() + b"{}",
    "too-deep": _profile_text(config={"fleet_repos": {"path": "x.yaml"}}),
    "bad-slug-traversal": _profile_text(github_slug="demo-org/.."),
    "bad-slug-shape": _profile_text(github_slug="-demo/x"),
    "bad-profile-id": _profile_text(profile_id="Demo"),
    "config-not-object": _profile_text(config=[]),
    "unknown-config-name": _profile_text(config={"model_catalog": "x.yaml"}),
    "absolute-config": _profile_text(config={"fleet_repos": "/etc/fleet_repos.yaml"}),
    "traversal-config": _profile_text(config={"fleet_repos": "../fleet_repos.yaml"}),
    "dot-config": _profile_text(config={"fleet_repos": "./fleet_repos.yaml"}),
    "empty-component-config": _profile_text(config={"fleet_repos": ".lu-machinery//fleet_repos.yaml"}),
    "trailing-slash-config": _profile_text(config={"fleet_repos": ".lu-machinery/"}),
    "backslash-config": _profile_text(config={"fleet_repos": ".lu-machinery\\fleet_repos.yaml"}),
    "control-config": _profile_text(config={"fleet_repos": "fleet\nrepos.yaml"}),
    "bidi-config": _profile_text(config={"fleet_repos": "fleet‮repos.yaml"}),
    "line-separator-config": _profile_text(config={"fleet_repos": "fleet repos.yaml"}),
    "surrogate-config": _VALID.replace(".lu-machinery/fleet_repos.yaml", "fleet\\ud800.yaml").encode(),
    "symlinked-profile": _symlinked_profile,
    "symlinked-profile-directory": _symlinked_directory,
    "fifo-profile": lambda owner: _replace(owner, "fifo"),
    "directory-profile": lambda owner: _replace(owner, "directory"),
    "profile-directory-is-a-file": _profile_dir_is_a_file,
}
_INVALID_CONFIG_TARGETS = {
    "missing-target": lambda owner: (owner / ".lu-machinery" / "fleet_repos.yaml").unlink(),
    "symlinked-target": lambda owner: _replace(owner, "symlink", ".lu-machinery/fleet_repos.yaml"),
    "fifo-target": lambda owner: _replace(owner, "fifo", ".lu-machinery/fleet_repos.yaml"),
    "directory-target": lambda owner: _replace(owner, "directory", ".lu-machinery/fleet_repos.yaml"),
    "symlinked-target-directory": _symlinked_config_directory,
}


@pytest.mark.parametrize(
    "case", [*(("profile", name) for name in _INVALID_PROFILES), *(("target", name) for name in _INVALID_CONFIG_TARGETS)],
    ids=lambda case: f"{case[0]}-{case[1]}",
)
def test_an_invalid_profile_refuses_and_never_falls_back(unconfigured, tmp_path, case):
    """Defect class: fail-open profile parsing, where a malformed, oversized, symlinked or special file,
    or a config entry that escapes or does not reach a regular file, is accepted or treated as no profile.
    """
    kind, name = case
    owner = _primary_checkout(tmp_path / "owner")
    _write_profile(owner, _PROFILE)
    change = (_INVALID_PROFILES if kind == "profile" else _INVALID_CONFIG_TARGETS)[name]
    if isinstance(change, bytes):
        (owner / profile.PROFILE_REL).write_bytes(change)
    else:
        change(owner)
    before = _tree(tmp_path)
    for env in ({rr.PRIMARY_ROOT_ENV: str(owner)}, {rr.LU_PRIMARY_ROOT_ENV: str(owner)}):
        # An agreeing slug variable never stands in for an unusable profile.
        agreeing = {**env, rr.LU_GITHUB_SLUG_ENV: _PROFILE["github_slug"]}
        if kind == "profile":
            for call in (profile.load_profile, rr.github_slug, rr.configured_github_slug):
                for selected in (env, agreeing):
                    with pytest.raises(profile.ProfileError, match="consumer profile"):
                        call(env=selected) if call is profile.load_profile else call(selected)
        else:
            assert profile.load_profile(env=env) is not None
            assert rr.github_slug(agreeing) == _PROFILE["github_slug"]
        for selected in (env, agreeing):
            with pytest.raises(profile.ProfileError):
                profile.config_path("fleet_repos", env=selected)
    assert _tree(tmp_path) == before


def test_non_git_state_root_and_explicit_repos_root_contract(unconfigured, tmp_path):
    """Defect class: separate non-Git LU_STATE_ROOT refused, or siblings resolving via state_root().parent."""
    state = tmp_path / "var-lib-machinery"
    state.mkdir()
    repos = tmp_path / "repos"
    repos.mkdir()
    primary = _primary_checkout(repos / "learn-ukrainian")
    linked = tmp_path / "linked-wt"
    _link_worktree(primary, linked)
    stray = tmp_path / "stray"
    (stray / ".git").mkdir(parents=True)
    plain_file = tmp_path / "plain-file"
    plain_file.write_text("x", encoding="utf-8")
    python = _executable(tmp_path / "runtime" / "bin" / "python")

    detached_env = {
        rr.LU_STATE_ROOT_ENV: str(state),
        rr.REPOS_ROOT_ENV: str(repos),
        rr.LU_INTERPRETER_ENV: str(python),
    }
    assert rr.configured_state_root(detached_env) == state
    assert rr.state_root(env=detached_env) == state
    assert rr.configured_repos_root(detached_env) == repos
    assert rr.repos_root(env=detached_env) == repos
    roots = rr.machinery_roots(detached_env)
    assert (roots.state, roots.primary, roots.repos, roots.interpreter) == (state, None, repos, python)
    with pytest.raises(rr.StateRootError, match=rr.LU_PRIMARY_ROOT_ENV):
        rr.primary_root(env=detached_env)

    # Without LU_REPOS_ROOT, repos_root falls back to primary_root().parent and never state_root().parent.
    assert rr.repos_root(env={rr.LU_PRIMARY_ROOT_ENV: str(primary)}) == repos
    with pytest.raises(rr.StateRootError, match=rr.REPOS_ROOT_ENV):
        rr.repos_root(env={rr.LU_STATE_ROOT_ENV: str(state)})

    # Real-Git and directory validation stay fail-closed for bad LU_STATE_ROOT and LU_REPOS_ROOT values.
    for bad_state in (str(linked), str(stray), str(plain_file), str(tmp_path / "missing"), "relative"):
        with pytest.raises(rr.MachineryConfigError):
            rr.state_root(env={rr.LU_STATE_ROOT_ENV: bad_state})
    for bad_repos in (str(primary), str(plain_file), str(tmp_path / "missing-repos"), "relative"):
        with pytest.raises(rr.MachineryConfigError):
            rr.repos_root(env={rr.REPOS_ROOT_ENV: bad_repos})


def test_host_profile_outside_state_root_via_lu_profile_path(unconfigured, tmp_path):
    """Defect class: host profile outside state_root ignored or falling back when missing/invalid."""
    state = tmp_path / "state"
    state.mkdir()
    _write_profile(state, {**_PROFILE, "github_slug": "state-org/state-repo"})
    host_dir = tmp_path / "etc-lu-machinery"
    host_dir.mkdir()
    (host_dir / "fleet_repos.yaml").write_text("version: 1\n", encoding="utf-8")
    host_profile = host_dir / "profile.v1.json"
    host_doc = {
        **_PROFILE,
        "profile_id": "host-plane",
        "github_slug": "host-org/host-repo",
        "config": {"fleet_repos": "fleet_repos.yaml"},
    }
    host_profile.write_bytes(json.dumps(host_doc).encode())

    # Works even without state_root, and overrides any profile inside state_root.
    for env in (
        {rr.PROFILE_PATH_ENV: str(host_profile)},
        {rr.LU_STATE_ROOT_ENV: str(state), rr.PROFILE_PATH_ENV: str(host_profile)},
    ):
        loaded = profile.load_profile(env=env)
        assert loaded is not None
        assert (loaded.path, loaded.owner_root, loaded.profile_id, loaded.github_slug) == (
            host_profile,
            host_dir,
            "host-plane",
            "host-org/host-repo",
        )
        assert rr.configured_github_slug(env) == rr.github_slug(env) == "host-org/host-repo"
        assert profile.config_path("fleet_repos", env=env) == host_dir / "fleet_repos.yaml"
        with pytest.raises(profile.ProfileError, match="the profile is the one authority"):
            rr.github_slug({**env, rr.LU_GITHUB_SLUG_ENV: "other-org/other-repo"})

    # Missing, relative, traversal, and symlinked LU_PROFILE_PATH fail closed without fallback.
    link = tmp_path / "linked-profile.json"
    link.symlink_to(host_profile)
    for bad in (
        str(tmp_path / "missing.json"),
        "relative/profile.v1.json",
        str(host_dir / ".." / host_dir.name / "profile.v1.json"),
        str(link),
    ):
        for call in (
            lambda e: profile.load_profile(env=e),
            lambda e: profile.config_path("fleet_repos", env=e),
            rr.github_slug,
        ):
            with pytest.raises(profile.ProfileError):
                call({rr.LU_STATE_ROOT_ENV: str(state), rr.PROFILE_PATH_ENV: bad})


@pytest.mark.parametrize(
    "layout_kind",
    ["today-primary", "non-git-clean", "non-git-with-legacy", "non-git-without-repos-root"],
)
def test_every_entry_point_converges_for_primary_and_non_git_state_root(
    unconfigured, tmp_path, monkeypatch, layout_kind: str
):
    """Convergence test: Monitor, CLI, dispatcher, session supervisor, control plane and keeper-facing
    resolvers all converge on the exact same DBs, task store, lock paths and sibling checkouts for
    (a) today's layout (state_root == primary checkout) and (b) a dedicated non-Git state root
    (both clean and with legacy env vars set).
    """
    from lu_machinery.api import config as api_config
    from lu_machinery.api import monitor_context
    from lu_machinery.common.task_store_paths import tasks_dir
    from lu_machinery.control_plane import storage as cp_storage
    from lu_machinery.dispatcher import git as dispatch_git
    from lu_machinery.dispatcher import primary as dispatch_primary
    from lu_machinery.fleet_comms.paths import DEFAULT_ROOT_REL, default_plane_root
    from lu_machinery.orchestration import (
        fleet_repos,
        merge_queue_keeper,
        pr_ownership,
        task_lifecycle,
        thread_handoff,
    )
    from lu_machinery.session_streams.db import (
        DEFAULT_RELATIVE_DATABASE,
        SessionStreamDatabase,
        canonical_state_root,
        default_database_path,
    )

    repos = tmp_path / "repos"
    repos.mkdir()
    primary = repos / ("custom-primary" if layout_kind == "non-git-without-repos-root" else "learn-ukrainian")
    sibling = repos / "machinery"
    for checkout in (primary, sibling):
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, timeout=30)
    worktree = primary / ".worktrees" / "dispatch" / "agy" / "conv"
    subprocess.run(["git", "-C", str(primary), "config", "user.email", "t@example.invalid"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(primary), "config", "user.name", "T"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(primary), "commit", "--allow-empty", "-qm", "init"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "conv", str(worktree)], check=True, timeout=30)
    python = _executable(tmp_path / "runtime" / "bin" / "python")

    for name in (
        "FLEET_COMMS_ROOT",
        "LU_TASKS_DIR",
        "LEARN_UKRAINIAN_OWNERSHIP_LEDGER",
        "LEARN_UKRAINIAN_BATCH_STATE_DIR",
        "LEARN_UK_REPO_ROOT",
        "BACKUP_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    api_config_caches = ("PROJECT_ROOT", "PRODUCT_ROOT", "LIVE_REPO_ROOT", "CURRICULUM_ROOT", "DASHBOARDS_DIR", "BATCH_STATE_DIR")
    for name in api_config_caches:
        if name in vars(api_config):
            monkeypatch.delitem(vars(api_config), name)
    monitor_context.production_context.cache_clear()

    try:
        if layout_kind == "today-primary":
            expected_state = primary.resolve()
            monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
            monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(primary))
            monkeypatch.setenv(rr.INTERPRETER_ENV, str(python))
            expected_lock_dir = (primary / ".git" / worktree_claims.LOCK_DIR_NAME).resolve()
        else:
            dedicated = tmp_path / "var-lib-machinery"
            dedicated.mkdir()
            expected_state = dedicated.resolve()
            monkeypatch.setenv(rr.LU_STATE_ROOT_ENV, str(dedicated))
            monkeypatch.setenv(rr.LU_INTERPRETER_ENV, str(python))
            if layout_kind != "non-git-without-repos-root":
                monkeypatch.setenv(rr.REPOS_ROOT_ENV, str(repos))
            if layout_kind == "non-git-with-legacy":
                monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
                monkeypatch.setenv("LEARN_UK_REPO_ROOT", str(primary))
            elif layout_kind == "non-git-without-repos-root":
                monkeypatch.setenv(rr.LU_PRIMARY_ROOT_ENV, str(primary))
            expected_lock_dir = (dedicated / "batch_state" / "locks" / worktree_claims.LOCK_DIR_NAME).resolve()

        expected_fleet_db = (expected_state / DEFAULT_ROOT_REL / "comms.sqlite3").resolve()
        expected_streams_db = (expected_state / DEFAULT_RELATIVE_DATABASE).resolve()
        expected_tasks = (expected_state / "batch_state" / "tasks").resolve()
        expected_ownership_db = expected_tasks / "write-ownership.sqlite3"

        # Monitor entry point
        ctx = monitor_context.production_context()
        assert ctx.roots.state_root.resolve() == expected_state
        assert ctx.roots.batch_state_dir.resolve() == expected_state / "batch_state"
        assert ctx.roots.session_streams_db_path.resolve() == expected_streams_db
        assert ctx.stores.session_streams_database is not None
        assert ctx.stores.session_streams_database.path.resolve() == expected_streams_db

        # Fleet Comms + Control Plane storage entry points
        assert (default_plane_root() / "comms.sqlite3").resolve() == expected_fleet_db
        assert (default_plane_root(repo_root=ctx.roots.state_root) / "comms.sqlite3").resolve() == expected_fleet_db
        assert (default_plane_root(repo_root=primary) / "comms.sqlite3").resolve() == expected_fleet_db
        assert cp_storage.sqlite_path(cp_storage.StoreId.FLEET_COMMS).resolve() == expected_fleet_db
        assert cp_storage.sqlite_path(cp_storage.StoreId.SESSION_STREAMS).resolve() == expected_streams_db
        assert cp_storage.sqlite_path(cp_storage.StoreId.WRITE_OWNERSHIP).resolve() == expected_ownership_db

        # Session Streams CLI / DB entry points (none, state_root, primary checkout, linked worktree)
        assert SessionStreamDatabase().path.resolve() == expected_streams_db
        assert default_database_path().resolve() == expected_streams_db
        assert default_database_path(expected_state).resolve() == expected_streams_db
        assert default_database_path(primary).resolve() == expected_streams_db
        assert default_database_path(worktree).resolve() == expected_streams_db
        assert canonical_state_root(expected_state) == expected_state
        assert canonical_state_root(primary) == expected_state
        assert canonical_state_root(worktree) == expected_state

        # Dispatcher entry points
        dispatch_primary.exec_primary(["status"])
        assert tasks_dir().resolve() == expected_tasks
        assert dispatch_git.resolve_repo("machinery")[0] == sibling.resolve()

        # Keeper-facing and orchestration resolvers
        assert worktree_claims.public_primary_root().resolve() == expected_state
        assert worktree_claims.control_plane_root(primary) == expected_state
        assert worktree_claims.control_plane_root(sibling) == expected_state
        assert worktree_claims.repository_lock_dir(worktree_claims.control_plane_root(primary)).resolve() == expected_lock_dir
        assert worktree_claims.repository_lock_dir(worktree_claims.control_plane_root(sibling)).resolve() == expected_lock_dir
        assert worktree_claims.repository_lock_dir(rr.state_root()).resolve() == expected_lock_dir
        assert fleet_repos.resolve_fleet_repo("machinery", primary_root=rr.state_root())[1] == sibling.resolve()
        assert fleet_repos.resolve_fleet_repo("public", primary_root=rr.state_root())[1] == primary.resolve()
        if layout_kind == "today-primary":
            assert dispatch_git.resolve_repo("public")[0] == primary.resolve()
        else:
            with monkeypatch.context() as mp:
                mp.setenv(rr.PRODUCT_ROOT_ENV, str(primary))
                assert dispatch_git.resolve_repo("public")[0] == primary.resolve()
                assert fleet_repos.resolve_fleet_repo("public", primary_root=rr.state_root())[1] == primary.resolve()
        keeper_state, keeper_lock = merge_queue_keeper.default_keeper_paths(primary)
        assert keeper_state.resolve() == expected_state / "batch_state" / "merge_queue_keeper.json"
        assert keeper_lock.resolve() == expected_state / "batch_state" / "locks" / "merge_queue_keeper.lock"
        sib_keeper_state, sib_keeper_lock = merge_queue_keeper.default_keeper_paths(sibling, "learn-ukrainian/machinery")
        assert sib_keeper_state.resolve() != keeper_state.resolve()
        assert sib_keeper_lock.resolve() != keeper_lock.resolve()
        assert pr_ownership.state_path().resolve() == expected_state / "batch_state" / "pr_ownership" / "state.json"
        assert task_lifecycle.canonical_state_root(expected_state) == expected_state
        assert task_lifecycle.canonical_state_root(primary) == expected_state
        assert task_lifecycle.canonical_state_root(worktree) == expected_state
        assert thread_handoff.canonical_state_root(primary).resolve() == expected_state

        # Session supervisor shell helper entry point
        sup_script = f'source "{rr.machinery_path("lib/session_supervisor.sh")}"; _canonical_state_root "$1"'
        sup_res = subprocess.run(
            ["bash", "-c", sup_script, "bash", str(primary)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert sup_res.returncode == 0, sup_res.stderr
        assert Path(sup_res.stdout.strip()).resolve() == expected_state
    finally:
        monitor_context.production_context.cache_clear()
        for name in api_config_caches:
            vars(api_config).pop(name, None)


def test_config_path_default_fallback_and_fail_closed(unconfigured, tmp_path):
    """Defect class: fail-open fallback to packaged default on invalid profile, slug conflict, unknown name, missing or symlinked target."""
    packaged = tmp_path / "packaged.yaml"
    packaged.write_text("default: true\n", encoding="utf-8")

    # No plane owner -> returns default; strict mode still refuses.
    assert profile.config_path("fleet_repos", env={}, default=packaged) == packaged
    with pytest.raises(profile.ProfileError, match="no plane owner is configured"):
        profile.config_path("fleet_repos", env={})

    # Plane owner with no profile -> returns default.
    owner = _primary_checkout(tmp_path / "owner")
    owner_env = {rr.LU_PRIMARY_ROOT_ENV: str(owner)}
    assert profile.config_path("fleet_repos", env=owner_env, default=packaged) == packaged

    # Valid profile: declared key resolves to profile file; omitted key returns default.
    _write_profile(owner, _PROFILE)
    assert profile.config_path("fleet_repos", env=owner_env, default=packaged) == owner / ".lu-machinery" / "fleet_repos.yaml"
    assert profile.config_path("issue_streams", env=owner_env, default=packaged) == packaged

    # Unknown config name never falls back to default.
    with pytest.raises(profile.ProfileError, match="not a consumer config name"):
        profile.config_path("model_catalog", env={}, default=packaged)
    with pytest.raises(profile.ProfileError, match="not a consumer config name"):
        profile.config_path("model_catalog", env=owner_env, default=packaged)

    # Slug conflict never falls back to default.
    with pytest.raises(profile.ProfileError, match="the profile is the one authority"):
        profile.config_path(
            "fleet_repos",
            env={**owner_env, rr.LU_GITHUB_SLUG_ENV: "other-org/other-repo"},
            default=packaged,
        )

    # Missing explicit LU_PROFILE_PATH never falls back to default.
    with pytest.raises(profile.ProfileError):
        profile.config_path(
            "fleet_repos",
            env={rr.PROFILE_PATH_ENV: str(tmp_path / "absent-profile.json")},
            default=packaged,
        )

    # Missing declared target file never falls back to default.
    (owner / ".lu-machinery" / "fleet_repos.yaml").unlink()
    with pytest.raises(profile.ProfileError, match="does not exist"):
        profile.config_path("fleet_repos", env=owner_env, default=packaged)

    # Symlinked declared target file never falls back to default.
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text("repos: {}\n", encoding="utf-8")
    (owner / ".lu-machinery" / "fleet_repos.yaml").symlink_to(elsewhere)
    with pytest.raises(profile.ProfileError, match="symlink"):
        profile.config_path("fleet_repos", env=owner_env, default=packaged)

    # Corrupt profile JSON never falls back to default.
    (owner / profile.PROFILE_REL).write_text("{not-json", encoding="utf-8")
    with pytest.raises(profile.ProfileError, match="not valid UTF-8 JSON"):
        profile.config_path("issue_streams", env=owner_env, default=packaged)


def test_registry_loaders_resolve_profile_config_at_call_time(unconfigured, tmp_path, monkeypatch):
    """Defect class: import-time path freeze or bypass of profile.config_path causing loaders to ignore profile changes after import."""
    from lu_machinery import agent_identity
    from lu_machinery.common import git_credentials
    from lu_machinery.dispatcher import git as dispatch_git
    from lu_machinery.fleet import hramatka_scope_gate, ignored_task_output, sibling_git
    from lu_machinery.ops import driver_lanes
    from lu_machinery.orchestration import (
        driver_agent_type,
        fleet_taxonomy,
        issue_stream_audit,
        slot_routing,
        validate_fleet_taxonomy,
    )
    from lu_machinery.session_streams import inventory as stream_inventory

    def _write_consumer_profile(name: str, slug: str, repo_key: str, epic: int, slot: str, agent_type: str, transport: str) -> Path:
        root = tmp_path / name
        root.mkdir()
        (root / "fleet_repos.yaml").write_text(
            f"repos:\n"
            f"  {repo_key}:\n"
            f"    github: {slug}\n"
            f"    local_name: {repo_key}-dir\n"
            f"    role: public-monorepo\n"
            f"    default: true\n"
            f"    transport: {transport}\n",
            encoding="utf-8",
        )
        (root / "issue_streams.yaml").write_text(
            f"streams:\n"
            f"  {name}-stream:\n"
            f"    title: {name}\n"
            f"    epics: [{epic}]\n",
            encoding="utf-8",
        )
        (root / "area_assignments.yaml").write_text(
            f"schema_version: 1\n"
            f"assignments:\n"
            f"  {name}-area:\n"
            f"    slots: [{slot}]\n"
            f"    driver_agent_type: {agent_type}\n",
            encoding="utf-8",
        )
        (root / "fleet_taxonomy.yaml").write_text(
            f"schema_version: 1\nareas:\n  {name}-area:\n    id: {name}-area\n    aliases: [{name}-lane]\n    epics:\n      - number: {epic}\n        name: {name}\n",
            encoding="utf-8",
        )
        shutil.copyfile(driver_lanes.DATA_FILE, root / "driver_lanes.json")
        (root / "terminal_abandon_succession.yaml").write_bytes(
            ignored_task_output.SUCCESSION_MAP.read_bytes() + f"# {name}\n".encode()
        )
        profile_file = root / "profile.v1.json"
        profile_file.write_text(
            json.dumps(
                {
                    "schema": profile.PROFILE_SCHEMA,
                    "version": profile.PROFILE_VERSION,
                    "profile_id": name,
                    "github_slug": slug,
                    "config": {
                        "fleet_repos": "fleet_repos.yaml",
                        "issue_streams": "issue_streams.yaml",
                        "area_assignments": "area_assignments.yaml",
                        "fleet_taxonomy": "fleet_taxonomy.yaml",
                        "driver_lanes": "driver_lanes.json",
                        "terminal_abandon_succession": "terminal_abandon_succession.yaml",
                    },
                }
            ),
            encoding="utf-8",
        )
        return profile_file

    # With no profile, every loader resolves to the packaged default (byte-identical to pre-profile behaviour).
    assert git_credentials.default_config_path() == git_credentials.CONFIG
    assert issue_stream_audit.default_registry_path() == issue_stream_audit.REGISTRY_PATH
    assert agent_identity.default_assignments_path() == agent_identity.ASSIGNMENTS_PATH
    assert validate_fleet_taxonomy.default_assignments_path() == validate_fleet_taxonomy.ASSIGNMENTS_PATH
    assert slot_routing._default_assignments_path() == slot_routing.DEFAULT_ASSIGNMENTS_PATH
    assert stream_inventory.resolve_streams_yaml(tmp_path) == stream_inventory.DEFAULT_STREAMS_YAML
    assert driver_lanes.default_data_path() == driver_lanes.DATA_FILE
    assert fleet_taxonomy.default_taxonomy_path() == fleet_taxonomy.DEFAULT_TAXONOMY_PATH
    assert ignored_task_output.default_succession_map_path() == ignored_task_output.SUCCESSION_MAP

    def _succession_digest(profile_file: Path) -> str:
        import hashlib

        return hashlib.sha256((profile_file.parent / "terminal_abandon_succession.yaml").read_bytes()).hexdigest()

    profile_a = _write_consumer_profile("alpha", "alpha-org/alpha-repo", "alpha", 111, "claude-alpha", "alpha-driver", "https")
    profile_b = _write_consumer_profile("beta", "beta-org/beta-repo", "beta", 222, "claude-beta", "beta-driver", "ssh")
    product_dir = tmp_path / "product"
    product_dir.mkdir()
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product_dir))

    try:
        # Activate Profile A AFTER import -> call-time resolution picks Profile A's registries.
        monkeypatch.setenv(rr.PROFILE_PATH_ENV, str(profile_a))
        assert git_credentials.default_config_path() == profile_a.parent / "fleet_repos.yaml"
        _, repo_a = dispatch_git.resolve_repo()
        assert repo_a["key"] == "alpha"
        assert repo_a["github"] == "alpha-org/alpha-repo"
        assert sibling_git._registry_transport("alpha") == "https"
        assert issue_stream_audit.load_registry() == {"alpha-stream": [111]}
        assert hramatka_scope_gate.load_stream_registry() == {"alpha-stream": [111]}
        assert stream_inventory.stream_map(tmp_path) == {"alpha-stream": [111]}
        assert "claude-alpha" in agent_identity.get_valid_agents()
        assert driver_agent_type.resolve_driver_agent_type("alpha-area") == "alpha-driver"
        assert slot_routing._default_assignments_path() == profile_a.parent / "area_assignments.yaml"
        assert validate_fleet_taxonomy.default_assignments_path() == profile_a.parent / "area_assignments.yaml"
        assert driver_lanes.default_data_path() == profile_a.parent / "driver_lanes.json"
        assert driver_lanes.load_registry().lanes
        assert fleet_taxonomy.resolve_area("alpha-lane").id == "alpha-area"
        assert driver_agent_type.resolve_driver_agent_type("alpha-lane") == "alpha-driver"
        assert ignored_task_output.succession_streams()[1] == _succession_digest(profile_a)

        # Switch to Profile B in the same process -> call-time resolution switches immediately.
        monkeypatch.setenv(rr.PROFILE_PATH_ENV, str(profile_b))
        assert git_credentials.default_config_path() == profile_b.parent / "fleet_repos.yaml"
        _, repo_b = dispatch_git.resolve_repo()
        assert repo_b["key"] == "beta"
        assert repo_b["github"] == "beta-org/beta-repo"
        assert sibling_git._registry_transport("beta") == "ssh"
        assert issue_stream_audit.load_registry() == {"beta-stream": [222]}
        assert hramatka_scope_gate.load_stream_registry() == {"beta-stream": [222]}
        assert stream_inventory.stream_map(tmp_path) == {"beta-stream": [222]}
        assert "claude-beta" in agent_identity.get_valid_agents()
        assert "claude-alpha" not in agent_identity.get_valid_agents()
        assert driver_agent_type.resolve_driver_agent_type("beta-area") == "beta-driver"
        assert driver_lanes.default_data_path() == profile_b.parent / "driver_lanes.json"
        assert fleet_taxonomy.resolve_area("beta-lane").id == "beta-area"
        with pytest.raises(fleet_taxonomy.UnknownAreaError):
            fleet_taxonomy.resolve_area("alpha-lane")
        assert driver_agent_type.resolve_driver_agent_type("beta-lane") == "beta-driver"
        assert ignored_task_output.succession_streams()[1] == _succession_digest(profile_b)

        # A declared lane or taxonomy file that is missing fails closed with each reader's own typed
        # error; the Fleet Comms roster reports its lane part unavailable instead of raising.
        from lu_machinery.fleet_comms import authority

        kept = {name: (profile_b.parent / name).read_bytes() for name in ("driver_lanes.json", "fleet_taxonomy.yaml")}
        for name in kept:
            (profile_b.parent / name).unlink()
        with pytest.raises(driver_lanes.LaneConfigError, match="driver_lanes"):
            driver_lanes.load_registry()
        with pytest.raises(fleet_taxonomy.FleetTaxonomyError, match="fleet_taxonomy"):
            fleet_taxonomy.resolve_area("beta-lane")
        authority._ROSTER_CACHE.clear()
        roster, complete = authority._load_roster()
        assert complete is False
        assert authority.DURABLE_CTO_RECIPIENT in roster
        for name, content in kept.items():
            (profile_b.parent / name).write_bytes(content)

        # Broken declared target in active profile fails closed rather than falling back to packaged defaults.
        (profile_b.parent / "issue_streams.yaml").unlink()
        with pytest.raises(profile.ProfileError, match="issue_streams"):
            issue_stream_audit.load_registry()
        with pytest.raises(profile.ProfileError, match="issue_streams"):
            stream_inventory.resolve_streams_yaml(tmp_path)

        # F6: explicit streams_yaml still works even when the unused profile default is missing,
        # and reports the explicit file as authority.
        explicit_streams = tmp_path / "explicit_streams.yaml"
        explicit_streams.write_text("streams:\n  explicit:\n    title: Explicit\n    epics: [123]\n", encoding="utf-8")
        snap = stream_inventory.inventory_snapshot(tmp_path, streams_yaml=explicit_streams)
        assert snap["epics"] == [123]
        assert snap["authority"] == str(explicit_streams)

        # F2: sibling_git catalog loader binds to the resolved profile registry path.
        assert "alpha" in sibling_git._load_catalog(profile_a.parent / "fleet_repos.yaml")

        # F5: observability rejects a fresh membership report belonging to a different profile.
        from lu_machinery.research import observability

        stale_profile_report = {"repository": "alpha-org/alpha-repo", "streams": {"alpha-stream": [111]}}
        assert not observability._membership_report_matches_active_profile(
            stale_profile_report, profile_a.parent / "issue_streams.yaml"
        )
        matching_report = {"repository": "beta-org/beta-repo", "streams": {"alpha-stream": [111]}}
        assert observability._membership_report_matches_active_profile(
            matching_report, profile_a.parent / "issue_streams.yaml"
        )

        # F1: malformed external_recipients marks the Fleet Comms roster incomplete.
        from lu_machinery.api import fleet_router
        from lu_machinery.fleet_comms import authority

        (profile_b.parent / "area_assignments.yaml").write_text(
            "schema_version: 1\nexternal_recipients: eggbot\nassignments:\n  beta-area:\n    slots: [claude-beta]\n    driver_agent_type: 7\n",
            encoding="utf-8",
        )
        authority._ROSTER_CACHE.clear()
        _, complete = authority._load_roster()
        assert complete is False

        # F3: non-string driver_agent_type raises DriverAgentConfigError and exits 2 in CLI.
        with pytest.raises(driver_agent_type.DriverAgentConfigError, match="driver_agent_type"):
            driver_agent_type.resolve_driver_agent_type("beta-area")
        assert driver_agent_type.main(["--lane", "beta-area"]) == 2

        # F7: mixed-type slots list raises SlotAssignmentsConfigError instead of falling back to suffix inference.
        (profile_b.parent / "area_assignments.yaml").write_text(
            "schema_version: 1\nassignments:\n  beta-area:\n    slots: [claude-beta, 7]\n",
            encoding="utf-8",
        )
        with pytest.raises(slot_routing.SlotAssignmentsConfigError, match="slots"):
            slot_routing._find_area_for_slot("claude-beta", profile_b.parent / "area_assignments.yaml")

        # F4: explicit visibility: private with role: null classifies as private in dispatcher checks.
        from lu_machinery.dispatcher import checks as dispatch_checks

        assert dispatch_checks._is_private_repo({"visibility": "private", "role": None}) is True
        assert dispatch_checks._is_private_repo({"visibility": "public", "role": None}) is False

        # F4: malformed assignments mapping raises ValueError in stalled_work.owner_lanes().
        from lu_machinery.orchestration import stalled_work

        (profile_b.parent / "area_assignments.yaml").write_text("schema_version: 1\nassignments: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="assignments"):
            stalled_work.owner_lanes()

        # F3: missing declared area_assignments yields distinct CLI exit 2 and degraded Monitor roster.
        (profile_b.parent / "area_assignments.yaml").unlink()
        assert driver_agent_type.main(["--lane", "beta-area"]) == 2
        _, _, external_roster = fleet_router._fleet_seat_roster()
        assert external_roster == "unavailable"

        # F2 (PR 1476 r1): YAML constructor failures (e.g. !!int "") in profile succession map raise ValueError.
        (profile_b.parent / "terminal_abandon_succession.yaml").write_text(
            'schema: terminal-abandon-succession-map.v1\nstreams:\n  "epic:1":\n    initiators: [!!int ""]\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="succession map unreadable"):
            ignored_task_output.succession_streams()
    finally:
        git_credentials.repository_registry.cache_clear()


def test_validate_fleet_taxonomy_refuses_deeply_nested_acyclic_aliases_without_recursion_error(tmp_path, capsys):
    """Defect class (F8): deeply nested acyclic YAML values in uniqueItems arrays crash schema validation with RecursionError."""
    from lu_machinery.orchestration import fleet_taxonomy, validate_fleet_taxonomy

    valid_assignments = tmp_path / "area_assignments.yaml"
    valid_assignments.write_text(
        "schema_version: 1\nassignments:\n  atlas:\n    driver_agent_type: atlas-driver\n    slots: [claude-atlas]\n",
        encoding="utf-8",
    )
    deep_a = "[" * 365 + "1" + "]" * 365
    deep_b = "[" * 365 + "2" + "]" * 365
    deep_taxonomy = tmp_path / "fleet_taxonomy.yaml"
    deep_taxonomy.write_text(
        f"schema_version: 1\nareas:\n  atlas:\n    id: atlas\n    aliases: [{deep_a}, {deep_b}]\n    epics: []\n",
        encoding="utf-8",
    )

    with pytest.raises(validate_fleet_taxonomy.FleetTaxonomyValidationError, match="recursion depth"):
        validate_fleet_taxonomy.validate_fleet_taxonomy(
            taxonomy_path=deep_taxonomy,
            assignments_path=valid_assignments,
        )
    assert (
        validate_fleet_taxonomy.main(
            ["--taxonomy", str(deep_taxonomy), "--assignments", str(valid_assignments), "--json"]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "recursion depth" in payload["error"]

    # Runtime taxonomy loader shares the same typed refusal.
    with pytest.raises(fleet_taxonomy.FleetTaxonomyError, match="recursion depth"):
        fleet_taxonomy.load_fleet_taxonomy(deep_taxonomy)

    # Sibling: deeply nested external_recipients in area_assignments.yaml also returns a typed refusal.
    valid_taxonomy = tmp_path / "valid_taxonomy.yaml"
    valid_taxonomy.write_text(
        "schema_version: 1\nareas:\n  atlas:\n    id: atlas\n    aliases: []\n    epics: []\n",
        encoding="utf-8",
    )
    deep_assignments = tmp_path / "deep_assignments.yaml"
    deep_assignments.write_text(
        "schema_version: 1\n"
        f"external_recipients: [{deep_a}, {deep_b}]\n"
        "assignments:\n  atlas:\n    driver_agent_type: atlas-driver\n    slots: [claude-atlas]\n",
        encoding="utf-8",
    )
    with pytest.raises(validate_fleet_taxonomy.FleetTaxonomyValidationError, match="recursion depth"):
        validate_fleet_taxonomy.validate_fleet_taxonomy(
            taxonomy_path=valid_taxonomy,
            assignments_path=deep_assignments,
        )

    # Shared acyclic anchors stay valid.
    shared_taxonomy = tmp_path / "shared_taxonomy.yaml"
    shared_taxonomy.write_text(
        "schema_version: 1\nareas:\n  atlas:\n    id: atlas\n    aliases: []\n    epics: &ep []\n"
        "  other:\n    id: other\n    aliases: []\n    epics: *ep\n",
        encoding="utf-8",
    )
    shared_assignments = tmp_path / "shared_assignments.yaml"
    shared_assignments.write_text(
        "schema_version: 1\nassignments:\n"
        "  atlas:\n    driver_agent_type: atlas-driver\n    slots: [claude-atlas]\n"
        "  other:\n    driver_agent_type: other-driver\n    slots: [claude-other]\n",
        encoding="utf-8",
    )
    assert (
        validate_fleet_taxonomy.validate_fleet_taxonomy(
            taxonomy_path=shared_taxonomy,
            assignments_path=shared_assignments,
        )["ok"]
        is True
    )

    # Valid YAML merge overrides (<<: *defaults with explicit id override) remain valid.
    merge_taxonomy = tmp_path / "merge_taxonomy.yaml"
    merge_taxonomy.write_text(
        "schema_version: 1\nareas:\n"
        "  atlas: &defaults\n    id: atlas\n    aliases: [atlas-alias]\n    epics: []\n"
        "  other:\n    <<: *defaults\n    id: other\n    aliases: [other-alias]\n",
        encoding="utf-8",
    )
    assert (
        validate_fleet_taxonomy.validate_fleet_taxonomy(
            taxonomy_path=merge_taxonomy,
            assignments_path=shared_assignments,
        )["ok"]
        is True
    )
    assert fleet_taxonomy.load_fleet_taxonomy(merge_taxonomy).areas["other"].id == "other"

    # Nonregular paths (e.g. a named pipe with no writer) fail closed immediately without blocking.
    fifo = tmp_path / "taxonomy.fifo"
    os.mkfifo(fifo)
    assert (
        validate_fleet_taxonomy.main(
            ["--taxonomy", str(fifo), "--assignments", str(valid_assignments), "--json"]
        )
        == 1
    )
    fifo_payload = json.loads(capsys.readouterr().out)
    assert fifo_payload["ok"] is False
    assert "Not a regular file" in fifo_payload["error"]
    with pytest.raises(fleet_taxonomy.FleetTaxonomyError, match="Not a regular file"):
        fleet_taxonomy.load_fleet_taxonomy(fifo)


def test_a_key_written_twice_in_one_config_mapping_merge_key_included_is_refused_by_every_fleet_config_loader(tmp_path):
    """Defect class: a config key written twice in one source mapping is silently last-wins (R2-YAML-MERGE).

    One rule for the taxonomy, assignments, succession map and lane data loaders: a key written twice
    is refused, the YAML merge key ``<<`` counts like any other key, and merging (``<<: *a``,
    ``<<: [*a, *b]``) with explicit overrides stays valid.
    """
    from lu_machinery.fleet import ignored_task_output
    from lu_machinery.ops import driver_lanes
    from lu_machinery.orchestration import fleet_taxonomy, validate_fleet_taxonomy

    assignments = tmp_path / "area_assignments.yaml"
    assignments.write_text(
        "schema_version: 1\nassignments:\n"
        "  atlas:\n    driver_agent_type: atlas-driver\n    slots: [claude-atlas]\n"
        "  other:\n    driver_agent_type: other-driver\n    slots: [claude-other]\n",
        encoding="utf-8",
    )
    # Valid in every other respect, so the second ``<<`` is the only reason to refuse.
    twice = tmp_path / "twice.yaml"
    twice.write_text(
        "schema_version: 1\nareas:\n"
        "  atlas: &a\n    id: atlas\n    aliases: [atlas-alias]\n    epics: []\n"
        "  other:\n    <<: *a\n    <<: *a\n    id: other\n    aliases: [other-alias]\n",
        encoding="utf-8",
    )
    with pytest.raises(validate_fleet_taxonomy.FleetTaxonomyValidationError, match="duplicate YAML key: '<<'"):
        validate_fleet_taxonomy.validate_fleet_taxonomy(taxonomy_path=twice, assignments_path=assignments)
    with pytest.raises(fleet_taxonomy.FleetTaxonomyError, match="duplicate YAML key: '<<'"):
        fleet_taxonomy.load_fleet_taxonomy(twice)
    twice_assignments = tmp_path / "twice_assignments.yaml"
    twice_assignments.write_text(
        "schema_version: 1\nassignments:\n"
        "  atlas: &x\n    driver_agent_type: atlas-driver\n    slots: [claude-atlas]\n"
        "  other:\n    <<: *x\n    <<: *x\n    driver_agent_type: other-driver\n",
        encoding="utf-8",
    )
    with pytest.raises(validate_fleet_taxonomy.FleetTaxonomyValidationError, match="duplicate YAML key: '<<'"):
        validate_fleet_taxonomy.load_validated_assignments(twice_assignments)

    # One merge key naming several mappings, with explicit overrides, is still valid.
    merged = tmp_path / "merged.yaml"
    merged.write_text(
        "schema_version: 1\nareas:\n"
        "  atlas: &a\n    id: atlas\n    aliases: [atlas-alias]\n    epics: []\n"
        "  other:\n    <<: [*a]\n    id: other\n    aliases: [other-alias]\n",
        encoding="utf-8",
    )
    assert validate_fleet_taxonomy.validate_fleet_taxonomy(taxonomy_path=merged, assignments_path=assignments)["ok"]
    assert fleet_taxonomy.load_fleet_taxonomy(merged).alias_to_area["other-alias"].id == "other"

    # The succession map follows the same rule: an override through one merge key is valid ...
    header = 'schema: terminal-abandon-succession-map.v1\nstreams:\n  "epic:1": &one\n    initiators: [a-x]\n'
    succession = tmp_path / "succession.yaml"
    succession.write_text(header + '  "epic:2":\n    <<: *one\n    initiators: [b-x]\n', encoding="utf-8")
    assert ignored_task_output.succession_streams(succession)[0] == {"a-x": "epic:1", "b-x": "epic:2"}
    # ... and a second merge key in one mapping is refused, never last-wins.
    succession.write_text(
        header + '  "epic:2": &two\n    initiators: [b-x]\n  "epic:3":\n    <<: *one\n    <<: *two\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="succession map unreadable"):
        ignored_task_output.succession_streams(succession)

    # Lane data is JSON: a member name written twice is refused too.
    lanes = tmp_path / "driver_lanes.json"
    packaged = driver_lanes._PACKAGED_DATA_FILE.read_text(encoding="utf-8")
    assert packaged.lstrip().startswith("{")
    lanes.write_text(packaged.replace("{", '{"lanes": [], ', 1), encoding="utf-8")
    with pytest.raises(driver_lanes.LaneConfigError, match="duplicate JSON key: 'lanes'"):
        driver_lanes.load_registry(lanes)
    lanes.write_text(packaged, encoding="utf-8")
    assert driver_lanes.load_registry(lanes).lanes


def test_a_non_regular_config_file_is_refused_without_a_blocking_open_by_every_touched_reader(tmp_path, monkeypatch):
    """Defect class: a config read that opens a FIFO or other non-regular file blocks (R2-NONREGULAR-PATH).

    Every config read this change adds or touches goes through ``read_regular_file_bytes``, which
    opens without blocking and checks the opened descriptor, so a file that is regular when
    inspected and a FIFO when opened is refused too. Each caller keeps its own typed refusal.
    """
    import signal

    from lu_machinery.fleet import ignored_task_output
    from lu_machinery.ops import driver_lanes
    from lu_machinery.orchestration import fleet_taxonomy, validate_fleet_taxonomy

    fifo = tmp_path / "config.fifo"
    os.mkfifo(fifo)
    regular = tmp_path / "regular.yaml"
    regular.write_text("schema_version: 1\n", encoding="utf-8")

    def _hung(signum, frame):
        raise AssertionError("a config read blocked on a FIFO")

    class _InspectsRegular:
        """``os`` whose ``stat`` reports the regular file: the FIFO appears only at open."""

        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def stat(path, *args, **kwargs):
            return os.stat(regular)

    previous = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(20)
    try:
        for swapped in (False, True):
            if swapped:  # regular at inspection, a FIFO at open: only the descriptor check can refuse
                monkeypatch.setattr(validate_fleet_taxonomy, "os", _InspectsRegular())
            with pytest.raises(ValueError, match="succession map unreadable"):
                ignored_task_output.succession_streams(fifo)
            with pytest.raises(driver_lanes.LaneConfigError, match="Not a regular file"):
                driver_lanes.load_registry(fifo)
            with pytest.raises(fleet_taxonomy.FleetTaxonomyError, match="Not a regular file"):
                fleet_taxonomy.load_fleet_taxonomy(fifo)
            with pytest.raises(validate_fleet_taxonomy.FleetTaxonomyValidationError, match="Not a regular file"):
                validate_fleet_taxonomy.load_validated_assignments(fifo)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_a_profile_refusal_on_a_default_config_path_is_each_callers_own_typed_refusal(tmp_path, monkeypatch):
    """Defect class: a default config path resolved outside the caller's error translation (PR1479-F1).

    A refused profile entry (a FIFO, a symlink, a bad profile) must surface as the same typed refusal
    the caller gives for an explicit path, never as a bare ``ProfileError`` or an exception from a
    fail-open reader.
    """
    from lu_machinery.fleet import ignored_task_output
    from lu_machinery.fleet_comms import authority
    from lu_machinery.ops import driver_lanes
    from lu_machinery.orchestration import driver_agent_type, fleet_taxonomy, validate_fleet_taxonomy

    def _refused(name, **_kwargs):
        raise profile.ProfileError(f"consumer config {name!r} is not a regular file")

    monkeypatch.setattr(profile, "config_path", _refused)
    assignments = tmp_path / "area_assignments.yaml"
    assignments.write_text(
        "schema_version: 1\nassignments:\n  atlas:\n    driver_agent_type: atlas-driver\n    slots: [claude-atlas]\n",
        encoding="utf-8",
    )

    for kwargs in ({}, {"assignments_path": assignments}):  # the second reaches the taxonomy default
        with pytest.raises(driver_agent_type.DriverAgentConfigError, match="not a regular file"):
            driver_agent_type.resolve_driver_agent_type("atlas-alias", **kwargs)
    assert driver_agent_type.main(["--lane", "atlas-alias"]) == 2
    with pytest.raises(validate_fleet_taxonomy.FleetTaxonomyValidationError, match="not a regular file"):
        validate_fleet_taxonomy.load_validated_assignments()
    with pytest.raises(validate_fleet_taxonomy.FleetTaxonomyValidationError, match="not a regular file"):
        validate_fleet_taxonomy.validate_fleet_taxonomy()
    with pytest.raises(fleet_taxonomy.FleetTaxonomyError, match="not a regular file"):
        fleet_taxonomy.load_fleet_taxonomy()
    with pytest.raises(ValueError, match="succession map unreadable"):
        ignored_task_output.succession_streams()
    with pytest.raises(driver_lanes.LaneConfigError, match="not a regular file"):
        driver_lanes.load_registry()
    # The recipient roster is fail-open by design: a refused source leaves it incomplete, never raises.
    monkeypatch.setattr(authority, "_ROSTER_CACHE", {})
    names, complete = authority._load_roster()
    assert complete is False
    assert authority.DURABLE_CTO_RECIPIENT in names
