"""Validate AGY guard registration without executing hook commands."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import runpy
import shlex
import subprocess
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import (
    configure_native_hook_roots,
    configured_interpreter,
    machinery_path,
    main_checkout_root,
)
from lu_machinery.common.safe_open import safe_open_below
from lu_machinery.host_context import (
    HostContextError,
    SandboxStateContext,
    hook_environment,
    hook_interpreter,
    load_hook_context,
    load_host_context,
)

GUARD_NAME = "shell-secret-print-guard"
GUARD_TIMEOUT_FLOOR = 10
# AGY accepts three aliases for .agents and a .gemini workspace discovery override.
AGY_WORKSPACE_CONFIG_DIRS = (".agents", ".agent", "_agents", "_agent", ".gemini")
PRIMARY_GUARD_NAME = "primary-checkout-write-guard"
PRIMARY_GUARD_ENTRY = "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
GUARD_MODE = "agy-secret-print"
# Claude's Python guard delegators, by mode: ``claude-<stem>`` runs the installed
# ``agents_extensions.shared.hooks.<stem>`` under the resolved interpreter.
CLAUDE_GUARD_MODES = (
    "claude-heal-core-bare",
    "claude-guard-branch-switch-in-main",
    "claude-guard-admin-merge",
    "claude-guard-pr-merge",
    "claude-guard-secret-print",
    "claude-guard-primary-checkout-write",
    "claude-guard-public-github-text",
)
_CODEX_SHELL_HOOKS = {
    "codex-session-setup": "session-setup.sh",
    "codex-post-compact": "post-compact.sh",
    "codex-goal-driver-stop": "goal-driver-stop.sh",
    "codex-release-thread-lease": "release-thread-lease.sh",
}
# Every mode a product hook manifest may name, across the standard, audit and guard tails.
NATIVE_HOOK_MODES = (
    GUARD_MODE,
    "agy-primary-write",
    "agy-pending-orders",
    "agy-hook",
    "agy-stop-hook",
    "agy-pretool-hook",
    "claude-hook",
    "claude-stop-hook",
    "claude-audit-hook",
    "codex-pre-tool-use",
    "codex-post-tool-use",
    "codex-user-prompt-submit",
    *CLAUDE_GUARD_MODES,
    *_CODEX_SHELL_HOOKS,
)

_HOOK_REFUSALS: dict[str, tuple[str, str | bool, str]] = {
    "agy-stop-hook": ("decision", "continue", "reason"),
    "claude-stop-hook": ("decision", "block", "reason"),
    "claude-hook": ("continue", False, "stopReason"),
}


def _hook_refusal(mode: str, diagnostic: str) -> dict[str, str | bool] | None:
    default = ("decision", "deny", "reason") if mode.startswith("agy-") else None
    refusal = _HOOK_REFUSALS.get(mode, default)
    if refusal is None:
        return None
    key, value, reason = refusal
    return {key: value, reason: diagnostic}


def _refuse_hook(mode: str, code: str, message: str) -> int:
    diagnostic = f"HOOK-BOOTSTRAP-FAILED: [{code}] {message}"
    print(diagnostic, file=sys.stderr)
    answer = _hook_refusal(mode, diagnostic)
    if answer is None:
        return 2
    print(json.dumps(answer))
    return 0


def _run_projected_secret_guard(context: SandboxStateContext) -> int:
    """Check shell text in the read-only sandbox without accepting a checkout binding."""
    from lu_machinery.agent_runtime.secret_print_hook import (
        MAX_STDIN_BYTES,
        emit_decision,
        extract_command,
        run_guard,
    )

    if not os.statvfs(context.path).f_flag & os.ST_RDONLY:
        return _refuse_hook(GUARD_MODE, "writable_projection", "sandbox state binding must be mounted read-only")
    if configured_interpreter() != Path(sys.executable):
        return _refuse_hook(GUARD_MODE, "interpreter_binding_mismatch", "guard requires the configured interpreter")
    product = os.environ.get("LU_PRODUCT_ROOT", "")
    if not Path(product).is_absolute() or Path(product).as_posix() != product or ".." in Path(product).parts:
        return _refuse_hook(GUARD_MODE, "invalid_product_binding", "guard requires the configured product path")
    raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_STDIN_BYTES:
        return _refuse_hook(GUARD_MODE, "invalid_hook_event", "native hook event exceeds its size limit")
    try:
        command = extract_command("agy", json_depth.loads(raw, max_depth=512))
    except ValueError:
        return _refuse_hook(GUARD_MODE, "invalid_hook_event", "native hook event has no valid tool input")
    # The guard analyses text. The OS boundary enforces write permissions.
    allowed, message = (True, "") if command is None else run_guard(command, harness="agy")
    return emit_decision("agy", allowed, message)


def run_native_hook(mode: str) -> int:
    """Validate the host binding before starting a registered hook."""
    if mode not in NATIVE_HOOK_MODES:
        return _refuse_hook("", "unsupported_hook_mode", "hook mode is not registered")
    try:
        if mode == GUARD_MODE:
            state = load_host_context(role="state")
            if isinstance(state, SandboxStateContext):
                return _run_projected_secret_guard(state)
        context, _ = load_hook_context(Path.cwd())
    except HostContextError as exc:
        return _refuse_hook(mode, exc.code, str(exc))
    except OSError:
        return _refuse_hook(mode, "invalid_invocation_git", "invocation directory or sandbox projection is unavailable")
    except ValueError:
        return _refuse_hook(mode, "invalid_hook_binding", "hook environment conflicts with the binding")

    env = {**os.environ, **hook_environment(context)}
    python = os.fspath(context.runtime.python)
    if mode in CLAUDE_GUARD_MODES:
        argv = [python, "-I", "-m", f"agents_extensions.shared.hooks.{mode.removeprefix('claude-')}"]
    elif mode == "claude-audit-hook":
        argv = [python, "-I", "-m", "agents_extensions.shared.hooks.audit-primary-checkout-dirt"]
    elif mode == "agy-primary-write":
        argv = [python, "-I", os.fspath(machinery_path(PRIMARY_GUARD_ENTRY))]
    elif mode == GUARD_MODE:
        argv = [python, "-I", "-m", "lu_machinery.agent_runtime.secret_print_hook", "--harness", "agy"]
    elif mode in _CODEX_SHELL_HOOKS:
        argv = ["/bin/bash", os.fspath(machinery_path(f"agents_extensions/shared/hooks/{_CODEX_SHELL_HOOKS[mode]}"))]
        if mode == "codex-post-compact":
            env["CODEX_COMPACT_SESSION_START"] = "1"
    elif mode.startswith("codex-"):
        argv = ["/bin/bash", os.fspath(machinery_path("agent_runtime/codex_hook_entry.sh")), mode.removeprefix("codex-")]
    elif mode == "agy-pending-orders":
        argv = ["/bin/bash", os.fspath(machinery_path("agy_hooks/pending_orders_inject.sh"))]
    else:
        argv = ["/bin/bash", os.fspath(machinery_path("agy_hooks/driver_state_inject.sh")), mode]
    try:
        if argv[0] == python and argv[2] == "-m" and importlib.util.find_spec(argv[3]) is None:
            return _refuse_hook(mode, "hook_unavailable", "installed hook module is missing")
        if mode in {GUARD_MODE, "agy-primary-write"}:
            result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                argv, env=env, check=False, timeout=GUARD_TIMEOUT_FLOOR - 1,
            )
            if result.returncode:
                return _refuse_hook(mode, "hook_exec_failed", "installed guard could not complete")
            return 0
        if argv[0] == python:
            # The native entry already runs under the validated isolated interpreter.
            os.environ.update(hook_environment(context))
            sys.argv[:] = [argv[3]]
            runpy.run_module(argv[3], run_name="__main__", alter_sys=True)
            return 0
        if not Path(argv[1]).is_file():
            return _refuse_hook(mode, "hook_unavailable", "installed hook script is missing")
        os.execve("/bin/bash", argv, env)
    except subprocess.TimeoutExpired:
        return _refuse_hook(mode, "hook_timeout", "installed guard exceeded its time limit")
    except (ImportError, OSError):
        return _refuse_hook(mode, "hook_exec_failed", "installed hook could not be started")


NATIVE_HOOK_DECLARATION = "lu-machinery-hook-v2"
# Fingerprints of the published commands let product sources migrate at deploy
# time. The old executable shell body is never rendered or run.
_LEGACY_HOOK_MODE = {
    "fc9ee7e141149783e3d2e02285522f0975922c295c27a4798fe862d2ba6e1f9a": "agy-secret-print",
    "d722a89eca07d83932020f7483d25a619a81b3b1241ff6376d4e02bf0a5186ea": "agy-primary-write",
    "57f030774516561030dd028c336b518ff69d6a9eca96e5d5afb393677590d252": "agy-pending-orders",
    "da2451ebe93f2fedd5f46a74f0543f9c6979d7c6839d8a8ed1f9d1b40dbba3f6": "agy-hook",
    "2c3ffdc110be4081e2e755c33a0072c19767e2152f59c8b9f49be788de52f3ac": "agy-stop-hook",
    "8f64c884b485c6fba3cb0423e4d8a41de22c0dcd754849559ba34a597031af64": "agy-pretool-hook",
    "9c07093232c7b899a129972fc53f27aecd400e833964e2a23b46bf7e22776d55": "claude-hook",
    "79defbb55ba4156729e2683a7230fba44e592939e0359479b653a583275fce8a": "claude-stop-hook",
    "cc04c744b0f94a1798a535eaf4476bbe39fcd61f5686bc366cb9675d7a4e0339": "claude-audit-hook",
    "4b9f92f31bfbaf76b7a6d7ce82c3ace0c610c109b3d4d31bdeb2d99c88eb7fd8": "codex-pre-tool-use",
    "cd8bb4732b0dc9088b515e1f0d9c055e2501d3e65040caff3fcd81dbe89f91c8": "codex-post-tool-use",
    "a9539431c1b5de8c17bee747e6402bbdcad7b1491210fcc9a2f0ee18541d56f0": "codex-user-prompt-submit",
    "a31855c82de580929ba8b9d5941a1aec4068e689d13d4e25097a8a68a4a87e97": "claude-heal-core-bare",
    "bfa38242e7dd49626bb8722c89b2f3de3a0d5aecef9bde5d186f72f93cf080cd": "claude-guard-branch-switch-in-main",
    "c517cc9348e7c7b20909113d635489ce1c95077dc398fbafc2918cfab2230e0a": "claude-guard-admin-merge",
    "755a6838ea933927fe42cc00aeaffc66468b664406ac346ad4d64a089c5bec8b": "claude-guard-pr-merge",
    "480d96c562fb568238340a849214c757082debbcc267fd96fbbadd6d6e0feda5": "claude-guard-secret-print",
    "2976e241d25f41d7c89d4cfcd6e5f06e635cc683cc7400c2579ea47f910dbf7f": "claude-guard-primary-checkout-write",
    "1a71485aefc055285e289067d727605774e02491c8318c2a58f418a44b91601a": "claude-guard-public-github-text",
    "a944eb852608f56a7ed1a587dcca604079908a33e1137dcf2c396b94349b7651": "codex-pre-tool-use",
    "6fb815eaaefe91f6f268a422bfe5e808d7350e8064accd96f246219951d45583": "codex-session-setup",
    "8665a604706b839c040f95a9e3464c440e7fd0664866bddae7f063f8ac367f8e": "codex-post-compact",
    "858e80b79302e1722e37baa4ae6ee6a5ed47e989087efea6e3d8117e5a53a004": "codex-goal-driver-stop",
    "53ba19034820bb6de1001ad44414eee497953c493c111146fda707dc235e14aa": "codex-release-thread-lease",
}


def hook_mode_for_source(command: str) -> str | None:
    """Identify an exact native hook declaration or a published legacy command."""
    if command.startswith(NATIVE_HOOK_DECLARATION):
        for mode in NATIVE_HOOK_MODES:
            if command == f"{NATIVE_HOOK_DECLARATION} {mode}":
                return mode
        raise AgyHookConfigError("unsupported_hook_mode")
    legacy_mode = _LEGACY_HOOK_MODE.get(sha256(command.encode()).hexdigest())
    if legacy_mode is not None:
        return legacy_mode
    if "lu-machinery-hook-v1" in command:
        raise AgyHookConfigError("invalid_legacy_hook_command")
    return None


@dataclass(frozen=True)
class HookConfigResult:
    status: str
    reason: str

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


class AgyHookConfigError(ValueError):
    """A startup refusal, safe to include in diagnostics."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"agy_hook_config_refused: {reason}")


