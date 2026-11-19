"""OS boundary for native read-only review dispatches (#423, #100).

Write workers run directly in their dispatch worktrees. Native read-only providers run under
bubblewrap, reusing agy_sandbox's canary, Sources relay and GitHub read relay. The worktree,
Git metadata and public checkouts are read-only mounts. HOME and temporary runtime state are
private; only declared outputs and the selected provider's refreshable login are writable.
GitHub credentials are masked and direct GitHub egress is refused. The host broker admits
only named github-read requests, with launch-bound repository and private-PR scope.

Every containment probe must pass before the provider starts. Setup or canary failure refuses
the run; nothing falls back to an uncontained launch. The receipt records the read-only proof.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lu_machinery.ai_agent_bridge.routing_guard import SAVANT_PROVIDER
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import machinery_path, main_checkout_root
from lu_machinery.common.scratch import ensure_scratch_root
from lu_machinery.dispatcher import agy_sandbox
from lu_machinery.dispatcher.agy_sandbox import REFUSED, Refused, _now, _remove, _write_private, _write_receipt

_PACKAGE = Path(__file__).absolute().parents[1].name
SCHEMA = 1
RECEIPT_SUFFIX = ".write-boundary.json"
_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    f"from {_PACKAGE}.dispatcher.write_boundary import main; raise SystemExit(main(sys.argv[2:]))"
)
_PROJECTED = {
    "canary.py": machinery_path("dispatcher/agy_canary.py"),
    "forwarder.py": machinery_path("agent_runtime/attempt_forwarder.py"),
    "sources-stdio.py": machinery_path("agent_runtime/attempt_proxy.py"),
}
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Private tmpfs inside: host sockets below these roots, including ones created after launch, are absent.
# /var/tmp stays a read-only view so driver-provided inputs remain readable; its sockets are masked.
_MASKED_ROOTS = (Path("/tmp"), Path("/run"), Path("/dev/shm"))
# Loopback services a worker must not reach (Monitor API, Sources, SSH); the namespace has no loopback route.
_LOOPBACK_PORTS = (22, 8765, 8766)
_CANARY_TIMEOUT = 60
_GIT_TIMEOUT = 300
# The primary's own Git state, kept read-only inside.
_PRIMARY_GIT_FILES = ("HEAD", "index", "config", "packed-refs", "hooks")
# Data roots outside HOME (decision A4): other users' homes and root's, service data, mounts, removable media,
# add-on packages and the shared persistent temporary directory, which holds every task's scratch (FHS 3.0
# §3.8, §3.14, §3.17, §3.11, §3.12, §3.13, §5.15). Each is an empty read-only tmpfs inside; the run's own
# paths below them are restored on top, and so is every /opt/<package> its PATH resolves into.
_DATA_ROOTS = (Path("/home"), Path("/root"), Path("/srv"), Path("/mnt"), Path("/media"), Path("/opt"), Path("/var/tmp"))
# HOME is an overlay whose lower layer is empty but for these toolchain locations, restored read-only: CLI entry
# points, npm global packages and the provider CLIs' and uv's own installations (XDG base directories).
_HOME_TOOLCHAIN = (".local/bin", ".local/lib", ".local/share/claude", ".local/share/cursor-agent",
                   ".local/share/uv/python", ".local/share/uv/tools")
# Private runtime state inside a restored public checkout, emptied inside: task records, agent and lease state,
# session handoffs, telemetry, secrets and local configuration, Fleet databases, logs and other tasks'
# worktrees. The run's own task records and worktree are restored on top.
# A checkout's ``.claude`` settings stay: Claude Code loads them and its launch guard binds them (#10619).
_CHECKOUT_PRIVATE = ("batch_state", ".agent", ".secrets", ".runtime", ".deploy-state", ".pids", ".codex", ".gemini",
                     ".mcp", ".env", ".worktrees", "logs", "docs/session-state", "data/telemetry",
                     "fleet_comms.db", ".fleet_comms.db", "comms.sqlite3")
# The selected Claude login's own state that its launch guard binds (#10619) and the CLI reads at start: projected
# as private copies into HOME's lower layer, so the CLI may rewrite them in the discarded upper layer. Only the
# guard's ``env`` map and account and organisation ids of the global config are copied.
_CLAUDE_LOGIN_FILES = ("settings.json", ".config.json")
_CLAUDE_GLOBAL_CONFIG = ".claude.json"
_MAX_LOGIN_FILE = 4 << 20
# The canary checks at most this many hidden host paths (other credentials, private state) for absence, and at
# most the second number in one directory.
_MAX_HIDDEN_PROBES = 1000
_MAX_PROBES_PER_DIRECTORY = 128


# The dispatcher's own configuration the launcher needs on the host (roots, scratch, task store, Fleet plane,
# interpreter, user bus). The provider's environment is minimal (``environment.child_env``), so the launcher gets
# these as ``--host-env NAME=VALUE`` and applies them to itself only; none is passed to the provider. Paths and
# addresses only: no credential is ever one of them.
_HOST_CONTEXT = frozenset({
    "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LEARN_UKRAINIAN_GITHUB_SLUG", "LEARN_UKRAINIAN_STATE_ROOT",
    "LEARN_UKRAINIAN_PYTHON", "LU_PRODUCT_ROOT", "LU_SCRATCH_ROOT", "LU_RUNTIME_TMP_BASE_ROOT", "LU_TASKS_DIR",
    "FLEET_COMMS_ROOT", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "GH_CONFIG_DIR", "DELEGATE_MONITOR_API", "LU_DISPATCH_ISOLATION",
})
# A provider may point these at its own private, empty roots (the Savant route does). The launcher's host steps
# (gh and the masks of its credential stores) always take the dispatcher's values, an empty value meaning unset;
# the provider keeps its own.
_HOST_ROOTS = frozenset({"XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"})

# Native read-only reviewers use the host's login for named GitHub reads only.
BROKER_ENV = "LU_WRITE_BOUNDARY_BROKER"
_BROKER_VERBS = ("github-read",)
# Named reads of the worker's own repository (the publisher's fixed GET templates); no security-alert reads.
GITHUB_READS = frozenset({"pull", "pr-files", "commits", "commit", "comments", "reviews", "timeline", "issue",
                          "checks", "check-annotations", "run", "jobs"})
# A private repository's github-read is the authorized PR (by its number) and its checks (by its head commit) only;
# reads by any other id (another PR or issue, a workflow run, a check run) are refused.
_PRIVATE_PR_READS = frozenset({"pull", "pr-files", "commits", "comments", "reviews", "timeline", "issue"})
_PRIVATE_SHA_READS = frozenset({"checks", "commit"})
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
# Direct GitHub egress is refused; the host read relays carry no write authority.
GITHUB_DOMAINS = ("github.com", "ghcr.io")
_GITHUB_META_KEYS = ("web", "api", "git", "packages")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?")
_MAX_REQUEST = 1 << 20
# The launcher's own directory appears inside at this short fixed path on the private /run (below the
# sockaddr_un length limit), read-only and as a mount point, so no worker can rename, remove or replace it or
# anything below it: the broker socket, the Sources relay socket and the projected Sources byte proxy. The MCP
# stdio command a provider names (``sources_command``) is therefore a constant, and no MCP file is read.
_INSIDE = "/run/lu-write-boundary"
_BROKER_INSIDE = "/run/lu-write-boundary/broker.sock"
_SOURCES_INSIDE = "/run/lu-write-boundary/sources.sock"
_SOURCES_STDIO_INSIDE = "/run/lu-write-boundary/sources-stdio.py"
# The task-local CODEX_HOME the launcher projects into the run's private scratch, beside its TMPDIR.
_CODEX_HOME = "codex-home"
# A write worker on these routes gets one task-local MCP file naming only the Sources relay (``sources_mcp_config``),
# written in the read-only boundary directory. AGY reads only HOME's ``.gemini/config/mcp_config.json`` (the file
# ``agy mcp add`` writes; ``adapters/agy.py``) and Cursor HOME's ``.cursor/mcp.json``, so the file is bound read-only
# there; OpenCode reads the file ``OPENCODE_CONFIG`` names, which is the file's fixed path inside.
_SOURCES_MCP = "sources-mcp.json"
_SOURCES_MCP_INSIDE = "/run/lu-write-boundary/sources-mcp.json"
# A public Savant run's tools read only this read-only snapshot of the published start commit (designated
# approval, Savant tool conditions C1-C4): regular files, none of whose path components names Git metadata, an
# environment file, a key or credential store, or an instruction or config file OpenCode loads by itself.
SNAPSHOT_INSIDE = "/run/lu-write-boundary/snapshot"
_SNAPSHOT_EXCLUDED = re.compile(
    r"\.git|\.gitconfig|\.env.*|.*\.env|\.?secrets?(\..*)?|\.ssh|\.gnupg|\.aws|\.docker|\.kube|\.netrc"
    r"|\.npmrc|\.pypirc|\.mcp\.json|\.git-credentials|\.?credentials(\..*)?|id_(rsa|dsa|ecdsa|ed25519)(\..*)?"
    r"|.*\.(pem|key|p12|pfx|jks|keystore|kdbx|gpg|asc)|auth\.json|agents\.md|claude\.md|context\.md"
    r"|opencode\.jsonc?|\.opencode",
    re.IGNORECASE)
# Content a snapshot file must not carry: a private key block or a known provider token format.
_SNAPSHOT_SECRET = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}"
    rb"|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})")
MAX_READ_PATHS = 64
MAX_SNAPSHOT_FILES = 20_000
_SOURCES_MCP_HOME = {"agy": ".gemini/config/mcp_config.json", "cursor": ".cursor/mcp.json", "opencode": None}
# Cursor gives a workspace's own ``.cursor/mcp.json`` precedence over HOME's for a server of the same name, and the
# product's names ``sources`` at a loopback URL the boundary refuses, so Cursor's relay entry has a name of its own.
_CURSOR_SOURCES_SERVER = "sources-relay"
# A read-only Cursor run's permission profile (``providers.cursor.read_only_cli_config``): Cursor reads per-command
# rules only from this file and rewrites it at every start through a temporary file beside it, so it is a private
# copy in HOME's lower layer, as the Claude login's copies are: Cursor's rewrite lands in the discarded upper layer.
_CURSOR_CLI_CONFIG_HOME = ".cursor/cli-config.json"
_BROKER_TIMEOUTS = {"gh": 120}
# Connections the broker serves at once; further requests wait in the listen backlog.
_BROKER_CONNECTIONS = 8


def sources_command() -> list[str]:
    """The MCP stdio argv that reaches the launcher's read-only Sources relay from inside the boundary.

    The launcher runs the provider with this interpreter and binds its proxy and the relay socket at these
    fixed paths. Without a relay the socket is absent: the server fails to start and the run goes on.
    """
    return [str(Path(sys.executable).resolve(strict=True)), "-I", _SOURCES_STDIO_INSIDE, _SOURCES_INSIDE]


def codex_home_config() -> str:
    """The task-local CODEX_HOME's ``config.toml``: Sources startup policy and one stdio server.

    The user's own configuration is not a layer of the run, so no MCP server, connector or transport key it
    defines can merge into this one. The dispatcher's argv adds the Sources tool policy and disables apps.
    """
    command, *args = sources_command()
    return "\n".join([
        "# Task-local CODEX_HOME projected by the write boundary: the Sources relay only.",
        # The default one-second grace can omit the still-starting relay from the first model turn.
        # Zero waits for startup or the existing server timeout; Sources stays optional on failure.
        "mcp_optional_startup_grace_ms = 0",
        "[mcp_servers.sources]",
        f"command = {json.dumps(command)}",
        "args = [" + ", ".join(json.dumps(arg) for arg in args) + "]",
        "",
    ])


def sources_mcp_config(agent: str) -> str:
    """The task-local MCP file of an AGY, Cursor or OpenCode write worker: one stdio server, the Sources relay.

    Each is in the shape that provider reads (AGY and Cursor ``mcpServers`` with ``command`` and ``args``; OpenCode
    ``mcp`` with a ``local`` server whose ``command`` is the whole argv). HOME is empty inside, so no other MCP
    server, credential or host configuration is a layer of the run.
    """
    command, *args = sources_command()
    if agent == "agy":
        config: dict = {"mcpServers": {"sources": {"command": command, "args": args}}}
    elif agent == "cursor":
        config = {"mcpServers": {_CURSOR_SOURCES_SERVER: {"type": "stdio", "command": command, "args": args}}}
    elif agent == "opencode":
        config = {"mcp": {"sources": {"type": "local", "command": [command, *args]}}}
    else:
        raise ValueError(f"write boundary: no Sources MCP projection for {agent!r}")
    return json.dumps(config, indent=2) + "\n"


def _interpreter_root() -> Path:
    """The installation ``sources_command``'s interpreter runs from (binary and standard library)."""
    interpreter = Path(sources_command()[0])
    prefix = Path(sys.base_prefix).resolve()
    return prefix if interpreter.is_relative_to(prefix) else interpreter.parent


