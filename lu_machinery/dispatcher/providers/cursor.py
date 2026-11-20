"""Cursor print-mode execution and assistant stream extraction."""

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from lu_machinery.agent_runtime.sources_read_only import sources_tool_sets
from lu_machinery.common import json_depth
from lu_machinery.common.jsonl import jsonl_lines
from lu_machinery.common.safe_open import safe_open_below
from lu_machinery.dispatcher.providers import (
    SourcesServerSpec,
    is_ukrainian_content_task,
    load_product_sources_server,
)
from lu_machinery.dispatcher.providers.stream_json import SOURCES_SERVER, one_model
from lu_machinery.dispatcher.providers.stream_json import model_observer as _observe
from lu_machinery.dispatcher.types import Invocation, ParsedResult, TaskSpec

MODEL_SOURCE = "cursor-stream-json"
_LOG = logging.getLogger(__name__)


def _init_model(event: object) -> str | None:
    """The model a ``system``/``init`` event reports; no other event attests one."""
    if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
        model = event.get("model")
        if isinstance(model, str) and model.strip():
            return model
    return None


def _reported(event: object) -> tuple[str, ...]:
    model = _init_model(event)
    return () if model is None else (model,)


def model_observer(stdout_path):
    """Attest the running task's model from complete init events, under ``parse``'s exactly-one rule.

    The review recorder resolves a Cursor author from its task record, so a running author's PR is
    reviewable only once the record carries this attestation (#12014); settlement re-derives it from
    the whole stream.
    """
    return _observe(stdout_path, _reported)


def _shell_rule(claude_rule: str) -> str:
    """Cursor's form of one Claude ``Bash(cmd sub *)`` deny: ``Shell(cmd:sub*)``, the base command and an args glob.

    Cursor's ``*`` matches any text and a rule matches each command of a compound one
    (https://cursor.com/docs/cli/reference/permissions), so ``git:push*`` refuses ``git push`` with any arguments.
    """
    command, _, arguments = claude_rule.removeprefix("Bash(").removesuffix(")").removesuffix(" *").partition(" ")
    return f"Shell({command}:{arguments}*)"


def read_only_cli_config(readers: tuple[str, ...], writers: tuple[str, ...]) -> str:
    """The ``cli-config.json`` a read-only run reads: the permission profile of a read-only Claude run.

    ``--mode ask`` refused every shell call, ``git diff`` and ``sha256sum`` included (2026-11-15 bench). Cursor
    takes per-command rules only from this file and rewrites it at every start, so the write boundary puts a private
    copy at HOME's ``.cursor/cli-config.json``, discarded with the run's scratch. Every shell command
    runs except the Git and GitHub writes Claude's profile denies. Native WebFetch can run without prompting.
    File writes and the Sources tools that persist entries are denied, and deny wins over allow.
    ``allowlist`` approval with no ``--force`` refuses any other call that needs approval.
    Cursor's own sandbox stays off, as Claude's per-shell prefix is stripped: a nested
    bubblewrap cannot start, and the read-only whole-process boundary is the launcher's.
    """
    from lu_machinery.dispatcher.providers.claude import _READ_DENIES
    from lu_machinery.dispatcher.write_boundary import _CURSOR_SOURCES_SERVER as server

    deny = [_shell_rule(rule) for rule in _READ_DENIES.split(",") if rule.startswith("Bash(")]
    config = {
        "version": 1,
        # Cursor's schema requires ``editor``; a profile without it is a repair case, and a failed repair falls back
        # to Cursor's defaults, which allow only ``Shell(ls)`` (cursor-agent 2026.11.13).
        "editor": {"vimMode": False},
        "approvalMode": "allowlist",
        "autoAcceptWebSearch": True,
        "sandbox": {"mode": "disabled"},
        "permissions": {
            "allow": ["Shell(*)", "Read(*)", "WebFetch(*)", *(f"Mcp({server}:{name})" for name in readers)],
            "deny": ["Write(*)", *deny, *(f"Mcp({server}:{name})" for name in writers)],
        },
    }
    return json.dumps(config, indent=2) + "\n"


def workspace_write_cli_config(readers: tuple[str, ...], writers: tuple[str, ...]) -> str:
    """The per-invocation ``cli-config.json`` for a workspace-write Cursor run: allow readers, deny writers.

    Passed via ``CURSOR_CONFIG_DIR`` so the host's Cursor settings are never modified.
    Cursor evaluates explicit ``permissions.deny`` before ``--force`` auto-approval.
    """
    config = {
        "version": 1,
        "editor": {"vimMode": False},
        "approvalMode": "allowlist",
        "autoAcceptWebSearch": True,
        "sandbox": {"mode": "disabled"},
        "permissions": {
            "allow": ["Shell(*)", "Read(*)", "Write(*)", *(f"Mcp({SOURCES_SERVER}:{name})" for name in readers)],
            "deny": [*(f"Mcp({SOURCES_SERVER}:{name})" for name in writers)],
        },
    }
    return json.dumps(config, indent=2) + "\n"