def hook_locations(cwd: Path, home: Path, agy_home: Path | None = None) -> tuple[Path, ...]:
    """List every filesystem candidate for this spawn's effective HOME."""
    if agy_home is not None and not agy_home.is_absolute():
        raise AgyHookConfigError("relative_scoped_home")
    effective_home = agy_home if agy_home is not None else home
    return (cwd / AGY_WORKSPACE_CONFIG_DIRS[0] / "hooks.json", effective_home / ".gemini/config/hooks.json")


def _read_config(path: Path) -> dict:
    def unique_members(pairs: list[tuple[str, object]]) -> dict:
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("duplicate_member")
            data[key] = value
        return data

    try:
        data = json_depth.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_members, max_depth=512)
    except FileNotFoundError:
        raise AgyHookConfigError("missing_file") from None
    except (OSError, UnicodeError, ValueError):
        raise AgyHookConfigError("invalid_json") from None
    if not isinstance(data, dict):
        raise AgyHookConfigError("invalid_json")
    return data


def native_hook_command(mode: str, *, python: Path | None = None) -> str:
    """Render a native hook with the machinery interpreter selected by deployment."""
    if mode not in NATIVE_HOOK_MODES:
        raise AgyHookConfigError("unsupported_hook_mode")
    if python is None:
        try:
            python = hook_interpreter()
        except HostContextError as exc:
            raise AgyHookConfigError(exc.code) from None
    command = "exec " + shlex.join([
        os.fspath(python), "-I", "-B", "-m", "lu_machinery.agent_runtime.agy_hook_config", "--run-hook", mode,
    ])
    # An interpreter that cannot start cannot format the provider's refusal itself.
    diagnostic = "HOOK-BOOTSTRAP-FAILED: [hook_exec_failed] machinery interpreter could not run the hook"
    refusal = _hook_refusal(mode, diagnostic)
    if refusal is not None:
        report = shlex.join(["printf", "%s\n", json.dumps(refusal)])
        return f"({command}) || {report}"
    return f"({command}) || exit 2"


