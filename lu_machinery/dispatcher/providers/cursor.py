"""Cursor print-mode execution and assistant stream extraction."""

import json

from lu_machinery.common.jsonl import jsonl_lines
from lu_machinery.dispatcher.providers.stream_json import model_observer as _observe
from lu_machinery.dispatcher.providers.stream_json import one_model
from lu_machinery.dispatcher.types import Invocation, ParsedResult, TaskSpec

MODEL_SOURCE = "cursor-stream-json"


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

    ``--mode ask`` refused every shell call, ``git diff`` and ``sha256sum`` included (2026-11-15 bench). Cursor takes per-command rules only from
    this file, so the write boundary binds it read-only at HOME's ``.cursor/cli-config.json``. Every shell command
    runs except the Git and GitHub writes Claude's profile denies; file writes and the Sources tools that persist
    entries are denied too, and deny wins over allow. ``allowlist`` approval with no ``--force`` refuses any other
    call that needs approval. Cursor's own sandbox stays off, as Claude's per-shell prefix is stripped: a nested
    bubblewrap cannot start, and the read-only whole-process boundary is the launcher's.
    """
    from lu_machinery.dispatcher.providers.claude import _READ_DENIES
    from lu_machinery.dispatcher.write_boundary import _CURSOR_SOURCES_SERVER as server

    deny = [_shell_rule(rule) for rule in _READ_DENIES.split(",") if rule.startswith("Bash(")]
    config = {
        "version": 1,
        # Cursor's schema requires ``editor``; without it Cursor rewrites the file to repair it, and since the file is
        # read-only it then falls back to its defaults, which allow only ``Shell(ls)`` (cursor-agent 2026.11.13).
        "editor": {"vimMode": False},
        "approvalMode": "allowlist",
        "autoAcceptWebSearch": True,
        "sandbox": {"mode": "disabled"},
        "permissions": {
            "allow": ["Shell(*)", "Read(*)", *(f"Mcp({server}:{name})" for name in readers)],
            "deny": ["Write(*)", *deny, *(f"Mcp({server}:{name})" for name in writers)],
        },
    }
    return json.dumps(config, indent=2) + "\n"


def build(spec: TaskSpec, env: dict[str, str]) -> Invocation:
    argv = ["cursor-agent", "-p", "--model", spec.model, "--output-format", "stream-json", "--trust"]
    argv += ["--workspace", str(spec.worktree)]
    if spec.mode == "read-only":
        # The rules are read_only_cli_config's, never a workspace's own .cursor/cli.json. --approve-mcps loads the
        # projected Sources relay; only the tools that file allows run.
        argv += ["--disable-project-configs", "--approve-mcps"]
    elif spec.mode == "workspace-write":
        argv += ["--approve-mcps", "--sandbox", "disabled", "--force"]
    else:
        raise ValueError(f"cursor: unsupported mode {spec.mode!r}")
    return Invocation(argv, spec.prompt_file.read_bytes(), env, None)


def parse(stdout: str, stderr: str, returncode: int, final_text: str | None) -> ParsedResult:
    parts, messages, result, error = [""], [], {}, None
    models = set()
    for line in jsonl_lines(stdout):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
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
