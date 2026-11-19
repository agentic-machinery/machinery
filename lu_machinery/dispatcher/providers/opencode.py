"""Native OpenCode routes for DeepSeek V4.1 Flash and SubQ Savant trials."""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
from pathlib import Path

from lu_machinery.ai_agent_bridge.routing_guard import SAVANT_MODEL, SAVANT_PROVIDER, savant_request_store
from lu_machinery.common import json_depth
from lu_machinery.common.jsonl import jsonl_lines
from lu_machinery.common.safe_open import safe_open_below
from lu_machinery.dispatcher.types import Invocation, ParsedResult, TaskSpec
from lu_machinery.orchestration import dispatch_isolation

# OpenCode allows edits and every shell command by default and has no OS sandbox, so read-only
# carries its own permission rules (https://opencode.ai/docs/permissions/): ``edit`` covers the
# edit, write and patch tools, and for bash the last matching pattern wins. Only read-only git
# forms run; a redirect, or an option that writes a file or starts an external diff program, stays
# denied (a redirect is not a separate command, so ``git log *`` alone would admit ``> file``). OpenCode
# merges ``OPENCODE_PERMISSION`` over the loaded config's ``permission``.
_READ_ONLY_PERMISSION = json.dumps(
    {
        "edit": "deny",
        "task": "deny",
        "bash": {
            "*": "deny",
            **{f"git {verb}{rest}": "allow" for verb in ("diff", "log", "show", "status") for rest in ("", " *")},
            "* --output*": "deny",
            "* --ext-diff*": "deny",
            "*>*": "deny",
        },
    },
    separators=(",", ":"),
)


# The endpoint is chosen explicitly with ``--provider`` and recorded on the task record; nothing
# switches it. Both run V4.1 Flash only (core P2). The OpenRouter route is pinned to the V4.1 Flash
# line and to the US provider allow-list in model-assignment.md, with no provider fallback.
DEFAULT_ENDPOINT = "deepseek"
MODELS = {
    "deepseek": "deepseek/deepseek-flash",
    "openrouter": "openrouter/deepseek/deepseek-v4.1-flash",
    SAVANT_PROVIDER: f"{SAVANT_PROVIDER}/{SAVANT_MODEL}",
}
_SAVANT_API = "https://api.subq.ai/v1"
_SAVANT_KEY_FILE_ENV = "LU_SUBQ_API_KEY_FILE"
_SAVANT_MANAGER_ENV_MAX_BYTES = 1024 * 1024
_SAVANT_CONTEXT = 4_000_000
_SAVANT_DEFAULT_MAX_TOKENS = 64_000
SAVANT_DEFAULT_EFFORT = "high"
# Revalidate the full request body before widening the compatible OpenCode version.
_SAVANT_OPENCODE_VERSION = "1.19.6"
# Public classes give Savant OpenCode's read-only file tools on the write boundary's snapshot of the published
# start commit; private classes keep every tool disabled and the prompt as the only input (designated approval,
# conditions C1-C4). The snapshot cap and the run's upstream byte ceiling bound what reaches SubQ. A public write
# run (#898) also gets the ``edit`` permission (OpenCode's edit, write and patch tools) on its own copy of that
# view; the boundary turns its edits into a patch once it exits.
_SAVANT_TOOL_CLASSES = frozenset({"public-code", "public-docs"})
# OpenCode 1.19.6 has no ``list`` tool: ``read`` lists a directory under the ``read`` permission.
_SAVANT_READ_TOOLS = ("read", "grep", "glob")
SAVANT_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
SAVANT_UPLOAD_MAX_BYTES = 16 * 1024 * 1024
OPENROUTER_PROVIDERS = ("fireworks/us", "baseten/us", "inference-net/us")
_OPENROUTER_MODEL_ID = MODELS["openrouter"].removeprefix("openrouter/")
_ROUTING = {"only": list(OPENROUTER_PROVIDERS), "allow_fallbacks": False, "data_collection": "deny"}
# OpenCode deep-merges this over the operator's and the checkout's config, and the merge keeps
# inherited keys (https://opencode.ai/docs/config/). So it pins what decides each call: only the
# OpenRouter provider is enabled and only the pinned model exists on it, under its own id. Routing
# sits in the model options (title calls) and the high variant, which OpenCode applies last. The
# title, summary and compaction agents use the same model. Whatever this cannot pin is checked on
# the resolved config before spawn (``_config_conflict``).
_OPENROUTER_PROVIDER = {
    "whitelist": [_OPENROUTER_MODEL_ID],
    "models": {_OPENROUTER_MODEL_ID: {
        "id": _OPENROUTER_MODEL_ID, "options": {"provider": _ROUTING}, "variants": {"high": {"provider": _ROUTING}},
    }},
}
_PINNED_AGENTS = ("build", "title", "summary", "compaction")
_OPENROUTER_CONFIG = json.dumps(
    {
        "enabled_providers": ["openrouter"],
        "small_model": MODELS["openrouter"],
        "agent": {name: {"model": MODELS["openrouter"]} for name in _PINNED_AGENTS},
        "provider": {"openrouter": _OPENROUTER_PROVIDER},
    },
    separators=(",", ":"),
)
# OpenCode merges a checkout's own config files and directory below the injected config.
_PROJECT_CONFIG = ("opencode.json", "opencode.jsonc", ".opencode")
_BUDGETS = Path(__file__).resolve().parents[2] / "config" / "agent_budgets.yaml"
_PROBE_TIMEOUT_S = 10.0
_CONFIG_TIMEOUT_S = 60.0