def _check_file(path: Path, command: str) -> None:
    guard = _read_config(path).get(GUARD_NAME)
    if not isinstance(guard, dict):
        raise AgyHookConfigError("missing_guard")
    if guard.get("enabled", True) is not True or guard.get("disabled", False) is not False:
        raise AgyHookConfigError("disabled")
    groups = guard.get("PreToolUse")
    if not isinstance(groups, list) or len(groups) != 1:
        raise AgyHookConfigError("wrong_event")
    group = groups[0]
    if not isinstance(group, dict):
        raise AgyHookConfigError("invalid_group")
    matcher = group.get("matcher")
    if not isinstance(matcher, str):
        raise AgyHookConfigError("invalid_matcher")
    try:
        matches = matcher in {"*", ""} or re.search(matcher, "run_command") is not None
    except re.error:
        raise AgyHookConfigError("invalid_matcher") from None
    if not matches:
        raise AgyHookConfigError("wrong_matcher")
    hooks = group.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        raise AgyHookConfigError("invalid_command")
    hook = hooks[0]
    timeout = hook.get("timeout")
    if type(timeout) is not int or timeout < GUARD_TIMEOUT_FLOOR:
        raise AgyHookConfigError("invalid_timeout")
    if any(item.get("enabled", True) is not True or item.get("disabled", False) is not False for item in (group, hook)):
        raise AgyHookConfigError("disabled")
    # Byte equality: a differently quoted equivalent could expand before argv forms.
    if hook.get("type") != "command" or hook.get("command") != command:
        raise AgyHookConfigError("invalid_command")


