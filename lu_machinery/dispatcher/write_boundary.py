"""OS and publication boundary for dispatched workers and reviewers (#423, #100).

The primary-checkout write guard parses shell commands and is only an accident tripwire. This boundary
makes the primary checkout read-only for the operating system instead, so no command spelling writes it.
``worker.supervise`` launches every ``workspace-write`` provider and native read-only providers other
than Claude through ``launcher``. Read-only Claude retains its per-shell boundary pending a separate
provider enrollment. Read-only AGY keeps its existing filesystem boundary and reuses the GitHub egress refusal.
The launcher runs the provider and all of its descendants under bubblewrap and
reuses the read-only AGY boundary's pieces (``agy_sandbox``): its bwrap resolver, its projected canary
with the report and go descriptors, the CONNECT-only egress proxy and its in-namespace forwarder.

Inside the boundary:

* the whole host file system is a read-only bind; ``/tmp``, ``/run`` and ``/dev/shm`` are private tmpfs,
  so host Unix sockets there (the user bus, systemd, agents, tmux) are absent, and every other listening
  host socket path is masked with ``/dev/null``;
* PID, IPC, UTS, cgroup, user and network namespaces are private, every capability is dropped, nested
  user namespaces are refused and the session is new. The only network route is the egress proxy:
  public hosts over HTTPS, loopback and private addresses refused, so no host service runs anything
  for the worker;
* the primary working tree is a read-only bind. Its Git directory is an overlay whose writes land in a
  private upper directory for write workers, so the worker can commit, fetch and push, but the host's HEAD, index,
  configuration, hooks, refs and objects never change; the primary's HEAD, index, configuration,
  ``packed-refs`` and hooks are also read-only binds inside, and so is the worktree's ``.git`` link;
* HOME is an overlay whose lower layer is empty (decision A4), so nothing the CLIs write outlives the run and
  no file of the real HOME is visible unless restored: the toolchain (``_HOME_TOOLCHAIN`` and HOME's PATH
  directories), this launcher's interpreter and installation, the public checkouts and the selected
  provider credential files named by ``--credential``, which are writable binds so a token refresh is not
  lost. No other provider's login or configuration, transcript or private folder exists inside;
* the data roots outside HOME (``_DATA_ROOTS``: other homes, service data, mounts, ``/opt``, ``/var/tmp``)
  are empty and read-only, but for the ``/opt`` packages the PATH resolves into. The public checkouts (the
  primary and the product) lose their private runtime state (``_CHECKOUT_PRIVATE``), and the task store,
  the Fleet plane and the scratch root are empty but for this run's own records and scratch. The commit
  hook cannot read the task record there, so the launcher projects that task's ownership fields to
  ``ownership.json`` beside the broker socket and refreshes the file when the record is written;
* a write worker's worktree, a private TMPDIR and the declared outputs (``--output``) are the only other
  writable paths. A read-only provider's worktree and Git metadata are read-only mounts, with no branch import.

The task store, the Fleet Comms plane and the primary's Git state are not writable inside, so the worker chain's
host steps (start its review, record the verdict, send a Fleet message, close out after merge) go through the
launcher's broker (``_Broker``, decision CONTAIN-BROKER-1113): one Unix socket with typed verbs whose
identities come from the launch. The worker holds no GitHub credential and the egress proxy refuses GitHub's
hosts, so it publishes (push, PR, comment, merge) only through the broker's publication verbs, which the host's
own login carries out behind the existing scanned publisher and push scan (decision PUBLISH-CRED-1113), and
only for a registered repository with a public role (decision A6; so does the read-only ``gh`` relay).
Read-only providers may use only ``github-read``; the owning worker records the review verdict after settlement.
Sources goes through the read-only AGY boundary's ``SourcesRelay``. Its proxy and socket sit at fixed paths
inside, so ``sources_command`` is the MCP stdio command providers name; a Codex run gets a task-local
``CODEX_HOME`` holding only that server and a link to the selected account's login, and an AGY, Cursor or OpenCode
write worker one read-only MCP file naming only that server, where its CLI reads it (``sources_mcp_config``).

The canary runs first and checks each boundary property; the provider starts only after the launcher
has checked every outcome. Any setup or canary failure refuses the run (exit ``REFUSED``) and nothing
ever falls back to an unsandboxed launch. After the sandbox exits, the launcher imports exactly one
thing: the dispatched branch's new tip, through a host-side ``git fetch`` from a private repository
whose alternates are the upper objects (index-pack hashes every object it accepts), then a
compare-and-swap ``update-ref``. Other refs, objects and Git metadata the worker wrote are dropped.
The receipt beside the task record is the run's sandbox evidence; ``verify`` checks it.
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
from pathlib import Path
from typing import Any

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
# The primary's own Git state, kept read-only inside on top of the private overlay.
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
    "FLEET_COMMS_ROOT", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME", "GH_CONFIG_DIR",
    "DELEGATE_MONITOR_API", "LU_DISPATCH_ISOLATION",
    # The host's test-slot governor settings: the launcher selects and sizes the pool it binds as the dispatcher's
    # own runs do (#557). The pool directory is never passed inside; the size, wait and switch are (``_slot_env``).
    "LU_TEST_SLOTS", "LU_TEST_SLOTS_DIR", "LU_TEST_SLOTS_COUNT", "LU_TEST_SLOTS_WAIT_S",
})

# The host broker (decision CONTAIN-BROKER-1113). Inside the boundary the task store, the Fleet plane and the
# primary's Git state are read-only, so the worker chain's host steps go through one launcher-owned socket with
# typed verbs. Each verb has a fixed schema, and every identity it acts under comes from the launch, never the
# request.
BROKER_ENV = "LU_WRITE_BOUNDARY_BROKER"
_BROKER_VERBS = ("review-dispatch", "review-wait", "budget-check", "record-verdict", "fleet-publish", "fleet-deliveries",
                 "closeout-request", "push", "fetch-base", "pr-create", "pr-edit", "pr-comment", "pr-merge",
                 "pr-ownership", "github-read")
# Named reads of the worker's own repository (the publisher's fixed GET templates); no security-alert reads.
GITHUB_READS = frozenset({"pull", "pr-files", "commits", "commit", "comments", "reviews", "timeline", "issue",
                          "checks", "check-annotations", "run", "jobs"})
# A registered private repository (#565, designated approval C-5541): the explicit allowlist of verbs the broker
# admits for it. Each is bound to the task's own repository, branch and PR identity frozen at launch; a verb added
# to _BROKER_VERBS later is refused for a private repository until it is listed here.
_PRIVATE_VERBS = frozenset({"review-dispatch", "review-wait", "budget-check", "record-verdict", "fleet-publish",
                            "fleet-deliveries", "closeout-request", "push", "fetch-base", "pr-create", "pr-edit",
                            "pr-comment", "pr-merge", "pr-ownership", "github-read"})
# A private repository's github-read is the authorized PR (by its number) and its checks (by its head commit) only;
# reads by any other id (another PR or issue, a workflow run, a check run) are refused.
_PRIVATE_PR_READS = frozenset({"pull", "pr-files", "commits", "comments", "reviews", "timeline", "issue"})
_PRIVATE_SHA_READS = frozenset({"checks", "commit"})
# A private task's only Fleet audience (#565 round 1, F2): its hand-off to the CTO seat on the CTO channel. Fixed
# here, never taken from the request, so private PR content read through the broker reaches no other seat.
_PRIVATE_HANDOFF_CHANNEL, _PRIVATE_HANDOFF_RECIPIENT = "cto", "claude-cto"
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
# GitHub's write-capable hosts. The egress proxy refuses them inside the boundary, by name and by the address ranges
# GitHub publishes for them, so no credential the worker could read anywhere is usable from inside; every
# publication goes through the broker (PUBLISH-CRED-1113).
GITHUB_DOMAINS = ("github.com", "ghcr.io")
_GITHUB_META_KEYS = ("web", "api", "git", "packages")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")  # a model, agent, profile or label: never a flag
_CHANNEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?")
_GITHUB_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_MAX_REQUEST = 1 << 20
_MAX_BODY = 64 << 10
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
_SOURCES_MCP_HOME = {"agy": ".gemini/config/mcp_config.json", "cursor": ".cursor/mcp.json", "opencode": None}
# Cursor gives a workspace's own ``.cursor/mcp.json`` precedence over HOME's for a server of the same name, and the
# product's names ``sources`` at a loopback URL the boundary refuses, so Cursor's relay entry has a name of its own.
_CURSOR_SOURCES_SERVER = "sources-relay"
_BROKER_TIMEOUTS = {"review-dispatch": 300, "record-verdict": 900, "fleet-publish": 120, "gh": 120,
                    "closeout-request": 900, "publish": 300, "pr-merge": 900, "pr-ownership": 300,
                    "budget-check": 300, "review-wait": 900}
# The longest one review-wait runs on the host; a longer wait is repeated by the client (``dispatch wait`` exits 124
# while the review still runs, as on the host).
REVIEW_WAIT_MAX = 840
# Connections the broker serves at once; a further one waits in the listen backlog until one finishes.
_BROKER_CONNECTIONS = 8
_RUN_NONCE = re.compile(r"[0-9a-f]{32}")
# A Fleet delivery id or artifact id as the authority service mints them; never a flag.
_DELIVERY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


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
        reply = json.loads(b"".join(chunks))
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
        "--receipt", str(receipt_file(task_file)), "--author-model", spec.model, "--mode", spec.mode,
    ]
    for path in declared_outputs(spec, invocation):
        argv += ["--output", str(path)]
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
    if spec.agent in _SOURCES_MCP_HOME and spec.mode == "workspace-write":
        argv += ["--sources-mcp", spec.agent]
    if spec.mode == "workspace-write" and spec.options.get("branch"):
        # The PR branch a continuation (``dispatch --branch``, recorded as ``publish_branch``) publishes to (#557).
        argv += ["--publish-branch", spec.options["branch"]]
    if spec.options.get("pr"):
        # The task record's PR, frozen here: a private repository's github-read is that PR and its checks (#565).
        argv += ["--pr", str(spec.options["pr"])]
    if spec.agent == "codex" and spec.mode == "read-only":
        from lu_machinery.agent_runtime.read_only_tmp import dispatch_lease

        argv += ["--runtime", str(dispatch_lease(spec.task_id))]
    for name in sorted(_HOST_CONTEXT):
        if os.environ.get(name) and name not in invocation.env:
            argv += ["--host-env", f"{name}={os.environ[name]}"]
    return [*argv, "--", str(Path(found).absolute()), *invocation.argv[1:]]


def verify(task_file: Path, task_id: str, mode: str = "workspace-write") -> tuple[dict, str | None]:
    """The boundary evidence and the refusal that fails the run, if any."""
    path = receipt_file(task_file)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "missing"}, "write_boundary_unproven: no boundary receipt"
    summary = {key: receipt.get(key) for key in (
        "status", "reason", "mode", "argv_sha256", "bwrap", "returncode", "import", "cleanup", "broker", "closeout_request")}
    summary["canary_passed"] = bool(receipt.get("canary", {}).get("passed"))
    if receipt.get("schema") != SCHEMA or receipt.get("task_id") != task_id:
        return summary, "write_boundary_unproven: the receipt names another run"
    if receipt.get("mode") != mode:
        return summary, "write_boundary_unproven: the receipt names another mode"
    if receipt.get("status") != "ran" or not summary["canary_passed"]:
        return summary, f"write_boundary_refused: {receipt.get('reason') or receipt.get('status')}"
    if (receipt.get("import") or {}).get("status") not in ("unchanged", "imported"):
        return summary, f"write_boundary_import_failed: {(receipt.get('import') or {}).get('status')}"
    return summary, None


def run_closeout(request: dict) -> dict:
    """``merge_closeout --apply`` for a worker's closeout request, run by the supervisor after settlement.

    The request comes from the launcher's receipt, which only the host writes; it is still checked here.
    """
    pr, repo, root = request.get("pr"), request.get("repo"), request.get("repo_root")
    if not (type(pr) is int and pr > 0 and isinstance(repo, str) and _GITHUB_SLUG.fullmatch(repo)
            and isinstance(root, str) and Path(root).is_absolute() and Path(root).is_dir()):
        return {"pr": pr, "returncode": None, "reason": "the closeout request is malformed"}
    code = (f"import sys; sys.path.insert(0, sys.argv[1]); from {_PACKAGE}.orchestration.merge_closeout "
            "import main; raise SystemExit(main(sys.argv[2:]))")
    argv = [sys.executable, "-I", "-c", code, str(machinery_path("dispatcher/write_boundary.py").parents[2]),
            str(pr), "--repo", repo, "--repo-root", root, "--apply", "--json"]
    try:
        # A fixed argv: this interpreter, the package's own closeout module and checked request fields.
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                                timeout=_BROKER_TIMEOUTS["closeout-request"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"pr": pr, "returncode": None, "reason": f"{type(exc).__name__}"}
    return {"pr": pr, "returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}


def _git(cwd: Path, *args: str, git_dir: Path | None = None, network: bool = False) -> str:
    """Host Git with no user or system configuration, hooks or fsmonitor: nothing the worker wrote runs.

    ``network`` admits HTTPS for the broker's publication verbs; their credential is the scoped helper of
    ``git_credentials.verification_credentials``, the only credential configuration Git then has.
    """
    env = {"PATH": os.defpath, "HOME": os.environ.get("HOME", "/"), "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}
    if network:  # gh, behind the scoped helper, finds the host login through its own configuration
        env.update({name: os.environ[name] for name in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME") if os.environ.get(name)})
    command = list(args)
    while command[:1] == ["-c"] and len(command) >= 2:
        command = command[2:]
    # Clone transport is not publication authority. The broker's existing resolver verifies the
    # registry and binds credentials to HTTPS, including when the protected origin uses SSH.
    if (network and command[:1] in (["push"], ["ls-remote"]) and len(command) >= 3
            and (match := _GITHUB_REMOTE.fullmatch(command[-2]))):
        try:
            command[-2], credentials = _Broker._remote(match.group(1))
        except _BrokerRefusal as exc:
            raise Refused(str(exc)) from None
        args = (*credentials, *command)
    git = "git"
    options = ["--no-replace-objects", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
               "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
               "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
               *(["-c", "protocol.https.allow=always"] if network else []),
               *([f"--git-dir={git_dir}"] if git_dir else []), "-C", str(cwd)]
    if command[:1] == ["push"]:
        # Broker and settlement share this exact-update scan. No worker hook is trusted, including
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
            cwd=cwd, input=update, capture_output=True, timeout=_BROKER_TIMEOUTS["publish"], check=False,
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


def _writable_tracked_file(repository: Path) -> Path:
    """The first tracked regular file the host user owns and may write, for the canary's read-only bind probe.

    The kernel checks file permissions before the mount's read-only flag, so a tracked file that is read-only
    on disk gives EACCES under any mount and would test its mode, not the bind. The sandbox maps only the host
    user, so a file writable only through its group or another owner gives EACCES inside it as well.
    """
    for name in _git(repository, "ls-files", "-z").split("\0"):
        if not name:
            continue
        path = repository / name
        try:
            status = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(status.st_mode) and status.st_uid == os.geteuid() and os.access(path, os.W_OK):
            return path
    raise Refused("the primary has no tracked regular file the host user may write, so the canary cannot "
                  "tell its read-only bind from file permissions")


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


def _pre_commit_store(home: Path) -> Path | None:
    """pre-commit's hook store, found as pre-commit finds it: ``PRE_COMMIT_HOME``, else ``pre-commit`` below
    ``XDG_CACHE_HOME`` or HOME's ``.cache``. None when the configured value is not absolute."""
    store = os.environ.get("PRE_COMMIT_HOME") or os.path.join(os.environ.get("XDG_CACHE_HOME") or home / ".cache",
                                                              "pre-commit")
    return Path(store) if os.path.isabs(store) else None