def _workspace_config_dir(
    spec: TaskSpec, env: Mapping[str, str], server: SourcesServerSpec | None,
) -> Path | None:
    """Write Cursor's permissions and MCP configuration inside the dispatch scratch."""
    from lu_machinery.common.repo_root import ProductRootError

    try:
        readers, writers = sources_tool_sets()
    except (FileNotFoundError, ProductRootError):
        if server is not None:
            raise
        return None
    scratch = Path(env.get("LU_TASK_SCRATCH", ""))
    if not scratch.is_absolute() or scratch.is_symlink():
        raise ValueError("cursor_config_dir_unsafe: LU_TASK_SCRATCH must be an absolute non-symlink path")
    if spec.options.get("dry_run") == "True":
        return scratch / "cursor-home" / ".cursor"

    configs = {"cli-config.json": workspace_write_cli_config(readers, writers)}
    if server is not None:
        entry: dict[str, object]
        if server.url is not None:
            entry = {"url": server.url, "headers": dict(server.headers)}
        else:
            entry = {"command": server.command, "args": list(server.args), "env": dict(server.env)}
            if server.cwd is not None:
                entry["cwd"] = server.cwd
        configs["mcp.json"] = json.dumps({"mcpServers": {SOURCES_SERVER: entry}}) + "\n"

    task_home = Path(tempfile.mkdtemp(prefix="cursor-home-", dir=scratch))
    directory = task_home / ".cursor"
    directory.mkdir(mode=0o700)
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        for name, content in configs.items():
            fd = safe_open_below(dir_fd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
    finally:
        os.close(dir_fd)
    if server is not None and "GIT_CONFIG_GLOBAL" not in env:
        home = Path(env.get("HOME") or Path.home())
        git_config = home / ".gitconfig"
        if git_config.is_file():
            # An include preserves the XDG layer and resolves relative includes at the original file.
            with (task_home / ".gitconfig").open("x", encoding="utf-8") as stream:
                stream.write(f"[include]\n\tpath = {json.dumps(str(git_config), ensure_ascii=False)}\n")
    return directory


def build(spec: TaskSpec, env: dict[str, str]) -> Invocation:
    argv = ["cursor-agent", "-p", "--model", spec.model, "--output-format", "stream-json", "--trust"]
    argv += ["--workspace", str(spec.worktree)]
    child_env = env.copy()
    if spec.mode == "read-only":
        # The rules are read_only_cli_config's, never a workspace's own .cursor/cli.json. --approve-mcps loads the
        # projected Sources relay; only the tools that file allows run.
        argv += ["--disable-project-configs", "--approve-mcps"]
    elif spec.mode == "workspace-write":
        server = load_product_sources_server("cursor")
        if server is None:
            if is_ukrainian_content_task(spec.options):
                raise ValueError(
                    "cursor_sources_mcp_unavailable: product .mcp.json declares no active 'sources' "
                    "MCP server for this Ukrainian-content write task"
                )
            _LOG.warning(
                "cursor_sources_mcp_unavailable: product .mcp.json declares no active 'sources' MCP server"
            )
        if (config_dir := _workspace_config_dir(spec, env, server)) is not None:
            child_env["CURSOR_CONFIG_DIR"] = str(config_dir)
            if server is not None:
                # Cursor loads mcp.json from HOME. Keep the existing Git and GitHub CLI configuration
                # available to the write worker's shell.
                home = Path(env.get("HOME") or Path.home())
                child_env.setdefault("XDG_CONFIG_HOME", str(home / ".config"))
                child_env["HOME"] = str(config_dir.parent)
        argv += ["--approve-mcps", "--sandbox", "disabled", "--force"]
    else:
        raise ValueError(f"cursor: unsupported mode {spec.mode!r}")
    return Invocation(argv, spec.prompt_file.read_bytes(), child_env, None)


def parse(stdout: str, stderr: str, returncode: int, final_text: str | None) -> ParsedResult:
    parts, messages, result, error = [""], [], {}, None
    models = set()
    for line in jsonl_lines(stdout):
        if not line.strip():
            continue
        try:
            event = json_depth.loads(line, max_depth=512)
        except ValueError:
            error = "cursor: invalid JSON stream"
            continue
        if not isinstance(event, dict):
            error = "cursor: invalid event"
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            if (model := _init_model(event)) is not None:
                models.add(model)
        elif event.get("type") == "result":
            result = event
            if event.get("is_error") or event.get("subtype") != "success":
                error = "cursor: terminal result unsuccessful"
        elif event.get("type") == "error":
            error = "cursor: terminal error"
        elif event.get("type") == "tool_call":
            # Text before a tool call narrates the work; the reply is the turn after the last one.
            parts, messages = [""], []
        elif event.get("type") == "text" and isinstance(event.get("content"), str):
            parts[-1] += event["content"]
        elif event.get("role") == "assistant" or event.get("type") == "assistant":
            content = event.get("message", event)
            content = content.get("content") if isinstance(content, dict) else None
            if isinstance(content, list):
                content = "".join(
                    p["text"]
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
                )
            if isinstance(content, str) and content.strip():
                messages.append(content.strip())
                parts.extend([content, ""])
    # The terminal result concatenates the run's assistant messages with no separator, gluing
    # a trailing "VERDICT:" line onto the previous sentence; the separate messages keep it.
    # Only messages of the final turn remain, so narration before a tool call is not the reply.
    text = "\n\n".join(messages)
    if not text:
        text = result.get("result")
        if not isinstance(text, str) or not text.strip():
            text = "\n\n".join(p.strip() for p in parts if p.strip()) or None
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else None
    if returncode:
        error = f"cursor: exited with status {returncode}"
    return ParsedResult(
        text,
        usage,
        error or (None if result and text else "cursor: missing result"),
        one_model(models),
    )
