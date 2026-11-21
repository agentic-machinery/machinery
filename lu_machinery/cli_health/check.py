"""Fleet agent CLI health check: installed vs latest version and sign-in state (#1078).

Reads the fleet CLI list from ``lu_machinery/config/stack_pins.yaml`` (or an
explicit YAML config passed via ``--config``), runs each CLI's own version
command, queries its official release source for the latest version, and
checks sign-in state only through the CLI's own status command or documented
status file.

Security boundaries:
- Never reads, stores, prints, or logs credentials, tokens, emails, account IDs,
  or raw subprocess/HTTP/file payloads.
- Rejects malformed config entries, shell execution, path-bearing command names,
  non-HTTPS URLs, and symlinked status files or output destinations.
- A failed version or latest lookup sets ``check_ok`` to ``0`` and leaves
  ``behind`` as ``unknown`` (omitted from the Prometheus ``behind`` gauge),
  never ``0``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from prometheus_client import CollectorRegistry, Gauge, generate_latest

from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import machinery_path

_logger = logging.getLogger(__name__)

SCHEMA_VERSION = "cli-health.v1"
DEFAULT_TIMEOUT_S = 15.0
MAX_TIMEOUT_S = 120.0
_MAX_CONFIG_BYTES = 256 * 1024
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_HTTP_BYTES = 1_000_000
_MAX_STATUS_FILE_BYTES = 64 * 1024
_USER_AGENT = "lu-machinery-cli-health"

_CLI_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_NPM_PACKAGE_RE = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-z0-9._-]+$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_ENV_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASS", "CREDENTIAL", "AUTH", "COOKIE", "PRIVATE")
_VERSION_TOKEN_RE = re.compile(r"^v?([0-9]+(?:\.[0-9]+){1,3})(?:-([A-Za-z0-9][A-Za-z0-9._-]{0,31}))?$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_CREDENTIAL_COUNT_RE = re.compile(r"(?<!\d)(\d+)\s+credentials?\b", re.IGNORECASE)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_HTTP_CHUNK_BYTES = 16 * 1024

# Minimal non-secret base environment variables needed for CLI lookup, XDG/keyring/CA/proxy support.
# Per-CLI config-dir variables come from config `env_passthrough` rather than code constants.
_BASE_SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)

SignedInValue = Literal[0, 1, "unknown"]
BehindValue = Literal[0, 1, "unknown"]


class CliHealthConfigError(ValueError):
    """Configuration or output path validation failed. Messages stay free of secrets."""


@dataclass(frozen=True)
class LatestSource:
    kind: Literal["npm", "github", "url_json", "url_regex", "command", "none"]
    target: str = ""
    field: str = "version"
    argv: tuple[str, ...] = ()
    pattern: str | None = None


@dataclass(frozen=True)
class AuthSource:
    kind: Literal["command", "status_file", "none"]
    argv: tuple[str, ...] = ()
    parser: Literal["json_bool", "text_match", "credential_count", "none"] = "none"
    field: str = ""
    signed_in_pattern: str = ""
    signed_out_pattern: str = ""
    allow_nonzero_false: bool = False
    allow_nonzero_for_signed_out: bool = False
    path: str = ""
    missing_means: Literal["unknown", "signed_out"] = "unknown"


@dataclass(frozen=True)
class CliSpec:
    cli: str
    version_argv: tuple[str, ...]
    latest: LatestSource
    auth: AuthSource
    env_passthrough: tuple[str, ...] = ()


@dataclass(frozen=True)
class CliHealthResult:
    cli: str
    installed: str
    latest: str
    behind: BehindValue
    signed_in: SignedInValue
    check_ok: Literal[0, 1]
    version_reason: str
    latest_reason: str
    auth_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cli": self.cli,
            "installed": self.installed,
            "latest": self.latest,
            "behind": self.behind,
            "signed_in": self.signed_in,
            "check_ok": self.check_ok,
            "version_reason": self.version_reason,
            "latest_reason": self.latest_reason,
            "auth_reason": self.auth_reason,
        }


@dataclass(frozen=True)
class _CommandOutput:
    returncode: int
    text: str


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow HTTPS redirects only so upstream checks cannot be redirected to file:// or http://."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("non-https redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(request: urllib.request.Request, *, timeout: float) -> Any:
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
    return opener.open(request, timeout=timeout)


def _validate_argv(raw: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise CliHealthConfigError(f"{label} must be a non-empty list of strings")
    parts: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item or any(ch in item for ch in ("\x00", "\r", "\n")):
            raise CliHealthConfigError(f"{label} contains an invalid argument")
        parts.append(item)
    cmd = parts[0]
    if cmd in (".", "..") or Path(cmd).name != cmd or "/" in cmd or "\\" in cmd:
        raise CliHealthConfigError(f"{label} executable must be a bare command name")
    return tuple(parts)


def _validate_https_url(raw: object, *, label: str) -> str:
    if not isinstance(raw, str) or not raw or any(ch in raw for ch in ("\x00", "\r", "\n")):
        raise CliHealthConfigError(f"{label} must be an HTTPS URL")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise CliHealthConfigError(f"{label} must be a plain HTTPS URL without credentials")
    return raw


def _validate_regex(raw: object, *, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise CliHealthConfigError(f"{label} must be a non-empty regex string")
    try:
        re.compile(raw)
    except re.error as exc:
        raise CliHealthConfigError(f"{label} is not a valid regex") from exc
    return raw


def _parse_latest(raw: object, cli: str) -> LatestSource:
    if not isinstance(raw, Mapping):
        raise CliHealthConfigError(f"cli {cli!r} latest section must be a mapping")
    kind = raw.get("kind")
    if kind == "npm":
        pkg = raw.get("package")
        if not isinstance(pkg, str) or not _NPM_PACKAGE_RE.fullmatch(pkg):
            raise CliHealthConfigError(f"cli {cli!r} has an invalid npm package name")
        return LatestSource(kind="npm", target=pkg)
    if kind == "github":
        repo = raw.get("repo")
        if not isinstance(repo, str) or not _GITHUB_REPO_RE.fullmatch(repo):
            raise CliHealthConfigError(f"cli {cli!r} has an invalid github repo slug")
        return LatestSource(kind="github", target=repo)
    if kind == "url_json":
        url = _validate_https_url(raw.get("url"), label=f"cli {cli!r} latest.url")
        field = raw.get("field", "version")
        if not isinstance(field, str) or not _FIELD_NAME_RE.fullmatch(field):
            raise CliHealthConfigError(f"cli {cli!r} latest.field must be a valid key name")
        return LatestSource(kind="url_json", target=url, field=field)
    if kind == "url_regex":
        url = _validate_https_url(raw.get("url"), label=f"cli {cli!r} latest.url")
        pattern = _validate_regex(raw.get("pattern"), label=f"cli {cli!r} latest.pattern")
        return LatestSource(kind="url_regex", target=url, pattern=pattern)
    if kind == "command":
        argv = _validate_argv(raw.get("argv"), label=f"cli {cli!r} latest.argv")
        cmd_pattern: str | None = None
        if raw.get("pattern") is not None:
            cmd_pattern = _validate_regex(raw.get("pattern"), label=f"cli {cli!r} latest.pattern")
        return LatestSource(kind="command", argv=argv, pattern=cmd_pattern)
    if kind == "none":
        return LatestSource(kind="none")
    raise CliHealthConfigError(f"cli {cli!r} has unsupported latest.kind")


def _parse_auth(raw: object, cli: str) -> AuthSource:
    if raw is None:
        return AuthSource(kind="none")
    if not isinstance(raw, Mapping):
        raise CliHealthConfigError(f"cli {cli!r} auth section must be a mapping")
    kind = raw.get("kind")
    if kind == "none":
        return AuthSource(kind="none")
    if kind == "command":
        argv = _validate_argv(raw.get("argv"), label=f"cli {cli!r} auth.argv")
        parser = raw.get("parser")
        if parser == "json_bool":
            field = raw.get("field")
            if not isinstance(field, str) or not _FIELD_NAME_RE.fullmatch(field):
                raise CliHealthConfigError(f"cli {cli!r} auth.field must be a valid key name")
            allow_nz = raw.get("allow_nonzero_false", False)
            if not isinstance(allow_nz, bool):
                raise CliHealthConfigError(f"cli {cli!r} auth.allow_nonzero_false must be a boolean")
            return AuthSource(
                kind="command",
                argv=argv,
                parser="json_bool",
                field=field,
                allow_nonzero_false=allow_nz,
            )
        if parser == "text_match":
            in_pat = _validate_regex(raw.get("signed_in_pattern"), label=f"cli {cli!r} auth.signed_in_pattern")
            out_pat = _validate_regex(raw.get("signed_out_pattern"), label=f"cli {cli!r} auth.signed_out_pattern")
            allow_nz_out = raw.get("allow_nonzero_for_signed_out", False)
            if not isinstance(allow_nz_out, bool):
                raise CliHealthConfigError(f"cli {cli!r} auth.allow_nonzero_for_signed_out must be a boolean")
            return AuthSource(
                kind="command",
                argv=argv,
                parser="text_match",
                signed_in_pattern=in_pat,
                signed_out_pattern=out_pat,
                allow_nonzero_for_signed_out=allow_nz_out,
            )
        if parser == "credential_count":
            return AuthSource(kind="command", argv=argv, parser="credential_count")
        raise CliHealthConfigError(f"cli {cli!r} has unsupported auth.parser")
    if kind == "status_file":
        raw_path = raw.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip() or any(ch in raw_path for ch in ("\x00", "\r", "\n")):
            raise CliHealthConfigError(f"cli {cli!r} auth.path must be a non-empty path string")
        cleaned_path = raw_path.strip()
        if ".." in Path(cleaned_path).parts:
            raise CliHealthConfigError(f"cli {cli!r} auth.path must not contain '..' components")
        field = raw.get("field")
        if not isinstance(field, str) or not _FIELD_NAME_RE.fullmatch(field):
            raise CliHealthConfigError(f"cli {cli!r} auth.field must be a valid key name")
        missing_means = raw.get("missing_means", "unknown")
        if missing_means not in ("unknown", "signed_out"):
            raise CliHealthConfigError(f"cli {cli!r} auth.missing_means must be 'unknown' or 'signed_out'")
        return AuthSource(
            kind="status_file",
            field=field,
            path=cleaned_path,
            missing_means=missing_means,
        )
    raise CliHealthConfigError(f"cli {cli!r} has unsupported auth.kind")


def _validate_env_passthrough(raw: object, *, cli: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CliHealthConfigError(f"cli {cli!r} env_passthrough must be a list of variable names")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not _ENV_VAR_NAME_RE.fullmatch(item):
            raise CliHealthConfigError(f"cli {cli!r} env_passthrough contains an invalid variable name")
        if any(token in item for token in _SECRET_ENV_SUBSTRINGS):
            raise CliHealthConfigError(f"cli {cli!r} env_passthrough refuses credential-shaped variable {item!r}")
        names.append(item)
    return tuple(dict.fromkeys(names))


def _parse_cli_entry(entry: Mapping[str, Any], *, fallback_name: str | None = None) -> CliSpec:
    raw_name = entry.get("cli", fallback_name)
    if not isinstance(raw_name, str) or not _CLI_NAME_RE.fullmatch(raw_name.strip()):
        raise CliHealthConfigError("cli name is missing or invalid")
    cli = raw_name.strip()
    version_argv = _validate_argv(entry.get("version_argv"), label=f"cli {cli!r} version_argv")
    latest = _parse_latest(entry.get("latest"), cli)
    auth = _parse_auth(entry.get("auth"), cli)
    env_passthrough = _validate_env_passthrough(entry.get("env_passthrough"), cli=cli)
    return CliSpec(cli=cli, version_argv=version_argv, latest=latest, auth=auth, env_passthrough=env_passthrough)


def default_config_path() -> Path:
    """Bundled ``lu_machinery/config/stack_pins.yaml``."""
    return machinery_path("config/stack_pins.yaml")


def load_cli_specs(path: Path | None = None) -> tuple[CliSpec, ...]:
    """Load and validate fleet CLI health entries from YAML config.

    Supports ``pins:`` entries carrying a ``cli_health:`` block (the default
    ``stack_pins.yaml`` format) or a standalone ``clis:`` list.
    """
    config_path = default_config_path() if path is None else path
    try:
        with config_path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise CliHealthConfigError("cli health config path must be a regular file")
            raw_bytes = handle.read(_MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise CliHealthConfigError("cli health config file could not be read") from exc
    if len(raw_bytes) > _MAX_CONFIG_BYTES:
        raise CliHealthConfigError("cli health config file exceeds size limit")
    try:
        data = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CliHealthConfigError("cli health config file is not valid UTF-8 YAML") from exc
    if not isinstance(data, Mapping):
        raise CliHealthConfigError("cli health config root must be a mapping")

    specs: list[CliSpec] = []
    seen: set[str] = set()
    if "clis" in data:
        rows = data["clis"]
        if not isinstance(rows, list):
            raise CliHealthConfigError("clis section must be a list")
        for row in rows:
            if not isinstance(row, Mapping):
                raise CliHealthConfigError("each clis entry must be a mapping")
            spec = _parse_cli_entry(row)
            if spec.cli in seen:
                raise CliHealthConfigError(f"duplicate cli entry: {spec.cli}")
            seen.add(spec.cli)
            specs.append(spec)
    elif "pins" in data:
        rows = data["pins"]
        if not isinstance(rows, list):
            raise CliHealthConfigError("pins section must be a list")
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            health = row.get("cli_health")
            if health is None:
                continue
            if not isinstance(health, Mapping):
                raise CliHealthConfigError("cli_health section must be a mapping")
            fallback = row.get("name") if isinstance(row.get("name"), str) else None
            spec = _parse_cli_entry(health, fallback_name=fallback)
            if spec.cli in seen:
                raise CliHealthConfigError(f"duplicate cli entry: {spec.cli}")
            seen.add(spec.cli)
            specs.append(spec)
    else:
        raise CliHealthConfigError("config file must contain a 'pins' or 'clis' list")

    if not specs:
        raise CliHealthConfigError("config file defines no cli_health entries")
    return tuple(specs)


def _normalize_version(raw: str) -> str:
    candidate = raw.strip().lstrip("([").rstrip(":,);].")
    if not candidate or any(mark in candidate for mark in ("://", "/", "@", "\\", "=")):
        return "unknown"
    match = _VERSION_TOKEN_RE.fullmatch(candidate)
    if match is None:
        return "unknown"
    base, suffix = match.group(1), match.group(2)
    if suffix is None and _IPV4_RE.fullmatch(base):
        return "unknown"
    return f"{base}-{suffix}" if suffix is not None else base


def extract_version(text: str, *, pattern: str | None = None) -> str:
    """Extract the first validated version string, or ``unknown``."""
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    if pattern is not None:
        match = re.search(pattern, cleaned, re.MULTILINE)
        if match is None:
            return "unknown"
        group = match.group(1) if match.lastindex else match.group(0)
        return _normalize_version(group)
    for token in cleaned.split():
        normalized = _normalize_version(token)
        if normalized != "unknown":
            return normalized
    return "unknown"


def _parse_version_key(version: str) -> tuple[tuple[int, ...], str | None] | None:
    match = _VERSION_TOKEN_RE.fullmatch(version)
    if match is None:
        return None
    base, suffix = match.group(1), match.group(2)
    if suffix is None and _IPV4_RE.fullmatch(base):
        return None
    return tuple(int(part) for part in base.split(".")), suffix


def compare_behind(installed: str, latest: str, *, allow_newer_installed: bool = True) -> BehindValue:
    """Return ``1`` if installed < latest, ``0`` if current/newer, ``unknown`` if incomparable.

    When ``allow_newer_installed`` is ``False`` (used for ``command``-kind latest
    sources), an installed version strictly newer than the reported latest fails
    closed as ``unknown`` so a lagging local source is never treated as current.
    """
    inst = _parse_version_key(installed)
    lat = _parse_version_key(latest)
    if inst is None or lat is None:
        return "unknown"
    if installed == latest:
        return 0
    inst_nums, inst_suffix = inst
    lat_nums, lat_suffix = lat
    width = max(len(inst_nums), len(lat_nums))
    padded_inst = inst_nums + (0,) * (width - len(inst_nums))
    padded_lat = lat_nums + (0,) * (width - len(lat_nums))
    if padded_inst < padded_lat:
        return 1
    if padded_inst > padded_lat:
        return 0 if allow_newer_installed else "unknown"
    if inst_suffix == lat_suffix:
        return 0
    if inst_suffix is not None and lat_suffix is None:
        return 1
    if inst_suffix is None and lat_suffix is not None:
        return 0 if allow_newer_installed else "unknown"
    # Same numeric base (e.g. same-day cursor-agent build) with a different release hash.
    return 1


def _sanitized_child_env(
    env: Mapping[str, str] | None,
    *,
    extra_keys: Sequence[str] = (),
) -> dict[str, str]:
    source = os.environ if env is None else env
    allowed = (*_BASE_SAFE_ENV_KEYS, *extra_keys)
    clean = {key: source[key] for key in allowed if source.get(key)}
    clean["NO_COLOR"] = "1"
    return clean


def _run_bounded_command(
    argv: tuple[str, ...],
    *,
    env: Mapping[str, str] | None,
    extra_env_keys: Sequence[str] = (),
    timeout: float,
    combine_stderr: bool,
) -> tuple[_CommandOutput | None, str]:
    """Run one validated bare command with bounded output, timeout, and process-group cleanup."""
    child_env = _sanitized_child_env(env, extra_keys=extra_env_keys)
    resolved = shutil.which(argv[0], path=child_env.get("PATH", ""))
    if resolved is None:
        return None, "not_found"
    full_argv = [resolved, *argv[1:]]
    deadline = time.monotonic() + timeout
    try:
        # Validated bare executable resolved on sanitized PATH; shell=False; minimal env.
        proc = subprocess.Popen(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            full_argv,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if combine_stderr else subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        return None, "spawn_failed"

    output = bytearray()
    failure_reason = ""
    returncode: int | None = None
    try:
        stdout = proc.stdout
        if stdout is None:
            failure_reason = "pipe_unavailable"
        else:
            with selectors.DefaultSelector() as selector:
                selector.register(stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        failure_reason = "timeout"
                        break
                    chunk = os.read(stdout.fileno(), _MAX_OUTPUT_BYTES + 1 - len(output))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > _MAX_OUTPUT_BYTES:
                        failure_reason = "output_limit"
                        break
            if not failure_reason:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure_reason = "timeout"
                else:
                    try:
                        returncode = proc.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        failure_reason = "timeout"
    except OSError:
        failure_reason = "read_failed"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()

    if failure_reason or returncode is None:
        return None, failure_reason or "command_failed"
    return _CommandOutput(returncode=returncode, text=output.decode("utf-8", "replace")), "ok"


def _fetch_http_bytes(
    url: str,
    *,
    accept: str,
    timeout: float,
    opener: Callable[..., Any],
) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    deadline = time.monotonic() + timeout
    body = bytearray()
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        with opener(request, timeout=remaining) as response:
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            read_chunk = getattr(response, "read1", response.read)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if sock is not None and hasattr(sock, "settimeout"):
                    with contextlib.suppress(OSError):
                        sock.settimeout(remaining)
                to_read = min(_HTTP_CHUNK_BYTES, _MAX_HTTP_BYTES + 1 - len(body))
                chunk = read_chunk(to_read)
                if not isinstance(chunk, bytes):
                    return None
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > _MAX_HTTP_BYTES or time.monotonic() > deadline:
                    return None
    except Exception:
        return None
    return bytes(body)


def _probe_installed(
    spec: CliSpec,
    *,
    env: Mapping[str, str] | None,
    timeout: float,
) -> tuple[str, str]:
    result, reason = _run_bounded_command(
        spec.version_argv,
        env=env,
        extra_env_keys=spec.env_passthrough,
        timeout=timeout,
        combine_stderr=True,
    )
    if result is None:
        return ("absent" if reason == "not_found" else "unknown"), reason
    if result.returncode != 0:
        return "unknown", "command_failed"
    version = extract_version(result.text)
    if version == "unknown":
        return "unknown", "parse_failed"
    return version, "ok"


def _probe_latest(
    spec: CliSpec,
    *,
    env: Mapping[str, str] | None,
    timeout: float,
    opener: Callable[..., Any],
) -> tuple[str, str]:
    src = spec.latest
    if src.kind == "none":
        return "unknown", "no_upstream"
    if src.kind == "command":
        result, reason = _run_bounded_command(
            src.argv,
            env=env,
            extra_env_keys=spec.env_passthrough,
            timeout=timeout,
            combine_stderr=False,
        )
        if result is None:
            return "unknown", reason
        if result.returncode != 0:
            return "unknown", "command_failed"
        version = extract_version(result.text, pattern=src.pattern)
        return (version, "ok") if version != "unknown" else ("unknown", "parse_failed")
    if src.kind == "npm":
        encoded = urllib.parse.quote(src.target, safe="@")
        raw = _fetch_http_bytes(
            f"https://registry.npmjs.org/{encoded}/latest",
            accept="application/json",
            timeout=timeout,
            opener=opener,
        )
        if raw is None:
            return "unknown", "lookup_failed"
        try:
            payload = json_depth.loads(raw.decode("utf-8"), max_depth=64)
        except (UnicodeDecodeError, ValueError):
            return "unknown", "parse_failed"
        if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
            return "unknown", "parse_failed"
        version = _normalize_version(payload["version"])
        return (version, "ok") if version != "unknown" else ("unknown", "parse_failed")
    if src.kind == "github":
        raw = _fetch_http_bytes(
            f"https://api.github.com/repos/{src.target}/releases/latest",
            accept="application/vnd.github+json",
            timeout=timeout,
            opener=opener,
        )
        if raw is None:
            return "unknown", "lookup_failed"
        try:
            payload = json_depth.loads(raw.decode("utf-8"), max_depth=64)
        except (UnicodeDecodeError, ValueError):
            return "unknown", "parse_failed"
        if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
            return "unknown", "parse_failed"
        tag = payload.get("tag_name")
        if not isinstance(tag, str):
            return "unknown", "parse_failed"
        version = _normalize_version(tag)
        return (version, "ok") if version != "unknown" else ("unknown", "parse_failed")
    if src.kind == "url_json":
        raw = _fetch_http_bytes(
            src.target,
            accept="application/json",
            timeout=timeout,
            opener=opener,
        )
        if raw is None:
            return "unknown", "lookup_failed"
        try:
            payload = json_depth.loads(raw.decode("utf-8"), max_depth=64)
        except (UnicodeDecodeError, ValueError):
            return "unknown", "parse_failed"
        if not isinstance(payload, dict) or not isinstance(payload.get(src.field), str):
            return "unknown", "parse_failed"
        version = _normalize_version(payload[src.field])
        return (version, "ok") if version != "unknown" else ("unknown", "parse_failed")
    if src.kind == "url_regex":
        raw = _fetch_http_bytes(
            src.target,
            accept="text/plain, */*",
            timeout=timeout,
            opener=opener,
        )
        if raw is None:
            return "unknown", "lookup_failed"
        version = extract_version(raw.decode("utf-8", "replace"), pattern=src.pattern)
        return (version, "ok") if version != "unknown" else ("unknown", "parse_failed")
    return "unknown", "unsupported_source"


def _expand_status_file_path(raw: str, env: Mapping[str, str] | None) -> Path | None:
    source = os.environ if env is None else env
    home = source.get("HOME", "")
    text = raw
    if text == "~" or text.startswith("~/"):
        if not home:
            return None
        text = home + text[1:]
    xdg_defaults = {
        "HOME": home,
        "XDG_CONFIG_HOME": source.get("XDG_CONFIG_HOME") or (f"{home}/.config" if home else ""),
        "XDG_STATE_HOME": source.get("XDG_STATE_HOME") or (f"{home}/.local/state" if home else ""),
        "XDG_DATA_HOME": source.get("XDG_DATA_HOME") or (f"{home}/.local/share" if home else ""),
    }
    for var, replacement in xdg_defaults.items():
        token = f"${var}"
        braced = f"${{{var}}}"
        if token in text or braced in text:
            if not replacement:
                return None
            text = text.replace(braced, replacement).replace(token, replacement)
    candidate = Path(text)
    return candidate if candidate.is_absolute() else None


def _probe_status_file(auth: AuthSource, *, env: Mapping[str, str] | None) -> tuple[SignedInValue, str]:
    path = _expand_status_file_path(auth.path, env)
    if path is None:
        return "unknown", "status_file_unresolvable"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return (0, "status_file_missing") if auth.missing_means == "signed_out" else ("unknown", "status_file_missing")
    except OSError:
        return "unknown", "status_file_unreadable"

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "unknown", "status_file_not_regular"
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(_MAX_STATUS_FILE_BYTES + 1)
    except OSError:
        return "unknown", "status_file_unreadable"
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)

    if len(raw) > _MAX_STATUS_FILE_BYTES:
        return "unknown", "status_file_too_large"
    try:
        payload = json_depth.loads(raw.decode("utf-8"), max_depth=64)
    except (UnicodeDecodeError, ValueError):
        return "unknown", "status_file_invalid"
    # Extract ONLY the allowlisted boolean status field; discard every other field immediately.
    if not isinstance(payload, dict):
        return "unknown", "status_file_invalid"
    flag = payload.get(auth.field)
    if not isinstance(flag, bool):
        return "unknown", "status_file_invalid"
    return (1 if flag else 0), "ok"


def _probe_auth(
    spec: CliSpec,
    *,
    env: Mapping[str, str] | None,
    timeout: float,
) -> tuple[SignedInValue, str]:
    auth = spec.auth
    if auth.kind == "none":
        return "unknown", "no_auth_interface"
    if auth.kind == "status_file":
        return _probe_status_file(auth, env=env)
    if auth.kind == "command":
        combine_stderr = auth.parser != "json_bool"
        result, reason = _run_bounded_command(
            auth.argv,
            env=env,
            extra_env_keys=spec.env_passthrough,
            timeout=timeout,
            combine_stderr=combine_stderr,
        )
        if result is None:
            return "unknown", reason
        if auth.parser == "json_bool":
            try:
                payload = json_depth.loads(result.text, max_depth=64)
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                return "unknown", ("command_failed" if result.returncode != 0 else "parse_failed")
            flag = payload.get(auth.field)
            if not isinstance(flag, bool):
                return "unknown", ("command_failed" if result.returncode != 0 else "parse_failed")
            if result.returncode == 0:
                return (1 if flag else 0), "ok"
            if auth.allow_nonzero_false and flag is False:
                return 0, "ok"
            return "unknown", "command_failed"
        if auth.parser == "text_match":
            cleaned = _ANSI_ESCAPE_RE.sub("", result.text)
            matched_out = bool(re.search(auth.signed_out_pattern, cleaned, re.MULTILINE))
            matched_in = bool(re.search(auth.signed_in_pattern, cleaned, re.MULTILINE))
            if matched_out and matched_in:
                return "unknown", "ambiguous_status"
            if matched_out:
                if result.returncode == 0 or auth.allow_nonzero_for_signed_out:
                    return 0, "ok"
                return "unknown", "command_failed"
            if matched_in and result.returncode == 0:
                return 1, "ok"
            return "unknown", ("command_failed" if result.returncode != 0 else "parse_failed")
        if auth.parser == "credential_count":
            if result.returncode != 0:
                return "unknown", "command_failed"
            cleaned = _ANSI_ESCAPE_RE.sub("", result.text)
            counts = {int(match.group(1)) for match in _CREDENTIAL_COUNT_RE.finditer(cleaned)}
            if len(counts) != 1:
                return "unknown", "parse_failed"
            count = next(iter(counts))
            return (1 if count >= 1 else 0), "ok"
    return "unknown", "unsupported_auth"


def _validate_timeout(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_S:
        raise CliHealthConfigError(f"timeout must be finite, > 0, and <= {MAX_TIMEOUT_S}")
    return timeout


def check_cli(
    spec: CliSpec,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    opener: Callable[..., Any] = _default_opener,
) -> CliHealthResult:
    """Check one fleet CLI's installed version, latest version, and sign-in state."""
    bounded_timeout = _validate_timeout(timeout)
    installed, version_reason = _probe_installed(spec, env=env, timeout=bounded_timeout)
    latest, latest_reason = _probe_latest(spec, env=env, timeout=bounded_timeout, opener=opener)
    signed_in, auth_reason = _probe_auth(spec, env=env, timeout=bounded_timeout)
    behind = compare_behind(installed, latest, allow_newer_installed=spec.latest.kind != "command")
    if behind == "unknown" and version_reason == "ok" and latest_reason == "ok":
        latest_reason = "stale_source"
    auth_ok = spec.auth.kind == "none" or signed_in in (0, 1)
    check_ok: Literal[0, 1] = 1 if (behind in (0, 1) and auth_ok) else 0
    _logger.debug(
        "cli_health cli=%s behind=%s signed_in=%s check_ok=%d reasons=%s/%s/%s",
        spec.cli,
        behind,
        signed_in,
        check_ok,
        version_reason,
        latest_reason,
        auth_reason,
    )
    return CliHealthResult(
        cli=spec.cli,
        installed=installed,
        latest=latest,
        behind=behind,
        signed_in=signed_in,
        check_ok=check_ok,
        version_reason=version_reason,
        latest_reason=latest_reason,
        auth_reason=auth_reason,
    )