def _toolchain(home: Path, executable: str) -> list[Path]:
    """Read-only restores below HOME and the data roots: the provider's toolchain and this launcher's own
    interpreter and installation (decision A4). Nothing of a provider's configuration is one of them.

    pre-commit's hook store is part of the toolchain: a product commit's hooks run from it, and inside it cannot
    be fetched (GitHub is refused). Read-only, pre-commit records nothing and runs the installed environments;
    a hook whose environment is not installed fails the commit instead of installing one."""
    path_env = os.environ.get("PATH", os.defpath)
    candidates = [home / name for name in _HOME_TOOLCHAIN]
    if (store := _pre_commit_store(home)) is not None:
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

    try:
        proc = github_client.run(["gh", "api", "meta"], capture_output=True, text=True, check=False, timeout=60)
        meta = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        meta = None
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
        raise Refused("GitHub's published address ranges cannot be read; the boundary could not refuse GitHub")
    return tuple(dict.fromkeys(networks))


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
    env["LEARN_UKRAINIAN_DISPATCH_TASK_ID"] = task_id  # the review child records this task as its parent
    return env


class _Broker:
    """The launcher-owned host side of the worker chain (decision CONTAIN-BROKER-1113).

    One Unix socket in the read-only boundary directory. A connection is served only when its peer is a
    descendant of this run's bubblewrap process, so no other sandbox or host process can act as this task.
    A request is one JSON object ``{"verb", "request_id", "payload"}``; a request id is accepted once per run.
    Each verb validates a fixed schema and runs one host step with argv built here, never a shell, and with
    the identities of the launch: the task id, its branch, its author model and the primary's repository.

    * ``review-dispatch`` starts the read-only review of the worker's PR at its exact live head: the PR's head
      branch is the dispatched branch, its live head is the worker's branch tip and the requested pinned head,
      and the review task id is ``<task>-review-<suffix>``. The host fetches that commit from the worker's
      private Git layer (index-pack hashes every object), then runs the dispatcher, whose review then runs
      in its own normal sandbox. The author model is the launch's model, so the family check is not forgeable.
    * ``review-wait`` waits (bounded) for a review task of this worker on the host and returns its record, with the
      reviewer's sealed reply once it is done, so the author reads every finding, withheld from the PR or not.
    * ``budget-check`` runs the recorder's read-only round admission for the worker's own open PR on the host.
    * ``record-verdict`` runs the recorder for a review task of this worker, on the worker's PR; the recorder
      takes the host's PR lock and writes its receipts in the task store, which is read-only inside.
    * ``fleet-publish`` appends one Fleet Comms message whose sender is the task id and whose idempotency key
      is prefixed with it; the body is untrusted text.
    * ``closeout-request`` records the worker's merged PR in the receipt; the supervisor runs ``merge_closeout --apply``
      on the host after it has settled the task's worktree, because closeout deletes refs in the primary's Git
      directory and refuses while the worktree is registered.
    * ``push``, ``fetch-base``, ``pr-create``, ``pr-edit``, ``pr-comment``, ``pr-merge`` and ``github-read`` are
      the worker's only GitHub access (PUBLISH-CRED-1113); docs/runbooks/write-worker-boundary.md lists them.
    * The repository they act on is frozen when the broker starts. For a registered private one (#565, C-5541)
      only ``_PRIVATE_VERBS`` are admitted, each bound to the task's own repository, branch and PR, and
      ``github-read`` reads only the authorized PR and its checks. That PR is the launch's ``--pr``, else the
      open own PR bound on the first PR operation; ``fleet-publish`` reaches only the fixed hand-off audience.
    """

    def __init__(self, boundary: _Boundary, path: Path):
        import socket

        self.boundary = boundary
        self.records: list[dict] = []
        self.seen: set[str] = set()
        self.closeout_request: dict | None = None
        self.children: set[subprocess.Popen[bytes]] = set()
        self.connections: set[threading.Thread] = set()
        self.closed = False
        # Each connection is served on its own thread, so a long review-wait never holds the worker's other
        # calls. Every other verb still runs one at a time under ``serial``, so the task's binding state (its
        # bound PR, its one private pr-create, its published messages) is checked and set as before; ``state``
        # guards the request ids, the records, the live host steps and the connection threads.
        self.serial, self.state = threading.Lock(), threading.Lock()
        self.slots = threading.BoundedSemaphore(_BROKER_CONNECTIONS)
        # The repository every GitHub verb acts on, and whether it is private, frozen now (#565): a later change
        # of the primary's origin or of the registry never moves this run's authority.
        self.launch_repository: tuple[str, bool] | _BrokerRefusal = self._resolve_repository()
        # A private task's own PR when the launch names none, bound once and never moved (#565 round 1, F1), and
        # the Fleet messages this task published, the only ones a private task may reply to (F2).
        self.bound_pr: int | None = None
        # Whether this private task has already forwarded its one pr-create (#565 round 2, F1): at most one PR,
        # whatever that create's outcome, since a failed or timed-out create may still have opened one.
        self.pr_create_spent = False
        self.published: set[str] = set()
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
                request = json.loads(b"".join(chunks))
            except ValueError:
                request = None
            if not isinstance(request, dict) or set(request) != {"verb", "request_id", "payload"}:
                raise _BrokerRefusal("the request is malformed")
            verb, request_id, payload = request["verb"], request["request_id"], request["payload"]
            if verb not in _BROKER_VERBS:
                verb = str(verb)[:40]
                raise _BrokerRefusal("the verb is not one of " + ", ".join(_BROKER_VERBS))
            if self.boundary.args.mode == "read-only" and verb != "github-read":
                raise _BrokerRefusal("read-only dispatches may only use github-read")
            if self._private() and verb not in _PRIVATE_VERBS:
                raise _BrokerRefusal("the verb is not admitted for a private repository")
            if not (isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)) or not isinstance(payload, dict):
                raise _BrokerRefusal("the request is malformed")
            with self.state:
                if request_id in self.seen:
                    raise _BrokerRefusal("the request id was already used: a request runs once")
                self.seen.add(request_id)
            step = getattr(self, "_" + verb.replace("-", "_"))
            # review-wait reads the host's task store and changes no broker state, so it waits outside ``serial``;
            # its one host step goes through ``_run``, which refuses once cleanup has begun.
            if verb == "review-wait":
                reply = step(payload)
            else:
                with self.serial:
                    # A verb queued behind another starts no Git or GitHub step once cleanup has begun.
                    if self.closed:
                        raise _BrokerRefusal("the write boundary is closing")
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
            return json.loads(proc.stdout) if proc.returncode == 0 else None
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

    @staticmethod
    def _remote(slug: str) -> tuple[str, list[str]]:
        """The HTTPS remote of ``slug`` and the scoped host credential settings for it, from the registry."""
        from lu_machinery.common.git_credentials import verification_credentials

        url = f"https://github.com/{slug}.git"
        settings = verification_credentials(url)
        if not settings:
            raise _BrokerRefusal("the repository is not a registered github.com destination")
        return url, [arg for setting in settings for arg in ("-c", setting)]

    def _repo_key(self, slug: str) -> str:
        """The fleet repository key whose checkout is this primary and whose GitHub name is ``slug``."""
        import yaml

        from lu_machinery.dispatcher import git as dispatch_git

        for key in yaml.safe_load(machinery_path("config/fleet_repos.yaml").read_text())["repos"]:
            with contextlib.suppress(Exception):
                root, item = dispatch_git.resolve_repo(key)
                if root == self.boundary.repository and str(item.get("github", "")).lower() == slug.lower():
                    return str(key)
        raise _BrokerRefusal("the primary is not a registered fleet repository")

    def _claimed(self, payload: dict, slug: str, base: str | None = None) -> None:
        """Refuse a request whose named target is not the launch's own.

        A forwarded command (``git push``, the publisher's PR verbs) keeps the target its caller named. The broker
        still acts only on the launch's repository, branch and default branch, so a named target narrows the
        request: one that differs is refused, never replaced.
        """
        repo = payload.get("repo")
        if repo is not None and not (isinstance(repo, str) and repo.lower() == slug.lower()):
            raise _BrokerRefusal("repo is not this task's repository")
        branch = payload.get("branch")
        if branch is not None and not (isinstance(branch, str)
                                       and "refs/heads/" + branch.removeprefix("refs/heads/") == self.boundary.target):
            raise _BrokerRefusal("branch is not this task's dispatched branch"
                                 + (f" ({self.boundary.target.removeprefix('refs/heads/')}, the PR it continues)"
                                    if self.boundary.target != self.boundary.branch else ""))
        claimed_base = payload.get("base")
        if claimed_base is not None and not (isinstance(claimed_base, str) and base is not None
                                             and claimed_base.removeprefix("refs/heads/") == base):
            raise _BrokerRefusal("base is not the repository's default branch")

    def _review_task(self, payload: dict) -> str:
        task = payload.get("task_id")
        prefix = f"{self.boundary.args.task_id}-review-"
        if not (isinstance(task, str) and task.startswith(prefix) and _TASK_ID.fullmatch(task)
                and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", task[len(prefix):])):
            raise _BrokerRefusal(f"the review task id must be {prefix}<suffix>")
        return task

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

    def _fleet(self, argv: list[str], stdin: bytes | None = None) -> dict:
        """One Fleet Comms CLI step on the dispatcher's plane.

        Without ``FLEET_COMMS_ROOT`` the plane is anchored at the working directory's primary checkout, and the one
        plane the fleet reads is the product's; a task whose primary is another registered checkout would otherwise
        publish to, and read from, a plane no seat watches (#557).
        """
        cwd = None if os.environ.get("FLEET_COMMS_ROOT") else _product_checkout()
        return self._module("fleet_comms.cli", argv, "fleet-publish", stdin=stdin, cwd=cwd)

    def _review_dispatch(self, payload: dict) -> dict:
        allowed = {"task_id", "pr", "pinned_head", "agent", "model", "effort", "prompt", "review_profile", "risk",
                   "review_mode", "codex_account", "claude_account"}
        if not set(payload) <= allowed:
            raise _BrokerRefusal("unknown fields: " + ", ".join(sorted(set(payload) - allowed)))
        task = self._review_task(payload)
        if not (isinstance(self.boundary.args.author_model, str) and _WORD.fullmatch(self.boundary.args.author_model)):
            raise _BrokerRefusal("the launch names no author model")
        words = {}
        for name in ("agent", "model", "effort", "review_profile", "risk", "review_mode", "codex_account",
                     "claude_account"):
            value = payload.get(name)
            if value is None and name in ("agent", "model", "review_profile", "risk"):
                raise _BrokerRefusal(f"{name} is required")
            if value is not None and not (isinstance(value, str) and _WORD.fullmatch(value)):
                raise _BrokerRefusal(f"{name} is malformed")
            words[name] = value
        if words["review_mode"] not in (None, "cross_family"):
            raise _BrokerRefusal("only a cross-family review is dispatched through the broker")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > _MAX_REQUEST // 2:
            raise _BrokerRefusal("prompt must be non-empty text")
        pinned = payload.get("pinned_head")
        if not (isinstance(pinned, str) and _OBJECT_ID.fullmatch(pinned)):
            raise _BrokerRefusal("pinned_head must be a full commit id")
        number, slug, facts = self._pull_request(payload, "OPEN")
        repo_key = self._repo_key(slug)
        tip = self.boundary.current_tip()
        if not (pinned == facts.get("headRefOid") == tip):
            raise _BrokerRefusal("pinned_head is not both the PR's live head and this task's branch tip")
        self.boundary.fetch_objects(tip)
        prompt_file = self.boundary.base / f"review-prompt-{uuid.uuid4().hex}.md"
        _write_private(prompt_file, prompt.encode(), 0o400)
        argv = ["dispatch", "--repo", repo_key, "--agent", words["agent"], "--model", words["model"],
                "--mode", "read-only", "--worktree", "--task-id", task, "--prompt-file", str(prompt_file),
                "--branch", self.boundary.target.removeprefix("refs/heads/"), "--pinned-head", pinned,
                "--pr", str(number), "--review-profile", words["review_profile"], "--risk", words["risk"],
                "--review-author-model", self.boundary.args.author_model, "--review-mode", "cross_family",
                "--initiator", self.boundary.args.task_id]
        for name in ("effort", "codex_account", "claude_account"):
            if words[name] is not None:
                argv += [f"--{name.replace('_', '-')}", words[name]]
        if words["review_profile"] in ("code", "infra"):
            argv.append("--require-review-verdict")
        return self._module("dispatcher.cli", argv, "review-dispatch")

    def _record_verdict(self, payload: dict) -> dict:
        if set(payload) != {"task_id", "pr"}:
            raise _BrokerRefusal("record-verdict takes exactly task_id and pr")
        task = self._review_task(payload)
        number, _slug, _facts = self._pull_request(payload, "OPEN")
        return self._module("review.record_cf_verdict", ["--task-id", task, "--pr", str(number)], "record-verdict")

    def _budget_check(self, payload: dict) -> dict:
        """The recorder's round admission for this task's own open PR (#575); it reads only and writes nothing."""
        if not {"pr"} <= set(payload) <= {"pr", "repo"}:
            raise _BrokerRefusal("budget-check takes pr and optionally repo")
        number, slug, _facts = self._pull_request(payload, "OPEN")
        return self._module("review.record_cf_verdict", ["budget-check", "--repository", slug, "--pr", str(number)],
                            "budget-check")

    def _review_wait(self, payload: dict) -> dict:
        """Wait on the host for a review this task dispatched and return its record and, once done, its reply.

        The task store is read-only and empty inside, so ``dispatch wait`` of the review cannot run there (#557).
        The review must be named ``<task>-review-<suffix>`` and record this task as its initiator. The reply is the
        reviewer's sealed result (its digest checked), so the author reads the findings the recorder withholds
        from the PR (#591); it reaches only this task.
        """
        if not {"task_id"} <= set(payload) <= {"task_id", "run_nonce", "timeout"}:
            raise _BrokerRefusal("review-wait takes task_id, and optionally run_nonce and timeout")
        task = self._review_task(payload)
        nonce, timeout = payload.get("run_nonce"), payload.get("timeout", 0)
        if nonce is not None and not (isinstance(nonce, str) and _RUN_NONCE.fullmatch(nonce)):
            raise _BrokerRefusal("run_nonce is malformed")
        if type(timeout) is not int or not 0 <= timeout <= REVIEW_WAIT_MAX:
            raise _BrokerRefusal(f"timeout must be 0 to {REVIEW_WAIT_MAX} seconds")
        # 0 (or none) is the host wait's unlimited sentinel; the broker never holds a connection unbounded, so it
        # waits the bounded maximum instead and the caller repeats the wait.
        timeout = timeout or REVIEW_WAIT_MAX
        reply = self._module("dispatcher.cli", ["wait", task, *(["--run-nonce", nonce] if nonce else []),
                                                "--timeout", str(timeout)], "review-wait")
        try:
            record = json.loads(reply["stdout"])
        except ValueError:
            record = None
        if not isinstance(record, dict):
            return reply
        if record.get("initiator") != self.boundary.args.task_id:
            raise _BrokerRefusal("the review task was not dispatched by this task")
        if record.get("status") == "done":
            from lu_machinery.common.task_store_paths import tasks_dir
            from lu_machinery.review import record_cf_verdict as recorder

            try:
                record["result"] = recorder._task(task, tasks_dir())[1]
            except (recorder.RecordError, ValueError, OSError) as exc:
                record["result_unavailable"] = str(exc)[:200]
        reply["stdout"] = json.dumps(record, indent=2) + "\n"
        return reply

    def _fleet_publish(self, payload: dict) -> dict:
        allowed = {"channel", "body", "recipients", "idempotency_key", "in_reply_to"}
        if not set(payload) <= allowed or not {"channel", "body", "idempotency_key"} <= set(payload):
            raise _BrokerRefusal("fleet-publish takes channel, body, idempotency_key, recipients and in_reply_to")
        channel, body, key = payload["channel"], payload["body"], payload["idempotency_key"]
        recipients = payload.get("recipients") or []
        if not (isinstance(channel, str) and _CHANNEL.fullmatch(channel)):
            raise _BrokerRefusal("channel is malformed")
        if not (isinstance(key, str) and _IDEMPOTENCY.fullmatch(key)):
            raise _BrokerRefusal("idempotency_key is malformed")
        if not (isinstance(recipients, list) and len(recipients) <= 8
                and all(isinstance(name, str) and _CHANNEL.fullmatch(name) for name in recipients)):
            raise _BrokerRefusal("recipients must be at most 8 agent names")
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > _MAX_BODY:
            raise _BrokerRefusal(f"body must be non-empty text of at most {_MAX_BODY} bytes")
        reply_to = payload.get("in_reply_to")
        if reply_to is not None and not (isinstance(reply_to, str) and _IDEMPOTENCY.fullmatch(reply_to)):
            raise _BrokerRefusal("in_reply_to is malformed")
        if self._private():
            # A private task's hand-off audience is fixed (#565 round 1, F2): the CTO seat on its channel, named
            # explicitly (no recipients would reach every subscriber), replying only within this task's own thread.
            if channel != _PRIVATE_HANDOFF_CHANNEL or recipients != [_PRIVATE_HANDOFF_RECIPIENT]:
                raise _BrokerRefusal(f"a private repository's task publishes only to {_PRIVATE_HANDOFF_RECIPIENT} "
                                     f"on channel {_PRIVATE_HANDOFF_CHANNEL}")
            if reply_to is not None and reply_to not in self.published:
                raise _BrokerRefusal("a private repository's task replies only to its own earlier messages")
        task = self.boundary.args.task_id
        argv = ["channel", "publish", channel, "-", "--sender", task, "--source", "write-worker",
                "--idempotency-key", f"{task}:{key}"]
        for name in recipients:
            argv += ["--recipient", name]
        if reply_to is not None:
            argv += ["--in-reply-to", reply_to]
        reply = self._fleet(argv, stdin=body.encode())
        if reply.get("returncode") == 0:
            with contextlib.suppress(ValueError, AttributeError, TypeError):
                message = json.loads(reply.get("stdout") or "").get("message_id")
                if isinstance(message, str) and message:
                    self.published.add(message)
        return reply

    def _fleet_deliveries(self, payload: dict) -> dict:
        """Receive or acknowledge one Fleet delivery addressed to this task, leased by this task (#557).

        The recipient and the lease owner are the task id, never the request's: a worker reads only what its
        dispatcher sent it. An acknowledgement first checks on the host that the delivery's recipient is this task.
        """
        task = self.boundary.args.task_id
        command = payload.get("command")
        for name in ("recipient", "worker_id"):
            if payload.get(name) is not None and payload[name] != task:
                raise _BrokerRefusal(f"{name} is not this task's id: a worker receives only its own deliveries")
        if command == "receive":
            if not set(payload) <= {"command", "recipient", "worker_id", "lease_seconds"}:
                raise _BrokerRefusal("receive takes recipient, worker_id and lease_seconds")
            argv = ["deliveries", "receive", "--recipient", task, "--worker-id", task]
            if (seconds := payload.get("lease_seconds")) is not None:
                if type(seconds) is not int or not 0 < seconds <= 3600:
                    raise _BrokerRefusal("lease_seconds must be 1 to 3600")
                argv += ["--lease-seconds", str(seconds)]
            return self._fleet(argv)
        if command == "ack":
            if not {"command", "delivery_id", "fence_token"} <= set(payload) <= {
                    "command", "delivery_id", "fence_token", "recipient", "worker_id"}:
                raise _BrokerRefusal("ack takes delivery_id, fence_token and worker_id")
            delivery, fence = payload["delivery_id"], payload["fence_token"]
            if not (isinstance(delivery, str) and _DELIVERY_ID.fullmatch(delivery)):
                raise _BrokerRefusal("delivery_id is malformed")
            if type(fence) is not int or not 0 < fence < 2**62:
                raise _BrokerRefusal("fence_token must be a positive whole number")
            found = self._fleet(["deliveries", "status", delivery])
            try:
                recipient = json.loads(found["stdout"]).get("recipient") if found["returncode"] == 0 else None
            except (ValueError, AttributeError):
                recipient = None
            if recipient != task:
                raise _BrokerRefusal("the delivery is not addressed to this task")
            return self._fleet(["deliveries", "ack", delivery, "--worker-id", task, "--fence-token", str(fence)])
        raise _BrokerRefusal("command must be receive or ack")

    def _closeout_request(self, payload: dict) -> dict:
        if set(payload) != {"pr"}:
            raise _BrokerRefusal("closeout-request takes exactly pr")
        number, slug, _facts = self._pull_request(payload, "MERGED")
        self.closeout_request = {"pr": number, "repo": slug, "repo_root": str(self.boundary.repository)}
        return {"returncode": 0, "stdout": f"closeout of PR {number} runs on the host after this task's worktree "
                "is settled\n", "stderr": ""}

    def _pr_ownership(self, payload: dict) -> dict:
        """``pr_ownership accept`` or ``queue-exit`` as this task, for its own PR only (#557).

        The PR must be open with the task's publication branch as its head: the dispatched branch, or for a
        continuation the PR branch it was dispatched with (``--branch``). The successor is the task id.
        """
        if not {"command", "pr"} <= set(payload) <= {"command", "pr", "repo", "custody", "from"}:
            raise _BrokerRefusal("pr-ownership takes command, pr, and optionally repo, custody and from")
        command, custody, predecessor = payload["command"], payload.get("custody", False), payload.get("from")
        if command not in ("accept", "queue-exit"):
            raise _BrokerRefusal("command must be accept or queue-exit")
        if type(custody) is not bool or (predecessor is not None and not (
                isinstance(predecessor, str) and _TASK_ID.fullmatch(predecessor))):
            raise _BrokerRefusal("custody must be true or false and from a task id")
        if command != "accept" and (custody or predecessor is not None):
            raise _BrokerRefusal("custody and from are accept options")
        number, slug, _facts = self._pull_request({"pr": payload["pr"], "repo": payload.get("repo")}, "OPEN")
        argv = [command, "--pr", str(number), "--repo", slug, *(["--custody"] if custody else []),
                *(["--from", predecessor] if predecessor is not None else [])]
        return self._module("orchestration.pr_ownership", argv, "pr-ownership")

    # -- publication (decision PUBLISH-CRED-1113): the worker holds no GitHub credential and has no route to
    # GitHub, so every publication is one of these verbs. The host's own login does the write, through the
    # existing scanned publisher and push scanner; repository, branch, base and PR ownership come from the launch.

    def _text(self, payload: dict, name: str, limit: int) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit or "\0" in value:
            raise _BrokerRefusal(f"{name} must be non-empty text of at most {limit} bytes")
        return value

    def _optional_text(self, payload: dict, name: str, limit: int) -> str | None:
        return None if payload.get(name) is None else self._text(payload, name, limit)

    def _text_file(self, text: str) -> str:
        """Write request text to the run's private scratch; the publisher reads it by the host's own path."""
        path = self.boundary.base / f"publish-{uuid.uuid4().hex}.md"
        _write_private(path, text.encode(), 0o400)
        return str(path)

    def _publish(self, verb: str, slug: str, fields: list[str], timeout_key: str = "publish") -> dict:
        return self._module("publish.github", [verb, "--repo", slug, *fields], timeout_key)

    def _push(self, payload: dict) -> dict:
        """Publish this task's branch at the exact commit named, fast-forward only, after the push scan."""
        try:
            return self._push_checked(payload)
        except Refused as exc:
            # Object import and ls-remote can fail before the final push; keep their Git diagnostic too.
            from lu_machinery.secret_redactor import redact_text

            return {"returncode": 2, "stdout": "", "stderr": redact_text(str(exc))}

    def _push_checked(self, payload: dict) -> dict:
        if "head" not in payload or not set(payload) <= {"head", "branch", "repo", "lease"}:
            raise _BrokerRefusal("push takes head, and optionally branch, repo and lease")
        head = payload["head"]
        if not (isinstance(head, str) and _OBJECT_ID.fullmatch(head)):
            raise _BrokerRefusal("head must be a full commit id")
        if head != self.boundary.current_tip():
            raise _BrokerRefusal("head is not this task's branch tip")
        slug = self._repository_slug()
        self._claimed(payload, slug)
        lease = payload.get("lease")
        if lease is not None and not (isinstance(lease, str) and (lease == "" or _OBJECT_ID.fullmatch(lease))):
            raise _BrokerRefusal("lease must be a full commit id, or empty for an absent branch")
        url, credentials = self._remote(slug)
        branch = self.boundary.target
        if branch != self.boundary.branch and branch == f"refs/heads/{self._default_branch(slug)}":
            raise _BrokerRefusal("a continuation never publishes to the repository's default branch")
        self.boundary.fetch_objects(head)
        listing = _git(self.boundary.repository, *credentials, "ls-remote", url, branch, git_dir=self.boundary.common,
                       network=True)
        # ls-remote matches a pattern against ref-name tails; only the exact ref counts.
        exact = [line.split("\t")[0] for line in listing.splitlines() if line.partition("\t")[2] == branch]
        if len(exact) > 1 or (exact and not _OBJECT_ID.fullmatch(exact[0])):
            raise _BrokerRefusal("the remote branch cannot be read")
        remote = exact[0] if exact else None
        if lease is not None and lease != (remote or ""):
            raise _BrokerRefusal("the remote branch is not at the lease's expected commit")
        if remote == head:
            return {"returncode": 0, "stdout": f"{branch} is already {head} on {slug}\n", "stderr": ""}
        if remote is not None:
            try:
                _git(self.boundary.repository, "merge-base", "--is-ancestor", remote, head, git_dir=self.boundary.common)
            except Refused:
                raise _BrokerRefusal("the remote branch is not an ancestor of head: only a fast-forward is "
                                     "published") from None
        lease = f"--force-with-lease={branch}:{remote or ''}"
        _git(self.boundary.repository, *credentials, "push", "--quiet", "--no-verify", "--porcelain", lease, url,
             f"{head}:{branch}", git_dir=self.boundary.common, network=True)
        return {"returncode": 0, "stdout": f"pushed {head} to {branch} on {slug}\n", "stderr": ""}

    def _fetch_base(self, payload: dict) -> dict:
        """Fetch the repository's default branch into the primary's remote-tracking ref (fast-forward only)."""
        if payload:
            raise _BrokerRefusal("fetch-base takes no fields")
        slug = self._repository_slug()
        base = self._default_branch(slug)
        url, credentials = self._remote(slug)
        _git(self.boundary.repository, *credentials, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
             "--no-recurse-submodules", url, f"refs/heads/{base}:refs/remotes/origin/{base}",
             git_dir=self.boundary.common, network=True)
        return {"returncode": 0, "stdout": f"fetched {base} into refs/remotes/origin/{base}\n", "stderr": ""}

    def _pr_create(self, payload: dict) -> dict:
        if not set(payload) <= {"title", "body", "draft", "head", "base", "repo"} or not {"title", "body"} <= set(payload):
            raise _BrokerRefusal("pr-create takes title, body, draft, and optionally head, base and repo")
        title, body = self._text(payload, "title", 256), self._text(payload, "body", _MAX_BODY)
        draft = payload.get("draft", False)
        if type(draft) is not bool:
            raise _BrokerRefusal("draft must be true or false")
        slug = self._repository_slug()
        base = self._default_branch(slug)
        self._claimed({"repo": payload.get("repo"), "branch": payload.get("head"), "base": payload.get("base")}, slug,
                      base)
        private = self._private()
        if private:
            self._private_create_admitted()
        fields = ["--head", self.boundary.target.removeprefix("refs/heads/"), "--base", base,
                  f"--title={title}", "--body-file", self._text_file(body)]
        if private:
            # Spent before the host step runs: a create that fails or times out may still have opened a PR.
            self.pr_create_spent = True
        reply = self._publish("pr-create", slug, [*fields, *(["--draft"] if draft else [])])
        if private and reply["returncode"] == 0:
            # Bound before the next serialized verb runs (``serial`` is held); a created PR whose
            # number the publisher did not print stays unbound, and the spent create still refuses another.
            created = {int(match.group(1)) for line in reply["stdout"].splitlines() if (match := re.fullmatch(
                rf"https://github\.com/{re.escape(slug)}/pull/([1-9][0-9]{{0,7}})", line.strip(), re.IGNORECASE))}
            if len(created) == 1:
                self.bound_pr = created.pop()
        return reply

    def _private_create_admitted(self) -> None:
        """Refuse a private task's pr-create unless it is the task's first and only PR (#565 round 2, F1).

        A private task creates at most one PR, and only while it has no authorized PR: none named by the launch's
        ``--pr``, none bound (open, merged or closed since), no earlier pr-create forwarded in this run, and no
        continuation (``--publish-branch``), which continues the PR its branch already has and never opens another.
        """
        if (authorized := self._authorized_pr()) is not None:
            raise _BrokerRefusal(f"a private task creates no PR once it has its own PR ({authorized})")
        if getattr(self, "pr_create_spent", False):
            raise _BrokerRefusal("a private task creates at most one PR, and this run has already requested it")
        if self.boundary.target != self.boundary.branch:
            raise _BrokerRefusal("a private continuation continues its branch's existing PR and creates none")

    def _pr_edit(self, payload: dict) -> dict:
        if not set(payload) <= {"pr", "title", "body", "repo"} or not {"title", "body"} & set(payload):
            raise _BrokerRefusal("pr-edit takes pr, at least one of title and body, and optionally repo")
        title = self._optional_text(payload, "title", 256)
        body = self._optional_text(payload, "body", _MAX_BODY)
        number, slug, _facts = self._pull_request(payload, "OPEN")
        # ``--title=`` keeps a title that starts with a dash a value, never an option.
        fields = ["--number", str(number), *([f"--title={title}"] if title else []),
                  *(["--body-file", self._text_file(body)] if body else [])]
        return self._publish("pr-edit", slug, fields)

    def _pr_comment(self, payload: dict) -> dict:
        if not {"pr", "body"} <= set(payload) <= {"pr", "body", "repo"}:
            raise _BrokerRefusal("pr-comment takes pr, body and optionally repo")
        body = self._text(payload, "body", _MAX_BODY)
        number, slug, _facts = self._pull_request(payload, "OPEN", "MERGED")
        # The publisher refuses review-verdict markers in comment text: only the recorder writes them.
        return self._publish("pr-comment", slug, ["--number", str(number), "--body-file", self._text_file(body)])

    def _approval(self, head: str) -> str:
        """The review task of this worker that approved ``head``, from the host's task store, or a refusal.

        A worker cannot write the task store, and its comments cannot carry a verdict marker, so neither a
        comment nor a forged record grants this: the review must have been dispatched through this broker
        (``initiator`` is this task, its author model is the launch's), be done, cross-family, and its sealed
        reply must approve exactly ``head``.
        """
        import glob

        from lu_machinery.common.task_store_paths import tasks_dir
        from lu_machinery.review import record_cf_verdict as recorder

        task_id, author = self.boundary.args.task_id, self.boundary.args.author_model
        author_family = recorder.resolve_family(author) if isinstance(author, str) and author else "unknown"
        if author_family == "unknown" or author_family in recorder.UNRESOLVED_AUTHOR_FAMILIES:
            raise _BrokerRefusal("the launch names no author model family")
        root = tasks_dir()
        for path in sorted(root.glob(f"{glob.escape(task_id)}-review-*.json")):
            name = path.name.removesuffix(".json")
            if not _TASK_ID.fullmatch(name) or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}",
                                                                name[len(task_id) + len("-review-"):]):
                continue
            try:
                task, reply = recorder._task(name, root)
                verdict = recorder._task_verdict(task, reply)
            except (recorder.RecordError, ValueError, OSError):
                continue
            if (verdict.verdict == "APPROVED" and verdict.sha == head and task.get("initiator") == task_id
                    and task.get("review_author_model") == author and task.get("review_mode") == "cross_family"
                    and verdict.family and verdict.family != author_family):
                return name
        raise _BrokerRefusal("no cross-family APPROVE of this head by a review this task dispatched is recorded "
                             "on the host")

    def _pr_merge(self, payload: dict) -> dict:
        """Enqueue the worker's own PR at its exact live head, once a host-held approval of that head exists.

        The publisher's own gates still run: the recorder's exact-head marker, the same-head green CI Gate,
        ``--match-head`` and merge-queue admission; the merge group's CI decides the landing.
        """
        if not {"pr", "head"} <= set(payload) <= {"pr", "head", "repo"}:
            raise _BrokerRefusal("pr-merge takes pr, head and optionally repo")
        head = payload["head"]
        if not (isinstance(head, str) and _OBJECT_ID.fullmatch(head)):
            raise _BrokerRefusal("head must be a full commit id")
        number, slug, facts = self._pull_request(payload, "OPEN")
        if not (head == facts.get("headRefOid") == self.boundary.current_tip()):
            raise _BrokerRefusal("head is not both the PR's live head and this task's branch tip")
        if facts.get("baseRefName") != self._default_branch(slug):
            raise _BrokerRefusal("the PR's base is not the repository's default branch")
        review = self._approval(head)
        reply = self._publish("pr-merge", slug, ["--number", str(number), "--match-head", head], "pr-merge")
        reply["stdout"] = f"approval: {review}\n" + reply["stdout"]
        return reply

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
        self.slot_env: dict[str, str] = {}  # the bound test-slot pool's settings for the worker (#557)

    def run(self) -> int:
        from lu_machinery.agent_runtime.attempt_network import AttemptEgress

        if reason := unavailable():
            raise Refused(reason)
        from lu_machinery.review.isolation import ReviewIsolationError, _resolve_fixed_system_executable

        try:
            bwrap = _resolve_fixed_system_executable("bwrap", reject_roots=())
        except ReviewIsolationError as exc:
            raise Refused("bubblewrap is unavailable") from exc
        # The private Git and HOME layers are bubblewrap overlays (--overlay-src, bubblewrap 0.10 and later).
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
        # What the broker publishes to and counts as the task's own PR: the dispatched branch, or the PR branch a
        # continuation was dispatched with (``--branch``, the task's ``publish_branch``), and never both (#557).
        self.target = self.branch
        if (publish := self.args.publish_branch) is not None:
            if (self.args.mode != "workspace-write" or not _BRANCH.fullmatch(publish) or publish.startswith("refs/")
                    or ".." in publish or publish.endswith((".lock", "/", ".")) or "//" in publish):
                raise Refused("--publish-branch is not a plain branch name of a write dispatch")
            self.target = f"refs/heads/{publish}"
        self.common, self.git_dir, self.repository = common, git_dir, repository
        # The primary's own Git state stays read-only inside, on top of the private overlay; that includes
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
        read_only = self.args.mode == "read-only"
        layer_names = ("home-upper", "home-work") if read_only else ("home-upper", "home-work", "git-upper", "git-work")
        layers = {name: self.base / name for name in layer_names}
        for directory in (boundary, tmp, *layers.values()):
            directory.mkdir(mode=0o700)
        for name, source in _PROJECTED.items():
            _write_private(boundary / name, source.read_bytes(), 0o500)

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
        checkouts = [repository, *([product] if product is not None and product != repository else [])]
        toolchain = _toolchain(home, command[0])
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
        restores = [*base_restores, *own, self.worktree, *outputs, *credentials, tmp, boundary, *projected_reads]
        slot_pool = None if read_only else self._test_slot_pool()
        if slot_pool is not None:
            restores.append(slot_pool[1])
        if self.args.runtime is not None:
            restores.append(Path(self.args.runtime))
        if self.args.codex_auth is not None:
            restores.append(self.base / _CODEX_HOME)
        sources_mcp = self._sources_mcp(boundary, home)
        if sources_mcp is not None:
            restores.append(sources_mcp)
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
        probes = self._probes(repository, common, sockets, outputs, tmp, nonce)
        probes += self._view_probes(home, data, masks, restores, private, credentials, own, nonce)
        for path in projected_reads:
            probes.append({"op": "create", "path": str(path / f".write-boundary-canary-{nonce}"), "expect_any": ["EROFS"]})
        if slot_pool is not None:
            probes.append({"op": "stat", "path": str(slot_pool[1]), "expect_any": ["OK"]})
            probes.append({"op": "rename", "path": str(slot_pool[1]), "expect_any": ["EBUSY", "EROFS"]})
            for name in ("queue", "holders"):
                probes.append({"op": "rename", "path": str(slot_pool[1] / name), "expect_any": ["EBUSY"]})
        self.receipt["canary"] = {"probes_planned": len(probes), "passed": False}
        self.receipt["view"] = {"data_roots": len(data), "toolchain": len(toolchain), "checkouts": len(checkouts),
                                "private_masks": len(private), "own_records": len(own)}

        lower = self.base / "home-lower"
        lower.mkdir(mode=0o700)
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
        argv += (["--ro-bind", str(common), str(common)] if read_only else
                 ["--overlay-src", str(common), "--overlay", str(layers["git-upper"]), str(layers["git-work"]), str(common)])
        for name in self.protected:
            argv += ["--ro-bind", str(common / name), str(common / name)]
        argv += ["--ro-bind" if read_only else "--bind", str(self.worktree), str(self.worktree),
                 "--ro-bind", str(dot_git), str(dot_git)]
        for path in credentials:
            argv += ["--bind", str(path), str(path)]
        for path in outputs:
            argv += ["--bind", str(path), str(path)]
        argv += ["--bind", str(tmp), str(tmp), "--ro-bind", str(boundary), str(boundary)]
        if slot_pool is not None:
            # The host's test-slot pool, writable where a sandboxed run finds it, so governed tests queue with the
            # host's own runs (#557); ``test_slots.launcher_bound`` accepts only this mount inside. Its queue and
            # holder directories are mount points too: host runs resolve them by name, so a worker that could rename
            # one and put a link in its place would steer host unlinks and creates outside the pool (review F1).
            for name in ("", "queue", "holders"):
                argv += ["--bind", str(slot_pool[0] / name), str(slot_pool[1] / name)]
        runtime = tmp
        if self.args.runtime is not None:
            from lu_machinery.agent_runtime.read_only_tmp import dispatch_lease

            runtime = Path(self.args.runtime)
            if not read_only or runtime != dispatch_lease(self.args.task_id) or runtime.is_symlink() or not runtime.is_dir():
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
        self.broker = _Broker(self, boundary / "broker.sock")
        argv += ["--ro-bind", str(boundary), _INSIDE]
        interpreter = sources_command()[0]
        # The emptied roots are read-only, so no worker moves a restored path's parent aside to plant files there.
        if alias is not None:
            argv += ["--symlink", str(home), str(alias)]
        emptied = [path for path in (*data, *private) if path.is_dir() and path != home and path not in restores]
        argv += [arg for path in emptied for arg in ("--remount-ro", str(path))]
        _home_lower(lower, home, argv)
        # Every dispatch, write or read-only, reads GitHub through the host's read-only gh relay: the worker holds
        # no GitHub login and the egress proxy refuses GitHub, so without it ``gh pr view``/``pr checks`` fail.
        (boundary / "bin").mkdir(mode=0o700)
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
        except Exception as exc:  # Sources is optional for a write worker; containment does not depend on it
            self.receipt["sources"] = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        argv += ["--cap-drop", "ALL", "--chdir", str(self.worktree)]

        self.egress = AttemptEgress(boundary / "egress.sock", refused_domains=GITHUB_DOMAINS,
                                    refused_networks=github_networks())
        report_read, report_write = os.pipe()
        go_read, go_write = os.pipe()
        _write_private(boundary / "plan.json", json.dumps(
            {"report_fd": report_write, "go_fd": go_read, "probes": probes}).encode(), 0o444)
        argv += [interpreter, "-I", str(boundary / "canary.py"), str(boundary / "plan.json"), "--",
                 interpreter, "-I", str(boundary / "forwarder.py"), str(boundary / "egress.sock"), *command]
        self.receipt["bwrap"] = str(bwrap)
        self.receipt["argv_sha256"] = __import__("hashlib").sha256(json.dumps(argv).encode()).hexdigest()
        env = {key: value for key, value in os.environ.items()
               if key not in {"SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "TMUX", "TMUX_PANE"}
               and key not in self.args.host_only and not _github_credential_variable(key)}
        (tmp / "scratch").mkdir(mode=0o700)
        (tmp / "run").mkdir(mode=0o700)
        # The namespace has no DNS or route out; Node's own fetch uses the forwarder's proxy only when told to.
        # The host's task scratch (/var/tmp/<task-id>) is read-only inside, so it moves under TMPDIR too; the worker
        # still creates it on demand, and it goes with the boundary's private scratch.
        env.update(TMPDIR=str(runtime), LU_SCRATCH_ROOT=str(runtime / "scratch"), XDG_RUNTIME_DIR=str(runtime / "run"),
                   LU_TASK_SCRATCH=str(runtime / "task"), GIT_TERMINAL_PROMPT="0", NODE_USE_ENV_PROXY="1", **{BROKER_ENV: _BROKER_INSIDE})
        env["PATH"] = str(boundary / "bin") + os.pathsep + env.get("PATH", os.defpath)
        env.update(git_push_settings(env, interpreter, str(machinery_path("dispatcher/write_boundary.py").parents[2])))
        if codex_home is not None:
            env["CODEX_HOME"] = str(codex_home)
        if self.args.sources_mcp == "opencode":
            env["OPENCODE_CONFIG"] = _SOURCES_MCP_INSIDE
        env.update(self.slot_env)
        if (store := _pre_commit_store(home)) is not None and (found := _resolved(store)) is not None and found.is_dir():
            # The store is restored at its resolved path (``_toolchain``); a link on pre-commit's lookup path, such
            # as HOME's .cache, is not in the view, so pre-commit is told the directory it resolves on the host.
            env["PRE_COMMIT_HOME"] = str(found)
        # Project after the boundary directory exists and immediately before the sandbox starts, under the
        # task lock, so a grant written during setup is in the view the hook reads.
        from lu_machinery.dispatcher.ownership import project_launch_view

        project_launch_view(self.args.task_id, boundary)
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
        self.receipt["import"] = self._finish_git()
        if self.broker.closeout_request is not None:
            # Run by the supervisor once it has settled this task's worktree (merge_closeout leaves registered
            # worktrees in place and reports them), never by the launcher while the worktree still exists.
            self.receipt["closeout_request"] = self.broker.closeout_request
        return returncode

    def _test_slot_pool(self) -> tuple[Path, Path] | None:
        """The host's test-slot pool and where a write worker finds it inside: its default location (#557).

        The pool is prepared and its whole path verified here, on the host, where owners are real; the bind then
        pins the directory, not its name. Without the governor (disabled, or a pool that cannot be prepared) nothing
        is bound and a governed run inside refuses, typed, as before: the run itself is not refused for it.
        """
        from lu_machinery.common import test_slots

        try:
            if not test_slots.enabled():
                self.receipt["test_slots"] = {"bound": False, "reason": "disabled"}
                return None
            settings = {test_slots.ENABLE_ENV: "on", test_slots.COUNT_ENV: str(test_slots.pool_size(os.environ)),
                        test_slots.WAIT_ENV: f"{test_slots.wait_timeout(os.environ):g}"}
            pool = test_slots._open_pool(os.environ).resolve(strict=True)
        except (test_slots.SlotError, OSError) as exc:
            reason = exc.reason if isinstance(exc, test_slots.SlotError) else type(exc).__name__
            self.receipt["test_slots"] = {"bound": False, "reason": reason}
            return None
        # Bound where it resolves, as every restore is, so its mount point already exists whatever covers its
        # parents inside; a sandboxed run finds the pool only at the default location, so only that one is bound.
        if pool != test_slots._default_pool():
            self.receipt["test_slots"] = {"bound": False, "reason": "pool_not_at_default_location"}
            return None
        if _overlaps(pool, self.worktree) or _overlaps(pool, self.repository) or _under(pool, _MASKED_ROOTS):
            self.receipt["test_slots"] = {"bound": False, "reason": "pool_overlaps_the_view"}
            return None
        # The worker's runs size and wait as the host's do; the pool itself is found by its mount (review F2).
        self.slot_env = settings
        self.receipt["test_slots"] = {"bound": True, "pool_size": int(settings[test_slots.COUNT_ENV])}
        return pool, pool

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
                    config = json.loads(data)
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
            helper = json.loads(copies.get(login / "settings.json", b"{}")).get("apiKeyHelper")
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
        if self.args.mode != "workspace-write":
            raise Refused("--sources-mcp projects a write worker's MCP file only")
        config = sources_mcp_config(agent).encode()
        _write_private(boundary / _SOURCES_MCP, config, 0o600)
        relative = _SOURCES_MCP_HOME[agent]
        target = home / relative if relative is not None else boundary / _SOURCES_MCP
        self.receipt["sources_mcp"] = {"provider": agent, "config_sha256": hashlib.sha256(config).hexdigest()}
        return target

    def _probes(self, repository: Path, common: Path, sockets: list[Path], outputs: list[Path], tmp: Path,
                nonce: str) -> list[dict]:
        probes: list[dict] = []
        readonly = self.args.mode == "read-only"
        mount_refusal = ("EROFS", "EBUSY") if readonly else ("EBUSY",)

        def expect(op: str, path: Path | str, *outcomes: str, port: int | None = None) -> None:
            probes.append({"op": op, "path": str(path), "expect_any": list(outcomes), **({"port": port} if port else {})})

        tracked = _writable_tracked_file(repository)
        absent = repository / f".write-boundary-canary-{nonce}"
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
        # Positive controls: the worktree, the private Git overlay, HOME's overlay and TMPDIR take writes.
        for directory in (self.worktree, common / "objects"):
            expect("create" if readonly else "scratch_cycle", directory / f".write-boundary-canary-{nonce}",
                   "EROFS" if readonly else "OK")
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
                    report = json.loads(b"".join(chunks))
                    return report.get("probes") if isinstance(report, dict) else None
                return None
            chunks.append(chunk)
        raise Refused("the write boundary canary did not report in time")

    def _upper_tip(self, upper: Path) -> str | None:
        """The dispatched branch's tip the worker left in the private overlay, or None when unchanged."""
        loose = upper / self.branch
        try:
            mode = os.lstat(loose).st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and stat.S_ISREG(mode):
            return loose.read_text(encoding="ascii", errors="replace").strip()
        if mode is not None and not stat.S_ISCHR(mode):
            raise Refused("the dispatched branch ref is not a file")
        # Absent from the upper layer, or a whiteout (the worker removed the loose ref, e.g. by packing it).
        packed = upper / "packed-refs"
        if os.path.lexists(packed):
            if not stat.S_ISREG(os.lstat(packed).st_mode):
                raise Refused("packed-refs is not a file")
            for line in packed.read_text(encoding="ascii", errors="replace").splitlines():
                value, _, name = line.partition(" ")
                if name == self.branch:
                    return value.strip()
        if mode is not None:
            raise Refused("the dispatched branch was deleted")
        return None

    def current_tip(self) -> str:
        """The dispatched branch's tip as the worker sees it now (its private layer, else the launch tip)."""
        tip = self._upper_tip(self.base / "git-upper")
        return self.tip if tip is None else tip

    def fetch_objects(self, tip: str) -> None:
        """Copy ``tip`` and its history from the worker's private layer into the primary's object store.

        A private repository whose alternates are the upper objects is fetched from, so index-pack hashes each
        received object and only objects whose names match their bytes arrive. No ref of the primary changes.
        """
        if not _OBJECT_ID.fullmatch(tip):
            raise Refused("the branch tip is not an object id")
        upper = self.base / "git-upper"
        private = self.base / f"import-{uuid.uuid4().hex}.git"
        _git(self.base, "init", "-q", "--bare", str(private))
        alternates = [str(self.common / "objects")]
        if (upper / "objects").is_dir():
            alternates.insert(0, str(upper / "objects"))
        (private / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (private / "objects" / "info" / "alternates").write_text("\n".join(alternates) + "\n")
        _git(private, "update-ref", "refs/import", tip, git_dir=private)
        _git(self.repository, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
             str(private), "refs/import", git_dir=self.common)

    def _import(self) -> dict:
        upper = self.base / "git-upper"
        head = upper / self.git_dir.relative_to(self.common) / "HEAD"
        result: dict = {"branch": self.branch, "before": self.tip}
        try:
            with contextlib.suppress(FileNotFoundError):
                if head.read_text(encoding="ascii", errors="replace").strip() != f"ref: {self.branch}":
                    result["head_moved"] = True
            tip = self._upper_tip(upper)
            if tip is None or tip == self.tip:
                return {**result, "status": "unchanged", "after": self.tip}
            if not _OBJECT_ID.fullmatch(tip):
                return {**result, "status": "invalid_tip"}
            self.fetch_objects(tip)
            _git(self.repository, "update-ref", self.branch, tip, self.tip, git_dir=self.common)
            _git(self.worktree, "reset", "--quiet", "--mixed")
            return {**result, "status": "imported", "after": tip}
        except (Refused, OSError, subprocess.SubprocessError) as exc:
            return {**result, "status": "failed", "reason": str(exc)[:200]}

    def _finish_git(self) -> dict:
        # Reviewers cannot update Git metadata; there is no private branch tip to import.
        if self.args.mode == "read-only":
            return {"status": "unchanged", "before": self.tip, "after": self.tip}
        return self._import()

    def cleanup(self) -> None:
        if self.process is not None and self.process.poll() is None:
            # bwrap is the PID namespace's parent: every process of the sandbox ends with it.
            with contextlib.suppress(OSError):
                self.process.kill()
            self.process.wait()
        if self.receipt.get("status") in ("ran", "interrupted") and "import" not in self.receipt:
            # A cancelled or timed-out run still hands over the commits it made on the dispatched branch.
            self.receipt["import"] = self._finish_git()
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
        for path in self.created_outputs:
            with contextlib.suppress(OSError):
                if path.stat().st_size == 0:
                    path.unlink()
        if self.created:
            try:
                _remove(self.base)
            except OSError as exc:
                self.receipt["cleanup"] = f"residue: {type(exc).__name__}"


# ``git push`` inside the boundary (#443). The launcher rewrites GitHub push URLs (``url.<base>.pushInsteadOf``) to
# Git's documented remote-helper transport (gitremote-helpers(7)) named ``GIT_HELPER``. Git runs a helper as
# ``git remote-<name>``; that name is a Git alias here, so no file, PATH entry or other tool is added or shadowed.
# Fetches keep their URL (and fail inside, as before). The helper sends each ref update as the broker's ``push``
# verb with the target the caller named; the broker refuses any target that is not the launch's own.
GIT_HELPER = "lu-write-boundary"
_GITHUB_PUSH_URLS = ("https://github.com/", "git@github.com:", "ssh://git@github.com/")


def git_push_settings(env: dict[str, str], interpreter: str, package_parent: str) -> dict[str, str]:
    """``GIT_CONFIG_*`` entries that send ``git push`` to GitHub through the broker, after any the caller set."""
    count = env.get("GIT_CONFIG_COUNT", "0")
    start = int(count) if count.isdecimal() else 0
    command = [interpreter, "-I", "-c", _BOOTSTRAP, package_parent, "git-remote"]
    settings = [(f"alias.remote-{GIT_HELPER}", "!exec " + " ".join(shlex.quote(word) for word in command))]
    settings += [(f"url.{GIT_HELPER}::https://github.com/.pushInsteadOf", prefix) for prefix in _GITHUB_PUSH_URLS]
    entries = {"GIT_CONFIG_COUNT": str(start + len(settings))}
    for index, (key, value) in enumerate(settings, start):
        entries[f"GIT_CONFIG_KEY_{index}"] = key
        entries[f"GIT_CONFIG_VALUE_{index}"] = value
    return entries


def git_remote_main(argv: list[str]) -> int:
    """Git's remote helper for ``git push`` inside the boundary: each ref update becomes one ``push`` request."""
    if len(argv) != 2:
        print("write boundary git helper: expected the remote and its URL", file=sys.stderr)
        return 2
    remote, url = argv
    match = _GITHUB_REMOTE.fullmatch(url)
    leases: dict[str, str] = {}
    pending: list[str] = []

    def reply(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT)

    def listing() -> str:
        """The remote-tracking refs as the remote's refs: Git's own checks then see what it last pushed or fetched.

        The broker checks the live remote branch itself (fast-forward only, and any lease).
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", remote):
            return ""
        prefix = f"refs/remotes/{remote}/"
        refs = git("for-each-ref", "--format=%(objectname) %(refname)", prefix).stdout.splitlines()
        return "".join(f"{oid} refs/heads/{name.removeprefix(prefix)}\n" for oid, _, name in
                       (line.partition(" ") for line in refs) if name != prefix + "HEAD")

    def push(spec: str) -> str:
        source, _, destination = spec.removeprefix("+").partition(":")
        if not source:
            return f"error {destination} deleting a branch is not published through the write boundary broker\n"
        if match is None:
            return f"error {destination} the remote is not a github.com repository\n"
        resolved = git("rev-parse", "--verify", "--end-of-options", source + "^{commit}")
        if resolved.returncode != 0:
            return f"error {destination} the source is not a commit\n"
        payload = {"head": resolved.stdout.strip(), "branch": destination, "repo": match.group(1)}
        if destination in leases:
            expected = leases[destination]
            payload["lease"] = "" if not expected.strip("0") else expected
        answer = broker_call("push", payload)
        sys.stderr.write(str(answer.get("stdout", "")) + str(answer.get("stderr", "")))
        if answer.get("returncode") == 0:
            return f"ok {destination}\n"
        reason = str(answer.get("stderr", "")).strip().splitlines()
        return f"error {destination} {(reason[-1] if reason else 'the broker did not push').replace(chr(13), ' ')}\n"

    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if line == "capabilities":
            reply("push\noption\n\n")
        elif line.startswith("option "):
            name, _, value = line.removeprefix("option ").partition(" ")
            reference, colon, expected = value.partition(":")
            if name == "cas" and colon and reference.startswith("refs/") and _OBJECT_ID.fullmatch(expected):
                leases[reference] = expected
                reply("ok\n")
            else:
                reply("unsupported\n")
        elif line in {"list", "list for-push"}:
            reply(listing() + "\n")
        elif line.startswith("push "):
            pending.append(line.removeprefix("push "))
        elif not line and pending:
            reply("".join(push(spec) for spec in pending) + "\n")
            pending = []
        elif not line:
            return 0
        else:
            print(f"write boundary git helper: unsupported command {line[:40]!r}", file=sys.stderr)
            return 1
    return 0


def call_main(argv: list[str]) -> int:
    """``call <verb>``: send one broker request from inside a write boundary; the payload is JSON on stdin."""
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.dispatcher.write_boundary call",
        description="Send one request to the write boundary's host broker (inside a dispatched write worker).\n"
                    "The payload is one JSON object on stdin; docs/runbooks/write-worker-boundary.md lists each verb's "
                    "fields.",
        epilog="Example:\n  echo '{\"head\": \"<40-hex>\"}' | python -m lu_machinery.dispatcher.write_boundary call push\n"
               "Outputs: the host step's stdout and stderr.\n"
               "Exit codes: the host step's status; 2 when the broker refuses the request or no broker is present.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("verb", choices=_BROKER_VERBS)
    args = parser.parse_args(argv)
    if broker_path() is None:
        print("write boundary broker: not inside a write boundary", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
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


def main(argv: list[str]) -> int:
    if argv[:1] == ["call"]:
        return call_main(argv[1:])
    if argv[:1] == ["git-remote"]:
        return git_remote_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="lu_machinery.dispatcher.write_boundary",
        description="Run one dispatched provider inside the dispatcher's bubblewrap boundary (#423, #100).\n"
                    "Internal launcher used by the worker; not run by hand.",
        epilog="Outputs: the provider's stdout and stderr; the boundary receipt at --receipt.\n"
               f"Exit codes: the provider's status, or {REFUSED} when the boundary refuses to start it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--mode", choices=("workspace-write", "read-only"), default="workspace-write")
    parser.add_argument("--runtime", help="The read-only Codex task's existing private scratch lease")
    parser.add_argument("--publish-branch", help="The PR branch a continuation dispatch publishes to (its "
                        "publish_branch); the broker pushes and owns that PR only")
    parser.add_argument("--pr", type=_pr_number, help="The PR the task record names (a review's PR); in a private "
                        "repository every PR verb and github-read act on this PR only (default: the open PR "
                        "of the dispatched branch against the default branch, bound on first use)")
    parser.add_argument("--author-model", help="The dispatched model, the review family check's author "
                        "(default: none; the broker then dispatches no review)")
    parser.add_argument("--output", action="append", help="A declared output file the provider writes")
    parser.add_argument("--credential", action="append", help="A provider credential file a refresh rewrites")
    parser.add_argument("--claude-login", help="The selected Claude login directory; its guard-bound state is "
                        "projected as private copies")
    parser.add_argument("--codex-auth", help="The selected Codex account's auth.json, linked into a task-local "
                        "CODEX_HOME that holds only the Sources server")
    parser.add_argument("--sources-mcp", choices=tuple(_SOURCES_MCP_HOME),
                        help="The write worker's provider, whose task-local MCP file names only the Sources relay "
                        "(default: none)")
    parser.add_argument("--host-env", action="append", default=[],
                        help="NAME=VALUE of the dispatcher's host configuration, for the launcher only (default: none)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    receipt = {"schema": SCHEMA, "task_id": args.task_id, "mode": args.mode, "status": "refused", "started_at": _now()}
    if not _TASK_ID.fullmatch(args.task_id):
        receipt["reason"] = "unsafe task id"
        _write_receipt(Path(args.receipt), receipt)
        return REFUSED
    args.host_only = set()
    for item in args.host_env:
        name, equals, value = item.partition("=")
        if name not in _HOST_CONTEXT or not equals or "\0" in value:
            receipt["reason"] = f"--host-env names an unknown variable: {name[:40]}"
            _write_receipt(Path(args.receipt), receipt)
            return REFUSED
        os.environ[name] = value  # the launcher's own host steps read it; the provider never sees it
        args.host_only.add(name)
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