def endpoint(provider: str | None) -> str:
    """The endpoint a ``--provider`` value selects; an unknown one is refused, never mapped."""
    selected = provider or DEFAULT_ENDPOINT
    if selected not in MODELS:
        raise ValueError(f"opencode_endpoint_unknown: --provider {provider!r}; use one of {', '.join(MODELS)}")
    return selected


def savant_token_cap(raw: str | None) -> int:
    if raw is None:
        return _SAVANT_DEFAULT_MAX_TOKENS
    if raw.isascii() and raw.isdecimal() and len(raw) <= len(str(_SAVANT_CONTEXT)):
        cap = int(raw)
        if 0 < cap < _SAVANT_CONTEXT:
            return cap
    raise ValueError("savant_token_cap_refused: --max-tokens must be an integer from 1 to 3999999")


def _savant_host_key_path() -> str:
    """Read the configured path from the local user manager, even from a sanitized worker."""
    name = f"{_SAVANT_KEY_FILE_ENV}="
    paths: list[str] = []
    try:
        result = subprocess.run(
            [
                "busctl", "--user", "--json=short", "--auto-start=no", "get-property",
                "org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                "Environment",
            ],
            env=dispatch_isolation.user_manager_env({"PATH": os.defpath}),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=dispatch_isolation.PROBE_TIMEOUT_S, check=False,
        )
        if result.returncode or len(result.stdout) > _SAVANT_MANAGER_ENV_MAX_BYTES:
            raise ValueError
        payload = json_depth.loads(result.stdout, max_depth=2)
        entries = payload.get("data") if isinstance(payload, dict) and payload.get("type") == "as" else None
        if not isinstance(entries, list):
            raise ValueError
        for entry in entries:
            if not isinstance(entry, str):
                raise ValueError
            if entry.startswith(name):
                paths.append(entry.removeprefix(name))
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        raise ValueError("savant_key_unavailable: could not read the systemd user manager environment") from None
    if len(paths) != 1:
        raise ValueError(f"savant_key_unavailable: expected one {_SAVANT_KEY_FILE_ENV} in the systemd user manager")
    return paths[0]


def _savant_api_key() -> str:
    """Read the operator's file. Keep the path and value out of diagnostics and task records."""
    override = os.environ.get(_SAVANT_KEY_FILE_ENV)
    path = Path(_savant_host_key_path() if override is None else override)
    if not path.is_absolute():
        raise ValueError(f"savant_key_unavailable: configure {_SAVANT_KEY_FILE_ENV} with an absolute path")
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            fd = safe_open_below(directory, path.name, os.O_RDONLY)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as stream:
            if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o600:
                raise ValueError
            raw = stream.read(4097)
        key = raw.removesuffix(b"\n").removesuffix(b"\r").decode("ascii")
        if len(raw) > 4096 or not key or any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError
    except (OSError, ValueError):
        raise ValueError("savant_key_unavailable: expected an owned mode-600 file with one raw key") from None
    return key


