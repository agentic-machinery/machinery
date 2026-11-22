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
    # A state root alone does not stand in for the primary anchor.
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(_primary_checkout(tmp_path / "primary")))
    with pytest.raises(rr.StateRootError, match=rr.PRIMARY_ROOT_ENV):
        rr.state_root()


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
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(tmp_path / "decoy"))
    with pytest.raises(rr.MachineryConfigError, match="state cutover"):
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
        with pytest.raises(rr.MachineryConfigError, match=f"^{name} .* must be the primary checkout"):
            rr.state_root(env={**anchored, name: str(other)})
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
