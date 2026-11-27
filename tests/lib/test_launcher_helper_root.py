"""Launcher helpers never run from a worktree-controlled root (#9121).

``launcher_resolve_roots`` sets ``LC_DURABLE_HELPER_ROOT``, the checkout whose
``.venv/bin/python`` and helper scripts every launcher helper site executes. It
used to come from ``git rev-parse --git-common-dir``, so a rewritten ``.git``
gitfile (or its ``commondir``) or an inherited ``GIT_DIR`` picked the root.

Every test builds real temporary repositories: an honest primary with a linked
worktree, plus an attacker repository. Each ``.venv/bin/python`` is a stub that
logs ``<owner> <argv>`` and runs nothing else, so the log shows which
interpreter every site executed; ``EVIL`` or ``WORKTREE`` lines mean a planted
executable ran.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path, machinery_root
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.host_context.conftest import HostLayout
from tests.host_context.conftest import host_layout as host_layout
from tests.launcher_sandbox import launcher_environment, sandbox_wrapper_text

REPO = Path(__file__).resolve().parents[2]
# Root start-*.sh wrappers are product files. Read the owned fixture, never
# product_checkout(), which follows the caller's LU_PRODUCT_ROOT.
_PRODUCT_TREE = REPO / "tests" / "fixtures" / "product_tree"

pytestmark = [pytest.mark.usefixtures("configured_roots"), pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available"), pytest.mark.usefixtures("hermetic_monitor")]

_COPIED = (
    "start-claude-driver.sh",
    "lu_machinery/lib/launcher_core.sh",
    "lu_machinery/lib/project_interpreter.sh",
    "lu_machinery/lib/handoff_identity.sh",
    "lu_machinery/lib/session_supervisor.sh",
    "lu_machinery/lib/profile_resolver.sh",
    "lu_machinery/launchers/claude.sh",
    "lu_machinery/launchers/codex.sh",
    "lu_machinery/config/issue_streams.yaml",
    "lu_machinery/config/launcher_stream_aliases.tsv",
)
# Helper scripts the sites name by path; the stub interpreters never run them.
_PLACEHOLDERS = (
    "lu_machinery/orchestration/thread_handoff.py",
    "lu_machinery/lib/context_profiles.py",
)
_SITES = (
    "renew_heartbeat",
    "lease_close",
    "driver_agent_type",
    "slot_registry",
    "claude_profile",
)
_EXPECTED_HONEST = {
    "renew_heartbeat": "lu_machinery.session_supervisor heartbeat",
    "lease_close": "lu_machinery.session_supervisor close",
    "driver_agent_type": "lu_machinery.orchestration.driver_agent_type",
    "slot_registry": "lu_machinery.orchestration.handoff_slot_registry",
    "claude_profile": "lu_machinery/lib/context_profiles.py",
}


def _tracked_source(relative: str) -> Path:
    """The file ``relative`` names: a product wrapper in the fixture tree, else this package."""
    if "/" not in relative:
        return _PRODUCT_TREE / relative
    return machinery_root().parent / relative


def _clean_env(**extra: str) -> dict[str, str]:
    env = launcher_environment()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _stub_python(root: Path, owner: str, log: Path) -> None:
    """A ``.venv/bin/python`` that logs its argv and runs nothing else."""
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\0' \"$@\" >> {str(log) + '.raw'!r}\n"
        'if [[ "${1:-}:${2:-}" == -I:-c && "${3:-}" == *"module bootstrap:"* ]]; then shift 5; fi\n'
        f"printf '%s %s\\n' {owner} \"$*\" >> {str(log)!r}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    python.chmod(0o755)


def _place_helper_scripts(root: Path) -> None:
    for relative in _PLACEHOLDERS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise SystemExit('stub interpreters never run this')\n", encoding="utf-8")


@dataclass
class Layout:
    tmp: Path
    primary: Path
    worktree: Path
    attacker: Path
    log: Path
    binding: HostLayout

    def environment(self, **extra: str) -> dict[str, str]:
        env = _clean_env()
        for name in ("FLEET_COMMS_ROOT", "LU_TASKS_DIR", "LU_GITHUB_CACHE_DIR"):
            env.pop(name, None)
        return {**env, **self.binding.env, "LU_PYTHON": str(self.binding.python), **extra}

    def lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def owners(self) -> set[str]:
        return {line.split(" ", 1)[0] for line in self.lines()}


@pytest.fixture()
def layout(tmp_path: Path, host_layout: HostLayout) -> Layout:
    tmp = tmp_path.resolve()
    log = tmp / "executions.log"
    primary = tmp / "primary"
    primary.mkdir()
    for relative in _COPIED:
        destination = primary / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_tracked_source(relative), destination)
        if relative.startswith("start-"):
            destination.write_text(sandbox_wrapper_text(destination.read_text()))
    wrapper = primary / "start-claude-driver.sh"
    wrapper.write_text(wrapper.read_text().replace('export LU_PRODUCT_ROOT="$ROOT"\n', ""))
    _place_helper_scripts(primary)
    (primary / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(primary, "init", "-q", "-b", "main")
    _git(primary, "remote", "add", "origin", "https://github.com/agentic-machinery/machinery.git")
    host_layout.data["repos"] = {"machinery": str(primary)}
    host_layout.data["products"]["public"]["repo"] = "machinery"
    host_layout.data["monitor_url"] = _clean_env()["LU_MONITOR_LOOPBACK"]
    host_layout.write()
    _git(primary, "add", ".")
    _git(primary, "commit", "-q", "-m", "init")
    worktree = tmp / "worktree"
    _git(primary, "worktree", "add", "-q", "-b", "feature", str(worktree))
    _stub_python(primary, "HONEST", log)

    attacker = tmp / "attacker"
    attacker.mkdir()
    _place_helper_scripts(attacker)
    _git(attacker, "init", "-q", "-b", "main")
    _git(attacker, "add", ".")
    _git(attacker, "commit", "-q", "-m", "attacker")
    _stub_python(attacker, "EVIL", log)
    return Layout(tmp, primary, worktree, attacker, log, host_layout)


def _hostile_env(layout: Layout) -> dict[str, str]:
    return {
        "GIT_DIR": f"{layout.attacker}/.git",
        "GIT_COMMON_DIR": f"{layout.attacker}/.git",
        "GIT_WORK_TREE": str(layout.attacker),
    }


_SITE_HARNESS = r"""
LC_ROOT="$1"
source "$LC_ROOT/lu_machinery/lib/launcher_core.sh"
launcher_resolve_roots
printf 'resolved=%s\n' "$LC_DURABLE_HELPER_ROOT"
printf 'hook_primary=%s\n' "$LU_PRIMARY_REPO_ROOT"
printf 'bound_python=%s\n' "$(launcher_project_python)"
source "$LC_ROOT/lu_machinery/lib/handoff_identity.sh"
# Only the helper calls below are observed through the fixture recorder. The
# root loader and interpreter selection above ran against the real binding.
project_python_run() {
  [ "$1" = "$LU_PYTHON" ] || return 97
  shift
  project_python_command "$FIXTURE_HELPER_RECORDER" || return
  "${PROJECT_PYTHON_COMMAND[@]}" "$@"
}
site() { printf 'SITE %s\n' "$1" >> "$SITE_LOG"; }