def savant_tools(data_class: str | None) -> bool:
    """Whether a Savant run of this admitted class gets the read-only file tools."""
    return data_class in _SAVANT_TOOL_CLASSES


def savant_read_paths(options: dict[str, str]) -> list[str]:
    """The ``--read-path`` values the task record carries, in order (none: the whole tree)."""
    raw = options.get("read_paths")
    paths = json_depth.loads(raw, max_depth=2) if raw else []
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError("savant_read_path_refused: the task record's read paths are malformed")
    return paths


def _savant_runtime_refusal(env: dict[str, str]) -> str | None:
    # OpenCode merges managed Linux config after OPENCODE_CONFIG_CONTENT. It can
    # change the destination, request options, permissions, or system prompt.
    try:
        Path("/etc/opencode").lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return "savant_runtime_refused: cannot verify that managed OpenCode configuration is absent"
    else:
        return "savant_runtime_refused: managed OpenCode configuration is present"
    try:
        result = subprocess.run(
            ["opencode", "--version"], env={"PATH": env.get("PATH", os.defpath)},
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return "savant_runtime_refused: OpenCode version probe failed"
    if result.returncode or result.stdout.strip() != _SAVANT_OPENCODE_VERSION:
        return f"savant_runtime_refused: OpenCode {_SAVANT_OPENCODE_VERSION} is required by the verified SubQ route"
    return None


def _savant_build(spec: TaskSpec, env: dict[str, str]) -> Invocation:
    from lu_machinery.dispatcher.checks import check_model_admission

    prompt = spec.prompt_file.read_bytes()
    check_model_admission(
        spec.agent, spec.model, spec.mode, {**spec.options, "provider": spec.provider, "effort": spec.effort}, prompt,
    )
    store = savant_request_store(spec.options.get("data_class"))
    effort = spec.effort
    if effort is None:
        raise ValueError("savant_effort_refused: the route requires a recorded effort")
    cap = savant_token_cap(spec.options.get("max_tokens"))
    if refusal := _savant_runtime_refusal(env):
        raise ValueError(refusal)
    key = _savant_api_key()
    if key.encode("ascii") in prompt:
        raise ValueError("savant_prompt_refused: prompt contains the configured SubQ credential")
    route = MODELS[SAVANT_PROVIDER]
    tools = savant_tools(spec.options.get("data_class"))
    if not tools and savant_read_paths(spec.options):
        raise ValueError("savant_read_path_refused: --read-path needs a public data class; private runs read no files")
    write = spec.mode == "workspace-write"
    if write and not tools:
        raise ValueError("savant_write_refused: a write run needs --data-class public-code or public-docs")
    config = {
        "model": route,
        "small_model": route,
        "enabled_providers": [SAVANT_PROVIDER],
        "autoupdate": False,
        "share": "disabled",
        "permission": {"*": "deny", **({"external_directory": "deny", **dict.fromkeys(_SAVANT_READ_TOOLS, "allow"),
                                        **({"edit": "allow"} if write else {})} if tools else {})},
        "provider": {SAVANT_PROVIDER: {
            "name": "SubQ",
            "npm": "@ai-sdk/openai-compatible",
            "env": ["SUBQ_API_KEY"],
            "options": {"baseURL": _SAVANT_API},
            "models": {SAVANT_MODEL: {
                "name": "SubQ Savant 1.4",
                "reasoning": True,
                "limit": {"context": _SAVANT_CONTEXT, "output": cap},
                "options": {"reasoningEffort": effort, **({} if store else {"store": False})},
            }},
        }},
    }
    child_env = {name: value for name, value in env.items() if name != "DEEPSEEK_API_KEY"}
    child_env.update(
        SUBQ_API_KEY=key,
        OPENCODE_CONFIG_CONTENT=json.dumps(config, separators=(",", ":")),
        OPENCODE_DISABLE_PROJECT_CONFIG="1",
        OPENCODE_DISABLE_CLAUDE_CODE="1",
        OPENCODE_DISABLE_DEFAULT_PLUGINS="1",
        OPENCODE_DISABLE_MODELS_FETCH="1",
        OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=str(cap),
        # /tmp is private to the read-only boundary. Isolate CLI auth, history and config from the host.
        XDG_CACHE_HOME="/tmp/savant/cache",
        XDG_CONFIG_HOME="/tmp/savant/config",
        XDG_DATA_HOME="/tmp/savant/data",
        XDG_STATE_HOME="/tmp/savant/state",
    )
    directory = "/tmp"  # private to the read-only boundary and empty
    if tools:
        from lu_machinery.dispatcher.write_boundary import SNAPSHOT_INSIDE

        directory = SNAPSHOT_INSIDE
    argv = ["opencode", "run", "--pure", "--dir", directory, "--title", "Savant trial", "--model", route,
            "--format", "json"]
    return Invocation(argv, prompt, child_env, None)


def _remaining_usd(selected: str, key: str) -> float | None:
    """Read sanitized prepaid USD; the legacy key argument is unused."""
    from lu_machinery.api.subscription_usage import _private_usage

    module = _private_usage()
    return None if module is None else module.prepaid_remaining_usd(selected, timeout_s=_PROBE_TIMEOUT_S)


def _config_conflict(resolved: object) -> str | None:
    """Why OpenCode's resolved config could reach another model or provider route, or None."""
    if not isinstance(resolved, dict):
        return "the resolved config could not be read"
    if resolved.get("enabled_providers") != ["openrouter"]:
        return "enabled_providers is overridden"
    if resolved.get("plugin"):
        return "a plugin is configured"
    if resolved.get("small_model") != MODELS["openrouter"]:
        return "small_model is overridden"
    if (resolved.get("provider") or {}).get("openrouter") != _OPENROUTER_PROVIDER:
        return "the openrouter provider block is overridden"
    for kind in ("agent", "mode"):
        entries = resolved.get(kind) or {}
        if not isinstance(entries, dict):
            return f"{kind} could not be read"
        for name, entry in entries.items():
            # Agent options are merged over the model's routing options on every call.
            if not isinstance(entry, dict) or entry.get("options"):
                return f"{kind} {name!r} sets request options"
            if name in _PINNED_AGENTS and entry.get("model", MODELS["openrouter"]) != MODELS["openrouter"]:
                return f"{kind} {name!r} sets another model"
    return None


def _resolved_config(env: dict[str, str]) -> object:
    """OpenCode's resolved config under the OpenRouter launch environment, or None. Never logged."""
    try:
        done = subprocess.run(
            ["opencode", "debug", "config"], env={**env, "OPENCODE_CONFIG_CONTENT": _OPENROUTER_CONFIG},
            cwd=env.get("HOME") or Path.home(), capture_output=True, text=True, timeout=_CONFIG_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return json_depth.loads(done.stdout, max_depth=512) if done.returncode == 0 else None
    except ValueError:
        return None


def _project_config(worktree: Path, repo: Path | None, sha: str | None) -> list[str] | None:
    """The checkout's OpenCode config paths (existing directory or start commit), or None when unreadable."""
    found = [name for name in _PROJECT_CONFIG if (worktree / name).exists()]
    if repo is not None and sha:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo), "ls-tree", "--name-only", sha, "--", *_PROJECT_CONFIG],
                capture_output=True, text=True, timeout=_CONFIG_TIMEOUT_S, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode:
            return None
        found += done.stdout.split()
    return sorted(set(found))