def check_hook_config(locations: tuple[Path, ...]) -> HookConfigResult:
    """Require a valid enabled guard in every existing loadable candidate.

    An absent workspace or effective-HOME candidate cannot load and is skipped.
    At least one candidate must exist and validate. Existing invalid files still
    refuse, even alongside a valid file. Scoped HOME replaces ordinary HOME.
    PASS certifies registration in every loadable candidate, not hook execution
    or native blocking on any route. On the review route, the load-bearing
    control is the exact validated command-denial settings: ``command(*)`` and
    ``write_file(*)`` denied, checked by ``_write_review_permissions`` before
    launch, plus ``--sandbox``. Scoped-home guard registration is defense in
    depth. PASS does not certify that the hook can execute inside the review OS
    sandbox: the probe at 69690b9667 found its interpreter and entry point
    unreadable there. The failure disposition of a hook that cannot start is
    UNKNOWN. It can only fire on ``run_command``, which the settings deny.
    """
    try:
        command = native_hook_command(GUARD_MODE)
        checked = 0
        for path in locations:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            _check_file(path, command)
            checked += 1
        if not checked:
            raise AgyHookConfigError("missing_file")
    except AgyHookConfigError as exc:
        return HookConfigResult("REFUSE", exc.reason)
    except (OSError, ValueError):
        return HookConfigResult("REFUSE", "unresolved_path")
    return HookConfigResult("PASS", "guard_registered")