site renew_heartbeat
(
  LC_MODE=driver LC_DRIVER_LEASE_CLAIMED=1 LC_DRY_RUN=0
  SESSION_STREAM_RENEW_INTERVAL_SECONDS=1 SESSION_STREAM_RENEW_JITTER_SECONDS=0
  sleep 1.6 & child=$!
  launcher_driver_renew_loop "$child"
  wait "$child"
  launcher_stop_driver_renew
) >/dev/null 2>&1
site lease_close
( LC_PROVIDER=claude LC_DRIVER_CLOSE_RETRY_SECONDS=0; launcher_close_driver_lease ) >/dev/null 2>&1
site driver_agent_type
( LC_PROVIDER=claude LC_EPIC=devops LC_DRY_RUN=0 LC_FORWARD_ARGS=(); launcher_inject_driver_agent ) >/dev/null 2>&1
site slot_registry
(
  [ "$(launcher_project_python)" = "$LU_PYTHON" ] || exit 97
  launcher_project_python() { printf '%s\n' "$FIXTURE_HELPER_RECORDER"; }
  _handoff_slot_registry --slot claude-devops
) >/dev/null 2>&1
site claude_profile
(
  [ "$(launcher_project_python)" = "$LU_PYTHON" ] || exit 97
  launcher_project_python() { printf '%s\n' "$FIXTURE_HELPER_RECORDER"; }
  LC_MODEL=claude-opus-5-5
  source "$LC_ROOT/lu_machinery/launchers/claude.sh"
  launcher_adapter_preflight
) >/dev/null 2>&1
exit 0
"""


def _run_sites(layout: Layout, root: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _SITE_HARNESS, "bash", str(root or layout.worktree)],
        cwd=layout.tmp,
        env=layout.environment(
            HOME=str(layout.tmp / "home"), SITE_LOG=str(layout.log),
            FIXTURE_HELPER_RECORDER=str(layout.primary / ".venv/bin/python"), **env,
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _site_executions(layout: Layout) -> dict[str, list[str]]:
    executions: dict[str, list[str]] = {}
    current = ""
    for line in layout.lines():
        if line.startswith("SITE "):
            current = line.split(" ", 1)[1]
            executions[current] = []
        else:
            executions.setdefault(current, []).append(line)
    return executions


def _assert_every_site_ran_the_primary(
    layout: Layout, result: subprocess.CompletedProcess[str], sites: tuple[str, ...] = _SITES
) -> None:
    assert result.returncode == 0, result.stderr
    assert f"resolved={layout.primary}" in result.stdout
    executions = _site_executions(layout)
    assert f"bound_python={layout.binding.python}" in result.stdout
    assert "" not in executions, executions
    assert list(executions) == list(_SITES), executions
    for name in sites:
        ran = executions[name]
        assert ran, f"site {name} executed no interpreter"
        assert all(line.startswith("HONEST ") for line in ran), (name, ran)
        assert any(_EXPECTED_HONEST[name] in line for line in ran), (name, ran)
    for line in layout.lines():
        assert str(layout.attacker) not in line, line
        assert str(layout.worktree / ".venv") not in line, line
    # The validated primary interpreter invokes the installed module (#161).
    raw = Path(str(layout.log) + ".raw").read_bytes().decode().split("\0")
    roots = [raw[index + 3:index + 5] for index, arg in enumerate(raw) if arg == "-I" and raw[index + 1] == "-c" and "module bootstrap:" in raw[index + 2]]
    assert roots and all(
        Path(root).resolve() in {Path(result.args[-1]).resolve(), layout.primary} and package == "lu_machinery"
        for root, package in roots
    ), roots


def _assert_refused_before_any_site(layout: Layout, result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "[invalid_invocation_git]" in result.stderr
    assert "resolved=" not in result.stdout
    assert layout.lines() == [], layout.lines()


# --- baseline: an honest worktree keeps using the primary ---------------------


def test_honest_worktree_every_site_runs_the_primary_interpreter(layout: Layout) -> None:
    _assert_every_site_ran_the_primary(layout, _run_sites(layout))


def test_worktree_reached_through_a_symlinked_path_still_resolves(layout: Layout) -> None:
    alias = layout.tmp / "alias"
    alias.symlink_to(layout.worktree)
    _assert_every_site_ran_the_primary(layout, _run_sites(layout, root=alias))


def test_relative_back_pointer_cannot_change_bound_repo_config(layout: Layout) -> None:
    """Git's ``worktree.useRelativePaths`` writes both pointers relative."""
    relative = layout.tmp / "relative"
    _git(layout.primary, "-c", "worktree.useRelativePaths=true", "worktree", "add", "-q", "-b", "rel", str(relative))
    assert (relative / ".git").read_text(encoding="utf-8").startswith("gitdir: ../")
    assert not (layout.primary / ".git" / "worktrees" / "relative" / "gitdir").read_text(
        encoding="utf-8"
    ).startswith("/")
    result = _run_sites(layout, root=relative)
    assert result.returncode == 3, result.stderr
    assert "[repo_origin_mismatch]" in result.stderr
    assert layout.lines() == []


