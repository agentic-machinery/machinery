"""Contract coverage for the public launcher estate."""

from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from lu_machinery.common.repo_root import PRODUCT_ROOT_ENV, machinery_path, machinery_root
from lu_machinery.common.scratch import ensure_scratch_root
from lu_machinery.session_streams.db import SessionStreamDatabase
from lu_machinery.session_streams.model import LeaseHolder, utc_now
from lu_machinery.session_streams.store import SessionStreamStore
from lu_machinery.session_supervisor import LaunchRole, SessionSupervisor
from tests.epics_monitor_stub import epics_monitor_stub
from tests.epics_monitor_stub import isolated_monitor_state as isolated_monitor_state
from tests.helpers import current_sol, product_checkout
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.host_context.conftest import git as fixture_git
from tests.launcher_sandbox import (
    bind_driver_protocol_fixture,
    bind_launcher_environment,
    copy_interactive_launcher_checkout,
    copy_launcher_sources,
    copy_slot_registry,
    launcher_environment,
    launcher_toolchain_path,
)
from tests.lib.test_launcher_driver_scope import install_scope_sandbox, manager_guard
from tests.rules_core_view import (
    _build_view,
    install_loader_bypass,
    rules_core_absent_when_marked,  # noqa: F401  (autouse: serves @rules_core_absent)
)

CURRENT_SOL = current_sol()

pytestmark = [
    pytest.mark.usefixtures("configured_roots", "hermetic_monitor", "isolated_monitor_state"),
    # Keep CI's timeout budget, but report an ordinary failure if it expires.
    # pytest-timeout's thread method exits the entire xdist worker.
    pytest.mark.timeout(method="signal"),
]

REPO = Path(__file__).resolve().parents[2]
# The checkout run_launcher starts launchers from; rules_core_absent tests get a
# view of REPO whose launcher loader loads nothing (tests/rules_core_view.py).
LAUNCH_ROOT = REPO
PUBLIC = (
    "start-claude.sh",
    "start-claude-driver.sh",
    "start-claude-cto.sh",
    "start-codex.sh",
    "start-codex-driver.sh",
    "start-codex-cto.sh",
    "start-gemini.sh",
    "start-gemini-driver.sh",
    "start-muse-driver.sh",
)


def _install_claude_usage_fixture(root: Path) -> None:
    """Supply usage only in copied fixtures; production has no test bypass."""
    (root / "lu_machinery/lib/claude_weekly_used.py").write_text("print('0')\n", encoding="utf-8")


def _run_launcher_process(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Keep launcher group signals and timeout cleanup away from pytest."""
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


_DRY_RUN_CHECKOUTS: dict[tuple[str, str, str, bool, str | None], Path] = {}


def _dry_run_checkout(*, claude_usage_stub: bool) -> Path:
    """Build each read-only dry-run checkout view once per process instead of per call."""
    rules_core_override = (
        (LAUNCH_ROOT / "lu_machinery/lib/rules_core.sh").read_text(encoding="utf-8")
        if LAUNCH_ROOT != REPO
        else None
    )
    base = tempfile.gettempdir()
    key = (
        base,
        str(product_checkout().resolve()),
        str(machinery_root().resolve()),
        claude_usage_stub,
        rules_core_override,
    )
    cached = _DRY_RUN_CHECKOUTS.get(key)
    if cached is not None and cached.is_dir():
        return cached
    temp_dir = Path(tempfile.mkdtemp(prefix="launcher-dry-run-", dir=base))
    atexit.register(shutil.rmtree, temp_dir, ignore_errors=True)
    overrides: dict[tuple[str, ...], str] = {}
    if claude_usage_stub:
        overrides[("lu_machinery", "lib", "claude_weekly_used.py")] = "print('0')\n"
    if rules_core_override is not None:
        overrides[("lu_machinery", "lib", "rules_core.sh")] = rules_core_override
    checkout = _build_view(temp_dir / "checkout", overrides)
    fixture_git(checkout, "remote", "add", "origin", "https://github.com/learn-ukrainian/learn-ukrainian.github.io.git")
    _DRY_RUN_CHECKOUTS[key] = checkout
    return checkout


def run_launcher(
    name: str,
    *args: str,
    env: dict[str, str] | None = None,
    dry_run: bool = True,
    root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if dry_run and root is None:
        checkout = _dry_run_checkout(
            claude_usage_stub=name in ("start-claude.sh", "start-claude-driver.sh", "start-claude-cto.sh"),
        )
        return run_launcher(name, *args, env=env, dry_run=True, root=checkout)
    if not dry_run and root is None:
        with TemporaryDirectory(prefix="launcher-checkout-") as temporary:
            checkout = Path(temporary) / "checkout"
            copy_interactive_launcher_checkout(checkout)
            _install_claude_usage_fixture(checkout)
            # The fixture has no package.json; the deploy gate is not under test here.
            (checkout / "lu_machinery/lib/deploy_extensions.sh").write_text("deploy_agent_extensions() { :; }\n", encoding="utf-8")
            # Every non-dry launch, interactive or driver, enters a scope (#9624).
            install_scope_sandbox(checkout)
            return run_launcher(name, *args, env=env, dry_run=False, root=checkout)
    launch_root = root if root is not None else LAUNCH_ROOT
    launch_env = launcher_environment({PRODUCT_ROOT_ENV: str(launch_root), "CODEX_CANONICAL_REPO_ROOT": str(launch_root)})
    launch_env["LAUNCHER_DRY_RUN"] = "1" if dry_run else "0"
    launch_env.update(env or {})
    with TemporaryDirectory(prefix="launcher-manager-guard-", dir=ensure_scratch_root()) as guard:
        if launch_root != REPO:
            launch_env = bind_launcher_environment(launch_root, launch_env, overrides=env)
        contact = Path(guard) / "real-manager-contact"
        if not dry_run:
            # After the directories a test put ahead of the inherited PATH (its
            # fake manager), before the system ones: an unfaked call fails the
            # test rather than reaching this host's real user manager.
            inherited, path = launcher_toolchain_path(), launch_env.get("PATH", "")
            if path.endswith(os.defpath):
                path = path[:-len(os.defpath)] + inherited
            prefix = path[: -len(inherited)] if inherited and path.endswith(inherited) else ""
            rest = path[len(prefix) :]
            launch_env["PATH"] = f"{prefix}{manager_guard(Path(guard) / 'bin')}{os.pathsep}{rest}"
            launch_env["LU_TEST_REAL_MANAGER_CONTACT"] = str(contact)
        result = _run_launcher_process(
            [str(launch_root / name), *args],
            cwd=launch_root,
            env=launch_env,
            timeout=30,
        )
        if contact.exists():
            pytest.fail(f"{name} reached for the real user manager: {contact.read_text()}")
    return result


def test_real_launcher_deploy_is_confined_to_temporary_checkout(tmp_path: Path) -> None:
    """Keep the real startup deploy and assert its actual output, not a stub."""
    checkout = tmp_path / "checkout with spaces"
    copy_interactive_launcher_checkout(checkout)
    # PR #133 requires an explicit consumer descriptor and provider-owned hook settings.
    from lu_machinery.agent_deploy.plan import release_inputs
    _files, inventory, _identity = release_inputs()
    descriptor = {"providers": ["claude", "codex", "agy"], "sources": {}, "preserve": []}
    for provider, configs in inventory["required_hooks"].items():
        source = checkout / ("product-" + provider)
        source.mkdir()
        descriptor["sources"][provider] = {"path": source.name, "required": True}
        for destination, requirements in configs.items():
            config = {}
            for requirement in requirements:
                current = config
                for key in requirement["path"][:-1]:
                    current = current.setdefault(key, {})
                current.setdefault(requirement["path"][-1], []).append({**requirement["group"], "hooks": requirement["hooks"]})
            (source / destination.split("/", 1)[1]).write_text(json.dumps(config))
    (checkout / "agents_extensions/deployment.v1.json").write_text(json.dumps(descriptor))
    # The isolated child must deploy this checkout's release, not whichever
    # version happens to be installed in the interpreter's site-packages.
    deploy_code = (
        f"import sys, runpy; sys.path.insert(0, {str(machinery_root().parent)!r}); "
        "runpy.run_module('lu_machinery.agent_deploy', run_name='__main__')"
    )
    deploy_command = shlex.join([sys.executable, "-I", "-c", deploy_code, "--project-root", str(checkout)])
    (checkout / "package.json").write_text(json.dumps({"scripts": {"agents:deploy": deploy_command}}))
    _install_claude_usage_fixture(checkout)
    install_scope_sandbox(checkout)
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nprintf 'provider cwd=%s\\n' \"$PWD\"\n", encoding="utf-8")
    binary.chmod(0o755)
    toolchain = launcher_toolchain_path()
    trace = tmp_path / "deployment-toolchain.log"
    for name in ("node", "npm"):
        executable = shutil.which(name, path=toolchain)
        assert executable is not None
        wrapper = binary.parent / name
        wrapper.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(name + '=' + executable)} >> {shlex.quote(str(trace))}\n"
            f"exec {shlex.quote(executable)} \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    result = run_launcher(
        "start-claude.sh",
        dry_run=False,
        root=checkout,
        env={
            "PATH": f"{binary.parent}{os.pathsep}{launcher_toolchain_path()}",
            "HOME": str(tmp_path / "home"),
            "LU_SKIP_PLANE_TUNNEL_CHECK": "1",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Agent extensions deployed (agents:deploy)" in result.stdout
    assert f"provider cwd={checkout}" in result.stdout
    executed = trace.read_text().splitlines()
    for name in ("node", "npm"):
        assert f"{name}={shutil.which(name, path=toolchain)}" in executed
    print(f"Deployment child toolchain: {executed}", flush=True)
    sources = machinery_path("agents_extensions/shared/skills")
    deployed = checkout / ".agents/skills"
    # A packaged skill file whose release record has no targets is never deployed.
    undeployed = {Path(name).relative_to("skills") for name, record in inventory["files"].items()
                  if name.startswith("skills/") and not record["targets"]}
    expected = {path.relative_to(sources) for path in sources.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts} - undeployed
    actual = {path.relative_to(deployed) for path in deployed.rglob("*") if path.is_file()}
    assert actual == expected
    assert any(path.name == "SKILL.md" for path in actual)
    for relative in expected:
        assert (deployed / relative).read_bytes() == (sources / relative).read_bytes()
    for name in ("session-setup.sh", "post-compact.sh"):
        canonical = (checkout / "agents_extensions/shared/hooks" / name).read_bytes()
        for destination in (".claude", ".codex", ".agent"):
            assert (checkout / destination / "hooks" / name).read_bytes() == canonical
    assert not (checkout / ".codex/skills").exists()


@pytest.mark.parametrize("name", PUBLIC)
def test_help_is_machine_usable(name: str) -> None:
    result = run_launcher(name, "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "EXIT CODES:" in result.stdout
    assert "LAUNCHER_DRY_RUN=1" in result.stdout
    if name in {"start-claude.sh", "start-claude-driver.sh"}:
        assert "project settings (Sonnet 5.5)" in result.stdout


@pytest.mark.parametrize("name", PUBLIC)
def test_unknown_launcher_flag_exits_usage_error(name: str) -> None:
    result = run_launcher(name, "--does-not-exist")
    assert result.returncode == 2
    assert "run --help" in result.stderr


@pytest.mark.parametrize(
    ("name", "message"),
    (
        ("start-claude.sh", "interactive launchers reject --epic"),
        ("start-codex.sh", "interactive launchers reject --epic"),
        ("start-gemini.sh", "interactive launchers reject --epic"),
    ),
)
def test_interactive_launchers_reject_driver_epic(name: str, message: str) -> None:
    result = run_launcher(name, "--epic", "devops")
    assert result.returncode == 2
    assert message in result.stderr
    assert "would exec" not in result.stdout


@pytest.mark.parametrize(
    ("name", "message"),
    (
        ("start-claude.sh", "interactive launchers reject --force"),
        ("start-codex.sh", "interactive launchers reject --force"),
        ("start-gemini.sh", "interactive launchers reject --force"),
    ),
)
def test_interactive_launchers_reject_force(name: str, message: str) -> None:
    result = run_launcher(name, "--force")
    assert result.returncode == 2
    assert message in result.stderr
    assert "would exec" not in result.stdout


@pytest.mark.parametrize(
    "name",
    (
        "start-claude-driver.sh",
        "start-codex-driver.sh",
        "start-gemini-driver.sh",
    ),
)
def test_driver_help_lists_force(name: str) -> None:
    result = run_launcher(name, "--help")
    assert result.returncode == 0, result.stderr
    assert "--force" in result.stdout
    assert "Attributed operator release" in result.stdout


def test_driver_dry_run_force_announces_release_then_claim() -> None:
    result = run_launcher("start-codex-driver.sh", "--epic", "infra", "--force")
    assert result.returncode == 0, result.stderr
    assert "would force-release any live holder on epic:" in result.stdout
    assert "then claim" in result.stdout


def test_driver_requires_certified_model_and_valid_epic() -> None:
    missing = run_launcher("start-claude-driver.sh")
    assert missing.returncode == 2
    untrusted = run_launcher("start-claude-driver.sh", "--epic", "devops", "--model", "not-certified")
    assert untrusted.returncode == 4
    invalid = run_launcher("start-claude-driver.sh", "--epic", "not-a-lane")
    assert invalid.returncode == 2


def test_codex_driver_preserves_transport_probe_and_lease_guard() -> None:
    sustained = run_launcher("start-codex-driver.sh", "--epic", "devops", "--model", CURRENT_SOL)
    assert sustained.returncode == 0, sustained.stderr
    assert "would probe" in sustained.stdout
    assert sustained.stdout.index("would claim lease") < sustained.stdout.index("would mint and bootstrap")
    assert sustained.stdout.index("would mint and bootstrap") < sustained.stdout.index("would bind drive-epic")
    prompt = shlex.split(sustained.stdout.split("would exec ", 1)[1])[-1]
    assert prompt.startswith(
        "First run lu-driver-state render --lane devops, then lu-driver-state check --lane devops."
    )
    assert "Load .agents/skills/drive-epic/SKILL.md from the workspace root" in prompt
    assert "agents_extensions/shared/skills/" not in prompt
    assert "newest full-content DB handoff_note as authority" in prompt
    assert "exactly one full-content DB handoff_note" in prompt
    assert "rollover/lineage IDs, full live task IDs/run nonces, open order message IDs, PR heads" in prompt
    assert "the next three actions" in prompt
    assert "A temporary path or markdown content cannot substitute" in prompt
    assert "pointer holds only pinned provider/stream facts and DB render instructions" in prompt
    assert "under 4 KiB and do not update it when a goal lands" in prompt
    assert "goals and next step live in .claude/" not in prompt
    assert "after the fourth compaction and before the eighth" in prompt
    assert "native automatic compaction remains enabled" in prompt

    governor = run_launcher("start-codex-driver.sh", "--governor", "AUTO", env={"SESSION_EPIC": "foreign"})
    assert governor.returncode == 0, governor.stderr
    assert f"--model {CURRENT_SOL}" in governor.stdout
    assert "governor SESSION_EPIC=<unset>" in governor.stdout
    assert "would claim lease" not in governor.stdout
    governor_prompt = shlex.split(governor.stdout.split("would exec ", 1)[1])[-1]
    assert governor_prompt.startswith("Follow .codex/prompts/dynamic-area-epic-fleet-governor.md ")


@pytest.mark.parametrize(
    ("launcher", "args", "model", "effort"),
    [
        ("start-codex.sh", [], CURRENT_SOL, "high"),
        ("start-codex-driver.sh", ["--epic", "devops"], CURRENT_SOL, "high"),
        # A non-default effort the current Sol offers (models.<id>.efforts); max is refused.
        ("start-codex.sh", ["--model", CURRENT_SOL, "--effort", "medium"], CURRENT_SOL, "medium"),
    ],
)
def test_codex_launchers_pin_roles_and_preserve_explicit_effort(launcher, args, model, effort):
    result = run_launcher(launcher, *args)
    assert result.returncode == 0, result.stderr
    assert f"--model {model}" in result.stdout
    assert f"model_reasoning_effort={effort}" in result.stdout
    if "--epic" in args:
        prompt = shlex.split(result.stdout.split("would exec ", 1)[1])[-1]
        assert prompt.startswith(
            "First run lu-driver-state render --lane devops, then lu-driver-state check --lane devops."
        )





def _core_canary_failure_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a provider-neutral driver whose canary failure is observable."""
    root = tmp_path / "repo"
    copy_launcher_sources(root)
    install_scope_sandbox(root)
    install_loader_bypass(root)
    # The fixture has no package.json; the deploy gate is not under test here.
    (root / "lu_machinery/lib/deploy_extensions.sh").write_text("deploy_agent_extensions() { :; }\n", encoding="utf-8")
    _install_claude_usage_fixture(root)
    watcher = root / "lu_machinery" / "ai_agent_bridge" / "inbox_watch.sh"
    watcher.parent.mkdir(parents=True, exist_ok=True)
    watcher.write_text("#!/usr/bin/env bash\nexec sleep 300\n", encoding="utf-8")
    watcher.chmod(0o755)
    # Driver launches check their handoff slot against the real roster (#8303).
    copy_slot_registry(root)

    claim_marker = tmp_path / "lease-claimed"
    close_marker = tmp_path / "lease-closed"
    python_stub = root / ".venv" / "bin" / "python"
    python_stub.parent.mkdir(parents=True)
    python_stub.write_text(
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "-I" && "${{2:-}}" == "-c" && "${{3:-}}" == *"module bootstrap:"* ]]; then shift 5; fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "lu_machinery.session_supervisor" && "${{3:-}}" == "open" ]]; then
  touch {os.fspath(claim_marker)!r}
  cat <<'JSON'
{{"identity":{{"lease":{{"session_id":"session-test","lease_id":"lease-test","generation":1,"fencing_token":1,"expires_at":"2026-07-23T00:00:00Z"}}}}}}
JSON
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "lu_machinery.session_supervisor" && "${{3:-}}" == "close" ]]; then
  touch {os.fspath(close_marker)!r}
  exit 0