def endpoint_refusal(
    provider: str | None,
    env: dict[str, str] | None = None,
    *,
    worktree: Path | None = None,
    repo: Path | None = None,
    sha: str | None = None,
) -> str | None:
    """The typed refusal for the selected endpoint before spawn, or None when it may run.

    An endpoint with no configured access, an unreadable balance or a balance below its
    ``dispatch_floor_usd`` is refused; the other endpoint is never tried instead. The OpenRouter
    endpoint is also refused when the checkout carries OpenCode config or the resolved config
    could still route a call off the pinned model or provider allow-list.
    """
    selected = endpoint(provider)
    if selected == SAVANT_PROVIDER:
        if refusal := _savant_runtime_refusal(env or {}):
            return refusal
        try:
            _savant_api_key()
        except ValueError as exc:
            return str(exc)
        return None
    import yaml

    from lu_machinery.api import subscription_usage as usage

    module = usage._private_usage()
    if module is None:
        return f"opencode_endpoint_balance_unknown: {selected} balance could not be read"
    if not module.api_key_present(selected):
        return f"opencode_endpoint_no_access: {selected} has no configured API key"
    if selected == "openrouter":
        project = _project_config(worktree, repo, sha) if worktree is not None else []
        if project is None:
            return "opencode_endpoint_config_conflict: openrouter: the checkout's OpenCode config could not be read"
        if project:
            return f"opencode_endpoint_config_conflict: openrouter: the checkout carries OpenCode config {', '.join(project)}"
        if conflict := _config_conflict(_resolved_config(env or {})):
            return f"opencode_endpoint_config_conflict: openrouter: {conflict}"
    floor = float((yaml.safe_load(_BUDGETS.read_text(encoding="utf-8")) or {})[selected]["dispatch_floor_usd"])
    remaining = _remaining_usd(selected, "")
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or not math.isfinite(remaining):
        return f"opencode_endpoint_balance_unknown: {selected} balance could not be read"
    if remaining < floor:
        return f"opencode_endpoint_below_floor: {selected} balance ${remaining:.2f} is below the ${floor:.2f} floor"
    return None