# --- environment redirection --------------------------------------------------


def test_ambient_git_dir_does_not_redirect_any_site(layout: Layout) -> None:
    _assert_every_site_ran_the_primary(layout, _run_sites(layout, **_hostile_env(layout)))


def test_claude_launch_checks_both_legacy_root_assertions(layout: Layout) -> None:
    """A legacy assertion can neither redirect the host binding nor silently disagree."""
    agreed = _run_sites(layout, LC_PROVIDER="claude", LEARN_UKRAINIAN_PRIMARY_REPO_ROOT=str(layout.primary))
    _assert_every_site_ran_the_primary(layout, agreed)
    assert f"hook_primary={layout.primary}" in agreed.stdout
    for setting in ("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LEARN_UKRAINIAN_STATE_ROOT"):
        layout.log.write_text("")
        refused = _run_sites(layout, LC_PROVIDER="claude", **{setting: str(layout.attacker)})
        assert refused.returncode == 3
        assert f"[legacy_conflict] {setting}" in refused.stderr
        assert "resolved=" not in refused.stdout
        assert layout.lines() == []


def test_worktree_local_venv_never_runs(layout: Layout) -> None:
    """The driver-agent site used to try the worktree's own .venv first."""
    _stub_python(layout.worktree, "WORKTREE", layout.log)
    result = _run_sites(layout, **_hostile_env(layout))
    _assert_every_site_ran_the_primary(layout, result)
    assert "WORKTREE" not in layout.owners()


def test_resolution_clears_git_local_environment(layout: Layout) -> None:
    script = (
        'LC_ROOT="$1"; source "$LC_ROOT/lu_machinery/lib/launcher_core.sh"; launcher_resolve_roots; '
        'env | grep -E "^GIT_(DIR|COMMON_DIR|WORK_TREE|INDEX_FILE|OBJECT_DIRECTORY|CONFIG_PARAMETERS)=" || true'
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(layout.worktree)],
        env=layout.environment(**_hostile_env(layout), GIT_INDEX_FILE="/x", GIT_OBJECT_DIRECTORY="/x",
                       GIT_CONFIG_PARAMETERS="'core.fsmonitor=/x'"),
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# --- hostile gitfile ----------------------------------------------------------


def test_hostile_gitfile_with_commondir_is_refused(layout: Layout) -> None:
    admin = layout.tmp / "attacker-admin"
    shutil.copytree(layout.primary / ".git" / "worktrees" / "worktree", admin)
    (admin / "commondir").write_text(f"{layout.attacker}/.git\n", encoding="utf-8")
    (layout.worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    # git itself follows the commondir to the attacker: the old resolver's source.
    assert _git(layout.worktree, "rev-parse", "--path-format=absolute", "--git-common-dir") == (
        f"{layout.attacker}/.git"
    )
    _assert_refused_before_any_site(layout, _run_sites(layout))


def test_gitfile_borrowing_another_repositorys_worktree_entry_is_refused(layout: Layout) -> None:
    """A well-shaped ``<attacker>/.git/worktrees/<name>`` whose back-pointer names another checkout."""
    _git(layout.attacker, "worktree", "add", "-q", "-b", "decoy", str(layout.tmp / "decoy"))
    (layout.worktree / ".git").write_text(f"gitdir: {layout.attacker}/.git/worktrees/decoy\n", encoding="utf-8")
    result = _run_sites(layout)
    _assert_refused_before_any_site(layout, result)


def test_commondir_tampered_inside_the_real_admin_dir_is_refused(layout: Layout) -> None:
    """Git invocation metadata must agree with its primary."""
    (layout.primary / ".git" / "worktrees" / "worktree" / "commondir").write_text(
        f"{layout.attacker}/.git\n", encoding="utf-8"
    )
    _assert_refused_before_any_site(layout, _run_sites(layout))


def test_codex_refuses_a_canonical_checkout_git_found_through_commondir(layout: Layout) -> None:
    (layout.primary / ".git" / "worktrees" / "worktree" / "commondir").write_text(
        f"{layout.attacker}/.git\n", encoding="utf-8"
    )
    script = r"""
LC_ROOT="$1"; source "$LC_ROOT/lu_machinery/lib/launcher_core.sh"; launcher_resolve_roots
LC_DRY_RUN=0; source "$LC_ROOT/lu_machinery/launchers/codex.sh"
launcher_codex_resolve_canonical_root
printf 'canonical=%s\n' "$LC_CODEX_CANONICAL_ROOT"
"""
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(layout.worktree)],
        env=layout.environment(HOME=str(layout.tmp / "home")), capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "[invalid_invocation_git]" in result.stderr
    assert "canonical=" not in result.stdout


def test_codex_accepts_the_validated_primary_as_canonical_checkout(layout: Layout) -> None:
    script = r"""
LC_ROOT="$1"; source "$LC_ROOT/lu_machinery/lib/launcher_core.sh"; launcher_resolve_roots
LC_DRY_RUN=0; source "$LC_ROOT/lu_machinery/launchers/codex.sh"
launcher_codex_resolve_canonical_root
printf 'canonical=%s\n' "$LC_CODEX_CANONICAL_ROOT"
"""
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(layout.worktree)],
        env=layout.environment(HOME=str(layout.tmp / "home")), capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert f"canonical={layout.primary}" in result.stdout