def broker_path() -> str | None:
    """The broker socket when this process runs inside a write boundary, else None."""
    return os.environ.get(BROKER_ENV) or None


def broker_call(verb: str, payload: dict) -> dict:
    """Send one request to the launcher's broker; the reply holds ``returncode``, ``stdout`` and ``stderr``."""
    import socket

    path = broker_path()
    if path is None:
        raise RuntimeError("not inside a write boundary")
    request = json.dumps({"verb": verb, "request_id": uuid.uuid4().hex, "payload": payload}).encode()
    if len(request) > _MAX_REQUEST:
        return {"returncode": 2, "stdout": "", "stderr": "write boundary broker: the request is too large\n"}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(path)
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    try:
        reply = json_depth.loads(b"".join(chunks), max_depth=512)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return {"returncode": 1, "stdout": "", "stderr": "write boundary broker: no reply\n"}
    return reply


def unavailable() -> str | None:
    """Why this host cannot start the write boundary, or None (bubblewrap on Linux, as for AGY)."""
    return agy_sandbox.unavailable()


def receipt_file(task_file: Path) -> Path:
    return task_file.with_suffix(RECEIPT_SUFFIX)


def scratch(task_id: str) -> Path:
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError("write boundary: the task id is unsafe as a scratch name")
    return ensure_scratch_root() / f"write-boundary-{task_id}"


def discard(task_id: str) -> str | None:
    """Remove the scratch of a launcher that was killed before its own cleanup; never raises."""
    try:
        _remove(scratch(task_id))
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def credential_files(agent: str, env: dict[str, str]) -> list[Path]:
    """Provider credential files a token refresh rewrites; they stay writable so a refresh is not lost."""
    home = Path(env.get("HOME") or Path.home())
    if agent == "claude":
        candidates = [Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / ".credentials.json"]
    elif agent == "codex":
        candidates = [Path(env.get("CODEX_HOME") or home / ".codex") / "auth.json"]
    elif agent == "agy":
        from lu_machinery.agent_runtime.review_mcp import _real_agy_token

        candidates = [_real_agy_token()]
    elif agent == "opencode":
        candidates = [home / ".local" / "share" / "opencode" / "auth.json"]
    elif agent == "cursor":  # the CLI's own login; an API key reaches it through the environment instead
        candidates = [home / ".config" / "cursor" / "auth.json"]
    else:
        candidates = []
    return [path for path in candidates if path.is_file() and not path.is_symlink()]


def declared_outputs(spec, invocation) -> list[Path]:
    """Files outside the worktree the provider writes: its final message and AGY's write log."""
    outputs = [invocation.final_file] if invocation.final_file else []
    if spec.agent == "agy":
        outputs.append(spec.final_file.with_suffix(".log"))
    return [Path(path) for path in outputs]