def guard_registration() -> dict:
    """Register the installed secret-print guard; read no product configuration.

    The registration is machinery's own: the one generated guard command, the exact
    bytes ``_check_file`` accepts, on ``run_command`` with the timeout floor. No product
    file takes part, so an out-of-tree workspace or a product without the AGY overlay
    is provisioned the same way, and a worktree-writable overlay cannot change it.
    """
    return {"PreToolUse": [{"matcher": "run_command", "hooks": [
        {"type": "command", "command": native_hook_command(GUARD_MODE), "timeout": GUARD_TIMEOUT_FLOOR}
    ]}]}


def primary_guard_registration() -> dict:
    """Register the installed primary-write guard using the native hook contract."""
    return {"PreToolUse": [{"matcher": "write_file|edit_file|run_command", "hooks": [
        {"type": "command", "command": native_hook_command("agy-primary-write"), "timeout": 10}
    ]}]}


def require_dispatch_guards(worktree: Path, home: Path) -> None:
    """Provision and verify both guards before a dispatched workspace-write AGY run."""
    try:
        configure_native_hook_roots(worktree)
    except (OSError, ValueError):
        raise AgyHookConfigError("hook_context_unavailable") from None
    try:
        install_worktree_guard(worktree)
        result = check_hook_config(hook_locations(worktree, home))
        if result.passed:
            registered = _read_config(worktree / ".agents/hooks.json").get(PRIMARY_GUARD_NAME)
            if registered != primary_guard_registration():
                result = HookConfigResult("REFUSE", "primary_guard_mismatch")
            elif not machinery_path(PRIMARY_GUARD_ENTRY).is_file():
                result = HookConfigResult("REFUSE", "missing_primary_guard_script")
    except AgyHookConfigError:
        raise
    except (OSError, ValueError):
        raise AgyHookConfigError("install_failed") from None
    if not result.passed:
        raise AgyHookConfigError(result.reason)


def install_worktree_guard(cwd: Path) -> None:
    """Merge only a missing guard into a dispatch workspace; never write homes."""
    target = cwd.resolve(strict=True)
    primary = main_checkout_root(target).resolve(strict=True)
    dispatch = (primary / ".worktrees/dispatch").resolve(strict=True) if primary != target else primary
    if ".." in cwd.parts or primary == target or target == dispatch or not target.is_relative_to(dispatch):
        raise AgyHookConfigError("install_requires_dispatch_worktree")
    provision_workspace_guard(cwd)


