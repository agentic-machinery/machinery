"""Provider registry. Each provider module exposes:

build(spec: TaskSpec, env: dict[str, str]) -> Invocation
parse(stdout: str, stderr: str, returncode: int, final_text: str | None) -> ParsedResult
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

from lu_machinery.ai_agent_bridge.routing_guard import SAVANT_PROVIDER
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import product_root
from lu_machinery.dispatcher.providers.stream_json import SOURCES_SERVER
from lu_machinery.dispatcher.types import TaskSpec

PROVIDERS = ("codex", "claude", "cursor", "agy", "muse", "opencode")
_SOURCES_JSON_MAX_DEPTH = 512
_SOURCES_CONFIG_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SourcesServerSpec:
    """Validated transport specification for the trusted Sources MCP server."""

    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in text)


def _valid_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or _has_control_chars(value) or any(char.isspace() for char in stripped):
        return None
    parsed = urlsplit(stripped)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return stripped


def _valid_str_map(value: object, *, forbid_equals: bool = False) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or _has_control_chars(key) or (forbid_equals and "=" in key):
            return None
        if not isinstance(item, str) or _has_control_chars(item):
            return None
        result[key] = item
    return result


def is_ukrainian_content_task(options: Mapping[str, object]) -> bool:
    """True when task metadata marks a Ukrainian-content authoring or review task."""
    from lu_machinery.review.language_lane import is_ukrainian_review

    normalized: dict[str, object] = {
        key: value.strip() if isinstance(value, str) else value for key, value in options.items()
    }
    if is_ukrainian_review(normalized):
        return True
    track = str(normalized.get("research_track") or "").casefold()
    return track.startswith("l2-uk")


def load_product_sources_server(agent: str) -> SourcesServerSpec | None:
    """Load and validate the ``sources`` entry from ``product_root()/.mcp.json`` (never a worktree copy).

    Returns ``None`` when the product declares no active ``sources`` entry, and raises a typed
    ``ValueError`` (``<agent>_sources_config_invalid``) on malformed declarations.
    """
    mcp_path = product_root() / ".mcp.json"
    try:
        raw = mcp_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{agent}_sources_config_invalid: could not read product .mcp.json: {exc}") from None
    try:
        payload = json_depth.loads(raw, max_depth=_SOURCES_JSON_MAX_DEPTH)
    except (ValueError, RecursionError):
        raise ValueError(f"{agent}_sources_config_invalid: product .mcp.json is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError(f"{agent}_sources_config_invalid: product .mcp.json root must be an object")
    servers = payload.get("mcpServers")
    if servers is None:
        return None
    if not isinstance(servers, dict):
        raise ValueError(f"{agent}_sources_config_invalid: mcpServers must be an object")
    entry = servers.get(SOURCES_SERVER)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ValueError(f"{agent}_sources_config_invalid: mcpServers.{SOURCES_SERVER} must be an object")
    disabled = entry.get("disabled")
    if disabled is not None and not isinstance(disabled, bool):
        raise ValueError(f"{agent}_sources_config_invalid: disabled must be a boolean")
    if disabled is True:
        return None
    has_url = "url" in entry and entry["url"] is not None
    has_cmd = "command" in entry and entry["command"] is not None
    if has_url and has_cmd:
        raise ValueError(f"{agent}_sources_config_invalid: sources entry cannot specify both url and command")
    if not has_url and not has_cmd:
        raise ValueError(f"{agent}_sources_config_invalid: sources entry requires url or command")
    if has_url:
        url = _valid_http_url(entry["url"])
        if url is None:
            raise ValueError(f"{agent}_sources_config_invalid: sources url must be a valid http(s) URL")
        raw_headers = entry.get("headers")
        headers: dict[str, str] = {}
        if raw_headers is not None:
            parsed_headers = _valid_str_map(raw_headers)
            if parsed_headers is None:
                raise ValueError(f"{agent}_sources_config_invalid: sources headers must map strings to strings")
            headers = parsed_headers
        return SourcesServerSpec(url=url, headers=headers)
    raw_command = entry["command"]
    if not isinstance(raw_command, str) or not raw_command.strip() or _has_control_chars(raw_command):
        raise ValueError(f"{agent}_sources_config_invalid: sources command must be a non-empty string")
    raw_args = entry.get("args")
    args: tuple[str, ...] = ()
    if raw_args is not None:
        if not isinstance(raw_args, list) or not all(
            isinstance(arg, str) and not _has_control_chars(arg) for arg in raw_args
        ):
            raise ValueError(f"{agent}_sources_config_invalid: sources args must be a list of strings")
        args = tuple(raw_args)
    raw_env = entry.get("env")
    env_map: dict[str, str] = {}
    if raw_env is not None:
        parsed_env = _valid_str_map(raw_env, forbid_equals=True)
        if parsed_env is None:
            raise ValueError(f"{agent}_sources_config_invalid: sources env must map valid env names to strings")
        env_map = parsed_env
    raw_cwd = entry.get("cwd")
    cwd: str | None = None
    if raw_cwd is not None:
        if not isinstance(raw_cwd, str) or not raw_cwd.strip() or _has_control_chars(raw_cwd):
            raise ValueError(f"{agent}_sources_config_invalid: sources cwd must be a non-empty string")
        cwd = raw_cwd
    return SourcesServerSpec(command=raw_command, args=args, env=env_map, cwd=cwd)


def _ambient_sources_entry_state(path: Path, *, allow_server_url: bool, allow_symlink: bool) -> bool | None:
    """Return True if ``path`` defines an active ``sources`` entry, False if disabled/invalid, None if absent."""
    try:
        if not allow_symlink and path.is_symlink():
            return False
        info = path.stat()
        if info.st_size > _SOURCES_CONFIG_MAX_BYTES:
            return False
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return False
    try:
        payload = json_depth.loads(raw, max_depth=_SOURCES_JSON_MAX_DEPTH)
    except (ValueError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict) or SOURCES_SERVER not in servers:
        return None
    entry = servers[SOURCES_SERVER]
    if not isinstance(entry, dict) or entry.get("disabled") is True:
        return False
    if _valid_http_url(entry.get("url")) is not None:
        return True
    if allow_server_url and _valid_http_url(entry.get("serverUrl")) is not None:
        return True
    command = entry.get("command")
    return isinstance(command, str) and bool(command.strip()) and not _has_control_chars(command)


def has_ambient_sources_config(agent: str, *, worktree: Path, env: Mapping[str, str] | None = None) -> bool:
    """Detect whether an ambient Sources entry exists where ``agent`` natively reads MCP config (read-only)."""
    home: Path | None
    if env is not None and "HOME" in env:
        home_raw = env["HOME"].strip()
        home = Path(home_raw) if home_raw else None
    else:
        home_raw = os.environ.get("HOME", "").strip()
        home = Path(home_raw) if home_raw else Path.home()
    if agent == "agy":
        if home is None:
            return False
        return (
            _ambient_sources_entry_state(
                home / ".gemini" / "config" / "mcp_config.json",
                allow_server_url=True,
                allow_symlink=True,
            )
            is True
        )
    if agent == "cursor":
        # Cursor gives workspace .cursor/mcp.json precedence over ~/.cursor/mcp.json for the same server key.
        project_state = _ambient_sources_entry_state(
            worktree / ".cursor" / "mcp.json",
            allow_server_url=False,
            allow_symlink=False,
        )
        if project_state is not None:
            return project_state
        if home is None:
            return False
        return (
            _ambient_sources_entry_state(
                home / ".cursor" / "mcp.json",
                allow_server_url=False,
                allow_symlink=True,
            )
            is True
        )
    return False


def workspace_sources_refusal(spec: TaskSpec, env: Mapping[str, str] | None = None) -> str | None:
    """Typed refusal for workspace-write Sources MCP availability/validity, or None when launch may proceed."""
    if spec.mode != "workspace-write":
        return None
    if spec.agent in {"claude", "muse"} or (spec.agent == "opencode" and spec.provider == SAVANT_PROVIDER):
        return None
    if spec.agent in {"codex", "opencode"}:
        try:
            server = load_product_sources_server(spec.agent)
        except ValueError as exc:
            return str(exc)
        if server is None and is_ukrainian_content_task(spec.options):
            return (
                f"{spec.agent}_sources_mcp_unavailable: product .mcp.json declares no active "
                f"{SOURCES_SERVER!r} MCP server for this Ukrainian-content write task"
            )
        return None
    if spec.agent in {"agy", "cursor"}:
        if not has_ambient_sources_config(spec.agent, worktree=spec.worktree, env=env) and is_ukrainian_content_task(
            spec.options
        ):
            location = (
                "~/.gemini/config/mcp_config.json"
                if spec.agent == "agy"
                else "worktree .cursor/mcp.json or ~/.cursor/mcp.json"
            )
            return (
                f"{spec.agent}_sources_mcp_unavailable: {spec.agent} CLI has no per-invocation MCP server flag or "
                f"env var and {location} defines no active {SOURCES_SERVER!r} server for this Ukrainian-content write task"
            )
        return None
    return None

# Read-only must be unable to edit. The supervisor confines native routes in write_boundary's
# read-only mounts. Claude receives the trusted boundary marker before provider construction.
# AGY retains its own read-only bubblewrap boundary (agy_sandbox, C-3027).
# Provider permissions are additional restrictions, never a substitute for the OS boundary.
READ_ONLY_REFUSALS: dict[str, str] = {}


def read_only_refusal(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """The pre-launch provider refusal for read-only, or None; the supervisor still enforces OS containment.

    AGY is refused where its bubblewrap boundary cannot start. With the run's environment, Claude is
    also refused while any managed policy could widen its lock.
    """
    reason = READ_ONLY_REFUSALS.get(name)
    if reason is None and name == "agy":
        from lu_machinery.dispatcher import agy_sandbox

        reason = agy_sandbox.unavailable()
    if reason is None and name == "claude" and env is not None:
        from lu_machinery.dispatcher.providers import claude

        reason = claude.managed_policy(env)
    if reason is None:
        return None
    usable = ", ".join(provider for provider in PROVIDERS if provider not in READ_ONLY_REFUSALS and provider != name)
    return f"read_only_unenforceable: {name} cannot run --mode read-only: {reason}; use {usable}"


def load(name: str) -> ModuleType:
    if name == "codex":
        from lu_machinery.dispatcher.providers import codex as module
    elif name == "claude":
        from lu_machinery.dispatcher.providers import claude as module
    elif name == "cursor":
        from lu_machinery.dispatcher.providers import cursor as module
    elif name == "agy":
        from lu_machinery.dispatcher.providers import agy as module
    elif name == "muse":
        from lu_machinery.dispatcher.providers import muse as module
    elif name == "opencode":
        from lu_machinery.dispatcher.providers import opencode as module
    else:
        raise ValueError(f"unknown agent {name!r}; known: {', '.join(PROVIDERS)}")
    return module