def launcher(spec, invocation, task_file: Path) -> list[str]:
    """The argv that runs ``invocation.argv`` inside the write boundary."""
    found = shutil.which(invocation.argv[0], path=invocation.env.get("PATH")) if invocation.argv else None
    if not found:
        raise ValueError(f"write_boundary_refused: the {spec.agent} executable was not found")
    argv = [
        sys.executable, "-I", "-c", _BOOTSTRAP, str(machinery_path("dispatcher/write_boundary.py").parents[2]),
        "--task-id", spec.task_id, "--worktree", str(spec.worktree), "--scratch", str(scratch(spec.task_id)),
        "--receipt", str(receipt_file(task_file)), "--mode", spec.mode,
    ]
    for path in declared_outputs(spec, invocation):
        argv += ["--output", str(path)]
    if not (spec.agent == "opencode" and spec.provider == SAVANT_PROVIDER):
        for path in credential_files(spec.agent, invocation.env):
            argv += ["--credential", str(path)]
    if spec.agent == "claude":
        # The selected login (a labelled account's verified directory, set by ``admission.claude_launch``).
        home = Path(invocation.env.get("HOME") or Path.home())
        argv += ["--claude-login", str(Path(invocation.env.get("CLAUDE_CONFIG_DIR") or home / ".claude"))]
    if spec.agent == "codex":
        # The selected account's login: the only thing the task-local CODEX_HOME takes from the real one.
        home = Path(invocation.env.get("HOME") or Path.home())
        argv += ["--codex-auth", str(Path(invocation.env.get("CODEX_HOME") or home / ".codex") / "auth.json")]
    if spec.agent in _SOURCES_MCP_HOME and (spec.mode == "workspace-write" or spec.agent == "cursor"):
        argv += ["--sources-mcp", spec.agent]
    if spec.options.get("pr"):
        # The task record's PR, frozen here: a private repository's github-read is that PR and its checks (#565).
        argv += ["--pr", str(spec.options["pr"])]
    if spec.agent == "codex" and spec.mode == "read-only":
        from lu_machinery.agent_runtime.read_only_tmp import dispatch_lease

        argv += ["--runtime", str(dispatch_lease(spec.task_id))]
    if spec.agent == "opencode" and spec.provider == SAVANT_PROVIDER:
        from lu_machinery.dispatcher.providers import opencode

        argv += ["--upload-ceiling", str(opencode.SAVANT_UPLOAD_MAX_BYTES), "--no-github-relays"]
        if opencode.savant_tools(spec.options.get("data_class")):
            argv += ["--snapshot-bytes", str(opencode.SAVANT_SNAPSHOT_MAX_BYTES)]
            argv += [arg for path in opencode.savant_read_paths(spec.options) for arg in ("--read-path", path)]
    host = {name: os.environ.get(name, "") for name in _HOST_CONTEXT
            if (os.environ.get(name) and name not in invocation.env) or (name in _HOST_ROOTS and name in invocation.env)}
    # The launcher starts with the provider's environment. Its own GitHub reads (the published address ranges, the
    # github-read relay) use the dispatcher's gh login, never gh under a provider's private config root, where it
    # reads anonymously and the client refuses it below the landing reserve. No GH_* variable reaches the provider.
    host["GH_CONFIG_DIR"] = os.environ.get("GH_CONFIG_DIR") or str(
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gh")
    for name in sorted(host):
        argv += ["--host-env", f"{name}={host[name]}"]
    return [*argv, "--", str(Path(found).absolute()), *invocation.argv[1:]]


def verify(task_file: Path, task_id: str, mode: str = "read-only") -> tuple[dict, str | None]:
    """The boundary evidence and the refusal that fails the run, if any."""
    path = receipt_file(task_file)
    try:
        receipt = json_depth.loads(path.read_text(encoding="utf-8"), max_depth=512)
    except (OSError, ValueError):
        return {"status": "missing"}, "write_boundary_unproven: no boundary receipt"
    summary = {key: receipt.get(key) for key in (
        "status", "reason", "mode", "argv_sha256", "bwrap", "returncode", "import", "cleanup", "broker")}
    summary["canary_passed"] = bool(receipt.get("canary", {}).get("passed"))
    if receipt.get("schema") != SCHEMA or receipt.get("task_id") != task_id:
        return summary, "write_boundary_unproven: the receipt names another run"
    if receipt.get("mode") != mode:
        return summary, "write_boundary_unproven: the receipt names another mode"
    if receipt.get("status") != "ran" or not summary["canary_passed"]:
        return summary, f"write_boundary_refused: {receipt.get('reason') or receipt.get('status')}"
    if (receipt.get("import") or {}).get("status") != "unchanged":
        return summary, f"write_boundary_import_failed: {(receipt.get('import') or {}).get('status')}"
    ceiling = receipt.get("upload_ceiling")
    if isinstance(ceiling, dict):
        summary["upload_ceiling"] = ceiling
        if ceiling.get("reached") is not False:
            return summary, (f"savant_upload_ceiling: the run tried to send more than {ceiling.get('limit')} bytes "
                             "upstream; the boundary cut the connection")
    if isinstance(receipt.get("snapshot"), dict):
        summary["snapshot"] = receipt["snapshot"]
    return summary, None


def _git_remote(slug: str) -> tuple[str, list[str]]:
    """Scoped HTTPS destination for host settlement and cleanup, independent of the review broker."""
    from lu_machinery.common.git_credentials import verification_credentials

    url = f"https://github.com/{slug}.git"
    settings = verification_credentials(url)
    if not settings:
        raise Refused("the repository is not a registered github.com destination")
    return url, [arg for setting in settings for arg in ("-c", setting)]


def _git(cwd: Path, *args: str, git_dir: Path | None = None, network: bool = False) -> str:
    """Host Git with no user or system configuration, hooks or fsmonitor: nothing the worker wrote runs.

    ``network`` admits HTTPS for host settlement and branch cleanup; their credential is the scoped helper of
    ``git_credentials.verification_credentials``, the only credential configuration Git then has.
    """
    env = {"PATH": os.defpath, "HOME": os.environ.get("HOME", "/"), "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}
    if network:  # gh, behind the scoped helper, finds the host login through its own configuration
        env.update({name: os.environ[name] for name in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME") if os.environ.get(name)})
    command = list(args)
    while command[:1] == ["-c"] and len(command) >= 2:
        command = command[2:]
    # Clone transport is not publication authority. Verify the registry and bind credentials to
    # HTTPS, including when the protected origin uses SSH.
    if (network and command[:1] in (["push"], ["ls-remote"]) and len(command) >= 3
            and (match := _GITHUB_REMOTE.fullmatch(command[-2]))):
        command[-2], credentials = _git_remote(match.group(1))
        args = (*credentials, *command)
    git = "git"
    options = ["--no-replace-objects", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
               "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
               "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
               *(["-c", "protocol.https.allow=always"] if network else []),
               *([f"--git-dir={git_dir}"] if git_dir else []), "-C", str(cwd)]
    if command[:1] == ["push"]:
        # Host settlement and branch cleanup share this exact-update scan. No worker hook is trusted, including
        # reference-transaction hooks, so the transport below always has hooks disabled.
        if (len(command) != 7 or command[:4] != ["push", "--quiet", "--no-verify", "--porcelain"]
                or not command[4].startswith("--force-with-lease=")):
            raise Refused("host push requires one exact update and lease")
        head, separator, branch = command[6].partition(":")
        target, expected_separator, remote = command[4].removeprefix("--force-with-lease=").partition(":")
        if (not separator or (head and not _OBJECT_ID.fullmatch(head)) or not branch.startswith("refs/heads/")
                or not expected_separator or target != branch or (remote and not _OBJECT_ID.fullmatch(remote))
                or (not head and not remote)):
            raise Refused("host push requires one exact update and lease")
        update = (f"{branch if head else '(delete)'} {head or '0' * len(remote)} "
                  f"{branch} {remote or '0' * len(head)}\n").encode()
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                f"from {_PACKAGE}.opsec.git_push import main; "
                "raise SystemExit(main(sys.argv[2:4], stdin=sys.stdin.buffer.read(), chain=lambda *_: 0, "
                "transport=sys.argv[4]))")
        package_parent = str(machinery_path("dispatcher/write_boundary.py").parents[2])
        # The scan describes the transport below and nothing else. It runs every Git query, the private route
        # proof included, with the transport's absolute Git, its environment and its leading options (the fixed
        # settings above, then the request's credentials or own settings), so it reads exactly the configuration
        # the push reads: no global, system or command-scope host setting can mask an unverified route (#565).
        # It receives the URL as Git's hook would for a URL push (as the remote name too) and treats one that a
        # rewrite would redirect as untrusted. The scanner keeps its own process environment for its lookups only.
        git = shutil.which("git", path=env["PATH"]) or ""
        if not os.path.isabs(git):
            raise Refused("host push requires an absolute host Git")
        transport = json.dumps({"git": git, "environment": env, "options": [*options, *args[:len(args) - len(command)]]})
        # Fixed isolated interpreter and scanner code; input is the validated update above, never a shell command.
        # Its last argument is data the scanner parses as JSON (Transport.from_json), never a command or option.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        scanned = subprocess.run(
            [sys.executable, "-I", "-c", code, package_parent, command[5], command[5], transport],  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args
            cwd=cwd, input=update, capture_output=True, timeout=_GIT_TIMEOUT, check=False,
        )
        if scanned.returncode:
            raise Refused(scanned.stderr.decode("utf-8", "replace") or "host push scan refused")
    # Fixed argv: "git", or for a push the absolute Git the scan verified, resolved from the fixed env PATH above.
    result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [git, *options, *args],  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args
        capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False, env=env, stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        from lu_machinery.secret_redactor import redact_text

        # Redact before selecting/truncating: a credential may cross a line or the receipt's limit.
        lines = (redact_text(result.stderr) or "").splitlines()
        detail = f": {lines[0]}" if lines else ""
        if command[:1] == ["push"] and "[rejected]" in result.stdout and "(stale info)" in result.stdout:
            raise Refused(f"host push [rejected] (stale info){detail}")
        raise Refused(f"git {command[0]} failed{detail}")
    return result.stdout


def _owner_writable(path: Path, kind: Callable[[int], bool]) -> bool:
    """Whether ``path`` (not followed) is of ``kind`` and is one the host user owns and may write on disk."""
    try:
        status = path.lstat()
    except OSError:
        return False
    return kind(status.st_mode) and status.st_uid == os.geteuid() and os.access(path, os.W_OK)


def _writable_tracked_file(repository: Path) -> Path | None:
    """The first tracked regular file the host user owns and may write, for the canary's read-only bind probe.

    The kernel checks file permissions before the mount's read-only flag, so a tracked file that is read-only
    on disk gives EACCES under any mount and would test its mode, not the bind. The sandbox maps only the host
    user, so a file writable only through its group or another owner gives EACCES inside it as well.

    None when no tracked file is writable, the primary write guard's intended steady state: the canary's
    absent-path probes (create, mkdir, rename, unlink in the repository root) then prove the bind alone, which
    holds only while the root directory is one the host user owns and may write on disk, so an EROFS there can
    come from the mount and not from the directory's mode. A root that is not refuses.
    """
    for name in _git(repository, "ls-files", "-z").split("\0"):
        if name and _owner_writable(repository / name, stat.S_ISREG):
            return repository / name
    if _owner_writable(repository, stat.S_ISDIR):
        return None
    raise Refused("the primary has no tracked regular file and no repository root directory the host user may "
                  "write, so the canary cannot tell its read-only bind from file permissions")


def _listening_socket_paths() -> list[Path]:
    """Every listening path-named Unix socket of the host, from the kernel's table."""
    paths = set()
    with contextlib.suppress(OSError):
        for line in Path("/proc/net/unix").read_text(errors="replace").splitlines()[1:]:
            fields = line.split(None, 7)  # the path is the rest of the line and may hold spaces
            if len(fields) == 8 and fields[5] == "01" and fields[7].startswith("/"):
                # Resolved, so a name through a link (/var/run is /run) is judged by where it lives.
                paths.add(Path(os.path.realpath(fields[7])))
    return sorted(paths)


def _resolved(path: Path) -> Path | None:
    with contextlib.suppress(OSError, ValueError):
        return path.resolve(strict=True)
    return None


def _product_checkout() -> Path | None:
    """The product's primary checkout, a public checkout every run reads (shared skills, project interpreter)."""
    from lu_machinery.common.repo_root import ProductRootError, product_root

    with contextlib.suppress(ProductRootError, OSError, ValueError):
        return _resolved(main_checkout_root(product_root()))
    return None


def _private_state(checkouts: list[Path]) -> list[Path]:
    """Host state no worker reads (decision A4): the task store, the Fleet Comms plane, the scratch root that
    holds every task's private layers, and each checkout's private runtime state (``_CHECKOUT_PRIVATE``)."""
    from lu_machinery.common.task_store_paths import tasks_dir
    from lu_machinery.guardrails.worktree_containment import canonicalize

    # Configured roots are emptied where their own readers resolve them: the task store and the scratch root as
    # the paths those helpers return, the Fleet plane through Fleet's own ``canonicalize`` (``~`` and links). A
    # relative plane root names a different plane in each process's working directory, so it, like a root that
    # cannot be resolved, refuses the run.
    candidates: list[Path] = []
    with contextlib.suppress(OSError, ValueError):
        candidates.append(Path(os.path.realpath(tasks_dir())))
    if plane := os.environ.get("FLEET_COMMS_ROOT"):
        if not Path(plane).expanduser().is_absolute():
            raise Refused("FLEET_COMMS_ROOT is relative; its plane depends on the reader's working directory")
        try:
            candidates.append(canonicalize(plane))
        except (OSError, RuntimeError, ValueError):
            raise Refused("FLEET_COMMS_ROOT cannot be resolved as Fleet resolves it") from None
    candidates.append(Path(os.path.realpath(ensure_scratch_root())))
    for checkout in checkouts:
        for name in _CHECKOUT_PRIVATE:
            path = checkout / name
            if os.path.lexists(path):
                # A link is emptied where it points, so the state is gone under either name.
                candidates += [path] if not path.is_symlink() else []
                candidates.append(Path(os.path.realpath(path)))
    paths: list[Path] = []
    for path in candidates:
        if path.is_absolute() and os.path.lexists(path) and not path.is_symlink() and path not in paths:
            paths.append(path)
    return paths


def _package_roots(path_env: str, executables: list[str]) -> list[Path]:
    """Each ``/opt/<package>`` (FHS 3.0 §3.13) that an executable on ``path_env`` or in ``executables`` resolves into."""
    names = list(executables)
    for directory in path_env.split(os.pathsep):
        if directory and os.path.isabs(directory):
            with contextlib.suppress(OSError), os.scandir(directory) as entries:
                names += [entry.path for entry in entries]
    roots = set()
    for name in names:
        target = Path(os.path.realpath(name))
        if target.is_relative_to("/opt") and len(target.parts) > 2:
            roots.add(Path("/opt", target.parts[2]))
    return sorted(roots)


def _pre_commit_store(home: Path, env: dict[str, str]) -> Path | None:
    """pre-commit's hook store, found as the provider's pre-commit finds it in ``env``: ``PRE_COMMIT_HOME``, else
    ``pre-commit`` below ``XDG_CACHE_HOME`` or HOME's ``.cache``. None when the configured value is not absolute.

    ``env`` is the provider's own environment, never the launcher's host context: a provider with a private cache
    root has no host store restored."""
    store = env.get("PRE_COMMIT_HOME") or os.path.join(env.get("XDG_CACHE_HOME") or home / ".cache", "pre-commit")
    return Path(store) if os.path.isabs(store) else None


def _toolchain(home: Path, executable: str, env: dict[str, str]) -> list[Path]:
    """Read-only restores below HOME and the data roots: the provider's toolchain and this launcher's own
    interpreter and installation (decision A4). Nothing of a provider's configuration is one of them.

    pre-commit's hook store is part of the toolchain: a product commit's hooks run from it, and inside it cannot
    be fetched (GitHub is refused). Read-only, pre-commit records nothing and runs the installed environments;
    a hook whose environment is not installed fails the commit instead of installing one."""
    path_env = os.environ.get("PATH", os.defpath)
    candidates = [home / name for name in _HOME_TOOLCHAIN]
    if (store := _pre_commit_store(home, env)) is not None:
        candidates.append(store)
    candidates += [Path(entry) for entry in path_env.split(os.pathsep) if entry and os.path.isabs(entry)]
    candidates += _package_roots(path_env, [executable, sources_command()[0]])
    candidates += [_interpreter_root(), Path(sys.prefix), machinery_path("dispatcher/write_boundary.py").parents[2]]
    hidden_roots = [home, *_MASKED_ROOTS, *_DATA_ROOTS]
    # Never a whole emptied root or a parent of HOME (a PATH entry such as /opt or /home restores nothing).
    paths = sorted({path for candidate in candidates if (path := _resolved(candidate)) is not None
                    and path.is_dir() and _under(path, hidden_roots) and path not in hidden_roots
                    and not home.is_relative_to(path)})
    return [path for path in paths if not any(path != other and path.is_relative_to(other) for other in paths)]


def _hidden(path: Path, masks: list[Path], restores: list[Path]) -> bool:
    """Whether ``path`` is absent or empty inside: its deepest covering mount is a mask, not a restore.

    Masks come before restores in the argv, so a restore of the same path wins, and a restore's parents exist
    inside as its mount point's directories.
    """
    if any(restore != path and restore.is_relative_to(path) for restore in restores):
        return False
    depth, hidden = -1, False
    for root, is_mask in [*((mask, True) for mask in masks), *((restore, False) for restore in restores)]:
        if _under(path, [root]) and (len(root.parts) > depth or (len(root.parts) == depth and not is_mask)):
            depth, hidden = len(root.parts), is_mask
    return hidden


def _home_lower(lower: Path, home: Path, argv: list[str]) -> None:
    """Create, in HOME's otherwise empty lower layer, every mount point the argv puts below HOME.

    Its directories then come from the read-only lower layer, which the overlay refuses to rename (EXDEV), so a
    worker cannot move a restored path's parent aside and plant its own files at that path (review F1).
    """
    for index, arg in enumerate(argv):
        if arg in ("--bind", "--ro-bind"):
            target, directory = Path(argv[index + 2]), Path(argv[index + 1]).is_dir()
        elif arg == "--tmpfs":
            target, directory = Path(argv[index + 1]), True
        else:
            continue
        if target == home or not target.is_relative_to(home):
            continue
        path = lower / target.relative_to(home)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory:
            path.mkdir(mode=0o700, exist_ok=True)
        elif not os.path.lexists(path):
            _write_private(path, b"", 0o600)


def _github_credential_variable(name: str) -> bool:
    """An environment variable that can carry a GitHub credential or a credential program into the boundary."""
    upper = name.upper()
    return upper.startswith(("GH_", "GITHUB_", "GIT_SSH")) or upper.endswith("_ASKPASS")


def _github_credential_paths(home: Path) -> list[Path]:
    """The host login's GitHub credential stores: gh's configuration, Git's credential files, netrc and SSH keys.

    Each existing one is emptied inside (a directory becomes an empty tmpfs, a file reads as ``/dev/null``).
    Containment does not rest on this list: the egress proxy refuses GitHub's hosts inside the boundary.
    """
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    candidates = [config / "gh", home / ".config" / "gh", config / "git" / "credentials",
                  home / ".git-credentials", home / ".netrc", home / ".ssh"]
    if os.environ.get("GH_CONFIG_DIR"):
        candidates.insert(0, Path(os.environ["GH_CONFIG_DIR"]))
    paths = []
    for path in candidates:
        # A link is masked where it points, so the store is empty under either name.
        for name in (path, Path(os.path.realpath(path))):
            if name.is_absolute() and os.path.lexists(name) and not name.is_symlink() and name not in paths:
                paths.append(name)
    return paths


def github_networks() -> tuple:
    """GitHub's published address ranges for its web, API, Git and package hosts, read on the host now.

    Fails closed: without them the proxy could not refuse another name that resolves to GitHub.
    """
    import ipaddress

    from lu_machinery.common import github_client

    why = "the reply lacks complete web, api, git and packages ranges"
    try:
        proc = github_client.run(["gh", "api", "meta"], capture_output=True, text=True, check=False, timeout=60)
        meta = json_depth.loads(proc.stdout, max_depth=512) if proc.returncode == 0 else None
        if proc.returncode:
            # The client's typed refusal (for example github_rate_limited: core_below_landing_reserve), never gh's text.
            try:
                typed = json_depth.loads(proc.stdout or "{}", max_depth=8)
            except ValueError:
                typed = None
            typed = typed if isinstance(typed, dict) else {}
            code = ": ".join(str(typed[key])[:60] for key in ("error", "reason") if isinstance(typed.get(key), str))
            why = f"gh api meta exited {proc.returncode}" + (f": {code}" if code else "")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        meta, why = None, f"gh api meta failed: {type(exc).__name__}"
    networks = []
    ranges: dict = meta if isinstance(meta, dict) else {}
    complete = all(isinstance(ranges.get(key), list) and ranges[key] for key in _GITHUB_META_KEYS)
    for key in _GITHUB_META_KEYS if complete else ():
        for value in ranges[key]:
            try:
                networks.append(ipaddress.ip_network(value))
            except (TypeError, ValueError):
                complete = False
    if not complete or not any(network.version == 4 for network in networks):
        raise Refused(f"GitHub's published address ranges cannot be read ({why}); the boundary could not refuse GitHub")
    return tuple(dict.fromkeys(networks))


def _plain_git(repository: Path, *args: str, data: bytes | None = None) -> bytes:
    """Raw output of a read-only Git plumbing command, with no user or system configuration, hooks or fsmonitor."""
    env = {"PATH": os.defpath, "HOME": os.environ.get("HOME", "/"), "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}
    result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        ["git", "--no-replace-objects", "--literal-pathspecs", "-c", f"core.hooksPath={os.devnull}",
         "-c", "core.fsmonitor=false", "-C", str(repository), *args],
        input=data, capture_output=True, timeout=_GIT_TIMEOUT, check=False, env=env)
    if result.returncode:
        raise Refused(f"savant_snapshot_refused: git {args[0]} failed")
    return result.stdout


def _public_origin(repository: Path) -> str:
    """The primary origin's GitHub slug, refused unless it names a registered repository with a public role."""
    match = _GITHUB_REMOTE.fullmatch(_git(repository, "remote", "get-url", "origin").strip())
    if not (match and agy_sandbox.public_repository(match.group(1))):
        raise Refused("savant_snapshot_refused: origin is not a registered public repository")
    return match.group(1)


def _remote_tips(slug: str) -> dict[str, str]:
    """Branch tips GitHub serves anonymously for ``slug`` now: no login, credential helper or checkout config."""
    env = {"PATH": os.defpath, "HOME": os.devnull, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):  # the host's own route out, if it has one
        if os.environ.get(name):
            env[name] = os.environ[name]
    try:
        result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            ["git", "-c", "credential.helper=", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
             "ls-remote", "--", f"https://github.com/{slug}.git", "refs/heads/*"],
            cwd="/", capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode:
        raise Refused("savant_snapshot_refused: the public remote's branches could not be read anonymously")
    tips: dict[str, str] = {}
    for line in result.stdout.splitlines():
        oid, tab, ref = line.partition("\t")
        if tab and _OBJECT_ID.fullmatch(oid):
            tips[ref] = oid
    return tips


def _published(repository: Path, commit: str) -> None:
    """Refuse unless GitHub serves publicly a branch whose tip is ``commit``, or whose main branch contains it.

    Ancestry is read from local objects of the anonymously listed tip: object ids bind that history.
    """
    tips = _remote_tips(_public_origin(repository))
    if commit in tips.values():
        return
    main = tips.get("refs/heads/main", "")
    if main:
        try:
            _git(repository, "merge-base", "--is-ancestor", commit, main)
            return
        except Refused:
            pass
    raise Refused("savant_snapshot_refused: no branch of the public remote contains the start commit now")


def _known_secrets() -> tuple[bytes, ...]:
    """Values of the launcher's own credential-named variables (the run's SubQ key among them), as needles."""
    names = re.compile(r".*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL).*", re.IGNORECASE)
    return tuple(value.encode() for name, value in os.environ.items() if names.fullmatch(name) and len(value) >= 16)


def _sensitive(data: bytes, needles: tuple[bytes, ...]) -> bool:
    """Whether a file's bytes hold a private key, a known token format or one of the host's OPSEC needles."""
    return bool(_SNAPSHOT_SECRET.search(data)) or any(needle in data for needle in needles)


def _snapshot(repository: Path, commit: str, paths: list[str], target: Path, limit: int) -> dict:
    """Write the published ``commit``'s regular files below ``paths`` (default: all) into ``target``, read-only.

    The bytes are the commit's own blobs (``ls-tree`` and ``cat-file``), never an export that attributes or
    filters change. Refuses an unpublished commit, a ``paths`` entry that names nothing, more than
    ``MAX_SNAPSHOT_FILES`` files or ``limit`` bytes, and an empty result. Leaves out excluded names
    (``_SNAPSHOT_EXCLUDED``), links and submodules, and files whose content looks sensitive (``_sensitive``).
    """
    from pathlib import PurePosixPath

    if not _OBJECT_ID.fullmatch(commit):
        raise Refused("savant_snapshot_refused: the start commit is not an object id")
    _published(repository, commit)
    from lu_machinery.opsec.needles import NeedlesError, load_needles

    try:
        loaded = load_needles()
    except NeedlesError:
        raise Refused("savant_snapshot_refused: the host's OPSEC needles cannot be read") from None
    needles = (*(needle.encode() for needle in (*loaded.run_users, *loaded.home_dirs) if needle), *_known_secrets())
    entries: list[tuple[str, int, tuple[str, ...]]] = []
    left_out = total = 0
    for record in _plain_git(repository, "ls-tree", "-r", "-l", "-z", "--full-tree", commit, "--", *paths).split(b"\0"):
        if not record:
            continue
        meta, _, raw = record.partition(b"\t")
        mode, kind, oid_field, size_field = meta.split()
        name = raw.decode("utf-8", "surrogateescape")
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise Refused("savant_snapshot_refused: the tree names a path outside itself")
        if (kind != b"blob" or mode not in (b"100644", b"100755") or not name.isprintable()
                or any(_SNAPSHOT_EXCLUDED.fullmatch(part) for part in parts)):
            left_out += 1
            continue
        total += int(size_field)
        if total > limit or len(entries) >= MAX_SNAPSHOT_FILES:
            raise Refused(f"savant_snapshot_refused: the files to read exceed {limit} bytes or {MAX_SNAPSHOT_FILES} "
                          "files; name the task's files or directories with --read-path")
        entries.append((oid_field.decode("ascii"), int(size_field), parts))
    names = ["/".join(parts) for _oid, _size, parts in entries]
    for path in paths:
        prefix = path.rstrip("/")
        if not any(name == prefix or name.startswith(prefix + "/") for name in names):
            raise Refused("savant_snapshot_refused: a --read-path names no readable file at the start commit")
    if not entries:
        raise Refused("savant_snapshot_refused: no readable file is left to snapshot")
    blobs = _plain_git(repository, "cat-file", "--batch", data="".join(f"{oid}\n" for oid, _s, _p in entries).encode())
    target.mkdir(mode=0o700)
    offset, files = 0, 0
    for oid, size, parts in entries:
        header_end = blobs.index(b"\n", offset)
        if blobs[offset:header_end] != f"{oid} blob {size}".encode():
            raise Refused("savant_snapshot_refused: git cat-file returned another object")
        data = blobs[header_end + 1:header_end + 1 + size]
        offset = header_end + 2 + size
        if _sensitive(data, needles):
            left_out += 1
            continue
        destination = target.joinpath(*parts)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private(destination, data, 0o400)
        files += 1
    if not files:
        raise Refused("savant_snapshot_refused: no readable file is left to snapshot")
    return {"commit": commit, "files": files, "bytes": total, "left_out": left_out, "read_paths": len(paths)}


def _under(path: Path, roots) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _overlaps(path: Path, other: Path) -> bool:
    return path == other or path.is_relative_to(other) or other.is_relative_to(path)


class _BrokerRefusal(Exception):
    pass


def _host_child_env(task_id: str) -> dict[str, str]:
    """The launcher's environment for a host step, minus the hosting CLI's session variables."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE", "CODEX_THREAD", "LU_WRITE_BOUNDARY")) and key != "CLAUDECODE"}
    env["LEARN_UKRAINIAN_DISPATCH_TASK_ID"] = task_id  # attribute the host read to this review
    return env


class _Broker:
    """Launch-bound GitHub reads, served only to descendants of this run's bubblewrap process."""

    def __init__(self, boundary: _Boundary, path: Path):
        import socket

        self.boundary = boundary
        self.records: list[dict] = []
        self.seen: set[str] = set()
        self.children: set[subprocess.Popen[bytes]] = set()
        self.connections: set[threading.Thread] = set()
        self.closed = False
        # Serialize read requests; the state lock protects request ids, receipts and live children.
        self.serial, self.state = threading.Lock(), threading.Lock()
        self.slots = threading.BoundedSemaphore(_BROKER_CONNECTIONS)
        # The repository every GitHub verb acts on, and whether it is private, frozen now (#565): a later change
        # of the primary's origin or of the registry never moves this run's authority.
        self.launch_repository: tuple[str, bool] | _BrokerRefusal = self._resolve_repository()
        # A private read without --pr can bind only the open PR of this run's branch.
        self.bound_pr: int | None = None
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:  # the descriptor path avoids the sockaddr_un length limit, as in agy_sandbox.SourcesRelay
            self.server.bind(f"/proc/self/fd/{directory}/{path.name}")
        finally:
            os.close(directory)
        self.server.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.closed:
            if not self.slots.acquire(timeout=1):
                continue
            try:
                connection, _ = self.server.accept()
            except OSError:
                self.slots.release()
                return
            thread = threading.Thread(target=self._connection, args=(connection,), daemon=True)
            with self.state:
                self.connections.add(thread)
            thread.start()

    def _connection(self, connection) -> None:
        try:
            with connection, contextlib.suppress(OSError, ValueError):
                self._handle(connection)
        finally:
            with self.state:
                self.connections.discard(threading.current_thread())
            self.slots.release()

    def _peer_inside(self, connection) -> bool:
        """Whether the connecting process descends from this run's bubblewrap (PID namespaces nest under it)."""
        import socket
        import struct

        process = self.boundary.process
        if process is None:
            return False
        pid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                        struct.calcsize("3i")))[0]
        for _ in range(256):
            if pid == process.pid:
                return True
            if pid <= 1:
                return False
            try:
                pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                return False
        return False

    def _handle(self, connection) -> None:
        connection.settimeout(30)
        verb = request_id = None
        reply: dict[str, Any] = {"returncode": 2, "stdout": "", "stderr": ""}
        try:
            chunks, size = [], 0
            while size <= _MAX_REQUEST and (chunk := connection.recv(65536)):
                chunks.append(chunk)
                size += len(chunk)
            # Checked before the request is even parsed; read first so the refusal reaches the client.
            if not self._peer_inside(connection):
                raise _BrokerRefusal("the peer is not a process of this write boundary")
            try:
                request = json_depth.loads(b"".join(chunks), max_depth=512)
            except ValueError:
                request = None
            if not isinstance(request, dict) or set(request) != {"verb", "request_id", "payload"}:
                raise _BrokerRefusal("the request is malformed")
            verb, request_id, payload = request["verb"], request["request_id"], request["payload"]
            if verb not in _BROKER_VERBS:
                verb = str(verb)[:40]
                raise _BrokerRefusal("the verb is not one of " + ", ".join(_BROKER_VERBS))
            if not (isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)) or not isinstance(payload, dict):
                raise _BrokerRefusal("the request is malformed")
            with self.state:
                if request_id in self.seen:
                    raise _BrokerRefusal("the request id was already used: a request runs once")
                self.seen.add(request_id)
            step = getattr(self, "_" + verb.replace("-", "_"))
            with self.serial:
                if self.closed:
                    raise _BrokerRefusal("the read-only boundary is closing")
                reply = step(payload)
        except _BrokerRefusal as exc:
            reply["stderr"] = f"write boundary broker refused: {exc}\n"
        except Exception as exc:  # a host step that fails is reported, never retried
            reply.update(returncode=1, stderr=f"write boundary broker failed: {type(exc).__name__}\n")
        with self.state:
            self.records.append({"verb": verb, "request_id": request_id, "returncode": reply["returncode"],
                                 "refused": reply["stderr"][:200] if reply["returncode"] == 2 else None})
        connection.settimeout(None)
        connection.sendall(json.dumps(reply).encode())

    # -- launch-bound facts --------------------------------------------------------------------------------------

    def _resolve_repository(self) -> tuple[str, bool] | _BrokerRefusal:
        """The primary's origin on github.com and whether it is private, or the refusal every GitHub verb gets.

        It must be a registered repository (decision A6, as amended for #565 by designated approval C-5541):
        with a public role, or with a private role, where every admitted verb is bound to this task's own
        repository, branch and PR, so the host login never moves private content anywhere but back to its own
        repository. An unknown or unresolved origin is refused.
        """
        try:
            url = _git(self.boundary.repository, "config", "--get", "remote.origin.url", git_dir=self.boundary.common)
        except Refused:
            return _BrokerRefusal("the primary's origin is not a github.com repository")
        if not (match := _GITHUB_REMOTE.fullmatch(url.strip())):
            return _BrokerRefusal("the primary's origin is not a github.com repository")
        slug = match.group(1)
        if agy_sandbox.public_repository(slug):
            return slug, False
        if agy_sandbox.private_repository(slug):
            return slug, True
        return _BrokerRefusal("the primary's origin is not a registered repository")

    def _private(self) -> bool:
        """Whether the launch's repository is a registered private one (its verbs and reads are then narrowed)."""
        frozen = getattr(self, "launch_repository", None)
        return isinstance(frozen, tuple) and frozen[1]

    def _repository_slug(self) -> str:
        """The launch's repository, the only one every GitHub verb acts on (frozen when the broker started)."""
        frozen = self.launch_repository
        if isinstance(frozen, _BrokerRefusal):
            raise _BrokerRefusal(str(frozen))
        return frozen[0]

    def _gh_json(self, argv: list[str]) -> Any:
        """One fresh read through the shared GitHub client (its budget and Retry-After admission apply)."""
        from lu_machinery.common import github_client

        proc = github_client.run(["gh", *argv], capture_output=True, text=True, check=False,
                                 timeout=_BROKER_TIMEOUTS["gh"], fresh=True)
        try:
            return json_depth.loads(proc.stdout, max_depth=512) if proc.returncode == 0 else None
        except (TypeError, ValueError):
            return None

    def _pull_request(self, payload: dict, *states: str) -> tuple[int, str, dict]:
        """The PR in ``payload["pr"]``, checked live on GitHub: same repository, head branch the dispatched one.

        For a private repository (#565 round 1, F1) the PR must also be the task's one authorized PR: the launch's
        ``--pr`` when it names one, else the PR this method bound on the task's first PR operation, which had to be
        open, from this repository, with the dispatched branch as head and the default branch as base. A head
        branch name alone never authorizes a PR: a closed or historical PR of the same branch, or one against
        another base, is refused.
        """
        number = payload.get("pr")
        if type(number) is not int or not 0 < number < 10**8:
            raise _BrokerRefusal("pr must be a PR number")
        slug = self._repository_slug()
        self._claimed({"repo": payload.get("repo")}, slug)
        private = self._private()
        authorized = self._authorized_pr() if private else None
        if authorized is not None and number != authorized:
            raise _BrokerRefusal(f"the PR is not this task's own PR ({authorized}) in a private repository")
        facts = self._gh_json(["pr", "view", str(number), "--repo", slug, "--json",
                               "state,headRefName,headRefOid,isCrossRepository,baseRefName"])
        if not isinstance(facts, dict):
            raise _BrokerRefusal("the PR cannot be read on GitHub")
        branch = self.boundary.target.removeprefix("refs/heads/")
        if facts.get("isCrossRepository") is not False or facts.get("headRefName") != branch:
            raise _BrokerRefusal("the PR's head is not this task's dispatched branch")
        if private:
            if facts.get("baseRefName") != self._default_branch(slug):
                raise _BrokerRefusal("the PR's base is not the repository's default branch")
            if authorized is None:
                if facts.get("state") != "OPEN":
                    raise _BrokerRefusal("a private task binds only its open PR as its own")
                self.bound_pr = number
        if facts.get("state") not in states:
            raise _BrokerRefusal(f"the PR is not {' or '.join(states)}")
        return number, slug, facts

    def _authorized_pr(self) -> int | None:
        """The private task's one authorized PR: the launch's ``--pr``, else its bound own PR, else none yet."""
        launch_pr = getattr(self.boundary.args, "pr", None)
        return launch_pr if launch_pr is not None else getattr(self, "bound_pr", None)

    def _default_branch(self, slug: str) -> str:
        """The repository's default branch on GitHub: the only base a worker's PR gets."""
        facts = self._gh_json(["repo", "view", slug, "--json", "defaultBranchRef"])
        name = (facts or {}).get("defaultBranchRef", {}).get("name") if isinstance(facts, dict) else None
        if not (isinstance(name, str) and _BRANCH.fullmatch(name)):
            raise _BrokerRefusal("the repository's default branch cannot be read on GitHub")
        return name

    def _claimed(self, payload: dict, slug: str) -> None:
        """Refuse a named repository other than the launch's own."""
        repo = payload.get("repo")
        if repo is not None and not (isinstance(repo, str) and repo.lower() == slug.lower()):
            raise _BrokerRefusal("repo is not this task's repository")
    # -- host steps ----------------------------------------------------------------------------------------------

    def _run(self, argv: list[str], timeout_key: str, *, stdin: bytes | None = None,
             env: dict[str, str] | None = None, cwd: Path | None = None) -> dict:
        env = {**_host_child_env(self.boundary.args.task_id), **(env or {})}
        with self.state:  # cleanup kills every live step, so none may start once it has begun
            if self.closed:
                raise _BrokerRefusal("the write boundary is closing")
            # A fixed argv built by this class from validated fields, never a shell.
            child = subprocess.Popen(argv, cwd=cwd or self.boundary.repository, env=env, stdin=subprocess.PIPE,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.children.add(child)
        try:
            out, err = child.communicate(stdin, timeout=_BROKER_TIMEOUTS[timeout_key])
            returncode = child.returncode
        except subprocess.TimeoutExpired:
            child.kill()
            out, err = child.communicate()
            returncode, err = 124, err + b"\nwrite boundary broker: the host step timed out\n"
        finally:
            with self.state:
                self.children.discard(child)
        return {"returncode": returncode, "stdout": out[:_MAX_REQUEST].decode("utf-8", "replace"),
                "stderr": err[:_MAX_REQUEST].decode("utf-8", "replace")}

    def _module(self, module: str, argv: list[str], timeout_key: str, stdin: bytes | None = None,
                cwd: Path | None = None) -> dict:
        code = (f"import sys; sys.path.insert(0, sys.argv[1]); from {_PACKAGE}.{module} import main; "
                "raise SystemExit(main(sys.argv[2:]))")
        package_parent = str(machinery_path("dispatcher/write_boundary.py").parents[2])
        return self._run([sys.executable, "-I", "-c", code, package_parent, *argv], timeout_key, stdin=stdin, cwd=cwd)

    def _github_read(self, payload: dict) -> dict:
        """A named read of this repository through the publisher's fixed GET templates (no write is possible)."""
        if not set(payload) <= {"name", "number", "sha", "repo", "paginate"} or "name" not in payload:
            raise _BrokerRefusal("github-read takes name, number, sha, repo and paginate")
        name, paginate = payload["name"], payload.get("paginate", False)
        if type(paginate) is not bool:
            raise _BrokerRefusal("paginate must be true or false")
        if name not in GITHUB_READS:
            raise _BrokerRefusal("name must be one of " + ", ".join(sorted(GITHUB_READS)))
        fields = []
        if (number := payload.get("number")) is not None:
            if type(number) is not int or not 0 < number < 10**12:
                raise _BrokerRefusal("number must be a positive id")
            fields += ["--number", str(number)]
        if (sha := payload.get("sha")) is not None:
            if not (isinstance(sha, str) and _OBJECT_ID.fullmatch(sha)):
                raise _BrokerRefusal("sha must be a full commit id")
            fields += ["--sha", sha]
        slug = self._repository_slug()
        self._claimed({"repo": payload.get("repo")}, slug)
        if self._private():
            self._private_read_scope(name, number, sha)
        return self._module("publish.github", ["read", name, "--repo", slug, *fields,
                                               *(["--paginate"] if paginate else [])], "gh")

    def _private_read_scope(self, name: str, number: int | None, sha: str | None) -> None:
        """Refuse a private repository's read that is not the authorized PR or its checks (C-5541).

        The authorized PR is the one the launch names (``--pr``, frozen from the task record: a review's PR),
        else the task's own PR bound by ``_pull_request`` (open, dispatched head branch, default base, checked
        live). Its checks are read by commit: this task's branch tip, or the authorized PR's live head. Reads by another id (a workflow run, a check
        run, another PR or issue) are refused, so no unrelated private content reaches a worker.
        """
        launch_pr = getattr(self.boundary.args, "pr", None)
        if name in _PRIVATE_PR_READS and number is not None and sha is None:
            if launch_pr is not None:
                if number != launch_pr:
                    raise _BrokerRefusal("a private repository's read names a PR other than this task's")
                return
            # The task's own PR only: bound on its first PR operation, never a same-named branch's other PR (F1).
            self._pull_request({"pr": number}, "OPEN", "MERGED", "CLOSED")
            return
        if name in _PRIVATE_SHA_READS and sha is not None and number is None:
            if sha == self.boundary.current_tip():
                return
            if (authorized := self._authorized_pr()) is not None:
                _number, _slug, facts = self._authorized_pull_request(authorized)
                if sha == facts.get("headRefOid"):
                    return
            raise _BrokerRefusal("a private repository's read names a commit other than this task's PR head")
        raise _BrokerRefusal("a private repository's github-read is only this task's PR (pull, pr-files, commits, "
                             "comments, reviews, timeline, issue by its number) and its checks (checks, commit by "
                             "its head)")

    def _authorized_pull_request(self, number: int) -> tuple[int, str, dict]:
        """The launch's own PR (``--pr``) read live: same repository, not from a fork."""
        slug = self._repository_slug()
        facts = self._gh_json(["pr", "view", str(number), "--repo", slug, "--json",
                               "state,headRefName,headRefOid,isCrossRepository,baseRefName"])
        if not isinstance(facts, dict) or facts.get("isCrossRepository") is not False:
            raise _BrokerRefusal("the task's PR cannot be read on GitHub")
        return number, slug, facts

    def cleanup(self) -> None:
        import socket

        with self.state:
            self.closed = True
            children = list(self.children)
        # On Linux, close() does not wake a thread blocked in accept(); shutdown() does (accept fails with EINVAL),
        # so the serve thread exits now instead of every boundary waiting out the join's whole timeout.
        with contextlib.suppress(OSError):
            self.server.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.server.close()
        for child in children:
            if child.poll() is None:
                with contextlib.suppress(OSError):
                    child.kill()
        # Within one 5 s budget, so the receipt keeps the record of every request whose step was just killed.
        deadline = time.monotonic() + 5
        self.thread.join(timeout=5)
        with self.state:
            connections = list(self.connections)
        for thread in connections:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


class _Boundary:
    def __init__(self, args: argparse.Namespace, receipt: dict):
        self.args, self.receipt = args, receipt
        self.worktree = Path(args.worktree).resolve(strict=True)
        self.base = Path(args.scratch)
        self.process: subprocess.Popen[bytes] | None = None
        self.egress: Any = None
        self.broker: _Broker | None = None
        self.relay: Any = None
        self.gh: Any = None
        self.created = False
        self.created_outputs: list[Path] = []

    def run(self) -> int:
        from lu_machinery.agent_runtime.attempt_network import AttemptEgress

        if reason := unavailable():
            raise Refused(reason)
        from lu_machinery.review.isolation import ReviewIsolationError, _resolve_fixed_system_executable

        try:
            bwrap = _resolve_fixed_system_executable("bwrap", reject_roots=())
        except ReviewIsolationError as exc:
            raise Refused("bubblewrap is unavailable") from exc
        # The private HOME layer uses bubblewrap overlays (--overlay-src, bubblewrap 0.10 and later).
        usage = subprocess.run([str(bwrap), "--help"], capture_output=True, text=True, timeout=30, check=False,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                               stdin=subprocess.DEVNULL)
        if "--overlay-src" not in usage.stdout + usage.stderr:
            raise Refused("bubblewrap 0.10 or later is required (--overlay-src)")
        command = self.args.command[1:] if self.args.command[:1] == ["--"] else self.args.command
        if not command or not Path(command[0]).is_absolute():
            raise Refused("the provider executable is not an absolute path")
        git_dir = Path(_git(self.worktree, "rev-parse", "--absolute-git-dir").strip()).resolve(strict=True)
        common = Path(_git(self.worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
                      ).resolve(strict=True)
        repository = common.parent
        if main_checkout_root(self.worktree).resolve(strict=True) != repository or common.name != ".git":
            raise Refused("store_primary_disagreement")
        if git_dir == common or git_dir.parent != common / "worktrees":
            raise Refused("the task does not run in a linked worktree of the primary")
        if not self.worktree.is_relative_to(repository) and _overlaps(self.worktree, Path.home()):
            raise Refused("the worktree overlaps HOME outside the primary")
        dot_git = self.worktree / ".git"
        if dot_git.is_symlink() or not dot_git.is_file():
            raise Refused("the worktree's .git is not a link file")
        self.branch = _git(self.worktree, "symbolic-ref", "-q", "HEAD").strip()
        if not self.branch.startswith("refs/heads/"):
            raise Refused("the worktree is not on a branch")
        self.tip = _git(self.worktree, "rev-parse", "--verify", "HEAD^{commit}").strip()
        self.target = self.branch
        self.common, self.git_dir, self.repository = common, git_dir, repository
        # The primary's own Git state stays read-only inside; that includes
        # its checked-out branch when the ref is a loose file (a packed one is covered by packed-refs).
        self.protected = [name for name in _PRIMARY_GIT_FILES if os.path.lexists(common / name)]
        try:
            primary_branch = _git(repository, "symbolic-ref", "-q", "HEAD").strip()
        except Refused:  # a detached primary has no checked-out branch; its HEAD file is protected
            primary_branch = ""
        if primary_branch.startswith("refs/heads/") and (common / primary_branch).is_file():
            self.protected.append(primary_branch)
        home = Path(os.environ.get("HOME") or Path.home()).resolve(strict=True)
        root = ensure_scratch_root().resolve()
        # The private layers must lie outside the overlay lowers and the read-only primary bind.
        for name, path in (("HOME", home), ("the primary", repository)):
            if root.is_relative_to(path):
                raise Refused(f"the scratch root is inside {name}")
        if not _under(self.base, [root]) or self.base.parent.resolve() != root:
            raise Refused("the scratch is not directly under the scratch root")

        self.base.mkdir(mode=0o700)  # exclusive: an existing scratch is never reused
        self.created = True
        boundary, tmp = self.base / "boundary", self.base / "tmp"
        layer_names = ("home-upper", "home-work")
        layers = {name: self.base / name for name in layer_names}
        for directory in (boundary, tmp, *layers.values()):
            directory.mkdir(mode=0o700)
        for name, source in _PROJECTED.items():
            _write_private(boundary / name, source.read_bytes(), 0o500)
        if self.args.snapshot_bytes is not None:
            self.receipt["snapshot"] = _snapshot(repository, self.tip, self.args.read_path,
                                                 boundary / Path(SNAPSHOT_INSIDE).name, self.args.snapshot_bytes)

        outputs = []
        for raw in self.args.output or []:
            path = Path(raw)
            resolved = Path(os.path.realpath(path)) if path.is_absolute() else path
            if (not path.is_absolute() or path.is_symlink() or _overlaps(resolved, common)
                    or _overlaps(resolved, self.worktree)):
                raise Refused("a declared output is not a plain path outside Git metadata and the worktree")
            if not path.exists():
                _write_private(path, b"", 0o600)
                self.created_outputs.append(path)
            elif not path.is_file():
                raise Refused("a declared output is not a regular file")
            outputs.append(Path(os.path.realpath(path)))
        credentials = []
        for raw in self.args.credential or []:
            path = Path(raw)
            # Bound where it resolves: a HOME named through a link holds no mount point of its own.
            resolved = Path(os.path.realpath(path)) if path.is_absolute() else path
            if path.is_symlink() or not resolved.is_file() or _overlaps(resolved, repository):
                raise Refused("a credential file is not a regular file outside the primary")
            credentials.append(resolved)

        if _under(_interpreter_root(), _MASKED_ROOTS):
            # Those roots stay writable inside, so a worker could move the installation's parent aside and plant
            # its own interpreter at the fixed Sources command's path (review F1 of PR 492, F2 of PR 528).
            raise Refused("the Sources interpreter is installed below a writable private root (/tmp, /run, /dev/shm)")
        # The run's view (decision A4). HOME's lower layer is empty but for the mount points below; the data
        # roots outside it are empty too. Restored read-only: the toolchain, the public checkouts (the primary and
        # the product) without their private runtime state, and this task's own records. The selected login stays
        # a writable bind so a refresh is kept; no other provider configuration is restored.
        from lu_machinery.dispatcher.runtime_context import shared_root, task_reads

        product = _product_checkout()
        # A snapshot run sees no checkout, worktree or Git metadata at all: its tools read the snapshot only,
        # and the OS view, not just OpenCode's permission rules, keeps everything else out of reach.
        snapshot_only = self.args.snapshot_bytes is not None
        checkouts = [] if snapshot_only else [repository, *([product] if product is not None and product != repository
                                                            else [])]
        toolchain = _toolchain(home, command[0], self.args.provider_env)
        data = list(dict.fromkeys(found for root in _DATA_ROOTS if (found := _resolved(root)) is not None
                                  and found.is_dir() and found != Path("/")))
        own = task_reads(self.args.task_id)
        base_masks, base_restores = [*_MASKED_ROOTS, *data, home], [*toolchain, *checkouts]
        private = [path for path in _private_state(checkouts) if not _hidden(path, base_masks, base_restores)]
        # One emptied directory covers everything below it, including another private path there.
        private = [path for path in private if not any(other != path and other.is_dir() and path.is_relative_to(other)
                                                       for other in private)]
        # A release or its advertised resources may live below /run or /tmp: restore the shared skills there,
        # never their siblings or the rest of the task store.
        shared = _resolved(shared_root())
        projected_reads = [shared] if shared is not None and shared.is_dir() and _under(shared, _MASKED_ROOTS) else []
        restores = [*base_restores, *own, *([] if snapshot_only else [self.worktree]), *outputs, *credentials, tmp,
                    boundary, *projected_reads]
        if self.args.runtime is not None:
            restores.append(Path(self.args.runtime))
        if self.args.codex_auth is not None:
            restores.append(self.base / _CODEX_HOME)
        sources_mcp = self._sources_mcp(boundary, home)
        if sources_mcp is not None:
            restores.append(sources_mcp)
        cursor_config = self._cursor_permissions()
        if cursor_config is not None:
            restores.append(home / _CURSOR_CLI_CONFIG_HOME)
        login, login_reads = self._claude_login(home, repository)
        restores += [*([login[1]] if login else []), *login_reads]
        masks = [*base_masks, *private]
        named_home = Path(os.environ.get("HOME") or Path.home()).absolute()
        # HOME named through a link the view empties (below a data root, a private tmpfs or a checkout's emptied
        # state) stays a link to the overlay. Every bind is at a resolved path, so nothing else is below the name.
        alias = named_home if named_home != home and _hidden(named_home, masks, restores) else None
        if alias is not None:
            restores.append(alias)
        sockets = [path for path in _listening_socket_paths()
                   if not _hidden(path, masks, restores) and os.path.lexists(path)]
        nonce = uuid.uuid4().hex
        probes = self._probes(repository, common, sockets, outputs, tmp, nonce, snapshot_only)
        probes += self._view_probes(home, data, masks, restores, private, credentials, own, nonce)
        for path in projected_reads:
            probes.append({"op": "create", "path": str(path / f".write-boundary-canary-{nonce}"), "expect_any": ["EROFS"]})
        self.receipt["canary"] = {"probes_planned": len(probes), "passed": False}
        self.receipt["view"] = {"data_roots": len(data), "toolchain": len(toolchain), "checkouts": len(checkouts),
                                "private_masks": len(private), "own_records": len(own)}

        lower = self.base / "home-lower"
        lower.mkdir(mode=0o700)
        if cursor_config is not None:
            (lower / _CURSOR_CLI_CONFIG_HOME).parent.mkdir(mode=0o700, parents=True)
            _write_private(lower / _CURSOR_CLI_CONFIG_HOME, cursor_config, 0o600)
        argv = [
            str(bwrap), "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            *(arg for masked in (*_MASKED_ROOTS, *data) for arg in ("--tmpfs", str(masked))),
            "--overlay-src", str(lower), "--overlay", str(layers["home-upper"]), str(layers["home-work"]), str(home),
            *(arg for path in base_restores for arg in ("--ro-bind", str(path), str(path))),
        ]
        for path in private:
            argv += ["--tmpfs", str(path)] if path.is_dir() else ["--ro-bind", "/dev/null", str(path)]
        if login is not None:  # the selected login's private projection; its key file and credential go on top
            argv += ["--bind", str(login[0]), str(login[1])]
        for path in (*own, *login_reads):
            argv += ["--ro-bind", str(path), str(path)]
        if not snapshot_only:
            argv += ["--ro-bind", str(common), str(common)]
            for name in self.protected:
                argv += ["--ro-bind", str(common / name), str(common / name)]
            argv += ["--ro-bind", str(self.worktree), str(self.worktree),
                     "--ro-bind", str(dot_git), str(dot_git)]
        for path in credentials:
            argv += ["--bind", str(path), str(path)]
        for path in outputs:
            argv += ["--bind", str(path), str(path)]
        argv += ["--bind", str(tmp), str(tmp), "--ro-bind", str(boundary), str(boundary)]
        runtime = tmp
        if self.args.runtime is not None:
            from lu_machinery.agent_runtime.read_only_tmp import dispatch_lease

            runtime = Path(self.args.runtime)
            if runtime != dispatch_lease(self.args.task_id) or runtime.is_symlink() or not runtime.is_dir():
                raise Refused("the runtime is not this read-only task's scratch lease")
            argv += ["--bind", str(tmp), str(runtime)]
        for path in projected_reads:
            argv += ["--ro-bind", str(path), str(path)]
        codex_home = None
        if self.args.codex_auth is not None:
            codex_home = self._codex_home(credentials)
            # Codex writes its state there, but the trusted Sources definition stays read-only (as review F1).
            argv += ["--bind", str(codex_home), str(codex_home),
                     "--ro-bind", str(codex_home / "config.toml"), str(codex_home / "config.toml")]
        if sources_mcp is not None and sources_mcp != boundary / _SOURCES_MCP:
            # The provider reads it from HOME; it stays read-only there, as Codex's Sources definition.
            argv += ["--ro-bind", str(boundary / _SOURCES_MCP), str(sources_mcp)]
        # No GitHub credential is readable inside (PUBLISH-CRED-1113): the host login's known stores are empty.
        for path in _github_credential_paths(home):
            if not _hidden(path, masks, restores):
                argv += ["--tmpfs", str(path)] if path.is_dir() else ["--ro-bind", "/dev/null", str(path)]
        for path in sockets:
            argv += ["--ro-bind", "/dev/null", str(path)]
        # A Savant run gets no GitHub relay at all: its tools read only the snapshot.
        self.broker = None if self.args.no_github_relays else _Broker(self, boundary / "broker.sock")
        argv += ["--ro-bind", str(boundary), _INSIDE]
        interpreter = sources_command()[0]
        # The emptied roots are read-only, so no worker moves a restored path's parent aside to plant files there.
        if alias is not None:
            argv += ["--symlink", str(home), str(alias)]
        emptied = [path for path in (*data, *private) if path.is_dir() and path != home and path not in restores]
        argv += [arg for path in emptied for arg in ("--remount-ro", str(path))]
        _home_lower(lower, home, argv)
        # Reviews read GitHub through the host's read-only gh relay: the reviewer holds
        # no GitHub login and the egress proxy refuses GitHub, so without it ``gh pr view``/``pr checks`` fail.
        (boundary / "bin").mkdir(mode=0o700)
        _write_private(boundary / "json_depth.py", Path(json_depth.__file__).read_bytes(), 0o400)
        if not self.args.no_github_relays:
            _write_private(boundary / "bin" / "gh", agy_sandbox._GH_SHIM.format(interpreter=interpreter).encode(), 0o500)
            self.gh = agy_sandbox._GhBroker(boundary / "gh.sock", self.worktree)
        try:
            # Sources goes through the read-only AGY boundary's relay: read-only tools only, one server per
            # connection, started by the launcher from the product checkout. Providers reach it through
            # ``sources_command``.
            from lu_machinery.agent_runtime.sources_read_only import sources_tool_sets

            readers, _writers = sources_tool_sets()
            self.relay = agy_sandbox.SourcesRelay(boundary / "sources.sock", frozenset(readers), {
                name: os.environ[name] for name in ("PATH", "HOME", "LANG", "TMPDIR") if name in os.environ})
        except Exception as exc:  # Sources is optional; containment does not depend on it
            self.receipt["sources"] = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        argv += ["--cap-drop", "ALL", "--chdir", SNAPSHOT_INSIDE if snapshot_only else str(self.worktree)]

        self.egress = AttemptEgress(boundary / "egress.sock", refused_domains=GITHUB_DOMAINS,
                                    refused_networks=github_networks(), upload_ceiling=self.args.upload_ceiling)
        report_read, report_write = os.pipe()
        go_read, go_write = os.pipe()
        _write_private(boundary / "plan.json", json.dumps(
            {"report_fd": report_write, "go_fd": go_read, "probes": probes}).encode(), 0o444)
        argv += [interpreter, "-I", str(boundary / "canary.py"), str(boundary / "plan.json"), "--",
                 interpreter, "-I", str(boundary / "forwarder.py"), str(boundary / "egress.sock"), *command]
        self.receipt["bwrap"] = str(bwrap)
        self.receipt["argv_sha256"] = __import__("hashlib").sha256(json.dumps(argv).encode()).hexdigest()
        env = {key: value for key, value in self.args.provider_env.items()
               if key not in {"SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "TMUX", "TMUX_PANE"}
               and not _github_credential_variable(key)}
        (tmp / "scratch").mkdir(mode=0o700)
        (tmp / "run").mkdir(mode=0o700)
        # The namespace has no DNS or route out; Node's own fetch uses the forwarder's proxy only when told to.
        # The host's task scratch (/var/tmp/<task-id>) is read-only inside, so it moves under TMPDIR too; the worker
        # still creates it on demand, and it goes with the boundary's private scratch.
        env.update(TMPDIR=str(runtime), LU_SCRATCH_ROOT=str(runtime / "scratch"), XDG_RUNTIME_DIR=str(runtime / "run"),
                   LU_TASK_SCRATCH=str(runtime / "task"), GIT_TERMINAL_PROMPT="0", NODE_USE_ENV_PROXY="1", **{BROKER_ENV: _BROKER_INSIDE})
        env["PATH"] = str(boundary / "bin") + os.pathsep + env.get("PATH", os.defpath)
        if codex_home is not None:
            env["CODEX_HOME"] = str(codex_home)
        if self.args.sources_mcp == "opencode":
            env["OPENCODE_CONFIG"] = _SOURCES_MCP_INSIDE
        if (store := _pre_commit_store(home, self.args.provider_env)) is not None and (found := _resolved(store)) is not None and found.is_dir():
            # The store is restored at its resolved path (``_toolchain``); a link on pre-commit's lookup path, such
            # as HOME's .cache, is not in the view, so pre-commit is told the directory it resolves on the host.
            env["PRE_COMMIT_HOME"] = str(found)
        try:
            # The argv is the fixed bubblewrap binary and launcher-validated paths; env is the dispatcher's
            # provider environment, passed on deliberately (the provider runs inside with it).
            process = subprocess.Popen(argv, env=env, cwd=self.worktree, pass_fds=(report_write, go_read))  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-tainted-env-args
            self.process = process
        finally:
            os.close(report_write)
            os.close(go_read)
        try:
            outcomes = self._report(report_read)
            failed = [probe for probe in outcomes or [] if probe.get("outcome") not in probe.get("expect_any", [])]
            passed = (isinstance(outcomes, list) and len(outcomes) == len(probes) and not failed and all(
                {key: probe.get(key) for key in planned} == planned for probe, planned in zip(outcomes, probes,
                                                                                              strict=True)))
            self.receipt["canary"].update(probes=outcomes, passed=passed)
            if not passed:
                raise Refused("the write boundary canary failed: " + ", ".join(
                    f"{probe.get('op')} {probe.get('path')} gave {probe.get('outcome')}" for probe in failed[:5]))
            os.write(go_write, b"1")
        finally:
            os.close(go_write)
            os.close(report_read)
        self.receipt["status"] = "ran"
        returncode = process.wait()
        self.receipt["returncode"] = returncode
        self.receipt["import"] = {"status": "unchanged", "before": self.tip, "after": self.tip}
        return returncode

    def _claude_login(self, home: Path, repository: Path) -> tuple[tuple[Path, Path] | None, list[Path]]:
        """The selected Claude login's projection, wherever it lives: a private directory bound at its resolved
        path, and the read-only reads its key helper needs.

        The directory holds copies of the state its launch guard binds (#10619) and the CLI reads at start: the
        user settings, legacy global config and policy caches whole, the global config only as its ``env`` map
        and account and organisation ids. The CLI may rewrite them there; the directory goes with the run's
        scratch. The selected credential is bound over it (``--credential``). An API-key login's key helper
        (C-2643) must be ``cat <absolute file>``: that file is its selected credential, restored read-only; any
        other form is refused, since what it reads cannot be named. A profile store the CLI would prefer to the
        login is refused too. Nothing else of the login directory, and nothing of another login, is visible.
        """
        from lu_machinery.agent_runtime.env_sanitize import (
            CLAUDE_POLICY_CACHE_NAMES,
            CLAUDE_PROFILE_STORE_ENTRIES,
            claude_profile_store_dirs,
        )

        if self.args.claude_login is None:
            return None, []
        login = Path(self.args.claude_login)
        if not login.is_absolute() or not login.is_dir():
            raise Refused("--claude-login is not an absolute login directory")
        login = login.resolve(strict=True)
        if home.is_relative_to(login) or _overlaps(login, repository) or _overlaps(login, self.worktree):
            raise Refused("the Claude login directory overlaps HOME itself or the checkout")
        for store in claude_profile_store_dirs(os.environ):
            if any(os.path.lexists(store / name) for name in CLAUDE_PROFILE_STORE_ENTRIES):
                raise Refused("a Claude profile store would replace the selected login; it is not projected")
        copies: dict[Path, bytes] = {}
        for name in (*_CLAUDE_LOGIN_FILES, *CLAUDE_POLICY_CACHE_NAMES, _CLAUDE_GLOBAL_CONFIG):
            path = login / name
            try:
                status = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_LOGIN_FILE:
                raise Refused("a Claude login file is not a regular file of bounded size")
            data = path.read_bytes()
            if name == _CLAUDE_GLOBAL_CONFIG:
                try:
                    config = json_depth.loads(data, max_depth=512)
                except ValueError:
                    raise Refused("the Claude global config is not JSON") from None
                if not isinstance(config, dict):
                    raise Refused("the Claude global config is not a JSON object")
                kept: dict[str, Any] = {key: config[key] for key in ("env",) if key in config}
                if isinstance(account := config.get("oauthAccount"), dict):
                    kept["oauthAccount"] = {key: account.get(key) for key in ("accountUuid", "organizationUuid")}
                data = json.dumps(kept).encode()
            copies[path] = data
        reads = []
        try:
            helper = json_depth.loads(copies.get(login / "settings.json", b"{}"), max_depth=512).get("apiKeyHelper")
        except (ValueError, AttributeError):
            helper = None
        if helper is not None:
            words = shlex.split(helper) if isinstance(helper, str) else []
            key = Path(words[1]) if len(words) == 2 and words[0] == "cat" else None
            if key is None or not key.is_absolute() or key.is_symlink() or not key.is_file():
                raise Refused("the selected Claude key helper is not 'cat <absolute file>'")
            reads.append(Path(os.path.realpath(key)))
        projection = self.base / "claude-login"
        projection.mkdir(mode=0o700)
        for path, content in copies.items():
            _write_private(projection / path.name, content, 0o600)
        self.receipt["claude_login"] = {"copies": sorted(path.name for path in copies), "key_file": bool(reads)}
        return (projection, login), reads

    def _codex_home(self, credentials: list[Path]) -> Path:
        """Project the task-local CODEX_HOME into this run's private scratch and return it.

        It holds the trusted Sources definition and a link to the selected account's ``auth.json``, which a
        ``--credential`` bind keeps writable for a token refresh. Nothing else of the real home is a layer
        of the run (A2: CLI overrides merge into the loaded layers rather than replace them). It sits beside
        TMPDIR, not in it: Codex creates no helper PATH aliases under its temporary directory.
        """
        auth = Path(self.args.codex_auth)
        if auth.is_absolute():
            auth = Path(os.path.realpath(auth))
        if not auth.is_absolute() or auth.name != "auth.json":
            raise Refused("--codex-auth is not an absolute auth.json path")
        home = self.base / _CODEX_HOME
        home.mkdir(mode=0o700)
        config = codex_home_config().encode()
        _write_private(home / "config.toml", config, 0o600)
        linked = auth.is_file()
        if linked:
            os.symlink(auth, home / "auth.json")
        self.receipt["codex_home"] = {"config_sha256": hashlib.sha256(config).hexdigest(),
                                      "auth": "linked" if linked else "absent",
                                      "auth_writable": auth in credentials}
        return home

    def _sources_mcp(self, boundary: Path, home: Path) -> Path | None:
        """Write the provider's task-local Sources MCP file (``--sources-mcp``) and return where it reads it.

        The file is in the boundary directory, read-only inside; AGY and Cursor read it at its bind below HOME,
        OpenCode at its fixed path inside (``OPENCODE_CONFIG``).
        """
        agent = self.args.sources_mcp
        if agent is None:
            return None
        if self.args.mode != "workspace-write" and agent != "cursor":
            raise Refused("--sources-mcp projects a write worker's MCP file only, or a read-only Cursor run's")
        config = sources_mcp_config(agent).encode()
        _write_private(boundary / _SOURCES_MCP, config, 0o600)
        relative = _SOURCES_MCP_HOME[agent]
        target = home / relative if relative is not None else boundary / _SOURCES_MCP
        self.receipt["sources_mcp"] = {"provider": agent, "config_sha256": hashlib.sha256(config).hexdigest()}
        return target

    def _cursor_permissions(self) -> bytes | None:
        """A read-only Cursor run's permission profile, else None.

        Without the Sources partition no Sources tool is allowed, and Cursor refuses an MCP call no rule allows.
        """
        if self.args.mode != "read-only" or self.args.sources_mcp != "cursor":
            return None
        from lu_machinery.agent_runtime.sources_read_only import sources_tool_sets
        from lu_machinery.dispatcher.providers.cursor import read_only_cli_config

        try:
            readers, writers = sources_tool_sets()
        except Exception:  # Sources is optional; the shell rules are not
            readers, writers = (), ()
        config = read_only_cli_config(readers, writers).encode()
        self.receipt["cursor_permissions"] = {"config_sha256": hashlib.sha256(config).hexdigest()}
        return config

    def _probes(self, repository: Path, common: Path, sockets: list[Path], outputs: list[Path], tmp: Path,
                nonce: str, snapshot_only: bool = False) -> list[dict]:
        probes: list[dict] = []
        mount_refusal = ("EROFS", "EBUSY")

        def expect(op: str, path: Path | str, *outcomes: str, port: int | None = None) -> None:
            probes.append({"op": op, "path": str(path), "expect_any": list(outcomes), **({"port": port} if port else {})})

        if snapshot_only:
            # Absent, not merely read-only: the worktree, the primary's tracked files and its Git metadata.
            expect("stat", self.worktree, "ENOENT")
            expect("stat", common / "HEAD", "ENOENT")
            if (tracked := _writable_tracked_file(repository)) is not None:
                expect("stat", tracked, "ENOENT")
            expect("create", Path(SNAPSHOT_INSIDE) / f".write-boundary-canary-{nonce}", "EROFS")
            return self._common_probes(probes, expect, sockets, outputs, tmp, nonce)
        absent = repository / f".write-boundary-canary-{nonce}"
        if (tracked := _writable_tracked_file(repository)) is not None:
            expect("write_open", tracked, "EROFS")
            expect("chmod", tracked, "EROFS")
        for op in ("create", "mkdir", "rename", "unlink"):
            expect(op, absent, "EROFS")
        for name in self.protected:
            path = common / name
            if path.is_dir():
                expect("create", path / f"canary-{nonce}", "EROFS")
            else:
                expect("write_open", path, "EROFS")
                expect("unlink", path, *mount_refusal)
            expect("rename", path, *mount_refusal)  # read-only parent or mount point: neither can be replaced
        dot_git = self.worktree / ".git"
        expect("write_open", dot_git, "EROFS")
        expect("rename", dot_git, *mount_refusal)
        # The worktree and Git stay read-only; HOME and TMPDIR are positive writable controls.
        for directory in (self.worktree, common / "objects"):
            expect("create", directory / f".write-boundary-canary-{nonce}", "EROFS")
        return self._common_probes(probes, expect, sockets, outputs, tmp, nonce)

    def _common_probes(self, probes: list[dict], expect: Callable, sockets: list[Path], outputs: list[Path],
                       tmp: Path, nonce: str) -> list[dict]:
        """Probes every view shares: writable HOME and TMPDIR, declared outputs, no loopback, bus or host socket."""
        for directory in (Path(os.environ.get("HOME") or Path.home()), tmp):
            expect("scratch_cycle", directory / f".write-boundary-canary-{nonce}", "OK")
        for path in outputs:
            expect("write_open", path, "OK")
        for port in _LOOPBACK_PORTS:
            expect("connect_tcp", "127.0.0.1", "ECONNREFUSED", "ENETUNREACH", port=port)
        # The fixed Sources command cannot be redirected: its directory is a mount point and its interpreter's
        # installation is read-only.
        expect("rename", _INSIDE, "EBUSY")
        expect("create", _interpreter_root() / f".write-boundary-canary-{nonce}", "EROFS")
        if (abstract := agy_sandbox._host_abstract_socket()) is not None:
            expect("connect_abstract", abstract, "ECONNREFUSED")
        for path in sockets:
            expect("connect_unix", path, "ECONNREFUSED")
        runtime_dir = Path(f"/run/user/{os.getuid()}")
        for masked in (runtime_dir / "bus", os.environ.get("SSH_AUTH_SOCK"), "/run/dbus/system_bus_socket"):
            if masked and os.path.lexists(masked):
                expect("connect_unix", masked, "ENOENT")
        return probes

    def _view_probes(self, home: Path, data: list[Path], masks: list[Path], restores: list[Path], private: list[Path],
                     credentials: list[Path], own: list[Path], nonce: str) -> list[dict]:
        """Canary checks of the view (decision A4): hidden host paths are absent, restored ones are present.

        Negative controls: the other providers' logins, each emptied private path, and every hidden entry below
        HOME and the data roots, found by walking from them through the restored paths' parents (bounded by
        ``_MAX_HIDDEN_PROBES``). They test existence or emptiness only and never read a file's content.
        Positive controls: the selected login, the checkouts and this task's own records are present.
        """
        probes: list[dict] = []
        seen: set[Path] = set()

        def absent(path: Path) -> None:
            if path not in seen and len(seen) < _MAX_HIDDEN_PROBES and _hidden(path, masks, restores):
                seen.add(path)
                probes.append({"op": "stat", "path": str(path), "expect_any": ["ENOENT"]})

        others = []
        for agent in ("claude", "codex", "agy", "opencode"):
            with contextlib.suppress(Exception):
                others += credential_files(agent, dict(os.environ))
        for path in others:
            for name in (path, Path(os.path.realpath(path))):
                if name not in credentials:
                    absent(name)
        for path in private:
            if not path.is_dir():
                # /dev/null on a nodev mount refuses the open (EACCES); either way nothing of the file is readable.
                probes.append({"op": "read_byte", "path": str(path), "expect_any": ["EMPTY", "EACCES"]})
        # Breadth first from HOME, each emptied private directory and the data roots, into restored paths' parents.
        for start in (home, *(path for path in private if path.is_dir()), *data):
            pending, walked = [start], 0
            while pending and walked < 64:
                directory, walked = pending.pop(0), walked + 1
                try:
                    children = sorted(directory.iterdir())
                except OSError:
                    continue
                for child in children[:_MAX_PROBES_PER_DIRECTORY]:
                    if child in restores:
                        continue
                    if child in masks or any(restore != child and restore.is_relative_to(child) for restore in restores):
                        pending.append(child)
                    else:
                        absent(child)
        for path in (*credentials, *own):
            probes.append({"op": "stat", "path": str(path), "expect_any": ["OK"]})
        # The fixed Sources interpreter's parents below an emptied root cannot be moved aside (review F1): HOME's
        # come from its read-only lower layer (EXDEV), a data root's or private root's tmpfs is read-only.
        interpreter = _interpreter_root()
        for root in (home, *data, *(path for path in private if path.is_dir())):
            if interpreter.is_relative_to(root) and interpreter != root:
                top = root / interpreter.relative_to(root).parts[0]
                probes.append({"op": "rename", "path": str(top), "expect_any": ["EXDEV", "EROFS", "EBUSY"]})
                break
        for path in private:
            if path.is_dir():
                probes.append({"op": "create", "path": str(path / f".write-boundary-canary-{nonce}"),
                               "expect_any": ["EROFS"]})
        if self.args.codex_auth is not None:
            # The task-local Codex configuration holds the trusted Sources definition: it refuses a write.
            probes.append({"op": "write_open", "path": str(self.base / _CODEX_HOME / "config.toml"),
                           "expect_any": ["EROFS"]})
        if (agent := self.args.sources_mcp) is not None:
            # The task-local Sources MCP file the provider reads refuses a write too.
            relative = _SOURCES_MCP_HOME[agent]
            probes.append({"op": "write_open", "path": str(home / relative) if relative else _SOURCES_MCP_INSIDE,
                           "expect_any": ["EROFS"]})
        if self.args.mode == "read-only" and self.args.sources_mcp == "cursor":
            # A read-only Cursor run's permission profile is present where Cursor reads it.
            probes.append({"op": "stat", "path": str(home / _CURSOR_CLI_CONFIG_HOME), "expect_any": ["OK"]})
        return probes

    def _report(self, descriptor: int) -> list | None:
        chunks: list[bytes] = []
        deadline = time.monotonic() + _CANARY_TIMEOUT
        while (remaining := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                break
            chunk = os.read(descriptor, 65536)
            if not chunk:
                with contextlib.suppress(ValueError):
                    report = json_depth.loads(b"".join(chunks), max_depth=512)
                    return report.get("probes") if isinstance(report, dict) else None
                return None
            chunks.append(chunk)
        raise Refused("the write boundary canary did not report in time")

    def current_tip(self) -> str:
        """The dispatched branch's tip as the worker sees it now (its private layer, else the launch tip)."""
        return self.tip

    def cleanup(self) -> None:
        if self.process is not None and self.process.poll() is None:
            # bwrap is the PID namespace's parent: every process of the sandbox ends with it.
            with contextlib.suppress(OSError):
                self.process.kill()
            self.process.wait()
        if self.receipt.get("status") in ("ran", "interrupted") and "import" not in self.receipt:
            # Read-only mounts leave the branch tip unchanged, including interrupted runs.
            self.receipt["import"] = {"status": "unchanged", "before": self.tip, "after": self.tip}
        if self.gh is not None:
            self.gh.cleanup()
            self.receipt["github_reads"] = list(self.gh.records)
        if self.relay is not None:
            with contextlib.suppress(Exception):
                self.relay.cleanup()
            self.receipt["sources"] = self.relay.evidence()
        if self.broker is not None:
            with contextlib.suppress(Exception):
                self.broker.cleanup()
            self.receipt["broker"] = list(self.broker.records)
        if self.egress is not None:
            with contextlib.suppress(Exception):
                self.egress.cleanup()
            self.receipt["egress"] = list(self.egress.records)
            self.receipt["egress_overflow_count"] = self.egress.overflow_count
            if self.egress.upload_ceiling is not None:
                self.receipt["upload_ceiling"] = {"limit": self.egress.upload_ceiling, "sent": self.egress.uploaded,
                                                  "reached": self.egress.ceiling_reached}
        for path in self.created_outputs:
            with contextlib.suppress(OSError):
                if path.stat().st_size == 0:
                    path.unlink()
        if self.created:
            try:
                _remove(self.base)
            except OSError as exc:
                self.receipt["cleanup"] = f"residue: {type(exc).__name__}"


def call_main(argv: list[str]) -> int:
    """``call <verb>``: send one broker request from inside a write boundary; the payload is JSON on stdin."""
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.dispatcher.write_boundary call",
        description="Send one named GitHub read to the read-only review boundary's host broker.\n"
                    "The payload is one JSON object on stdin; docs/runbooks/write-worker-boundary.md lists each verb's "
                    "fields.",
        epilog="Example:\n  echo '{\"name\": \"pull\", \"number\": 123}' | python -m lu_machinery.dispatcher.write_boundary call github-read\n"
               "Outputs: the host step's stdout and stderr.\n"
               "Exit codes: the host step's status; 2 when the broker refuses the request or no broker is present.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("verb", choices=_BROKER_VERBS, help="Named read relay verb (github-read only)")
    args = parser.parse_args(argv)
    if broker_path() is None:
        print("write boundary broker: not inside a write boundary", file=sys.stderr)
        return 2
    try:
        payload = json_depth.loads(sys.stdin.read() or "{}", max_depth=512)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        print("write boundary broker: the payload is not one JSON object", file=sys.stderr)
        return 2
    reply = broker_call(args.verb, payload)
    sys.stdout.write(str(reply.get("stdout", "")))
    sys.stderr.write(str(reply.get("stderr", "")))
    code = reply.get("returncode")
    return code if type(code) is int and 0 <= code < 256 else 1


def _pr_number(text: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,7}", text):
        raise argparse.ArgumentTypeError("--pr must be a PR number")
    return int(text)


def _positive_bytes(value: str) -> int:
    if not (value.isascii() and value.isdecimal() and len(value) <= 12 and int(value) > 0):
        raise argparse.ArgumentTypeError("expected a positive byte count")
    return int(value)


def read_path(value: str) -> str:
    """A repository-relative path in Git's form: no absolute, dot, empty or control-character component."""
    from pathlib import PurePosixPath

    parts = PurePosixPath(value).parts
    if (not value or len(value) > 4096 or value.startswith(("/", "-")) or not value.isprintable()
            or any(part in (".", "..") for part in parts) or "//" in value):
        raise argparse.ArgumentTypeError("expected a repository-relative path such as lu_machinery/config")
    return value


def main(argv: list[str]) -> int:
    if argv[:1] == ["call"]:
        return call_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="lu_machinery.dispatcher.write_boundary",
        description="Run one dispatched provider inside the dispatcher's bubblewrap boundary (#423, #100).\n"
                    "Internal launcher used by the worker; not run by hand.",
        epilog="Example (internal):\n  python -m lu_machinery.dispatcher.write_boundary --task-id review-628 "
               "--worktree /path/to/worktree --scratch /path/to/scratch --receipt /path/to/receipt -- /usr/bin/true\n"
               "Outputs: the provider's stdout and stderr; the boundary receipt at --receipt.\n"
               f"Exit codes: the provider's status, or {REFUSED} when the boundary refuses to start it.\n"
               "Related: docs/runbooks/write-worker-boundary.md",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task-id", required=True, help="Dispatched task id, for example review-628")
    parser.add_argument("--worktree", required=True, help="Absolute path of the linked review worktree")
    parser.add_argument("--scratch", required=True, help="This task's private boundary scratch directory")
    parser.add_argument("--receipt", required=True, help="Boundary receipt output path")
    parser.add_argument("--mode", choices=("read-only",), default="read-only",
                        help="The dispatch mode (read-only only; default: read-only)")
    parser.add_argument("--runtime", help="The read-only Codex task's existing private scratch lease")
    parser.add_argument("--pr", type=_pr_number, help="The PR the task record names (a review's PR); in a private "
                        "repository github-read acts on this PR only (default: the open PR "
                        "of the dispatched branch against the default branch, bound on first use)")
    parser.add_argument("--output", action="append", help="A declared output file the provider writes")
    parser.add_argument("--credential", action="append", help="A provider credential file a refresh rewrites")
    parser.add_argument("--claude-login", help="The selected Claude login directory; its guard-bound state is "
                        "projected as private copies")
    parser.add_argument("--codex-auth", help="The selected Codex account's auth.json, linked into a task-local "
                        "CODEX_HOME that holds only the Sources server")
    parser.add_argument("--sources-mcp", choices=tuple(_SOURCES_MCP_HOME),
                        help="Sources projection selector; read-only dispatches use cursor only, with AGY/OpenCode "
                        "definitions retained (default: none)")
    parser.add_argument("--upload-ceiling", type=_positive_bytes,
                        help="At most this many bytes sent upstream by the whole run, e.g. 16777216 (default: none)")
    parser.add_argument("--no-github-relays", action="store_true",
                        help="Start neither GitHub read relay nor the gh shim inside (default: both start)")
    parser.add_argument("--snapshot-bytes", type=_positive_bytes,
                        help="Write a read-only snapshot of the published start commit at most this large, e.g. "
                        f"67108864, at {SNAPSHOT_INSIDE} (default: none)")
    parser.add_argument("--read-path", action="append", default=[], type=read_path,
                        help="Repository path the snapshot is limited to, e.g. lu_machinery/config; repeatable, "
                        f"at most {MAX_READ_PATHS} (default: the whole tree)")
    parser.add_argument("--host-env", action="append", default=[],
                        help="NAME=VALUE of the dispatcher's host configuration, for the launcher only (default: none)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Provider argv after --; executable must be absolute")
    args = parser.parse_args(argv)
    if len(args.read_path) > MAX_READ_PATHS or (args.read_path and args.snapshot_bytes is None):
        parser.error(f"--read-path needs --snapshot-bytes and is repeatable at most {MAX_READ_PATHS} times")
    receipt = {"schema": SCHEMA, "task_id": args.task_id, "mode": args.mode, "status": "refused", "started_at": _now()}
    if not _TASK_ID.fullmatch(args.task_id):
        receipt["reason"] = "unsafe task id"
        _write_receipt(Path(args.receipt), receipt)
        return REFUSED
    args.provider_env = dict(os.environ)  # the provider's own environment, before the host context is applied
    for item in args.host_env:
        name, equals, value = item.partition("=")
        if name not in _HOST_CONTEXT or not equals or "\0" in value:
            receipt["reason"] = f"--host-env names an unknown variable: {name[:40]}"
            _write_receipt(Path(args.receipt), receipt)
            return REFUSED
        # The launcher's own host steps read it; the provider keeps its own environment.
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
    boundary = None

    def cancelled(_signum, _frame):
        raise Refused("cancelled")

    previous = signal.signal(signal.SIGTERM, cancelled)
    try:
        boundary = _Boundary(args, receipt)
        return boundary.run()
    except Exception as exc:  # every failure refuses; nothing falls back to an unsandboxed run
        if receipt["status"] != "ran":
            receipt["reason"] = str(exc) if isinstance(exc, Refused) else f"setup failed: {type(exc).__name__}: {exc}"[:500]
            print(f"write boundary refused: {receipt['reason']}", file=sys.stderr)
            return REFUSED
        receipt.update(status="interrupted", reason=f"{exc if isinstance(exc, Refused) else type(exc).__name__}")
        return REFUSED
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if boundary is not None:
                boundary.cleanup()
        finally:
            receipt["finished_at"] = _now()
            _write_receipt(Path(args.receipt), receipt)
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