# --- symlinked layouts --------------------------------------------------------


def test_symlinked_worktree_gitfile_is_refused(layout: Layout) -> None:
    real = layout.tmp / "gitfile"
    real.write_text(f"gitdir: {layout.primary}/.git/worktrees/worktree\n", encoding="utf-8")
    (layout.worktree / ".git").unlink()
    (layout.worktree / ".git").symlink_to(real)
    _assert_refused_before_any_site(layout, _run_sites(layout))


def test_gitfile_through_a_symlinked_primary_git_dir_is_refused(layout: Layout) -> None:
    fake = layout.tmp / "fake-primary"
    fake.mkdir()
    (fake / ".git").symlink_to(layout.primary / ".git")
    _stub_python(fake, "EVIL", layout.log)
    (layout.worktree / ".git").write_text(f"gitdir: {fake}/.git/worktrees/worktree\n", encoding="utf-8")
    _assert_refused_before_any_site(layout, _run_sites(layout))


def test_gitfile_through_a_symlinked_worktrees_dir_is_refused(layout: Layout) -> None:
    fake = layout.tmp / "fake-primary"
    (fake / ".git").mkdir(parents=True)
    (fake / ".git" / "worktrees").symlink_to(layout.primary / ".git" / "worktrees")
    _stub_python(fake, "EVIL", layout.log)
    (layout.worktree / ".git").write_text(f"gitdir: {fake}/.git/worktrees/worktree\n", encoding="utf-8")
    _assert_refused_before_any_site(layout, _run_sites(layout))


def test_symlinked_back_pointer_file_is_refused(layout: Layout) -> None:
    admin = layout.primary / ".git" / "worktrees" / "worktree"
    real = layout.tmp / "pointer"
    real.write_text((admin / "gitdir").read_text(encoding="utf-8"), encoding="utf-8")
    (admin / "gitdir").unlink()
    (admin / "gitdir").symlink_to(real)
    _assert_refused_before_any_site(layout, _run_sites(layout))


# --- forged fake primary ------------------------------------------------------


def test_fake_primary_without_a_back_pointer_is_refused(layout: Layout) -> None:
    fake = layout.tmp / "fake-primary"
    (fake / ".git" / "worktrees" / "worktree").mkdir(parents=True)
    _stub_python(fake, "EVIL", layout.log)
    (layout.worktree / ".git").write_text(f"gitdir: {fake}/.git/worktrees/worktree\n", encoding="utf-8")
    result = _run_sites(layout)
    _assert_refused_before_any_site(layout, result)


def test_fake_primary_pointing_back_at_another_worktree_is_refused(layout: Layout) -> None:
    fake = layout.tmp / "fake-primary"
    admin = fake / ".git" / "worktrees" / "worktree"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"{layout.tmp}/elsewhere/.git\n", encoding="utf-8")
    (layout.tmp / "elsewhere").mkdir()
    _stub_python(fake, "EVIL", layout.log)
    (layout.worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    _assert_refused_before_any_site(layout, _run_sites(layout))


def test_residual_fully_forged_primary_is_locally_indistinguishable(layout: Layout) -> None:
    """Documented #9121 residual (see lu_machinery/lib/project_interpreter.sh).

    Whoever can rewrite the gitfile can also forge the back-pointer, so a
    complete fake primary validates. This pins the known limit of the gitfile
    check; the operator-set override closes it on a Codex launch
    (``test_override_refuses_a_forged_primary_gitfile``).
    """
    fake = layout.tmp / "fake-primary"
    admin = fake / ".git" / "worktrees" / "worktree"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"{layout.worktree}/.git\n", encoding="utf-8")
    (layout.worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    script = 'source "$1/lu_machinery/lib/project_interpreter.sh"; project_primary_root_resolve "$1"'
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(layout.worktree)],
        env=_clean_env(), capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(fake)


@pytest.mark.parametrize("via", ["direct", "symlinked-path"])
def test_snapshot_uses_the_host_runtime_without_trusting_its_local_venv(layout: Layout, via: str) -> None:
    """A code snapshot without Git metadata cannot select the runtime or product."""
    _stub_python(layout.worktree, "SELF", layout.log)
    (layout.worktree / ".git").unlink()
    root = layout.worktree
    if via == "symlinked-path":
        root = layout.tmp / "alias"
        root.symlink_to(layout.worktree)
    _assert_every_site_ran_the_primary(layout, _run_sites(layout, root=root, **_hostile_env(layout)))


@pytest.mark.parametrize("shape", ["state-anchor", "no-anchor", "empty-dotgit"])
def test_product_root_without_git_never_exports_its_own_venv(layout: Layout, shape: str) -> None:
    """Defect class: interpreter laundering through the launcher (#304 round 3 F3, #308 round 1 F4).

    A product named by LU_PRODUCT_ROOT that has no ``.git`` (a product subdirectory),
    or only an empty ``.git`` directory, resolved as its own primary, so the launcher
    exported its planted ``.venv`` as ``LEARN_UKRAINIAN_PYTHON``, which every hook then
    trusts as the operator override. Only the launcher's own code tree may resolve to
    itself without ``.git`` (the #9121 snapshot rule above); any other such root takes
    the validated shared-state primary, else refuses, and a ``.git`` that is not a Git
    repository refuses.
    """
    sub = layout.primary / "sub"
    sub.mkdir()
    _stub_python(sub, "PLANTED", layout.log)
    if shape == "empty-dotgit":
        (sub / ".git").mkdir()
    script = (
        'LC_ROOT="$1"; source "$2/lu_machinery/lib/launcher_core.sh"; launcher_resolve_roots; '
        'printf "resolved=%s\\npython=%s\\n" "$LC_DURABLE_HELPER_ROOT" "${LU_PYTHON:-}"'
    )
    env = layout.environment(HOME=str(layout.tmp / "home"))
    if shape == "no-anchor":
        env.pop("LU_HOST_CONTEXT")
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(sub), str(layout.worktree)],
        cwd=layout.tmp, env=env, capture_output=True, text=True, check=False, timeout=60,
    )
    assert str(sub / ".venv") not in result.stdout, result.stdout
    assert "PLANTED" not in layout.owners()
    if shape == "state-anchor":
        assert result.returncode == 0, result.stderr
        assert f"resolved={layout.primary}\n" in result.stdout
        assert f"python={layout.binding.python}\n" in result.stdout
    elif shape == "no-anchor":
        assert result.returncode == 3, result.stderr
        assert "[missing_context]" in result.stderr
    else:
        assert result.returncode == 3, result.stderr
        assert "[invalid_invocation_git]" in result.stderr