def provision_workspace_guard(cwd: Path) -> None:
    """Provision a caller-owned workspace, including existing bridge scratch.

    The caller owns the directory and its lifecycle; never use an ordinary HOME.
    Walk real directories and atomically merge missing guards, retaining existing
    keys (including disabled guards) and unrelated bytes. Published files have
    mode 0600.
    """
    if ".." in cwd.parts:
        raise AgyHookConfigError("unsafe_install_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open(cwd.absolute().anchor, flags)
    temporary = None
    try:
        for component in cwd.absolute().parts[1:]:
            child_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        if os.fstat(directory_fd).st_uid != os.geteuid():
            raise AgyHookConfigError("unsafe_install_path")
        with suppress(FileExistsError):
            os.mkdir(".agents", dir_fd=directory_fd)
        child_fd = os.open(".agents", flags, dir_fd=directory_fd)
        os.close(directory_fd)
        directory_fd = child_fd
        try:
            fd = safe_open_below(directory_fd, "hooks.json", os.O_RDONLY)
        except FileNotFoundError:
            original = "{}"
        else:
            with os.fdopen(fd, "r", encoding="utf-8", newline="") as handle:
                original = handle.read()
        try:
            data = json_depth.loads(original, max_depth=512)
        except (ValueError, UnicodeError):
            raise AgyHookConfigError("invalid_json") from None
        if not isinstance(data, dict):
            raise AgyHookConfigError("invalid_json")
        missing = {
            name: register()
            for name, register in ((GUARD_NAME, guard_registration), (PRIMARY_GUARD_NAME, primary_guard_registration))
            if name not in data  # Preserve explicitly disabled or invalid user wiring.
        }
        if not missing:
            return
        # Insert only missing members, keeping unrelated hook bytes unchanged.
        end = original.rindex("}")
        member = json.dumps(missing, indent=2)[1:-1]
        payload = original[:end] + ("," if data else "") + member + original[end:]
        temporary = f".hooks-{uuid.uuid4().hex}.tmp"
        fd = safe_open_below(directory_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, "hooks.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except OSError:
        raise AgyHookConfigError("unsafe_install_path") from None
    finally:
        if temporary is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def require_hook_config(cwd: Path, home: Path, agy_home: Path | None = None, *, install: bool = False) -> None:
    """Refuse before provider subprocesses; provision missing dispatch registrations.

    Every existing candidate must validate, and at least one must contain the
    guard. Never repair an existing invalid candidate or write ordinary HOME.
    Validate the host binding and the invocation checkout before provisioning.
    The provider and its hooks inherit the validated product and state roots.
    """
    try:
        configure_native_hook_roots(cwd)
    except (OSError, ValueError):
        raise AgyHookConfigError("hook_context_unavailable") from None
    try:
        if install and main_checkout_root(cwd) != cwd:
            require_dispatch_guards(cwd, agy_home or home)
        result = check_hook_config(hook_locations(cwd, home, agy_home))
    except (OSError, ValueError) as exc:
        if isinstance(exc, AgyHookConfigError):
            raise
        raise AgyHookConfigError("install_failed") from None
    if not result.passed:
        raise AgyHookConfigError(result.reason)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] == "--run-hook":
        return run_native_hook(argv[1])
    # Named canonically, under the unresolved name of the package directory this file is in.
    entry = f"-m {Path(__file__).absolute().parents[1].name}.agent_runtime.agy_hook_config"
    parser = argparse.ArgumentParser(
        description="Read effective AGY hooks files and refuse missing no-print wiring.",
        epilog=f"Example: .venv/bin/python {entry} --cwd .\n"
        "Outputs: PASS or REFUSE with a reason code. Writes: none. Exit: 0 PASS, 2 REFUSE.",
    )
    parser.add_argument("--cwd", type=Path, required=True, help="AGY workspace directory.")
    parser.add_argument("--home", type=Path, default=Path.home(), help="Effective ordinary HOME.")
    args = parser.parse_args(argv)
    result = check_hook_config(hook_locations(args.cwd, args.home))
    print(f"{result.status}: {result.reason}")
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