def check_all(
    specs: Sequence[CliSpec],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    opener: Callable[..., Any] = _default_opener,
) -> dict[str, Any]:
    """Run all configured CLI health checks concurrently and return the JSON report payload."""
    bounded_timeout = _validate_timeout(timeout)
    if not specs:
        raise CliHealthConfigError("at least one CLI spec is required")
    workers = min(len(specs), 7)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: check_cli(s, env=env, timeout=bounded_timeout, opener=opener), specs))
    checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": SCHEMA_VERSION,
        "checked_at": checked_at,
        "clis": [item.to_dict() for item in results],
    }


def render_prometheus(report: Mapping[str, Any], *, now_timestamp: float | None = None) -> str:
    """Render a ``cli-health.v1`` report as Prometheus textfile exposition lines.

    ``lu_cli_health_behind`` is emitted only when both installed and latest
    versions resolved (``0`` or ``1``), so a version-check failure never
    produces ``behind 0``. ``lu_cli_health_signed_in`` is emitted only when
    sign-in state is known (``1`` or ``0``); ``lu_cli_health_info`` carries
    ``signed_in="unknown"`` when unknown.
    """
    registry = CollectorRegistry()
    info_gauge = Gauge(
        "lu_cli_health_info",
        "Fleet agent CLI version and sign-in metadata (value is always 1).",
        ["cli", "installed", "latest", "signed_in"],
        registry=registry,
    )
    behind_gauge = Gauge(
        "lu_cli_health_behind",
        "1 when installed CLI version is behind latest released version, 0 when current; omitted when unknown.",
        ["cli"],
        registry=registry,
    )
    signed_in_gauge = Gauge(
        "lu_cli_health_signed_in",
        "1 when CLI reports signed in, 0 when signed out; omitted when unknown.",
        ["cli"],
        registry=registry,
    )
    check_ok_gauge = Gauge(
        "lu_cli_health_check_ok",
        "1 when all configured version, upstream and sign-in probes succeeded, 0 otherwise.",
        ["cli"],
        registry=registry,
    )
    ts_gauge = Gauge(
        "lu_cli_health_last_run_timestamp_seconds",
        "Unix epoch timestamp of the latest CLI health check run.",
        registry=registry,
    )
    ts_value = time.time() if now_timestamp is None else now_timestamp
    ts_gauge.set(int(ts_value))

    for row in report.get("clis", ()):
        cli = str(row["cli"])
        installed = str(row["installed"])
        latest = str(row["latest"])
        signed_in = row["signed_in"]
        signed_in_label = str(signed_in)
        info_gauge.labels(cli=cli, installed=installed, latest=latest, signed_in=signed_in_label).set(1)
        check_ok_gauge.labels(cli=cli).set(1 if row["check_ok"] == 1 else 0)
        if row["behind"] in (0, 1):
            behind_gauge.labels(cli=cli).set(int(row["behind"]))
        if signed_in in (0, 1):
            signed_in_gauge.labels(cli=cli).set(int(signed_in))

    return generate_latest(registry).decode("utf-8")


