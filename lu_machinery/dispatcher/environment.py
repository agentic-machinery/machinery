"""Construct a provider environment without inherited infrastructure credentials."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from lu_machinery.agent_runtime.read_only_tmp import dispatch_lease
from lu_machinery.common.git_credentials import SELECTOR_ENV
from lu_machinery.common.repo_root import (
    INTERPRETER_ENV,
    PRIMARY_ROOT_ENV,
    PRODUCT_ROOT_ENV,
    STATE_ROOT_ENV,
    project_interpreter,
)
from lu_machinery.common.task_store_paths import tasks_dir
from lu_machinery.dispatcher import site_deps, store
from lu_machinery.dispatcher.types import TaskSpec

# A worker's scratch lives at /var/tmp/<task-id>, exported as LU_TASK_SCRATCH. A write worker's TMPDIR is that
# scratch: the supervisor creates it before launch and settlement removes it once the provider is proven gone,
# whatever the terminal status (dispatcher.scratch). Inside a read-only boundary the launcher points both under
# the boundary's private TMPDIR instead. Tests replace the base.
TASK_SCRATCH_BASE = Path("/var/tmp")
# The longest scratch path a write task may get. It is the worker's TMPDIR, and a Unix socket path holds 107 bytes:
# Python's multiprocessing puts its listener 32 bytes below TMPDIR (pymp-XXXXXXXX/listener-XXXXXXXX) and ssh-agent
# about 31 (ssh-XXXXXXXXXXXX/agent.<pid>), so 72 leaves room for both. A longer path refuses the launch
# (dispatcher.scratch.create). Tests replace it with their base.
TASK_SCRATCH_PATH_MAX = 72


def task_scratch_path(task_id: str) -> Path:
    """The task's scratch directory: exactly the base's child named by a valid task id; never created here."""
    store.record_path(task_id)  # raises ValueError for an id the task store would refuse
    return TASK_SCRATCH_BASE / task_id


@dataclass(frozen=True, slots=True)
class OrchestratorIdentity:
    """Resolved lane, SESSION_EPIC and registered Fleet slot for one orchestrator turn."""

    stream: str
    lane: str
    session_epic: str
    slot: str


def _provider_slot_prefix(agent: str, model: str | None = None) -> str:
    if agent == "agy":
        return "gemini"
    if agent == "cursor" and isinstance(model, str) and model.startswith("grok"):
        return "grok"
    return agent


def stream_lane(stream: str) -> str:
    """The canonical driver-state lane / SESSION_EPIC for a registered ``epic:<N>`` stream."""
    from lu_machinery.orchestration.issue_stream_audit import load_registry
    from lu_machinery.orchestration.launcher_aliases import load_launcher_bindings

    if not isinstance(stream, str) or not stream.startswith("epic:") or not stream[5:].isdigit():
        raise ValueError(f"orchestrator_stream_unregistered: {stream!r} is not a canonical epic:<N> stream")
    epic_number = int(stream[5:])
    registry = load_registry()
    stream_keys = [key for key, epics in registry.items() if epic_number in epics]
    if len(stream_keys) != 1:
        raise ValueError(f"orchestrator_stream_unregistered: {stream} is not uniquely registered in issue_streams.yaml")
    stream_key = stream_keys[0]
    bindings = load_launcher_bindings()
    matched_lanes = {lane for mapped_stream, lane in bindings.values() if mapped_stream == stream_key}
    if len(matched_lanes) > 1:
        raise ValueError(f"orchestrator_stream_unregistered: {stream} resolves to multiple launcher lanes")
    if matched_lanes:
        slot_lane = next(iter(matched_lanes))
        if bindings.get(slot_lane, (None, None))[0] == stream_key:
            return slot_lane
    return stream_key


def orchestrator_identity(stream: str, *, agent: str, model: str | None = None) -> OrchestratorIdentity:
    """Resolve lane, SESSION_EPIC and registered slot for an orchestrator turn; fail closed if unregistered."""
    from lu_machinery.orchestration import handoff_slot_registry
    from lu_machinery.orchestration.fleet_taxonomy import FleetTaxonomyError, load_fleet_taxonomy, resolve_area
    from lu_machinery.orchestration.issue_stream_audit import load_registry
    from lu_machinery.orchestration.launcher_aliases import load_launcher_bindings
    from lu_machinery.orchestration.validate_fleet_taxonomy import ASSIGNMENTS_PATH, _load_yaml

    session_epic = stream_lane(stream)
    epic_number = int(stream[5:])
    registry = load_registry()
    stream_key = next(key for key, epics in registry.items() if epic_number in epics)
    bindings = load_launcher_bindings()
    matched_lanes = {lane for mapped_stream, lane in bindings.values() if mapped_stream == stream_key}
    slot_lane = next(iter(matched_lanes)) if len(matched_lanes) == 1 else session_epic
    try:
        taxonomy = load_fleet_taxonomy()
        assignments = _load_yaml(ASSIGNMENTS_PATH).get("assignments", {})
    except (FleetTaxonomyError, OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"orchestrator_stream_unregistered: {stream} registry unavailable ({type(exc).__name__})") from None
    resolved_areas: list[str] = []
    for key in dict.fromkeys((stream_key, slot_lane)):
        try:
            resolved_areas.append(resolve_area(key).id)
        except FleetTaxonomyError:
            continue
    epic_area = taxonomy.epic_to_area.get(epic_number)
    if epic_area is not None:
        resolved_areas.append(epic_area.id)
    unique_areas = set(resolved_areas)
    if len(unique_areas) != 1:
        detail = "registries disagree on area" if len(unique_areas) > 1 else "has no registered area"
        raise ValueError(f"orchestrator_stream_unregistered: {stream} {detail}")
    area_id = next(iter(unique_areas))
    area_spec = assignments.get(area_id) if isinstance(assignments, dict) else None
    if not isinstance(area_spec, dict):
        raise ValueError(f"orchestrator_stream_unregistered: {stream} (area {area_id}) missing from area_assignments.yaml")
    if not handoff_slot_registry.registry_readable():
        raise ValueError("orchestrator_slot_unregistered: fleet handoff slot registry is unreadable")
    prefix = _provider_slot_prefix(agent, model)
    candidate_slot = f"{prefix}-{slot_lane}"
    if not handoff_slot_registry.is_registered_slot(candidate_slot):
        raise ValueError(
            f"orchestrator_slot_unregistered: {candidate_slot!r} is not a registered launcher slot "
            f"for {stream} (area {area_id})"
        )
    return OrchestratorIdentity(stream=stream, lane=session_epic, session_epic=session_epic, slot=candidate_slot)

# LU_SENSE_PROOF_DIR is a host path with no secret in the name; pack-verify reads the proofs inside it.
# LU_FLEET_REPO_SELECTOR_FILE names the host-owned, read-only dual-slug selector; passing its path grants nothing.
# LU_NODE_TOOLCHAIN_DIRS configures host Node toolchain roots (e.g. /opt/lu-node/{major}/bin), never a credential.
# LU_DRIVER_STATE_CLI configures the host lu-driver-state CLI executable path for handoff-note checks, never a credential.
_CONTEXT = (
    "PATH", "HOME", "LANG", "TERM", "TMPDIR", "LU_SENSE_PROOF_DIR", SELECTOR_ENV,
    site_deps.NODE_TOOLCHAIN_DIRS_ENV, store.DRIVER_STATE_CLI_ENV,
)
_AUTH = {
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME"),
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY", "CLAUDE_CONFIG_DIR"),
    "cursor": ("CURSOR_API_KEY",),
    "agy": ("AGY_API_KEY", "AGY_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
    "muse": ("MUSE_API_KEY", "MUSE_TOKEN"),
    "opencode": ("DEEPSEEK_API_KEY",),
}
_NPM_SHIMS = Path(__file__).resolve().parent / "npm_shims"
# Any checkout's npm shim directory, named by this file's location (design-v1 amendment 1).
_NPM_SHIM_PARTS = (Path(os.path.abspath(__file__)).parents[1].name, "dispatcher", "npm_shims")


def npm_shim_dir() -> Path | None:
    """Share only the npm/npx protection, without importing the legacy runner."""
    runtime = Path(__file__).resolve().parents[1] / "agent_runtime"
    executables = [
        Path(sys.executable), *(_NPM_SHIMS / name for name in ("npm", "npx")),
        *(runtime / "shims" / name for name in ("npm", "npx")),
    ]
    guard = runtime / "npm_guard.py"
    if (all(os.access(path, os.X_OK) and path.is_file() for path in executables)
            and os.access(guard, os.R_OK) and guard.is_file()):
        return _NPM_SHIMS
    return None


def claude_config_dir(account: str, home: str | None = None) -> str | None:
    """The login directory a Claude account runs in until ``admission.claude_launch`` binds the verified one.

    An API-key label's directory exists only in its private binding (C-2643), so the read-only policy
    check before launch examines that directory; an unbound one is ``None``, and admission refuses it.
    """
    from lu_machinery.agent_runtime.attempt_boundary import (
        CLAUDE_API_KEY_ACCOUNT_LABELS,
        ClaudeAccountRefused,
        load_claude_account_binding,
    )

    if account in CLAUDE_API_KEY_ACCOUNT_LABELS:
        try:
            return str(load_claude_account_binding(account).config_dir)
        except ClaudeAccountRefused:
            return None
    suffix = "" if account == "a" else "-b"
    return str(Path(home or os.environ.get("HOME") or str(Path.home())) / f".claude{suffix}")


def probe_env(spec: TaskSpec) -> dict[str, str]:
    """``child_env`` for a provider check that runs before the task's scratch exists: the caller's own TMPDIR."""
    env = child_env(spec)
    env.pop("TMPDIR", None)
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


def _safe_resolve(entry: str) -> Path | None:
    try:
        return Path(entry).resolve()
    except (OSError, RuntimeError):
        return None


def child_env(spec: TaskSpec) -> dict[str, str]:
    source = os.environ
    if spec.agent == "codex":
        from lu_machinery.agent_runtime.codex_accounts import codex_account_env

        source = codex_account_env(spec.codex_account, source)
    names = _CONTEXT + _AUTH[spec.agent]
    if spec.provider == "openrouter" and spec.agent == "opencode":
        names = (*_CONTEXT, "OPENROUTER_API_KEY")
    env = {name: source[name] for name in names if name in source}
    if spec.agent == "cursor" and not env.get("CURSOR_API_KEY", "").strip():
        from lu_machinery.agent_runtime.adapters.cursor import _load_cursor_api_key_from_env_file

        env.pop("CURSOR_API_KEY", None)
        file_key = _load_cursor_api_key_from_env_file()
        if file_key:
            env["CURSOR_API_KEY"] = file_key
    if spec.agent == "claude" and spec.claude_account:
        config_dir = claude_config_dir(spec.claude_account, env.get("HOME"))
        if config_dir is None:
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = config_dir
    toolchain_dir = site_deps.resolve_toolchain_dir(spec.worktree, env=env)
    shims = npm_shim_dir()
    if shims is not None:
        original_entries = []
        for entry in env.get("PATH", os.defpath).split(os.pathsep):
            if not entry:
                continue
            resolved = _safe_resolve(entry)
            if (
                resolved == shims
                or Path(entry).parts[-3:] == _NPM_SHIM_PARTS
                or (resolved is not None and resolved.parts[-3:] == _NPM_SHIM_PARTS)
                or (toolchain_dir is not None and (entry == str(toolchain_dir) or resolved == toolchain_dir))
            ):
                continue
            original_entries.append(entry)
        if toolchain_dir is not None:
            original_entries.insert(0, str(toolchain_dir))
        original_path = os.pathsep.join(original_entries)
        env.update(
            PATH=f"{shims}{os.pathsep}{original_path}" if original_path else str(shims),
            AGENT_ORIGINAL_PATH=original_path,
            LEARN_UKRAINIAN_PYTHON=str(project_interpreter(env=source)),
        )
    elif any((spec.worktree / name).is_symlink() for name in ("node_modules", "site/node_modules")):
        raise ValueError("npm/npx guard unavailable for shared node_modules links")
    elif toolchain_dir is not None:
        base_entries = [
            entry for entry in env.get("PATH", os.defpath).split(os.pathsep)
            if entry and entry != str(toolchain_dir) and _safe_resolve(entry) != toolchain_dir
        ]
        env["PATH"] = os.pathsep.join([str(toolchain_dir), *base_entries])
    env.update(
        LEARN_UKRAINIAN_DISPATCH_TASK_ID=spec.task_id,
        LEARN_UKRAINIAN_DISPATCH_AGENT=spec.agent,
        LU_TASKS_DIR=str(tasks_dir().resolve()),
        LU_TASK_SCRATCH=str(task_scratch_path(spec.task_id)),
        # eslint, tsc and prettier call module.enableCompileCache(), which writes a copy of its cache into
        # os.tmpdir()/node-compile-cache for every new worktree path and is never removed (#12474).
        NODE_DISABLE_COMPILE_CACHE="1",
    )
    if spec.mode == "workspace-write":
        # A write worker runs on the host. Python (pytest's basetemp included), uv, Git and Node take their temp
        # directory from TMPDIR, so whatever they leave goes when the task settles. Only dispatched workers get
        # this: a seat or hook outside a task keeps its own TMPDIR.
        env["TMPDIR"] = env["LU_TASK_SCRATCH"]
    elif spec.agent == "codex" and spec.mode == "read-only":
        # Its read-only boundary exposes this private temp lease; the worker creates and removes it.
        env["TMPDIR"] = str(dispatch_lease(spec.task_id))
    if spec.mode == "read-only":
        # Restrict explicit URLs too; a worktree or ref snapshot cannot observe an arbitrary remote push.
        env["GIT_ALLOW_PROTOCOL"] = "file"
    # Every worker's hooks take the interpreter only from the configured shared-state anchor and
    # refuse without it (#100), so a configured anchor reaches every child, whatever its mode.
    if os.environ.get(PRIMARY_ROOT_ENV, ""):
        env[PRIMARY_ROOT_ENV] = os.environ[PRIMARY_ROOT_ENV]
    # Write providers need these paths for Git hooks. No credential is added.
    # Read-only AGY, Claude and Muse launchers also need the configured interpreter and product.
    if spec.mode == "workspace-write" or (spec.mode == "read-only" and spec.agent in {"agy", "claude", "muse"}):
        names = (INTERPRETER_ENV, PRODUCT_ROOT_ENV)
        if spec.mode == "workspace-write":
            names += (STATE_ROOT_ENV,)
        for name in names:
            configured = os.environ.get(name, "")
            if configured:
                env[name] = configured
    options = getattr(spec, "options", None)
    orch_stream = options.get("orchestrator_stream") if isinstance(options, dict) else None
    if orch_stream:
        run_nonce = options.get("orchestrator_run_nonce") if isinstance(options, dict) else None
        if not isinstance(run_nonce, str) or not run_nonce:
            raise ValueError("orchestrator_env_invalid: orchestrator turn requires a non-empty run nonce")
        identity = orchestrator_identity(orch_stream, agent=spec.agent, model=spec.model)
        env["LU_INBOX_WORKER"] = f"{spec.task_id}-{run_nonce}"
        env["SESSION_HANDOFF_AGENT"] = identity.slot
        env["SESSION_EPIC"] = identity.session_epic
    return env