fi
exec {sys.executable!r} "$@"
""",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)
    bind_driver_protocol_fixture(root, python_stub)

    adapter = root / "lu_machinery" / "launchers" / "claude.sh"
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(
        """#!/usr/bin/env bash
launcher_adapter_validate() { :; }
launcher_adapter_preflight() { :; }
launcher_adapter_canary() { return 1; }
launcher_adapter_exec() { printf 'PROVIDER_EXEC\\n'; }
""",
        encoding="utf-8",
    )
    adapter.chmod(0o755)
    initialized = subprocess.run(
        ["git", "init", "-q", "-b", "main", os.fspath(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=launcher_environment(),
    )
    assert initialized.returncode == 0, initialized.stderr
    return root / "start-claude-driver.sh", claim_marker, close_marker


def test_canary_failure_closes_lease_and_refuses_driver_launch(tmp_path: Path) -> None:
    launcher, claim_marker, close_marker = _core_canary_failure_fixture(tmp_path)
    env = launcher_environment()
    result = _run_launcher_process(
        ["bash", os.fspath(launcher), "--epic", "devops"],
        cwd=launcher.parent,
        env=env,
        timeout=30,
    )

    assert result.returncode == 1
    assert claim_marker.is_file()
    # Mutation guard: deleting the shared-core close call leaves this marker
    # absent and recreates the six-hour lease leak for every provider driver.
    assert close_marker.is_file()
    assert "PROVIDER_EXEC" not in result.stdout


# Bound for the child-and-parent readiness handshake. Startup under a loaded
# runner can take much longer than the old 2s provider lifetime; the wait is
# still finite so a hung launcher fails the test.
_LAUNCHER_READY_TIMEOUT_S = 60


def _blocking_signal_provider(signal_marker: Path, child_ready: Path) -> str:
    """Trap first, publish readiness, then block until the signal arrives.

    A fixed ``sleep`` budget exits 88 under load before the parent forwards.
    The ready file is touched only after the trap is installed.
    """
    marker = shlex.quote(os.fspath(signal_marker))
    ready = shlex.quote(os.fspath(child_ready))
    return f'trap "touch {marker}; exit 0" INT TERM HUP\ntouch {ready}\nwhile :; do\n  sleep 0.1\ndone\n'


def _append_forward_ready_hook(launcher_core: Path, forward_ready: Path) -> None:
    """Publish readiness from the driver wait loop, after forwarding traps exist."""
    ready = shlex.quote(os.fspath(forward_ready))
    launcher_core.write_text(
        launcher_core.read_text(encoding="utf-8")
        + "\n# Test override of the launcher_driver_wait_hook seam (#8556).\n"
        + "launcher_driver_wait_hook() {\n"
        + f"  if [ ! -e {ready} ]; then touch {ready}; fi\n"
        + "}\n",
        encoding="utf-8",
    )


def _wait_for_launcher_readiness(
    process: subprocess.Popen[str],
    ready_paths: list[Path],
    *,
    timeout: float = _LAUNCHER_READY_TIMEOUT_S,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.is_file() for path in ready_paths):
            return
        if process.poll() is not None:
            break
        time.sleep(0.05)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    stdout, stderr = process.communicate(timeout=10)
    missing = ", ".join(os.fspath(path) for path in ready_paths if not path.is_file())
    pytest.fail(
        f"timed out waiting for launcher readiness (missing {missing}, rc={process.returncode}):\n{stdout}{stderr}"
    )


def _core_driver_exit_fixture(
    tmp_path: Path,
    *,
    provider_body: str,
    failed_close_attempts: int = 0,
    close_stderr: str = "",
    forward_ready: Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    """Build a provider-neutral driver with observable close attempts.

    ``close_stderr`` (when a close attempt is configured to fail) is written
    to a file the stub cats to its own stderr, rather than interpolated into
    the stub's bash source — a hostile marker (quotes, `` ` ``, `$()`) must
    never become executable just by being a close failure's stderr.
    """
    root = tmp_path / "repo"
    copy_launcher_sources(root)
    install_scope_sandbox(root)
    install_loader_bypass(root)
    # The fixture has no package.json; the deploy gate is not under test here.
    (root / "lu_machinery/lib/deploy_extensions.sh").write_text("deploy_agent_extensions() { :; }\n", encoding="utf-8")
    _install_claude_usage_fixture(root)
    if forward_ready is not None:
        _append_forward_ready_hook(root / "lu_machinery/lib/launcher_core.sh", forward_ready)
    watcher = root / "lu_machinery" / "ai_agent_bridge" / "inbox_watch.sh"
    watcher.parent.mkdir(parents=True, exist_ok=True)
    watcher.write_text("#!/usr/bin/env bash\nexec sleep 300\n", encoding="utf-8")
    watcher.chmod(0o755)
    # Driver launches check their handoff slot against the real roster (#8303).
    copy_slot_registry(root)

    close_attempts = tmp_path / "close-attempts"
    close_marker = tmp_path / "lease-closed"
    child_started = tmp_path / "child-started"
    close_stderr_path = tmp_path / "close-stderr.txt"
    close_stderr_path.write_text(close_stderr, encoding="utf-8")
    python_stub = root / ".venv" / "bin" / "python"
    python_stub.parent.mkdir(parents=True)
    python_stub.write_text(
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "-I" && "${{2:-}}" == "-c" && "${{3:-}}" == *"module bootstrap:"* ]]; then shift 5; fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "lu_machinery.session_supervisor" && "${{3:-}}" == "open" ]]; then
  cat <<'JSON'
{{"identity":{{"lease":{{"session_id":"session-test","lease_id":"lease-test","generation":1,"fencing_token":1,"expires_at":"2026-07-23T00:00:00Z"}}}}}}
JSON
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "lu_machinery.session_supervisor" && "${{3:-}}" == "close" ]]; then
  count=0
  if [[ -f {os.fspath(close_attempts)!r} ]]; then count="$(< {os.fspath(close_attempts)!r})"; fi
  count=$((count + 1))
  printf '%s\n' "$count" > {os.fspath(close_attempts)!r}
  if [[ "$count" -le {failed_close_attempts} ]]; then
    cat {os.fspath(close_stderr_path)!r} >&2
    exit 1
  fi
  touch {os.fspath(close_marker)!r}
  exit 0
fi
exec {sys.executable!r} "$@"
""",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)
    bind_driver_protocol_fixture(root, python_stub)

    provider = root / "provider.sh"
    provider.write_text(
        f"#!/usr/bin/env bash\n{provider_body}\n",
        encoding="utf-8",
    )
    provider.chmod(0o755)

    adapter = root / "lu_machinery" / "launchers" / "claude.sh"
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(
        f"""#!/usr/bin/env bash
launcher_adapter_validate() {{ :; }}
launcher_adapter_preflight() {{ :; }}
launcher_adapter_canary() {{ return 0; }}
launcher_adapter_exec() {{ launcher_exec_command {os.fspath(provider)!r}; }}
""",
        encoding="utf-8",
    )
    adapter.chmod(0o755)
    initialized = subprocess.run(
        ["git", "init", "-q", "-b", "main", os.fspath(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=launcher_environment(),
    )
    assert initialized.returncode == 0, initialized.stderr
    return root / "start-claude-driver.sh", close_attempts, close_marker, child_started


@pytest.mark.parametrize("resolution", ["recorded", "local-bin", "missing"])
def test_supervisory_wake_preflights_cli_before_provider_stop_and_lease_close(
    tmp_path: Path,
    resolution: str,
) -> None:
    """Exercise the real wait loop with a PATH change after the provider starts."""
    events = tmp_path / "events"
    ready = tmp_path / "ready"
    finish = tmp_path / "finish"
    resumed = tmp_path / "watch-resumed"
    sent = tmp_path / "wake-sent"
    home = tmp_path / "home"
    cli = tmp_path / "cli with spaces" / "fixture-cli"
    cli.parent.mkdir()
    cli.write_text(
        f"""#!/bin/bash
if [ "${{1:-}}" = --successor ]; then
  printf 'successor\\n' >> {shlex.quote(str(events))}
  exit 0
fi
trap 'printf "stopped\\n" >> {shlex.quote(str(events))}; exit 0' TERM
if [ {shlex.quote(resolution)} != recorded ]; then chmod -x "$0"; fi
touch {shlex.quote(str(ready))}
while [ ! -f {shlex.quote(str(finish))} ]; do sleep 0.05; done
printf 'normal-exit\\n' >> {shlex.quote(str(events))}
""",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    if resolution == "local-bin":
        fallback = home / ".local/bin/fixture-cli"
        fallback.parent.mkdir(parents=True)
        shutil.copy2(cli, fallback)
    successor = tmp_path / "start-codex-driver.sh"
    successor.write_text("#!/bin/bash\nexec fixture-cli --successor\n", encoding="utf-8")
    successor.chmod(0o755)
    script = f"""
set -euo pipefail
source {shlex.quote(str(machinery_path('lib/launcher_core.sh')))}
source {shlex.quote(str(machinery_path('lib/session_supervisor.sh')))}
LC_ROOT={shlex.quote(str(tmp_path))}
LC_MODE=driver LC_PROVIDER=codex LC_DRIVER_LEASE_CLAIMED=1
LC_DRIVER_ORIGINAL_ARGS=()
export SESSION_STREAM_ID=epic:9999 SESSION_STREAM_GENERATION=1
launcher_driver_renew_loop() {{ :; }}
launcher_driver_wait_hook() {{ export PATH={shlex.quote(os.defpath)}; }}
session_supervisor_start_inbox_watch() {{
  LC_SUPERVISORY_DELIVERY=''
  if [ -f {shlex.quote(str(sent))} ]; then
    touch {shlex.quote(str(resumed))}
    (exec sleep 300) &
  else
    (while [ ! -f {shlex.quote(str(ready))} ]; do sleep 0.01; done; exit 75) &
    touch {shlex.quote(str(sent))}
  fi
  LC_SUPERVISORY_WATCH_PID=$!
}}
session_supervisor_read_wake() {{
  local rc=0
  wait "$LC_SUPERVISORY_WATCH_PID" || rc=$?
  LC_SUPERVISORY_WATCH_PID=''
  [ "$rc" = 75 ] || return 1
  export PATH={shlex.quote(os.defpath)}
  if type -P fixture-cli >/dev/null; then return 99; fi
  LC_SUPERVISORY_DELIVERY=fixture-delivery
}}
launcher_close_driver_lease() {{
  printf 'close\\n' >> {shlex.quote(str(events))}
  LC_DRIVER_LEASE_CLOSED=1
}}
launcher_exec_command fixture-cli
"""
    env = {**launcher_environment(), "HOME": str(home), "PATH": f"{cli.parent}{os.pathsep}{launcher_toolchain_path()}", "LU_PYTHON": sys.executable}
    process = subprocess.Popen(
        ["/bin/bash", "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        if resolution == "missing":
            _wait_for_launcher_readiness(process, [ready, resumed])
            assert not events.exists(), "preflight failure must not stop the provider or close its lease"
            assert process.poll() is None
            finish.touch()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stdout + stderr
        if resolution == "missing":
            assert "provider-cli-unavailable" in stderr
            assert events.read_text().splitlines() == ["normal-exit", "close"]
        else:
            assert events.read_text().splitlines() == ["stopped", "close", "successor"]
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)


def test_restarted_watcher_retains_same_delivery_until_successor_cli_recovers(tmp_path: Path) -> None:
    """Use real delivery claims and watcher ticks after a post-preparation CLI loss."""
    from lu_machinery.fleet_comms.authority import AuthorityService

    events = tmp_path / "events"
    ready = tmp_path / "ready"
    polls = tmp_path / "polls"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    binding = tmp_path / "watcher-state.json"
    binding.write_text(json.dumps({"schema": "machinery.sandbox-state.v1", "state_root": str(state)}))
    plane = tmp_path / "fleet"
    cli = tmp_path / "bin with spaces" / "fixture-cli"
    cli.parent.mkdir()
    cli.write_text(
        f"""#!/bin/bash
trap 'printf "stopped\\n" >> {shlex.quote(str(events))}; exit 0' TERM
touch {shlex.quote(str(ready))}
while :; do sleep 0.05; done
""",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    copy_launcher_sources(tmp_path)
    lib = tmp_path / "lu_machinery/lib"
    lib.mkdir(parents=True, exist_ok=True)
    for name in ("launcher_core.sh", "session_supervisor.sh", "project_interpreter.sh"):
        shutil.copy2(machinery_path('lib') / name, lib / name)
    watcher = tmp_path / "lu_machinery/ai_agent_bridge/inbox_watch.sh"
    watcher.parent.mkdir(exist_ok=True)
    harness = tmp_path / "watcher.py"
    harness.write_text(
        f"""import sys, time
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, {str(REPO)!r})
sys.path.insert(0, {str(REPO / "lu_machinery")!r})
from lu_machinery.ai_agent_bridge import _inbox_watch
from lu_machinery.fleet_comms.authority import AuthorityService

ready = Path({str(ready)!r})
while not ready.exists(): time.sleep(0.01)
events = Path({str(events)!r})
polls = Path({str(polls)!r})
cli = Path({str(cli)!r})
lease = SimpleNamespace(stream_id='epic:9999', session_id='predecessor', generation=1)
def handoff(**kwargs):
    with events.open('a') as handle: handle.write('prepared\\n')
    # Simulate the executable disappearing between watcher and launcher checks.
    if events.read_text().splitlines().count('prepared') == 1: cli.chmod(0o644)
supervisor = SimpleNamespace(
    remote=SimpleNamespace(_lease_payload=lambda lease: {{'session_id': lease.session_id}}),
    build_capsule=lambda **kwargs: None, handoff_driver=handoff,
)
consume = _inbox_watch.consume_supervisory_event
def tick(*args, **kwargs):
    count = len(polls.read_text().splitlines()) if polls.exists() else 0
    # Cross delivery TTLs deterministically without a minute-long test. The
    # remote lease remains live; only local delivery time is advanced.
    result = consume(*args, now=(datetime.now(UTC) + timedelta(seconds=61 * count)).isoformat(), **kwargs)
    with polls.open('a') as handle: handle.write('tick\\n')
    return result
with (
    patch('lu_machinery.session_streams.hooks.lease_from_environment', return_value=lease),
    patch('lu_machinery.session_supervisor.SessionSupervisor', return_value=supervisor),
    patch('lu_machinery.fleet_comms.authority.AuthorityService',
          partial(AuthorityService, root=Path({str(plane)!r}))),
    patch.object(_inbox_watch, 'consume_supervisory_event', side_effect=tick),
):
    raise SystemExit(_inbox_watch.run_live_supervisory_watcher(interval_seconds=0.05))
""",
        encoding="utf-8",
    )
    watcher.write_text(f"#!/bin/bash\nexec {shlex.quote(sys.executable)} {shlex.quote(str(harness))}\n")
    watcher.chmod(0o755)
    successor = tmp_path / "start-codex-driver.sh"
    successor.write_text(f"#!/bin/bash\nprintf 'successor\\n' >> {shlex.quote(str(events))}\n")
    successor.chmod(0o755)
    script = f"""
set -euo pipefail
source {shlex.quote(str(lib / "launcher_core.sh"))}
source {shlex.quote(str(lib / "session_supervisor.sh"))}
LC_ROOT={shlex.quote(str(tmp_path))}
LC_MODE=driver LC_PROVIDER=codex LC_DRIVER_LEASE_CLAIMED=1
LC_DRIVER_ORIGINAL_ARGS=()
export SESSION_STREAM_ID=epic:9999 SESSION_STREAM_GENERATION=1
launcher_driver_renew_loop() {{ :; }}
launcher_close_driver_lease() {{
  printf 'close\\n' >> {shlex.quote(str(events))}
  LC_DRIVER_LEASE_CLOSED=1
}}
launcher_exec_command fixture-cli
"""
    env = {
        **launcher_environment(),
        "HOME": str(tmp_path / "home"),
        "PATH": f"{cli.parent}{os.pathsep}{launcher_toolchain_path()}",
        "LU_HOST_CONTEXT": str(binding),
        "LU_PYTHON": sys.executable,
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": os.environ["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"],
        "LU_PRODUCT_ROOT": os.environ["LU_PRODUCT_ROOT"],
    }
    for key in ("FLEET_COMMS_ROOT", "LU_TASKS_DIR", "LU_GITHUB_CACHE_DIR"):
        del env[key]
    with AuthorityService(root=plane) as service:
        did = service.publish_message(
            sender="fixture-operator",
            recipients=("supervisor:epic:9999",),
            body=json.dumps(
                {"schema": "supervisory-wake.v1", "action": "restart", "stream_id": "epic:9999", "generation": 1}
            ),
            kind="supervisory-request",
            correlation_id="fixture-cycle",
            idempotency_key="restart",
        ).delivery_ids[0]
        process = subprocess.Popen(
            ["/bin/bash", "-c", script],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not polls.exists() or len(polls.read_text().splitlines()) < 5:
                assert time.monotonic() < deadline, "restarted watcher did not retry the retained delivery"
                assert process.poll() is None, process.communicate(timeout=5)
                time.sleep(0.02)
            assert events.read_text().splitlines() == ["prepared"]
            assert service.get_delivery(did).attempt_count == 1
            assert service.supervisory_delivery_status(did) == "live_driver_consumed"
            cli.chmod(0o755)
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stdout + stderr
            assert stderr.count("Predecessor retained") == 1
            assert "restart retained for retry" in stderr
            assert events.read_text().splitlines() == ["prepared", "prepared", "stopped", "close", "successor"]
            assert service.get_delivery(did).attempt_count == 2
            assert service.supervisory_delivery_status(did) == "live_driver_consumed"
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)


def test_driver_normal_exit_closes_with_bounded_idempotent_retry(tmp_path: Path) -> None:
    launcher, close_attempts, close_marker, child_started = _core_driver_exit_fixture(
        tmp_path,
        provider_body=f"touch {os.fspath(tmp_path / 'child-started')!r}\nexit 7",
        failed_close_attempts=1,
    )
    env = launcher_environment()
    result = _run_launcher_process(
        ["bash", os.fspath(launcher), "--epic", "devops"],
        cwd=launcher.parent,
        env=env,
        timeout=30,
    )

    assert result.returncode == 7
    assert child_started.is_file()
    assert close_marker.is_file()
    assert close_attempts.read_text(encoding="utf-8").strip() == "2"


@pytest.mark.parametrize(
    ("termination_signal", "expected_exit"),
    ((signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)),
)
def test_driver_termination_forwards_signal_and_closes_exact_lease(
    tmp_path: Path,
    termination_signal: signal.Signals,
    expected_exit: int,
) -> None:
    signal_marker = tmp_path / "signal-received"
    child_ready = tmp_path / "child-ready"
    forward_ready = tmp_path / "forward-ready"
    launcher, close_attempts, close_marker, _child_started = _core_driver_exit_fixture(
        tmp_path,
        provider_body=_blocking_signal_provider(signal_marker, child_ready),
        forward_ready=forward_ready,
    )
    env = launcher_environment()
    original_handler = signal.signal(termination_signal, signal.SIG_DFL)
    try:
        with subprocess.Popen(
            ["bash", os.fspath(launcher), "--epic", "devops"],
            cwd=launcher.parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                _wait_for_launcher_readiness(process, [child_ready, forward_ready])
                process.send_signal(termination_signal)
                stdout, stderr = process.communicate(timeout=_LAUNCHER_READY_TIMEOUT_S)
            except BaseException:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                raise
        assert signal_marker.is_file(), (
            f"provider child never received forwarded signal (rc={process.returncode}):\n{stdout}{stderr}"
        )
        assert process.returncode == expected_exit, stdout + stderr
        assert close_marker.is_file()
        assert close_attempts.read_text(encoding="utf-8").strip() == "1"
    finally:
        signal.signal(termination_signal, original_handler)


def test_missing_close_environment_fails_fast_without_raw_stderr(
    tmp_path: Path,
) -> None:
    """Missing lease configuration must fail immediately and never echo stderr."""
    hostile_stderr = (
        "session-supervisor: missing required hook environment: SESSION_STREAM_ID\n"
        "SECRET_TOKEN_MUST_NOT_LEAK=s3kr1t\n"
        "`rm -rf /nonexistent`\n"
        "$(echo injected)\n"
    )
    launcher, close_attempts, close_marker, _child_started = _core_driver_exit_fixture(
        tmp_path,
        provider_body="exit 0",
        failed_close_attempts=2,
        close_stderr=hostile_stderr,
    )
    env = launcher_environment()
    result = _run_launcher_process(
        ["bash", os.fspath(launcher), "--epic", "devops"],
        cwd=launcher.parent,
        env=env,
        timeout=30,
    )

    assert result.returncode == 1
    assert not close_marker.is_file()
    assert close_attempts.read_text(encoding="utf-8").strip() == "1"
    assert "close_failure_reason=missing-required-environment" in result.stderr
    combined = result.stdout + result.stderr
    assert "SECRET_TOKEN_MUST_NOT_LEAK" not in combined
    assert "rm -rf /nonexistent" not in combined
    assert "echo injected" not in combined


def test_provider_nonzero_with_close_failure_keeps_provider_exit_and_classifies(
    tmp_path: Path,
) -> None:
    """#671: a failed canary/provider run must not have its exit code stolen by close."""
    hostile_stderr = "session-supervisor: LEASE LOST: Monitor fenced the exact lease\n`id`\n"
    launcher, close_attempts, close_marker, child_started = _core_driver_exit_fixture(
        tmp_path,
        provider_body=f"touch {os.fspath(tmp_path / 'child-started')!r}\nexit 7",
        failed_close_attempts=2,
        close_stderr=hostile_stderr,
    )
    env = launcher_environment()
    result = _run_launcher_process(
        ["bash", os.fspath(launcher), "--epic", "devops"],
        cwd=launcher.parent,
        env=env,
        timeout=30,
    )

    assert result.returncode == 7, result.stdout + result.stderr
    assert child_started.is_file()
    assert not close_marker.is_file()
    assert close_attempts.read_text(encoding="utf-8").strip() == "1"
    assert "close_failure_reason=lease-fenced" in result.stderr
    assert "`id`" not in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    ("termination_signal", "expected_exit"),
    ((signal.SIGTERM, 143), (signal.SIGHUP, 129)),
)
def test_signal_forwarding_with_close_failure_keeps_signal_exit_and_classifies(
    tmp_path: Path,
    termination_signal: signal.Signals,
    expected_exit: int,
) -> None:
    """#671: a forwarded TERM/HUP exit code must not become the close failure's."""
    signal_marker = tmp_path / "signal-received"
    child_ready = tmp_path / "child-ready"
    forward_ready = tmp_path / "forward-ready"
    hostile_stderr = "session-supervisor: Monitor API unreachable; no remote claim was made\n$(evil)\n"
    launcher, close_attempts, close_marker, _child_started = _core_driver_exit_fixture(
        tmp_path,
        provider_body=_blocking_signal_provider(signal_marker, child_ready),
        failed_close_attempts=100,
        close_stderr=hostile_stderr,
        forward_ready=forward_ready,
    )
    env = launcher_environment()
    env["LC_DRIVER_CLOSE_RETRY_SECONDS"] = "1"
    original_handler = signal.signal(termination_signal, signal.SIG_DFL)
    try:
        with subprocess.Popen(
            ["bash", os.fspath(launcher), "--epic", "devops"],
            cwd=launcher.parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                _wait_for_launcher_readiness(process, [child_ready, forward_ready])
                process.send_signal(termination_signal)
                stdout, stderr = process.communicate(timeout=_LAUNCHER_READY_TIMEOUT_S)
            except BaseException:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                raise
        assert signal_marker.is_file(), (
            f"provider child never received forwarded signal (rc={process.returncode}):\n{stdout}{stderr}"
        )
        assert process.returncode == expected_exit, stdout + stderr
        assert not close_marker.is_file()
        assert int(close_attempts.read_text(encoding="utf-8").strip()) in {1, 2}
        assert "close_failure_reason=monitor-unreachable" in stderr
        assert "$(evil)" not in (stdout + stderr)
    finally:
        signal.signal(termination_signal, original_handler)


def test_renew_loops_spawn_no_orphanable_sleeps() -> None:
    """#7832: renew subshells must not create children that can outlive them.

    A background `sleep &` orphaned between its spawn and the `$!` capture (or
    a long foreground `sleep` in an untrapped subshell) survives the launcher's
    TERM teardown and holds the launcher's output pipes open past any caller
    timeout. The subshells wait on anonymous FIFO slices instead.
    """
    body = (machinery_path('lib/launcher_core.sh')).read_text(encoding="utf-8")
    for name in ("launcher_driver_renew_loop",):
        section = body.split(f"{name}() {{", 1)[1].split("\n}", 1)[0]
        subshell = section.split("(\n", 1)[1].rsplit(") &", 1)[0]
        assert not re.search(r"sleep[^\n|;]*&", subshell), f"{name} backgrounds a sleep"
        assert "$!" not in subshell, f"{name} captures a background child PID"
        assert not re.search(r"^\s+sleep (?!0\.1\b)", subshell, re.MULTILINE), f"{name} runs a long foreground sleep"


def test_driver_signal_between_watcher_spawn_and_pid_capture_reaps_children(tmp_path: Path) -> None:
    """A startup signal cannot orphan a watcher holding the output pipe open."""
    child_started = tmp_path / "child-started"
    signal_marker = tmp_path / "signal-received"
    launcher, close_attempts, close_marker, _ = _core_driver_exit_fixture(
        tmp_path,
        provider_body=(
            f"trap 'touch {os.fspath(signal_marker)!r}; exit 0' TERM\n"
            f"touch {os.fspath(child_started)!r}\n"
            "while :; do sleep 0.1; done\n"
        ),
    )
    helper = launcher.parent / "lu_machinery/lib/session_supervisor.sh"
    # Inject at the exact scheduling boundary instead of depending on CI load.
    body = helper.read_text(encoding="utf-8")
    boundary = "  LC_SUPERVISORY_WATCH_PID=$!"
    assert body.count(boundary) == 1
    helper.write_text(
        body.replace(
            boundary,
            f'  while [ ! -f {os.fspath(child_started)!r} ]; do sleep 0.01; done\n  kill -TERM "$$"\n' + boundary,
        ),
        encoding="utf-8",
    )
    env = launcher_environment()
    process = subprocess.Popen(
        ["bash", os.fspath(launcher), "--epic", "devops"],
        cwd=launcher.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=_LAUNCHER_READY_TIMEOUT_S)
        assert process.returncode == 143, stdout + stderr
        assert signal_marker.is_file()
        assert close_marker.is_file()
        assert close_attempts.read_text(encoding="utf-8").strip() == "1"
    finally:
        # Also reap fixture descendants if the regression leaves an orphan.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=_LAUNCHER_READY_TIMEOUT_S)


def test_real_store_driver_close_successor_and_expired_recovery(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    copy_launcher_sources(root)
    install_scope_sandbox(root)
    install_loader_bypass(root)
    # The fixture has no package.json; the deploy gate is not under test here.
    (root / "lu_machinery/lib/deploy_extensions.sh").write_text("deploy_agent_extensions() { :; }\n", encoding="utf-8")
    _install_claude_usage_fixture(root)
    watcher = root / "lu_machinery" / "ai_agent_bridge" / "inbox_watch.sh"
    watcher.parent.mkdir(parents=True, exist_ok=True)
    watcher.write_text("#!/usr/bin/env bash\nexec sleep 300\n", encoding="utf-8")
    watcher.chmod(0o755)
    python_bin = root / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.write_text(
        f'#!/usr/bin/env bash\nexec {sys.executable!r} "$@"\n',
        encoding="utf-8",
    )
    python_bin.chmod(0o755)
    provider = root / "provider.sh"
    provider.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    provider.chmod(0o755)
    adapter = root / "lu_machinery" / "launchers" / "claude.sh"
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(
        f"""#!/usr/bin/env bash
launcher_adapter_validate() {{ :; }}
launcher_adapter_preflight() {{ :; }}
launcher_adapter_canary() {{ return 0; }}
launcher_adapter_exec() {{ launcher_exec_command {os.fspath(provider)!r}; }}
""",
        encoding="utf-8",
    )
    adapter.chmod(0o755)
    subprocess.run(["git", "init", "-q", "-b", "main", os.fspath(root)], check=True, timeout=30, env=launcher_environment())
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "Initial launcher fixture",
        ],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=30,
        env=launcher_environment(),
    )
    env = launcher_environment()
    env["PYTHONPATH"] = os.fspath(REPO)
    env["LEARN_UK_REPO_ROOT"] = os.fspath(root)
    store = SessionStreamStore(SessionStreamDatabase(root / ".agent/session-streams/v1/session-streams.sqlite3"))
    with epics_monitor_stub(store) as monitor_url:
        env["LU_MONITOR_LOOPBACK"] = env["DELEGATE_MONITOR_API"] = monitor_url
        bound = bind_launcher_environment(root, env)
        result = _run_launcher_process(
            ["bash", os.fspath(root / "start-claude-driver.sh"), "--epic", "devops"],
            cwd=root,
            env=bound,
            timeout=30,
        )
    assert result.returncode == 0, result.stderr + result.stdout

    history = store.dump_stream("epic:5703")
    assert history["sessions"][0]["state"] == "closed"
    assert history["lease"]["state"] == "released"
    assert [event["event_type"] for event in history["lease_events"]] == ["acquired", "released"]

    supervisor = SessionSupervisor(store, repo_root=root)
    successor = supervisor.open_driver(
        role=LaunchRole.DRIVER,
        stream_id="epic:5703",
        holder=LeaseHolder(
            agent="codex",
            harness="codex-cli",
            instance_id="successor-after-clean-close",
            process_id=os.getpid(),
        ),
        lineage_id="lineage-successor-after-clean-close",
        ttl_seconds=300,
    )
    assert successor.generation == 2
    assert successor.fencing_token == 2
    assert supervisor.close_driver(role=LaunchRole.DRIVER, lease=successor) == "closed"

    expired_stream = "epic:4708"
    store.open_session(
        stream_id=expired_stream,
        holder=LeaseHolder(
            agent="claude",
            harness="claude-code",
            instance_id="expired-dead-holder",
            process_id=999_999_009,
        ),
        lineage_id="lineage-expired-dead-holder",
        ttl_seconds=30,
        now=utc_now() - timedelta(minutes=5),
    )
    rerouted = supervisor.open_driver(
        role=LaunchRole.DRIVER,
        stream_id=expired_stream,
        holder=LeaseHolder(
            agent="codex",
            harness="codex-cli",
            instance_id="ttl-reroute-successor",
            process_id=os.getpid(),
        ),
        lineage_id="lineage-ttl-reroute-successor",
        ttl_seconds=300,
    )
    assert rerouted.generation == 2
    assert rerouted.fencing_token == 2
    recovered_history = store.dump_stream(expired_stream)
    assert [session["state"] for session in recovered_history["sessions"]] == ["closed", "open"]
    assert "force_closed" in [event["event_type"] for event in recovered_history["lease_events"]]
    assert supervisor.close_driver(role=LaunchRole.DRIVER, lease=rerouted) == "closed"


def test_linked_worktree_helper_calls_use_durable_helper_python(tmp_path: Path) -> None:
    """Lease close uses the bound interpreter when the linked worktree has no .venv."""
    session_root = tmp_path / "worktree"
    session_root.mkdir()  # deliberately no session_root/.venv
    helper_root = tmp_path / "primary"
    log = tmp_path / "invocations.log"
    stub = helper_root / ".venv" / "bin" / "python"
    stub.parent.mkdir(parents=True)
    stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >> {log}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    script = f"""
set -euo pipefail
LC_PROVIDER=claude
LC_MODE=driver
LC_DRY_RUN=0
LC_EPIC=devops
LC_ROOT={session_root}
LC_SESSION_ROOT={session_root}
LC_DURABLE_HELPER_ROOT={helper_root}
source {machinery_path('lib/launcher_core.sh')}
launcher_close_driver_lease
"""
    result = _run_launcher_process(["bash", "-c", script], timeout=30, env=launcher_environment(LU_PYTHON=str(stub)))
    assert result.returncode == 0, result.stderr
    body = log.read_text(encoding="utf-8")
    assert "-m\nlu_machinery.session_supervisor\nclose\n--role\ndriver\n" in body


def test_cto_wrapper_start_builds_cto_prompt_not_lane_driver_prompt(tmp_path: Path) -> None:
    """#12496: the CTO seat holds the epics lease but binds lane cto and the cto skill."""
    argv_file = tmp_path / "cto-argv"
    cto = run_launcher(
        "start-claude-cto.sh",
        "--epic",
        "epics",
        "--model",
        "claude-opus-5-5",
        "KICK",
        env={"LAUNCHER_DRY_RUN_ARGV_FILE": str(argv_file)},
    )
    assert cto.returncode == 0, cto.stderr
    assert "launcher: would bind cto after lease" in cto.stdout
    prompt = argv_file.read_bytes().decode().removesuffix("\0").split("\0")[-1]
    assert prompt.startswith("First run lu-driver-state render --lane cto, then lu-driver-state check --lane cto.")
    assert "Load .claude/skills/cto/SKILL.md from the workspace root" in prompt
    assert "agents_extensions/shared/skills/" not in prompt
    assert "/api/rules?scope=task:fleet-comms" in prompt
    assert "fleet-comms-coordination.md" not in prompt
    assert "The launcher already claimed the epics lease" in prompt
    assert "first repeat lu-driver-state render --lane cto and lu-driver-state check --lane cto" in prompt
    assert "drive-epic" not in prompt
    assert "--lane epics" not in prompt
    assert prompt.endswith("Caller instructions:\nKICK")

    # The same lane through the lane-driver launcher keeps the drive-epic binding.
    lane = run_launcher("start-claude-driver.sh", "--epic", "epics")
    assert lane.returncode == 0, lane.stderr
    assert "launcher: would bind drive-epic after lease" in lane.stdout
    lane_prompt = shlex.split(lane.stdout.split("would exec ", 1)[1])[-1]
    assert lane_prompt.startswith(
        "First run lu-driver-state render --lane epics, then lu-driver-state check --lane epics."
    )
    assert "Load .claude/skills/drive-epic/SKILL.md from the workspace root" in lane_prompt


def test_launcher_passes_single_system_prompt_with_core_then_role(tmp_path: Path) -> None:
    """#1589 defect class: a second --append-system-prompt silently drops the rules core, since
    Claude Code keeps only the last addition. Native Claude, the CTO wrapper's role and Codex on
    Claude Code each pass one inline addition, the core then the caller's text, in the caller's
    own argument slot. An argv it cannot prove keeps main's shape; a refusal precedes any lease."""
    role = "You are the operator-appointed CTO AND cross-epic lead."
    role_file = tmp_path / "role.txt"
    role_file.write_text(role, encoding="utf-8")
    argv_file = tmp_path / "argv"
    cto_core_bytes = 0
    cto = ("start-claude-cto.sh", "--epic", "epics", "--model", "claude-opus-5-5", "KICK", "--")
    for args, flag in (
        ((*cto, "--dangerously-skip-permissions", "--append-system-prompt", role), b"--append-system-prompt"),
        (("start-claude.sh", "--", "-c", f"--append-system-prompt={role}", "--resume"), b"--append-system-prompt="),
        (("start-codex.sh", "--harness", "claude-code", "--", "--append-system-prompt", role), b"--append-system-prompt"),
    ):
        argv_file.unlink(missing_ok=True)
        result = run_launcher(*args, env={"LAUNCHER_DRY_RUN_ARGV_FILE": str(argv_file)})
        assert result.returncode == 0, result.stdout + result.stderr
        core_bytes = int(re.search(r"launcher: rules core seat=\S+ bytes=(\d+)", result.stdout).group(1))
        cto_core_bytes = core_bytes if args[0] == "start-claude-cto.sh" else cto_core_bytes
        argv = argv_file.read_bytes().removesuffix(b"\0").split(b"\0")
        additions = [arg for arg in argv if arg.startswith(b"--append-system-prompt")]
        assert argv[0] == b"claude" and len(additions) == 1, (args, [arg[:40] for arg in additions])
        assert additions[0] == flag or (flag.endswith(b"=") and additions[0].startswith(flag)), args
        value = argv[argv.index(flag) + 1] if flag == b"--append-system-prompt" else additions[0][len(flag):]
        core = value[:core_bytes]
        assert core.startswith(b"<rules-core ") and core.rstrip().endswith(b"</rules-core>"), args
        assert value[core_bytes:] == b"\n\n" + role.encode(), args
        # The caller's arguments around its addition keep their places.
        caller = [arg.encode() for arg in args[args.index("--") + 1:]]
        slot = next(index for index, arg in enumerate(caller) if arg.startswith(b"--append-system-prompt"))
        at, width = argv.index(additions[0]), 2 if flag == b"--append-system-prompt" else 1
        assert argv[at - slot:at] == caller[:slot], args
        assert argv[at + width:at + len(caller) - slot] == caller[slot + width:], args
    # An addition the launcher cannot prove keeps main's argv: the core's own addition first,
    # then the caller's arguments unchanged (#1589 residual, as on main).
    # Verify the CTO wrapper once and exercise the remaining shared-core shapes through the
    # lighter interactive wrapper so the test stays well within its CI deadline.
    for prefix, extra in (
        (cto, ("--append-system-prompt-file", str(role_file))),
        (("start-claude.sh", "--"), (f"--append-system-prompt-file={role_file}",)),
        (("start-claude.sh", "--"), ("--append-system-prompt", "one", "--append-system-prompt=two")),
        (("start-claude.sh", "--"), ("--name", "--append-system-prompt", role)),
        (("start-claude.sh", "--"), ("--tools", "--append-system-prompt", role)),
        (("start-claude.sh", "--"), ("--append-system-prompt", role, "--", "question")),
    ):
        argv_file.unlink(missing_ok=True)
        result = run_launcher(*prefix, *extra, env={"LAUNCHER_DRY_RUN_ARGV_FILE": str(argv_file)})
        assert result.returncode == 0, (extra, result.stdout + result.stderr)
        argv = argv_file.read_bytes().removesuffix(b"\0").split(b"\0")
        core = argv[argv.index(b"--append-system-prompt") + 1]
        assert len(core) == cto_core_bytes and core.startswith(b"<rules-core "), extra
        caller = [arg.encode() for arg in extra]
        assert any(argv[at:at + len(caller)] == caller for at in range(len(argv))), extra
    for extra in (
        ("--append-system-prompt",),
        ("--append-system-prompt", "x" * 100_000),
        # The joined slot reaches the per-argument limit only with its flag prefix.
        ("--append-system-prompt=" + "x" * (os.sysconf("SC_PAGE_SIZE") * 32 - 25 - cto_core_bytes),),
    ):
        argv_file.unlink(missing_ok=True)
        result = run_launcher(*cto, *extra, env={"LAUNCHER_DRY_RUN_ARGV_FILE": str(argv_file)})
        assert result.returncode == 2 and "refusing to launch" in result.stderr, (extra[0][:40], result.stderr)
        assert "would claim lease" not in result.stdout and not argv_file.exists(), extra[0][:40]


def test_codex_keeps_core_before_caller_developer_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: a caller developer_instructions setting replaces the rules core (sibling of #1559).

    Both the native Codex launcher and dispatcher runtime_context merge a single provable
    developer_instructions setting (plain text or a TOML 1.0 string) in place as core, a
    blank line, then the caller's text (TOML-encoded), keep main's argv for every other
    shape (containers, numbers, dates and times, TOML 1.1-only forms, invalid quoting), and
    refuse missing-value or over-limit settings before any side effect."""
    from lu_machinery.dispatcher import runtime_context
    from lu_machinery.dispatcher.types import Invocation, TaskSpec
    from lu_machinery.lib import rules_core

    def _dev_settings(argv: list[str]) -> list[tuple[int, str, str]]:
        found: list[tuple[int, str, str]] = []
        for idx, arg in enumerate(argv):
            if arg in ("-c", "--config") and idx + 1 < len(argv):
                payload = argv[idx + 1]
                if runtime_context.classify_codex_config_payload(payload) is not None:
                    found.append((idx + 1, "", payload))
            else:
                for pfx in ("--config=", "-c=", "-c"):
                    if arg.startswith(pfx) and len(arg) > len(pfx):
                        payload = arg[len(pfx):]
                        if runtime_context.classify_codex_config_payload(payload) is not None:
                            found.append((idx, pfx, payload))
                        break
        return found

    role = 'You are the CTO.\nStay "in" scope.'
    argv_file = tmp_path / "argv"
    cto = ("start-codex-cto.sh", "--epic", "epics", "KICK", "--")
    cto_core_toml_bytes = 0
    # End-to-end launcher runs prove every Codex wrapper (CTO, interactive, driver) wires
    # launcher_codex_developer_instructions into its real argv, dry-run placeholder and
    # pre-lease refusal gates.
    for args, expected_role, expected_prefix in (
        ((*cto, "--search", "-c", f"developer_instructions={rules_core.toml_basic_string(role)}", "--no-alt-screen"), role, ""),
        (("start-codex.sh", "--", "--config", "developer_instructions=Raw caller text", "--search"), "Raw caller text", ""),
        (("start-codex-driver.sh", "--epic", "devops", "-c", "developer_instructions=Driver role", "Driver task"), "Driver role", ""),
    ):
        argv_file.unlink(missing_ok=True)
        result = run_launcher(*args, env={"LAUNCHER_DRY_RUN_ARGV_FILE": str(argv_file)})
        assert result.returncode == 0, result.stdout + result.stderr
        core_bytes = int(re.search(r"launcher: rules core seat=\S+ bytes=(\d+)", result.stdout).group(1))
        would_exec = result.stdout.split("would exec ", 1)[1]
        assert f"rules-core\\ seat=core\\ bytes={core_bytes}" in would_exec or f"rules-core seat=core bytes={core_bytes}" in would_exec, args
        assert '<rules-core seat="core"' not in result.stdout
        argv = [arg.decode("utf-8") for arg in argv_file.read_bytes().removesuffix(b"\0").split(b"\0")]
        assert argv[0] == "codex", args
        settings = _dev_settings(argv)
        assert len(settings) == 1, (args, settings)
        slot_idx, pfx, payload = settings[0]
        assert pfx == expected_prefix, args
        decoded = tomllib.loads(payload)["developer_instructions"]
        core_part = decoded.encode("utf-8")[:core_bytes].decode("utf-8")
        assert core_part.startswith("<rules-core ") and core_part.rstrip().endswith("</rules-core>"), args
        assert decoded[len(core_part):] == "\n\n" + expected_role, args
        if args[0] == "start-codex-cto.sh":
            cto_core_toml_bytes = len(rules_core.toml_basic_string(core_part).encode("utf-8"))
        # Caller arguments around the rewritten slot keep their exact places.
        if "--" in args:
            caller = list(args[args.index("--") + 1:])
            rel = next(
                i for i, a in enumerate(caller)
                if (
                    runtime_context.classify_codex_config_payload(a if expected_prefix == "" else a[len(expected_prefix):])
                    or ("", None)
                )[0] == "setting"
                and (expected_prefix == "" or a.startswith(expected_prefix))
            )
            flag_rel = rel - 1 if expected_prefix == "" else rel
            flag_at = slot_idx - 1 if expected_prefix == "" else slot_idx
            width = 2 if expected_prefix == "" else 1
            assert argv[flag_at - flag_rel:flag_at] == caller[:flag_rel], args
            assert argv[flag_at + width:flag_at + width + len(caller) - flag_rel - width] == caller[flag_rel + width:], args

    # Unprovable shape keeps main's argv end to end on the CTO wrapper.
    unprovable_cto = ("-c", "developer_instructions=one", "-c", "developer_instructions=two")
    argv_file.unlink(missing_ok=True)
    result = run_launcher(*cto, *unprovable_cto, env={"LAUNCHER_DRY_RUN_ARGV_FILE": str(argv_file)})
    assert result.returncode == 0, (unprovable_cto, result.stdout + result.stderr)
    argv = [arg.decode("utf-8") for arg in argv_file.read_bytes().removesuffix(b"\0").split(b"\0")]
    first_c = next(
        i for i, a in enumerate(argv[:-1])
        if a == "-c" and argv[i + 1].startswith("developer_instructions=")
    )
    core_val = tomllib.loads(argv[first_c + 1])["developer_instructions"]
    assert core_val.startswith("<rules-core ") and core_val.rstrip().endswith("</rules-core>"), unprovable_cto
    assert any(argv[at:at + len(unprovable_cto)] == list(unprovable_cto) for at in range(len(argv))), unprovable_cto

    # Provable missing value and exact MAX_ARG_STRLEN boundary refuse with exit 2 before lease/exec.
    page_limit = os.sysconf("SC_PAGE_SIZE") * 32
    # Joined slot at exact MAX_ARG_STRLEN: "--config=developer_instructions=" (32)
    # + core TOML (cto_core_toml_bytes, includes quotes) + "\\n\\n" (4) + filler == page_limit.
    joined_boundary_filler = "x" * (page_limit - len("--config=developer_instructions=") - cto_core_toml_bytes - 4)
    for extra in (
        ("-c", "developer_instructions"),
        (f"--config=developer_instructions={joined_boundary_filler}",),
    ):
        argv_file.unlink(missing_ok=True)
        result = run_launcher(*cto, *extra, env={"LAUNCHER_DRY_RUN_ARGV_FILE": str(argv_file)})
        assert result.returncode == 2 and "refusing to launch" in result.stderr, (extra[0][:40], result.stderr)
        assert "would claim lease" not in result.stdout and not argv_file.exists(), extra[0][:40]

    # Exercise the remaining bash flag spellings, argv-structure branches and refusal shapes
    # directly against the real launcher_codex_developer_instructions and codex.sh arity table
    # in one shell session, loading the real rules core once instead of booting the full
    # launcher wrapper 30+ times.
    expected_core = rules_core.core_block(rules_core.resolve_seat())
    shell_merge_cases = (
        (("--search", f"--config=developer_instructions={rules_core.toml_basic_string(role)}"), role, "--config="),
        (("-c=developer_instructions='Literal \\n text'",), r"Literal \n text", "-c="),
        (("-cdeveloper_instructions=Attached short",), "Attached short", "-c"),
        (("-c", "developer_instructions="), "", ""),
        # Round 1 & 2 F1: Unicode White_Space key padding, whitespace before dot left
        # unrelated, trimmed multiline quoted value.
        (("-c", "\u00a0developer_instructions\u00a0=Sample"), "Sample", ""),
        (("-c", "developer_instructions .sub=other", "-c", "developer_instructions=Sample"), "Sample", ""),
        (("-c", 'developer_instructions=\n"Sample"'), "Sample", ""),
    )
    shell_unprovable_cases = (
        ("-p", "-c", "developer_instructions=role"),
        ("--enable", "-cdeveloper_instructions=role"),
        ("-c", "-c", "developer_instructions=role"),
        ("--unknown-opt", "-c", "developer_instructions=role"),
        ("-c", "developer_instructions=role", "--", "question"),
        ("--", "-c", "developer_instructions=role"),
        # Representative decoder-refused and classifier-other shapes through bash NUL piping;
        # the full decoder value matrix is exercised in-process via runtime_context.build below.
        ("-c", "developer_instructions=123"),
        ("-c", 'developer_instructions="""Sample\r\nb"""'),
        ("-c", "\x1cdeveloper_instructions=Sample"),
        ("-c", "developer_instructions.sub=x"),
    )
    shell_refusal_cases = (
        ("--config=developer_instructions",),
        ("-cdeveloper_instructions",),
        ("-c", "developer_instructions=" + "x" * 100_000),
    )
    shell_out = tmp_path / "codex-shell-matrix"
    shell_out.mkdir()
    py_no_bc = tmp_path / "py-no-bc"
    py_no_bc.write_text(f"#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} -B \"$@\"\n", encoding="utf-8")
    py_no_bc.chmod(0o755)
    shell_lines = [
        "set -euo pipefail",
        f"source {shlex.quote(str(machinery_path('lib/launcher_core.sh')))}",
        f"source {shlex.quote(str(machinery_path('launchers/codex.sh')))}",
        f"source {shlex.quote(str(machinery_path('lib/rules_core.sh')))}",
        f"launcher_project_python() {{ printf '%s\\n' {shlex.quote(str(py_no_bc))}; }}",
        "LC_PROVIDER=codex LC_MODE=interactive LC_HARNESS=codex LC_DRY_RUN=1",
        f"rules_core_load {shlex.quote(str(py_no_bc))} '' codex",
        "run_ok() {",
        '  local out="$1"; shift',
        '  LC_FORWARD_ARGS=("$@")',
        "  LC_CODEX_DEVELOPER_INSTRUCTIONS_ARGS=()",
        '  LC_RULES_CORE_TOML=""',
        "  launcher_codex_developer_instructions",
        '  if [ "${#LC_CODEX_DEVELOPER_INSTRUCTIONS_ARGS[@]}" -gt 0 ]; then printf "%s\\0" "${LC_CODEX_DEVELOPER_INSTRUCTIONS_ARGS[@]}" > "$out.core"; else : > "$out.core"; fi',
        '  if [ "${#LC_FORWARD_ARGS[@]}" -gt 0 ]; then printf "%s\\0" "${LC_FORWARD_ARGS[@]}" > "$out.fwd"; else : > "$out.fwd"; fi',
        "}",
        "run_refuse() {",
        '  local out="$1"; shift',
        "  set +e",
        '  (LC_FORWARD_ARGS=("$@"); LC_CODEX_DEVELOPER_INSTRUCTIONS_ARGS=(); LC_RULES_CORE_TOML=""; launcher_codex_developer_instructions) >"$out.out" 2>"$out.err"',
        '  printf "%s\\n" "$?" > "$out.rc"',
        "  set -e",
        "}",
    ]
    for idx, (case_args, _, _) in enumerate(shell_merge_cases):
        shell_lines.append("run_ok " + shlex.join([str(shell_out / f"merge-{idx}"), *case_args]))
    for idx, case_args in enumerate(shell_unprovable_cases):
        shell_lines.append("run_ok " + shlex.join([str(shell_out / f"unprov-{idx}"), *case_args]))
    for idx, case_args in enumerate(shell_refusal_cases):
        shell_lines.append("run_refuse " + shlex.join([str(shell_out / f"refuse-{idx}"), *case_args]))
    matrix_result = _run_launcher_process(
        ["bash", "-c", "\n".join(shell_lines)],
        cwd=tmp_path,
        env=launcher_environment(),
        timeout=60,
    )
    assert matrix_result.returncode == 0, matrix_result.stdout + matrix_result.stderr

    def _read_nul(path: Path) -> list[str]:
        raw = path.read_bytes()
        return [] if not raw else [part.decode("utf-8") for part in raw.removesuffix(b"\0").split(b"\0")]

    for idx, (case_args, expected_role, expected_prefix) in enumerate(shell_merge_cases):
        core_args = _read_nul(shell_out / f"merge-{idx}.core")
        fwd_args = _read_nul(shell_out / f"merge-{idx}.fwd")
        assert core_args == [], (case_args, core_args)
        settings = _dev_settings(fwd_args)
        assert len(settings) == 1, (case_args, settings)
        slot_idx, pfx, payload = settings[0]
        assert pfx == expected_prefix, case_args
        assert tomllib.loads(payload)["developer_instructions"] == f"{expected_core}\n\n{expected_role}", case_args
        caller = list(case_args)
        rel = next(
            i for i, a in enumerate(caller)
            if (
                runtime_context.classify_codex_config_payload(a if expected_prefix == "" else a[len(expected_prefix):])
                or ("", None)
            )[0] == "setting"
            and (expected_prefix == "" or a.startswith(expected_prefix))
        )
        flag_rel = rel - 1 if expected_prefix == "" else rel
        flag_at = slot_idx - 1 if expected_prefix == "" else slot_idx
        width = 2 if expected_prefix == "" else 1
        assert fwd_args[:flag_at] == caller[:flag_rel], case_args
        assert fwd_args[flag_at + width:] == caller[flag_rel + width:], case_args

    for idx, case_args in enumerate(shell_unprovable_cases):
        core_args = _read_nul(shell_out / f"unprov-{idx}.core")
        fwd_args = _read_nul(shell_out / f"unprov-{idx}.fwd")
        assert fwd_args == list(case_args), (case_args, fwd_args)
        assert len(core_args) == 2 and core_args[0] == "-c", (case_args, core_args)
        assert tomllib.loads(core_args[1])["developer_instructions"] == expected_core, case_args

    for idx, case_args in enumerate(shell_refusal_cases):
        rc = int((shell_out / f"refuse-{idx}.rc").read_text(encoding="utf-8").strip())
        err = (shell_out / f"refuse-{idx}.err").read_text(encoding="utf-8")
        assert rc == 2 and "refusing to launch" in err, (case_args[0][:40], rc, err)

    # Dispatcher runtime_context.build enforces the same ruling C contract for Codex workers.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_bytes(b"Do the task.\n")
    spec = TaskSpec("codex-core", "codex", CURRENT_SOL, "high", "workspace-write", worktree, brief, tmp_path / "final")
    base_argv = ["codex", "exec", "--json", "-C", str(worktree), "-m", CURRENT_SOL, "--dangerously-bypass-approvals-and-sandbox"]

    def _make_stub_provider(extra_args: list[str]):
        class _StubProvider:
            @staticmethod
            def build(_spec: TaskSpec, env: dict[str, str]) -> Invocation:
                return Invocation([*base_argv, *extra_args], b"Do the task.\n", env, tmp_path / "final")

        return _StubProvider

    for extra, expected_role, expected_prefix in (
        (["-c", f"developer_instructions={rules_core.toml_basic_string(role)}"], role, ""),
        (["--config=developer_instructions=Worker raw"], "Worker raw", "--config="),
        (["-c", "developer_instructions="], "", ""),
        (["-c", "\u00a0developer_instructions\u00a0=Sample"], "Sample", ""),
        (["-c", "developer_instructions .sub=other", "-c", "developer_instructions=Sample"], "Sample", ""),
        (["-c", 'developer_instructions=\n"Sample"'], "Sample", ""),
    ):
        provider = _make_stub_provider(extra)
        monkeypatch.setattr(runtime_context.providers, "load", lambda _agent, _p=provider: _p)
        inv = runtime_context.build(spec, {})
        settings = _dev_settings(inv.argv)
        assert len(settings) == 1, (extra, settings)
        assert settings[0][1] == expected_prefix
        assert tomllib.loads(settings[0][2])["developer_instructions"] == f"{expected_core}\n\n{expected_role}"

    for extra in (
        ["-c", "developer_instructions=one", "-c", "developer_instructions=two"],
        ["-p", "-c", "developer_instructions=role"],
        ["--", "-c", "developer_instructions=role"],
        ["-c", "developer_instructions=123"],
        ["-c", "developer_instructions=true"],
        ["-c", 'developer_instructions={ note = "sample", }'],
        ["-c", 'developer_instructions="""Sample\r\nb"""'],
        ["-c", "developer_instructions='''Sample\r\nb'''"],
        ["-c", "developer_instructions=" + "[" * 100 + "]" * 100],
        ["-c", "developer_instructions=12:34"],
        ["-c", r'developer_instructions="\e\x41"'],
        ["-c", 'developer_instructions="Sample'],
        ["-c", "developer_instructions=12:34:56 # note"],
        ["-c", f"developer_instructions={2**63}"],
        ["-c", "developer_instructions=" + "9" * 4500],
        ["-c", "developer_instructions=1e400"],
        ["-c", "developer_instructions=2026-11-28"],
        ["-c", "developer_instructions=inf"],
        ["-c", "\x1cdeveloper_instructions=Sample"],
        ["-c", "developer_instructions.sub=x"],
    ):
        provider = _make_stub_provider(extra)
        monkeypatch.setattr(runtime_context.providers, "load", lambda _agent, _p=provider: _p)
        inv = runtime_context.build(spec, {})
        assert inv.argv[:len(base_argv) + len(extra)] == [*base_argv, *extra]
        assert inv.argv[-2] == "-c"
        assert tomllib.loads(inv.argv[-1])["developer_instructions"] == expected_core

    for extra in (
        ["-c", "developer_instructions"],
        ["--config=developer_instructions"],
        ["-c", "developer_instructions=" + "x" * 100_000],
    ):
        provider = _make_stub_provider(extra)
        monkeypatch.setattr(runtime_context.providers, "load", lambda _agent, _p=provider: _p)
        with pytest.raises(ValueError, match="refusing to launch"):
            runtime_context.build(spec, {})

    # Round 1 F3: dispatch admission validates the Codex core slot before reserving worktrees/leases.
    from contextlib import nullcontext
    from types import SimpleNamespace

    from lu_machinery.dispatcher import cli as dispatcher_cli

    runtime_context.check_codex_core_arg_length()
    monkeypatch.setattr(rules_core, "core_block", lambda seat="core", root=None: "x" * page_limit)
    monkeypatch.setattr(dispatcher_cli.admission, "record_refusal", lambda *a, **kw: nullcontext())
    with pytest.raises(ValueError, match="refusing to launch"):
        dispatcher_cli._dispatch(
            SimpleNamespace(
                agent="codex",
                model=CURRENT_SOL,
                mode="read-only",
                prompt="hi",
                prompt_file=None,
                review_attempt=None,
                require_review_verdict=False,
                owned_path=(),
                pr=None,
                repo=None,
                initiator=None,
                print_advisory_binding=False,
            )
        )


def test_fleet_cold_clause_keeps_lane_drivers_on_drive_epic(tmp_path: Path) -> None:
    """#12496: only the CTO seat names another skill; the lane-driver clause is unchanged."""
    script = (
        'source "$1"; printf "%s\\0%s\\0%s" '
        '"$(fleet_comms_cold_clause)" "$(fleet_comms_cold_clause drive-epic)" "$(fleet_comms_cold_clause cto)"'
    )
    offline_rule = tmp_path / ".agent/rules/fleet-comms-coordination.md"
    offline_rule.parent.mkdir(parents=True)
    shutil.copyfile(machinery_path("agents_extensions/shared/rules/fleet-comms-coordination.md"), offline_rule)
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(machinery_path('lib/fleet_comms_cold_start.sh'))],
        env={**launcher_environment(), "FLEET_COMMS_PLANE_MODE": "authority", "LC_ROOT": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    default, lane, cto = result.stdout.split("\0")
    assert default == lane
    assert "read /api/rules?scope=task:fleet-comms." in lane
    assert "Offline, read .agent/rules/fleet-comms-coordination.md from the workspace root." in lane
    assert "agents_extensions/shared/rules/" not in lane
    assert "Method playbook: load skill drive-epic (provider drivers inject the binding after lease + canary). " in lane
    assert cto == lane.replace("load skill drive-epic", "load skill cto")
    # #12671: the closeout sentence names the proof that still exists.
    closeout = (
        "Closeout: use the task-prescribed interpreter with "
        "`-m lu_machinery.orchestration.merge_closeout <N> --repo <owner/name> --repo-root <checkout> --apply`; "
        "the cleanup proof is that command exiting 0 after the worker's task has settled "
        "(its worktree removed by the dispatcher). "
    )
    assert closeout in lane
    assert closeout in cto
    assert "common-reaper" not in result.stdout
    assert "post-task-reaper" not in result.stdout


@pytest.mark.rules_core_absent
def test_claude_driver_injects_lane_agent_type() -> None:
    """--epic <lane> selects the lane's driver_agent_type from area_assignments.yaml (#F1, prompt audit)."""
    result = run_launcher("start-claude-driver.sh", "--epic", "infra")
    assert result.returncode == 0, result.stderr
    assert "launcher: would select agent infra-orchestrator for lane infra" in result.stdout
    # Opus is always blocked, so the defaulted Opus driver seat runs on Sonnet.
    command = shlex.split(next(line for line in result.stdout.splitlines() if line.startswith("would exec ")))
    assert command[:9] == [
        "would",
        "exec",
        "claude",
        "--model",
        "claude-sonnet-5-5",
        "--effort",
        "high",
        "--agent",
        "infra-orchestrator",
    ]

    explicit = run_launcher("start-claude-driver.sh", "--epic", "infra", "--agent", "curriculum-orchestrator")
    assert explicit.returncode == 0, explicit.stderr
    assert "would select agent" not in explicit.stdout
    assert "--effort high --agent curriculum-orchestrator " in explicit.stdout

    # A registry stream key with no roster slot would mint an unregistered handoff
    # identity, so the launcher refuses it instead of starting the session (#8303).
    alias = run_launcher("start-claude-driver.sh", "--epic", "atlas-practice")
    assert alias.returncode == 2
    assert "claude-atlas-practice" in alias.stderr
    assert "not registered" in alias.stderr


@pytest.mark.parametrize("reason", ["Monitor API unreachable", "Monitor API returned an error (503)"])
@pytest.mark.parametrize("recover", [True, False])
def test_close_waits_with_capped_backoff_and_safe_exhaustion(tmp_path: Path, reason: str, recover: bool) -> None:
    launcher, attempts, closed, _ = _core_driver_exit_fixture(
        tmp_path,
        provider_body="exit 0",
        failed_close_attempts=8 if recover else 100,
        close_stderr=reason + "\n$(hostile-secret)",
    )
    # Advance Bash's elapsed clock deterministically; verify the production
    # ten-minute budget without spending ten minutes on every regression run.
    script = f"""
source {launcher.parent / "lu_machinery/lib/launcher_core.sh"}
LC_PROVIDER=claude
LC_SESSION_ROOT={launcher.parent}
LC_DURABLE_HELPER_ROOT={launcher.parent}
unset LC_DRIVER_CLOSE_RETRY_SECONDS SECONDS
SECONDS=0
sleep() {{ printf 'delay=%s\n' "$1"; SECONDS=$((SECONDS + $1)); }}
launcher_close_driver_lease
"""
    result = _run_launcher_process(
        ["bash", "-c", script], cwd=REPO, timeout=10,
        env=launcher_environment(LU_PYTHON=str(launcher.parent / ".venv/bin/python")),
    )
    assert result.returncode == (0 if recover else 1), result.stderr
    delays = [int(line.split("=")[1]) for line in result.stdout.splitlines()]
    assert delays[:6] == [1, 2, 4, 8, 16, 30]
    assert max(delays) == 30
    assert int(attempts.read_text()) > 2
    assert closed.exists() is recover
    assert "waiting for Monitor API to recover (close attempt 3)" in result.stderr
    assert "hostile-secret" not in result.stdout + result.stderr
    if not recover:
        assert sum(delays) == 600
        expected = "monitor-unreachable" if "unreachable" in reason else "monitor-error"
        assert f"close_failure_reason={expected}" in result.stderr


@pytest.mark.parametrize("watcher_exit", [76, 2, 0, 75])
@pytest.mark.parametrize("notify", [False, True])
def test_watcher_exit_recovery_preserves_provider_and_rejects_false_wakes(
    tmp_path: Path,
    watcher_exit: int,
    notify: bool,
) -> None:
    completed = tmp_path / "provider-completed"
    restarted = tmp_path / "watcher-restarted"
    first = tmp_path / "watcher-first"
    # The provider outlives the first watcher by up to one second, as before, but
    # finishes as soon as a recovered watcher has restarted.
    launcher, _, closed, _ = _core_driver_exit_fixture(
        tmp_path,
        provider_body=(
            f"for _ in $(seq 100); do [ -f {str(restarted)!r} ] && break; sleep 0.01; done\n"
            f"touch {str(completed)!r}\nexit 0"
        ),
    )
    watcher = launcher.parent / "lu_machinery/ai_agent_bridge/inbox_watch.sh"
    watcher.write_text(
        "#!/usr/bin/env bash\n"
        f"if [ -f {str(first)!r} ]; then touch {str(restarted)!r}; exec sleep 300; fi\n"
        f"touch {str(first)!r}\n" + ('kill -USR1 "$PPID"\n' if notify else "") + f"exit {watcher_exit}\n",
    )
    result = _run_launcher_process(
        ["bash", str(launcher), "--epic", "devops"],
        cwd=launcher.parent,
        timeout=10,
        env=launcher_environment(),
    )
    # Even 75 without a delivery is not a valid prepared wake.
    assert result.returncode == (0 if watcher_exit == 76 else 1), result.stdout + result.stderr
    assert restarted.exists() is (watcher_exit == 76)
    assert completed.exists() is (watcher_exit == 76)
    assert closed.exists()


def test_launcher_death_stops_renewal_with_live_provider(tmp_path: Path) -> None:
    """SIGKILL cannot run cleanup; renewal must stop so normal TTL can expire."""
    owner = tmp_path / "launcher-owner"
    heartbeats = tmp_path / "heartbeats"
    launcher, _, closed, _ = _core_driver_exit_fixture(
        tmp_path,
        provider_body=f'printf "%s\\n" "$PPID" > {shlex.quote(str(owner))}\nwhile :; do sleep 0.1; done',
    )
    python_stub = launcher.parent / ".venv/bin/python"
    body = python_stub.read_text()
    first, rest = body.split("\n", 1)
    python_stub.write_text(
        first
        + "\n"
        + f"""
if [[ "${{1:-}}" == "-I" && "${{2:-}}" == "-c" && "${{3:-}}" == *"module bootstrap:"* ]]; then shift 5; fi
if [[ "${{1:-}}" == -m && "${{2:-}}" == lu_machinery.session_supervisor && "${{3:-}}" == heartbeat ]]; then
  printf 'renew\\n' >> {shlex.quote(str(heartbeats))}
  exit 0
fi
"""
        + rest
    )
    env = launcher_environment()
    env.update(SESSION_STREAM_RENEW_INTERVAL_SECONDS="1", SESSION_STREAM_RENEW_JITTER_SECONDS="0")
    process = subprocess.Popen(
        ["bash", str(launcher), "--epic", "devops"],
        cwd=launcher.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not heartbeats.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.05)
        assert heartbeats.exists() and owner.exists()
        launcher_pid = int(owner.read_text())
        assert launcher_pid > 1 and os.getsid(launcher_pid) == process.pid
        os.kill(launcher_pid, signal.SIGKILL)
        assert process.wait(timeout=10) == 137
        # Give an in-flight request time to finish, then observe two intervals.
        time.sleep(0.3)
        settled = heartbeats.read_text()
        time.sleep(2.2)
        assert heartbeats.read_text() == settled
        assert not closed.exists()  # Recovery is the existing TTL, not cleanup.
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)


@pytest.mark.parametrize(
    ("name", "args", "env", "model", "code"),
    [
        ("start-gemini.sh", [], {}, "gemini-4-hydrogen-medium", 0),
        ("start-gemini.sh", ["--effort", "high"], {}, "gemini-4-hydrogen-high", 0),
        ("start-gemini.sh", [], {"LAUNCHER_EFFORT": "low"}, "gemini-4-hydrogen-low", 0),
        # An effort never replaces a chosen model or names a variant AGY lacks.
        ("start-gemini.sh", ["--model", "gemini-4-neon-high", "--effort", "high"], {}, "gemini-4-neon-high", 0),
        ("start-gemini.sh", ["--model", "gemini-4-neon-high"], {"LAUNCHER_EFFORT": "low"}, "", 2),
        ("start-gemini.sh", ["--effort", "low", "--model", "gemini-4-neon-high"], {}, "", 2),
        ("start-gemini.sh", [], {"LAUNCHER_MODEL": "gemini-3.8-flash-high", "LAUNCHER_EFFORT": "medium"}, "", 2),
        ("start-gemini.sh", ["--effort", "xhigh"], {}, "", 2),
        ("start-gemini.sh", ["--effort", "max"], {}, "", 2),
        ("start-gemini-driver.sh", ["--epic", "infra", "--effort", "low"], {}, "", 4),
        ("start-gemini-driver.sh", ["--epic", "infra"], {"LAUNCHER_EFFORT": "high"}, "", 4),
        ("start-gemini-driver.sh", ["--epic", "infra", "--", "--effort", "low"], {}, "", 4),
        ("start-gemini-driver.sh", ["--epic", "infra", "--", "--effort=low"], {}, "", 4),
        ("start-gemini-driver.sh", ["--epic", "infra", "--", "-effort", "low"], {}, "", 4),
        ("start-gemini-driver.sh", ["--epic", "infra", "--", "-effort=low"], {}, "", 4),
        ("start-gemini-driver.sh", ["--epic", "infra", "--", "-model", "gemini-4-neon-high"], {}, "", 4),
        ("start-gemini-driver.sh", ["--epic", "infra", "--", "-model=gemini-4-neon-high"], {}, "", 4),
        ("start-gemini-driver.sh", ["--epic", "infra", "--effort", "medium"], {}, "gemini-4-hydrogen-medium", 0),
    ],
)
def test_gemini_effort_does_not_conflict_or_bypass_driver_certification(name, args, env, model, code):
    """Conflicting AGY variants and uncertified driver efforts must not reach spawn."""
    result = run_launcher(name, *args, env=env)
    assert result.returncode == code, result.stdout + result.stderr
    if code:
        assert "effort" in result.stderr
        assert "would claim lease" not in result.stdout
        assert "would exec" not in result.stdout
    else:
        command = result.stdout.split("would exec ", 1)[1]
        # launcher_print_argv uses Bash quoting for the multiline rules prompt.
        # The unquoted model/effort prefix is sufficient to pin AGY selection.
        assert command.startswith(f"agy --model {model} ")
        assert not command.startswith(f"agy --model {model} --effort ")
        if name == "start-gemini-driver.sh":
            assert "Load .agents/skills/drive-epic/SKILL.md from the workspace root" in command