def write_atomic_text(destination: Path, text: str, *, mode: int = 0o600) -> None:
    """Atomically write ``text`` to ``destination``, refusing symlinks, non-regular files, or extra hard links.

    Temporary files are always created owner-only (``0o600``) with ``O_EXCL | O_NOFOLLOW``,
    then set to ``mode & 0o644`` via ``fchmod`` only after the full payload is written.
    """
    raw_dest = os.fspath(destination)
    if not raw_dest or any(ch in raw_dest for ch in ("\x00", "\r", "\n")):
        raise CliHealthConfigError("output destination path is invalid")
    if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode & ~0o644:
        raise CliHealthConfigError("output mode must stay within 0o644")
    try:
        info = destination.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise CliHealthConfigError("output destination could not be inspected") from exc
    if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
        raise CliHealthConfigError("output destination must be a regular unlinked file")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_path = parent / f".{destination.name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fchmod(stream.fileno(), mode & 0o644)
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lu_machinery.cli_health",
        description=(
            "Report installed vs latest version and sign-in state for every configured fleet agent CLI.\n"
            "Use for host monitoring and manual health audits; do not use it to upgrade or sign in CLIs."
        ),
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.cli_health check\n"
            "  .venv/bin/python -m lu_machinery.cli_health check "
            "--prom-out <textfile-dir>/lu_cli_health.prom --quiet\n"
            "  .venv/bin/python -m lu_machinery.cli_health check --cli cursor-agent --cli claude\n"
            "\n"
            "Outputs:\n"
            "  stdout: cli-health.v1 JSON report unless --quiet is passed.\n"
            "  --json-out: atomic write of the same JSON document.\n"
            "  --prom-out: atomic Prometheus textfile (lu_cli_health_info, lu_cli_health_behind,\n"
            "              lu_cli_health_signed_in, lu_cli_health_check_ok,\n"
            "              lu_cli_health_last_run_timestamp_seconds).\n"
            "\n"
            "Exit codes:\n"
            "  0 when checks completed and all requested outputs were written.\n"
            "  1 when config is invalid, an unknown --cli was requested, or an output write failed.\n"
            "  2 on invalid CLI arguments.\n"
            "\n"
            "Related:\n"
            "  Config: lu_machinery/config/stack_pins.yaml\n"
            "  Runbook: docs/runbooks/cli-health.md\n"
            "  Issue: machinery #1078"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser(
        "check",
        help="Check installed vs latest version and sign-in state for configured fleet CLIs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML config file defining fleet CLI entries (default: bundled lu_machinery/config/stack_pins.yaml).",
    )
    check_parser.add_argument(
        "--cli",
        action="append",
        default=[],
        metavar="NAME",
        help="Check only the named CLI from config; repeatable (default: all configured CLIs; e.g. --cli cursor-agent).",
    )
    check_parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Path to write the cli-health.v1 JSON report atomically (default: none; e.g. /tmp/cli-health.json).",
    )
    check_parser.add_argument(
        "--prom-out",
        type=Path,
        default=None,
        help="Path to write the Prometheus textfile atomically (default: none; e.g. /tmp/lu_cli_health.prom).",
    )
    check_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per-probe timeout in seconds, >0 and <={int(MAX_TIMEOUT_S)} (default: {DEFAULT_TIMEOUT_S}).",
    )
    check_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress JSON output on stdout; useful with --prom-out or --json-out (default: false).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        timeout = _validate_timeout(args.timeout)
        specs = load_cli_specs(args.config)
        if args.cli:
            by_name = {spec.cli: spec for spec in specs}
            missing = [name for name in args.cli if name not in by_name]
            if missing:
                raise CliHealthConfigError(f"unknown --cli requested: {', '.join(sorted(set(missing)))}")
            specs = tuple(by_name[name] for name in dict.fromkeys(args.cli))
        report = check_all(specs, timeout=timeout)
        json_text = json.dumps(report, indent=2) + "\n"
        if args.json_out is not None:
            write_atomic_text(args.json_out, json_text, mode=0o600)
        if args.prom_out is not None:
            write_atomic_text(args.prom_out, render_prometheus(report), mode=0o644)
    except CliHealthConfigError as exc:
        print(f"cli_health failed: {exc}", file=sys.stderr, flush=True)
        return 1
    except OSError:
        print("cli_health failed: output write error", file=sys.stderr, flush=True)
        return 1
    if not args.quiet:
        print(json_text, end="", flush=True)
    return 0