# --- operator-set CODEX_CANONICAL_REPO_ROOT override --------------------------


def _separate_git_dir_layout(layout: Layout) -> Layout:
    """A ``git init --separate-git-dir`` primary: no ``<primary>/.git/worktrees`` tie."""
    primary = layout.tmp / "sgd"
    primary.mkdir()
    for relative in _COPIED:
        destination = primary / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_tracked_source(relative), destination)
        if relative.startswith("start-"):
            destination.write_text(sandbox_wrapper_text(destination.read_text()))
    _place_helper_scripts(primary)
    (primary / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(primary, "init", "-q", "-b", "main", "--separate-git-dir", str(layout.tmp / "sgd-git"))
    _git(primary, "add", ".")
    _git(primary, "commit", "-q", "-m", "init")
    worktree = layout.tmp / "sgd-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "feature", str(worktree))
    _stub_python(primary, "HONEST", layout.log)
    return Layout(layout.tmp, primary, worktree, layout.attacker, layout.log, layout.binding)


def _run_codex_sites(layout: Layout, override: Path | None, root: Path | None = None, **env: str):
    if override is not None:
        env["CODEX_CANONICAL_REPO_ROOT"] = str(override)
    return _run_sites(layout, root, LC_PROVIDER="codex", **env)


def _assert_override_refused(layout: Layout, result: subprocess.CompletedProcess[str], message: str) -> None:
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert message in result.stderr
    assert "resolved=" not in result.stdout
    assert layout.lines() == [], layout.lines()


def test_override_names_a_separate_git_dir_primary(layout: Layout) -> None:
    sgd = _separate_git_dir_layout(layout)
    refused = _run_codex_sites(sgd, None)
    _assert_refused_before_any_site(sgd, refused)

    _assert_refused_before_any_site(sgd, _run_codex_sites(sgd, sgd.primary))


def test_codex_adapter_uses_the_validated_override(layout: Layout) -> None:
    """Canonical checkout, driver transport probe and canary all use the named primary."""
    script = r"""
LC_ROOT="$1"; LC_PROVIDER=codex; source "$LC_ROOT/lu_machinery/lib/launcher_core.sh"; launcher_resolve_roots
LC_DRY_RUN=0; source "$LC_ROOT/lu_machinery/launchers/codex.sh"
launcher_codex_resolve_canonical_root
printf 'canonical=%s\n' "$LC_CODEX_CANONICAL_ROOT"
printf 'python=%s\n' "$(launcher_project_python)"
LC_DRY_RUN=1 LC_MODEL=gpt-6.1-sol launcher_codex_transport_probe
"""
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(layout.worktree)],
        env=layout.environment(HOME=str(layout.tmp / "home"), CODEX_CANONICAL_REPO_ROOT=str(layout.primary)),
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert f"canonical={layout.primary}" in result.stdout
    assert f"python={layout.binding.python}" in result.stdout
    assert f"would probe {layout.binding.python} -m lu_machinery.orchestration.codex_transport_health" in (
        result.stdout
    )