def build(spec: TaskSpec, env: dict[str, str]) -> Invocation:
    selected = endpoint(spec.provider)
    if selected == SAVANT_PROVIDER:
        return _savant_build(spec, env)
    argv = ["opencode", "run", "--model", MODELS[selected], "--format", "json", "--variant", "high"]
    child_env = env.copy()
    if selected == "openrouter":
        child_env["OPENCODE_CONFIG_CONTENT"] = _OPENROUTER_CONFIG
    if spec.mode == "workspace-write":
        argv.append("--auto")
    elif spec.mode == "read-only":
        child_env["OPENCODE_PERMISSION"] = _READ_ONLY_PERMISSION
    else:
        raise ValueError(f"opencode: unsupported mode {spec.mode!r}")
    argv += ["--", spec.prompt_file.read_text(encoding="utf-8")]
    return Invocation(argv, None, child_env, None)


def parse(stdout: str, stderr: str, returncode: int, final_text: str | None) -> ParsedResult:
    text, chunks, usage, reason = None, [], None, None
    error = f"opencode exited with code {returncode}" if returncode else None
    for line in jsonl_lines(stdout):
        try:
            event = json_depth.loads(line, max_depth=512)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        part = event.get("part") or {}
        if not isinstance(part, dict):
            part = {}
        if kind == "error" or event.get("error") is not None:
            error = error or "opencode terminal error"
        if kind == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
        elif chunks and kind in ("tool", "tool_use", "step_start", "step_finish", "step-start", "step-finish"):
            text, chunks = "".join(chunks).strip(), []
        if kind in ("step_finish", "step-finish") or part.get("type") in ("step_finish", "step-finish"):
            reason = part.get("reason") or event.get("reason")
            if reason not in ("stop", "completed", "tool-calls"):
                error = error or "opencode terminal failure"
            if isinstance(part.get("tokens"), dict):
                usage = part["tokens"]
        state = part.get("state")
        rejected = ("rejected", "cancelled", "permission_denied", "permission_rejected")
        if kind in ("tool", "tool_use") and isinstance(state, dict) and state.get("status") in rejected:
            error = error or "opencode tool permission rejected"
    text = "".join(chunks).strip() if chunks else text
    if reason is not None and reason not in ("stop", "completed"):
        error = error or "opencode incomplete terminal outcome"
    return ParsedResult(text or None, usage, error or (None if text else "opencode missing final response"))