def test_override_to_the_real_primary_does_not_pass_a_hostile_gitfile(layout: Layout) -> None:
    admin = layout.tmp / "attacker-admin"
    shutil.copytree(layout.primary / ".git" / "worktrees" / "worktree", admin)
    (admin / "commondir").write_text(f"{layout.attacker}/.git\n", encoding="utf-8")
    (layout.worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    _assert_refused_before_any_site(layout, _run_codex_sites(layout, layout.primary))


def test_override_refuses_a_forged_primary_gitfile(layout: Layout) -> None:
    """The fully forged fake primary (residual above) does not survive the override."""
    fake = layout.tmp / "fake-primary"
    admin = fake / ".git" / "worktrees" / "worktree"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"{layout.worktree}/.git\n", encoding="utf-8")
    _stub_python(fake, "EVIL", layout.log)
    (layout.worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    _assert_refused_before_any_site(layout, _run_codex_sites(layout, layout.primary))


def test_override_naming_an_unrelated_repository_is_refused(layout: Layout) -> None:
    _assert_override_refused(
        layout, _run_codex_sites(layout, layout.attacker), "is not a checkout of this Git common directory"
    )


def test_override_naming_a_primary_off_main_is_refused(layout: Layout) -> None:
    _git(layout.primary, "checkout", "-q", "-b", "elsewhere")
    _assert_override_refused(layout, _run_codex_sites(layout, layout.primary), "canonical checkout must be on main")


def test_override_naming_a_linked_worktree_on_main_is_refused(layout: Layout) -> None:
    _git(layout.primary, "checkout", "-q", "--detach")
    on_main = layout.tmp / "on-main"
    _git(layout.primary, "worktree", "add", "-q", str(on_main), "main")
    _stub_python(on_main, "EVIL", layout.log)
    _assert_override_refused(layout, _run_codex_sites(layout, on_main), "is a linked worktree, not the primary")


@pytest.mark.parametrize("borrow", ["symlink", "gitfile"])
def test_override_naming_a_lookalike_of_the_primary_is_refused(layout: Layout, borrow: str) -> None:
    fake = layout.tmp / "fake-primary"
    fake.mkdir()
    if borrow == "symlink":
        (fake / ".git").symlink_to(layout.primary / ".git")
    else:
        (fake / ".git").write_text(f"gitdir: {layout.primary}/.git\n", encoding="utf-8")
    _stub_python(fake, "EVIL", layout.log)
    _assert_override_refused(layout, _run_codex_sites(layout, fake), "borrows another checkout's .git")


def test_override_copied_separate_git_dir_gitfile_residual(layout: Layout) -> None:
    """A separate Git directory and a copy of its pointer are both outside the binding."""
    sgd = _separate_git_dir_layout(layout)
    copy = layout.tmp / "sgd-copy"
    copy.mkdir()
    shutil.copy2(sgd.primary / ".git", copy / ".git")
    _place_helper_scripts(copy)
    _stub_python(copy, "COPY", layout.log)

    _assert_refused_before_any_site(sgd, _run_codex_sites(sgd, copy))
    _git(sgd.primary, "config", "core.worktree", str(sgd.primary))
    _assert_refused_before_any_site(sgd, _run_codex_sites(sgd, copy))


def test_override_is_read_only_on_codex_launches(layout: Layout) -> None:
    """Other providers resolve through the gitfile check even when the variable is inherited."""
    result = _run_sites(layout, LC_PROVIDER="claude", CODEX_CANONICAL_REPO_ROOT=str(layout.attacker))
    _assert_every_site_ran_the_primary(layout, result)


# --- real launcher entry point -------------------------------------------------


def _run_driver(layout: Layout, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(layout.worktree / "start-claude-driver.sh"), "--epic", "devops"],
        cwd=layout.worktree,
        env=layout.environment(HOME=str(layout.tmp / "home"), LAUNCHER_DRY_RUN="1", **env),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_real_driver_launcher_refuses_a_hostile_gitfile(layout: Layout) -> None:
    _git(layout.attacker, "worktree", "add", "-q", "-b", "decoy", str(layout.tmp / "decoy"))
    (layout.worktree / ".git").write_text(f"gitdir: {layout.attacker}/.git/worktrees/decoy\n", encoding="utf-8")
    result = _run_driver(layout)
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "[invalid_invocation_git]" in result.stderr
    assert layout.lines() == []


def test_session_supervisor_state_root_ignores_git_redirection(layout: Layout) -> None:
    script = 'source "$1/lu_machinery/lib/session_supervisor.sh"; project_host_context --directory "$1" && printf "%s\\n" "$LU_STATE_ROOT"'
    ambient = subprocess.run(
        ["bash", "-c", script, "bash", str(layout.worktree)],
        env=layout.environment(**_hostile_env(layout)), capture_output=True, text=True, check=False, timeout=30,
    )
    assert ambient.returncode == 0, ambient.stderr
    assert ambient.stdout.strip() == layout.binding.data["state_root"]

    _git(layout.attacker, "worktree", "add", "-q", "-b", "decoy", str(layout.tmp / "decoy"))
    (layout.worktree / ".git").write_text(f"gitdir: {layout.attacker}/.git/worktrees/decoy\n", encoding="utf-8")
    hostile = subprocess.run(
        ["bash", "-c", script, "bash", str(layout.worktree)],
        env=layout.environment(), capture_output=True, text=True, check=False, timeout=30,
    )
    assert hostile.returncode == 2
    assert "[invalid_invocation_git]" in hostile.stderr
    assert hostile.stdout == ""


@pytest.mark.parametrize("via", ["symlinked-checkout", "symlinked-lib-directory"])
def test_machinery_dir_is_the_physical_package_when_reached_through_a_symlink(tmp_path: Path, via: str) -> None:
    """Defect class: sourced helpers taken from a symlinked path instead of the physical package.

    A planted environment value is replaced, and the value is readonly once set.
    """
    real = (tmp_path / "real").resolve()
    (real / "lu_machinery" / "lib").mkdir(parents=True)
    for name in ("launcher_core.sh", "project_interpreter.sh"):
        shutil.copy2(machinery_path("lib/" + name), real / "lu_machinery/lib" / name)
    if via == "symlinked-checkout":
        (tmp_path / "alias").symlink_to(real)
    else:
        (tmp_path / "alias" / "lu_machinery").mkdir(parents=True)
        (tmp_path / "alias" / "lu_machinery" / "lib").symlink_to(real / "lu_machinery" / "lib")
    core = tmp_path / "alias" / "lu_machinery/lib/launcher_core.sh"
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; source "$1"; printf "%s\\n" "$LC_MACHINERY_DIR"; readonly -p | grep -c " LC_MACHINERY_DIR="',
            "bash",
            str(core),
        ],
        env=_clean_env(LC_MACHINERY_DIR="/planted"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(real / "lu_machinery"), "1"]


# --- the product root (LC_ROOT) of an installed core -------------------------

# Observe the real root resolution before a lease or provider runs.
_PRODUCT_ROOT_PROBE = r"""
source "$1"
launcher_resolve_roots
printf 'product-root=%s\n' "$LC_ROOT"
printf 'exported=%s\n' "$LU_PRODUCT_ROOT"
"""


def _primary_checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def _bind_product(binding: HostLayout, product: Path) -> dict[str, str]:
    _git(product, "remote", "add", "origin", "https://github.com/learn-ukrainian/learn-ukrainian.github.io.git")
    binding.data["repos"]["public"] = str(product)
    binding.data["monitor_url"] = _clean_env()["LU_MONITOR_LOOPBACK"]
    binding.write()
    return {**binding.env, "LU_PYTHON": str(binding.python)}


def _probe_product_root(tmp: Path, core: Path, **env: str) -> subprocess.CompletedProcess[str]:
    clean = _clean_env(HOME=str(tmp), **env)
    for name in ("FLEET_COMMS_ROOT", "LU_TASKS_DIR", "LU_GITHUB_CACHE_DIR"):
        clean.pop(name, None)
    return subprocess.run(
        ["bash", "-c", _PRODUCT_ROOT_PROBE, "bash", str(core)],
        cwd=tmp,
        env=clean,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("product", ["checkout", "unset", "relative"])
def test_installed_core_takes_the_product_root_from_lu_product_root(tmp_path: Path, host_layout: HostLayout, product: str) -> None:
    """The installed core obtains its product from the binding and checks legacy assertions."""
    tmp = tmp_path.resolve()
    site = tmp / "venv" / "lib" / "python3" / "site-packages"
    shutil.copytree(machinery_root(), site / "lu_machinery", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    checkout = _primary_checkout(tmp / "product")
    env = _bind_product(host_layout, checkout)
    if product == "checkout":
        env["LU_PRODUCT_ROOT"] = str(checkout)
    elif product == "relative":
        env["LU_PRODUCT_ROOT"] = "product"

    result = _probe_product_root(tmp, site / "lu_machinery/lib/launcher_core.sh", **env)

    if product != "relative":
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [f"product-root={checkout}", f"exported={checkout}"]
        return
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "LU_PRODUCT_ROOT" in result.stderr
    assert "product-root=" not in result.stdout


def test_in_tree_core_keeps_its_own_checkout_as_the_product_root(tmp_path: Path, host_layout: HostLayout) -> None:
    """An in-tree installation refuses a conflicting product assertion."""
    tmp = tmp_path.resolve()
    checkout = _primary_checkout(tmp / "product")
    shutil.copytree(machinery_root(), checkout / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    other = _primary_checkout(tmp / "other")
    env = _bind_product(host_layout, checkout)

    refused = _probe_product_root(tmp, checkout / "scripts/lib/launcher_core.sh", **env, LU_PRODUCT_ROOT=str(other))
    assert refused.returncode == 3, refused.stderr
    assert "[legacy_conflict] LU_PRODUCT_ROOT" in refused.stderr

    result = _probe_product_root(tmp, checkout / "scripts/lib/launcher_core.sh", **env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"product-root={checkout}", f"exported={checkout}"]


# --- the consumers of an installed core's roots -------------------------------

# Runs the launcher's own root steps in launcher_main order (product root, helper
# roots, publication PATH), then one consumer of the roots they export.
_INSTALLED_CONSUMER_PROBE = r"""
source "$1"
shift
LC_PROVIDER=codex LC_MODE=driver LC_EPIC=devops LC_DRY_RUN=0
launcher_resolve_product_root || exit 3
launcher_resolve_roots
launcher_publication_path
cd "$LC_ROOT"
eval "$1"
"""

_INSTALLED_CONSUMERS = {
    # The agent shims the publication PATH puts first: each runs its guard on the
    # project interpreter before the (stub) real tool.
    "npm-shim": 'npm --version',
    "gh-shim": 'gh --version',
    # The live supervisory watcher keeps its pid lock under the shared-state anchor.
    "inbox-watcher": '"$LC_MACHINERY_DIR/ai_agent_bridge/inbox_watch.sh" codex --stop',
}


def _installed_release(tmp: Path) -> tuple[Path, Path]:
    """A site-packages copy of the package and a product primary whose .venv runs it."""
    site = tmp / "venv" / "lib" / "python3" / "site-packages"
    shutil.copytree(machinery_root(), site / "lu_machinery", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    product = _primary_checkout(tmp / "product")
    (product / ".venv").symlink_to(Path(sys.prefix))
    return site / "lu_machinery", product


def _stub_tool(tmp: Path, name: str) -> Path:
    tool = tmp / "real-bin" / name
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text(f"#!/bin/sh\necho real-{name} \"$@\"\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


@pytest.mark.parametrize(
    "consumer",
    [
        *_INSTALLED_CONSUMERS,
        "lane-wrapper-help",
        "lane-wrapper-scope",
        "lane-wrapper-successor",
        "in-tree-export",
        "token-shaped-product-path",
        "inadmissible-state-anchor",
    ],
)
def test_installed_release_consumers_serve_only_lu_product_root(tmp_path: Path, host_layout: HostLayout, consumer: str) -> None:
    """Defect class: an installed release's consumers deriving roots from site-packages (PR 301 F1-F4).

    A product that sets LU_PRODUCT_ROOT must get a working launch: the agent
    shims and the inbox watcher take the interpreter and shared-state anchor the
    launcher resolved, not their own site-packages location (F1); a packaged lane
    wrapper shows help with nothing configured, is an allowlisted scope entry and is
    replayed from the package (F2); the in-tree copy exports the checkout it serves,
    so Python never follows an ambient LU_PRODUCT_ROOT elsewhere (F3); and the
    child-environment filter keeps LU_PRODUCT_ROOT when its path has a token-shaped
    segment (F4). The launcher never exports a state anchor that shared-state admission
    refuses.
    """
    tmp = tmp_path.resolve()
    package, product = _installed_release(tmp)
    core = package / "lib/launcher_core.sh"
    env = _clean_env(
        HOME=str(tmp),
        LU_PRODUCT_ROOT=str(product),
        **_bind_product(host_layout, product),
        AGENT_REAL_NPM=str(_stub_tool(tmp, "npm")),
        AGENT_REAL_GH=str(_stub_tool(tmp, "gh")),
    )
    for name in ("LEARN_UKRAINIAN_PYTHON", "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LEARN_UKRAINIAN_STATE_ROOT",
                 "FLEET_COMMS_ROOT", "LU_TASKS_DIR", "LU_GITHUB_CACHE_DIR"):
        env.pop(name, None)

    def run(*argv: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv), cwd=tmp, env={**env, **extra}, capture_output=True, text=True, check=False, timeout=120
        )

    if consumer in _INSTALLED_CONSUMERS:
        result = run("bash", "-c", _INSTALLED_CONSUMER_PROBE, "bash", str(core), _INSTALLED_CONSUMERS[consumer])
        assert result.returncode == 0, (result.stdout, result.stderr)
        expected = {
            "npm-shim": "real-npm --version",
            "gh-shim": "real-gh --version",
            "inbox-watcher": "No inbox watcher lock exists for 'codex'.",
        }[consumer]
        assert expected in result.stdout + result.stderr, (result.stdout, result.stderr)
        return
    if consumer == "lane-wrapper-help":
        del env["LU_PRODUCT_ROOT"]
        for wrapper in ("start-kube-dojo-driver.sh", "start-bio-driver.sh"):
            result = run("bash", str(package / wrapper), "--help")
            assert result.returncode == 0, (wrapper, result.stderr)
            assert result.stdout.startswith("Usage: "), (wrapper, result.stdout)
        return
    if consumer == "lane-wrapper-scope":
        scope = r"""
source "$1"
LC_PROVIDER=claude LC_EPIC=kube-dojo LC_MODE="$2"
launcher_resolve_product_root || exit 3
source "$LC_MACHINERY_DIR/lib/driver_scope.sh" || exit 6
driver_scope_invoked_entry || exit 6
printf '%s %s %s %s\n' "$DS_ENTRY_PATH" "$DS_ENTRY_PROVIDER" "$DS_ENTRY_MODE" "$DS_ENTRY_FILE"
"""
        for wrapper, mode in (("start-kube-dojo-driver.sh", "driver"), ("start-bio-driver.sh", "interactive")):
            result = run("bash", "-c", scope, str(package / wrapper), str(core), mode)
            assert result.returncode == 0, (wrapper, result.stderr)
            assert result.stdout.split() == [f"lu_machinery/{wrapper}", "claude", mode, str(package / wrapper)]
        stray = tmp / "stray" / "start-kube-dojo-driver.sh"
        stray.parent.mkdir()
        shutil.copy2(package / "start-kube-dojo-driver.sh", stray)
        assert run("bash", "-c", scope, str(stray), str(core), "driver").returncode == 6
        return
    if consumer == "lane-wrapper-successor":
        successor = r"""
source "$1"
LC_PROVIDER=claude LC_MODE=driver LC_EPIC=kube-dojo
launcher_resolve_product_root || exit 3
source "$LC_MACHINERY_DIR/lib/session_supervisor.sh"
LC_DRIVER_LEASE_CLOSED=1 LC_SUPERVISORY_DELIVERY=delivery-1 SESSION_STREAM_ID=probe
LC_DRIVER_ORIGINAL_ARGS=(--help)
session_supervisor_exec_successor
"""
        result = run("bash", "-c", successor, str(package / "start-kube-dojo-driver.sh"), str(core))
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("Usage: "), result.stdout
        return
    if consumer == "inadmissible-state-anchor":
        roots = 'printf "python=%s anchor=%s\\n" "${LU_PYTHON:+set}" "$LU_STATE_ROOT"'
        result = run("bash", "-c", _INSTALLED_CONSUMER_PROBE, "bash", str(core), roots)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [f"python=set anchor={host_layout.data['state_root']}"]
        refused = run("bash", "-c", _INSTALLED_CONSUMER_PROBE, "bash", str(core), roots, LU_STATE_ROOT=str(product))
        assert refused.returncode == 3, refused.stderr
        assert "[legacy_conflict] LU_STATE_ROOT" in refused.stderr
        return
    if consumer == "in-tree-export":
        shutil.copytree(machinery_root(), product / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        other = _primary_checkout(tmp / "other")
        roots = """project_python_run "$LU_PYTHON" -c 'from scripts.common.repo_root import product_root; print(product_root())'"""
        core = product / "scripts/lib/launcher_core.sh"
        refused = run("bash", "-c", _INSTALLED_CONSUMER_PROBE, "bash", str(core), roots, LU_PRODUCT_ROOT=str(other))
        assert refused.returncode == 3, refused.stderr
        assert "[legacy_conflict] LU_PRODUCT_ROOT" in refused.stderr
        result = run("bash", "-c", _INSTALLED_CONSUMER_PROBE, "bash", str(core), roots)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [str(product)]
        return
    # The launcher child keeps every root the launcher exports, and so does a dispatched
    # agent's environment for the product root.
    token_shaped = str(tmp / ".worktrees" / "dispatch" / "codex" / "fix-sk-routing")
    names = ("LU_PRODUCT_ROOT", "LEARN_UKRAINIAN_PYTHON", "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT")
    sanitize = (
        "import os, sys\n"
        "from unittest.mock import patch\n"
        "from lu_machinery.agent_runtime.env_sanitize import build_agent_env, child_credential_env\n"
        "child = child_credential_env(os.environ, 'codex', driver_launcher=True)\n"
        "print(*(child.get(name) for name in sys.argv[1:]), sep='\\n')\n"
        "with patch('lu_machinery.agent_runtime.agent_github_identity.resolve_agent_github_identity', return_value=None):\n"
        "    agent = build_agent_env(provider='codex', model='gpt-6.1-sol')\n"
        "print(agent.get('LU_PRODUCT_ROOT'), agent.get('LEARN_UKRAINIAN_PRIMARY_REPO_ROOT'), sep='\\n')\n"
    )
    result = run(sys.executable, "-I", "-c", sanitize, *names, **dict.fromkeys(names, token_shaped))
    assert result.returncode == 0, result.stderr
    # The agent keeps the shared-state anchor too: every hook bootstrap refuses without it (#100).
    assert result.stdout.splitlines() == [token_shaped] * 5
