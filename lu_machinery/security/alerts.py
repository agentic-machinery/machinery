"""GitHub security-alert watcher and routed fix-brief generator.

Reads the fleet repository registry (``fleet_repos.yaml``), queries the three
GitHub security alert APIs strictly through ``lu_machinery.common.github_client``,
emits JSON and Prometheus textfile metrics, and renders a fix brief + dispatch
command routed through the model catalog without ever dispatching.

The same read also records fleet-security enrollment (machinery#1081): whether
the default branch calls the pinned reusable workflow, whether that call's CI
evidence could be read, and any OSS findings the workflow annotations expose.
"""

from __future__ import annotations

import base64
import math
import os
import re
import shlex
import tempfile
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import yaml
from prometheus_client import CollectorRegistry, Gauge, generate_latest, write_to_textfile

from lu_machinery.common import github_client
from lu_machinery.orchestration.fleet_repos import (
    FleetRepo,
    FleetRepoError,
    canonical_repo_key,
    load_fleet_repos,
)
from lu_machinery.review.model_catalog import ModelCatalogError, load_model_catalog, validate_catalog
from lu_machinery.review.role_resolution import RoleCandidate, resolve_role

TOOLS: tuple[str, ...] = ("code_scanning", "dependabot", "secret_scanning")
STANDARD_SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
SEVERITIES: tuple[str, ...] = (*STANDARD_SEVERITIES, "unknown")

DEFAULT_SECURITY_ROLE = "code_review_critical"
_CODING_ROLES = frozenset({"agentic_coding", "implementation"})

METRIC_OPEN_ALERTS = "lu_security_alerts_open"
METRIC_FEATURE_ENABLED = "lu_security_alerts_feature_enabled"
METRIC_SOURCE_OK = "lu_security_alerts_source_ok"
METRIC_SCAN_TIMESTAMP = "lu_security_alerts_scan_timestamp_seconds"
# New series (machinery#1081). Existing metric names stay stable.
METRIC_FLEET_SECURITY_STATE = "lu_security_alerts_fleet_security_state"
METRIC_FLEET_SECURITY_FINDINGS = "lu_security_alerts_fleet_security_findings"

# A default-branch workflow is enrolled only when it calls this reusable workflow at a full SHA.
_FLEET_SECURITY_WORKFLOW = "learn-ukrainian/machinery/.github/workflows/fleet-security.yml"
FLEET_SECURITY_STATES: tuple[str, ...] = (
    "missing_enrollment",
    "inaccessible_evidence",
    "zero_findings",
    "findings",
)
_FLEET_GATE_JOB = "Fleet security gate"
_FLEET_JOB_NAMES: tuple[str, ...] = (
    "actionlint",
    "ShellCheck",
    "zizmor",
    "Secret scan (TruffleHog)",
    "Trivy dependencies and licences",
    "Trivy IaC and misconfiguration",
    "Trivy container image",
    _FLEET_GATE_JOB,
)
_MAX_WORKFLOW_FILES = 100
_MAX_WORKFLOW_BYTES = 1_048_576
_MAX_OSS_FINDINGS = 500

_SCAN_SCHEMA_VERSION = "security-alerts-scan.v1"
_BRIEF_SCHEMA_VERSION = "security-alerts-brief.v1"
_TRIAGE_RUNBOOK_PATH = "docs/runbooks/secret-scanning.md"
_MAX_PAGES = 100
_DEFAULT_TIMEOUT = 30.0
_MAX_MESSAGE_CHARS = 240

_SLUG_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_ENDPOINT_SUFFIXES: dict[str, str] = {
    "code_scanning": "code-scanning/alerts?state=open&per_page=100",
    "dependabot": "dependabot/alerts?state=open&per_page=100",
    "secret_scanning": "secret-scanning/alerts?state=open&per_page=100&hide_secret=true",
}
_NUMERIC_REPO_PATH_RE = re.compile(r"^repositories/([0-9]+)/(.+)$")

# Explicit GitHub REST phrases that positively identify a disabled security feature.
# Generic 403/404 responses (permissions, rate limits, missing repository) stay API errors.
_SHARED_DISABLED_PHRASES: tuple[str, ...] = (
    "advanced security must be enabled",
    "advanced security is not enabled",
    "code security must be enabled",
    "code security is not enabled",
)
_TOOL_DISABLED_PHRASES: dict[str, tuple[str, ...]] = {
    "code_scanning": (
        "code scanning is not enabled",
        "code scanning is disabled",
        "no analysis found",
    ),
    "dependabot": (
        "dependabot alerts are disabled",
        "dependabot alerts are not enabled",
        "vulnerability alerts are disabled",
        "vulnerability alerts are not enabled",
    ),
    "secret_scanning": (
        "secret scanning is disabled",
        "secret scanning is not enabled",
    ),
}

_FLEET_USES_PREFIX = _FLEET_SECURITY_WORKFLOW + "@"
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_ALLOWED_RUN_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})
_WORKFLOW_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml\Z")
_YAML_NAME_RE = re.compile(r"\.ya?ml\Z", re.IGNORECASE)
_BRANCH_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,250}\Z")
_REF_RE = re.compile(r"[A-Za-z0-9._/-]{1,128}\Z")
_CHECK_RUN_URL_RE = re.compile(r"^https://api\.github\.com/repos/([^/]+)/([^/]+)/check-runs/([0-9]+)/?\Z")

_SEVERITY_MAP: dict[str, str] = {
    "critical": "critical",
    "high": "high",
    "error": "high",
    "medium": "medium",
    "moderate": "medium",
    "warning": "medium",
    "low": "low",
    "note": "low",
}


class SecurityAlertsError(RuntimeError):
    """Fail-closed error for security-alert scanning, routing, or output."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class WorkerSelection:
    role: str
    seat: str
    route_name: str
    model_id: str
    wire_id: str
    family: str
    route: str
    transport: str
    effort: str
    argv: tuple[str, ...]
    catalog_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "seat": self.seat,
            "route_name": self.route_name,
            "model_id": self.model_id,
            "wire_id": self.wire_id,
            "family": self.family,
            "route": self.route,
            "transport": self.transport,
            "effort": self.effort,
            "argv": list(self.argv),
            "catalog_digest": self.catalog_digest,
        }


@dataclass(frozen=True)
class BriefResult:
    repo_key: str
    github: str
    generated_at: str
    worker: WorkerSelection
    alerts: Mapping[str, Any]
    fleet_security: Mapping[str, Any]
    owned_paths: tuple[str, ...]
    dispatch_argv: tuple[str, ...]
    dispatch_command: str
    brief_markdown: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _BRIEF_SCHEMA_VERSION,
            "repo": self.repo_key,
            "github": self.github,
            "generated_at": self.generated_at,
            "worker": self.worker.to_dict(),
            "alerts": dict(self.alerts),
            "fleet_security": dict(self.fleet_security),
            "owned_paths": list(self.owned_paths),
            "dispatch_argv": list(self.dispatch_argv),
            "dispatch_command": self.dispatch_command,
            "brief_markdown": self.brief_markdown,
        }


def _validate_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise SecurityAlertsError("timeout must be a positive finite number", exit_code=2)
    return float(timeout)


def validate_output_path(path: Path | str, *, label: str) -> Path:
    raw = str(path)
    if not raw or not raw.strip():
        raise SecurityAlertsError(f"{label} must be a non-empty path", exit_code=2)
    if "\x00" in raw or any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in raw):
        raise SecurityAlertsError(f"{label} contains unsafe control or format characters", exit_code=2)
    dest = Path(raw)
    if dest.is_symlink():
        raise SecurityAlertsError(f"{label} refuses symlink destination", exit_code=2)
    if dest.exists() and not dest.is_file():
        raise SecurityAlertsError(f"{label} must be a regular file path", exit_code=2)
    if dest.parent.exists() and (dest.parent.is_symlink() or not dest.parent.is_dir()):
        raise SecurityAlertsError(f"{label} parent must be a real directory", exit_code=2)
    return dest


def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise SecurityAlertsError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}", exit_code=2)
    return task_id


def _validate_prompt_file_arg(prompt_file: str) -> str:
    if not isinstance(prompt_file, str) or not prompt_file.strip():
        raise SecurityAlertsError("prompt_file must be a non-empty string", exit_code=2)
    if "\x00" in prompt_file or any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in prompt_file):
        raise SecurityAlertsError("prompt_file contains unsafe control or format characters", exit_code=2)
    return f"./{prompt_file}" if prompt_file.startswith("-") else prompt_file


def _sanitize_text(value: Any, *, max_chars: int = _MAX_MESSAGE_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _ANSI_RE.sub("", value)
    cleaned = "".join(
        " " if ch in "\r\n\t" else ch
        for ch in cleaned
        if unicodedata.category(ch) not in {"Cc", "Cf", "Zl", "Zp"}
    )
    collapsed = " ".join(cleaned.split())
    if len(collapsed) > max_chars:
        return collapsed[: max_chars - 3].rstrip() + "..."
    return collapsed


def _sanitize_md_cell(value: Any, *, max_chars: int = _MAX_MESSAGE_CHARS) -> str:
    return _sanitize_text(value, max_chars=max_chars).replace("|", "\\|").replace("`", "'")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_owned_path(raw: Any) -> str | None:
    """Validate one alert-derived repository-relative path for ``--owned-path``."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if (
        not text
        or text in {"?"}
        or text.startswith(("/", "\\", "-"))
        or "\\" in text
        or "\x00" in text
        or "`" in text
        or "|" in text
    ):
        return None
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in text):
        return None
    parts = text.split("/")
    if any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts):
        return None
    return "/".join(parts)


def _repo_endpoint(repo: FleetRepo, tool: str) -> str:
    parts = repo.github.split("/")
    if len(parts) != 2 or not all(_SLUG_SEGMENT_RE.fullmatch(p) and p not in {".", ".."} for p in parts):
        raise SecurityAlertsError(f"invalid GitHub repository slug for {repo.key!r}")
    owner, name = (quote(p, safe="") for p in parts)
    return f"repos/{owner}/{name}/{_ENDPOINT_SUFFIXES[tool]}"


def _is_feature_disabled(tool: str, status: int, error: str | None, value: Any) -> bool:
    if error == "github_rate_limited" or status not in {403, 404}:
        return False
    if not isinstance(value, Mapping):
        return False
    message = value.get("message")
    if not isinstance(message, str) or not message.strip():
        return False
    lowered = message.strip().lower()
    if "rate limit" in lowered:
        return False
    phrases = (*_SHARED_DISABLED_PHRASES, *_TOOL_DISABLED_PHRASES[tool])
    return any(phrase in lowered for phrase in phrases)


def _error_code(result: github_client.Result) -> str:
    if result.error == "github_rate_limited":
        return f"github_rate_limited:{result.reason}" if result.reason else "github_rate_limited"
    if result.error == "github_http_error":
        return f"github_http_{result.status}"
    if result.error:
        return str(result.error)
    return f"github_http_{result.status}"


def _normalize_severity(tool: str, alert: Mapping[str, Any]) -> str:
    if tool == "code_scanning":
        rule = alert.get("rule")
        if isinstance(rule, Mapping):
            for field in ("security_severity_level", "severity"):
                raw = rule.get(field)
                if isinstance(raw, str):
                    mapped = _SEVERITY_MAP.get(raw.strip().lower())
                    if mapped is not None:
                        return mapped
        return "unknown"
    if tool == "dependabot":
        for container_key in ("security_vulnerability", "security_advisory"):
            container = alert.get(container_key)
            if isinstance(container, Mapping):
                raw = container.get("severity")
                if isinstance(raw, str):
                    mapped = _SEVERITY_MAP.get(raw.strip().lower())
                    if mapped is not None:
                        return mapped
        return "unknown"
    if tool == "secret_scanning":
        raw = alert.get("severity")
        if isinstance(raw, str):
            mapped = _SEVERITY_MAP.get(raw.strip().lower())
            if mapped is not None:
                return mapped
        validity = alert.get("validity")
        if isinstance(validity, str) and validity.strip().lower() == "active":
            return "critical"
        return "high"
    return "unknown"


def _fetch_tool_alerts(
    client: Any,
    repo: FleetRepo,
    tool: str,
    *,
    timeout: float,
) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    """Fetch all open alerts for one ``(repo, tool)`` via ``github_client``.

    Returns ``(status, alerts, error_code)`` where ``status`` is one of
    ``"ok"``, ``"not_enabled"``, or ``"error"``.
    """
    try:
        initial_endpoint = _repo_endpoint(repo, tool)
    except SecurityAlertsError:
        return "error", None, "invalid_repository_slug"
    expected_path = initial_endpoint.split("?", 1)[0]
    expected_tool_suffix = _ENDPOINT_SUFFIXES[tool].split("?", 1)[0]
    endpoint: str | None = initial_endpoint
    known_repo_id: str | None = None
    repo_id_loaded = False

    collected: list[dict[str, Any]] = []
    seen_endpoints: set[str] = set()
    for _ in range(_MAX_PAGES):
        if endpoint is None or endpoint in seen_endpoints:
            return "error", None, "github_pagination_loop"
        seen_endpoints.add(endpoint)
        try:
            result = client.request("GET", endpoint, timeout=timeout, fresh=True)
        except github_client.GitHubRateLimited as exc:
            reason = f":{exc.reason}" if exc.reason else ""
            return "error", None, f"github_rate_limited{reason}"
        except Exception as exc:
            return "error", None, f"github_client_error:{type(exc).__name__}"

        if not isinstance(result, github_client.Result):
            return "error", None, "github_invalid_result"
        if result.error or result.status not in {200, 304}:
            if _is_feature_disabled(tool, result.status, result.error, result.value):
                return "not_enabled", None, None
            return "error", None, _error_code(result)
        if not isinstance(result.value, list) or any(not isinstance(item, dict) for item in result.value):
            return "error", None, "github_invalid_payload"
        collected.extend(result.value)
        headers = {k.lower(): v for k, v in (result.headers or {}).items()}
        next_url = github_client.next_link(headers)
        if not next_url:
            return "ok", collected, None
        split = _split_next_link(next_url)
        if split is None:
            return "error", None, "github_invalid_next_link"
        next_path, next_query = split
        if next_path != expected_path:
            numeric_match = _NUMERIC_REPO_PATH_RE.fullmatch(next_path)
            if not numeric_match or numeric_match.group(2) != expected_tool_suffix:
                return "error", None, "github_invalid_next_link"
            if not repo_id_loaded:
                known_repo_id = _load_repo_numeric_id(client, repo, timeout)
                repo_id_loaded = True
            if known_repo_id is None or numeric_match.group(1) != known_repo_id:
                return "error", None, "github_invalid_next_link"
        if tool == "secret_scanning":
            query = parse_qs(next_query, keep_blank_values=True)
            if query.get("hide_secret") != ["true"]:
                return "error", None, "github_invalid_next_link"
        endpoint = f"{next_path}?{next_query}" if next_query else next_path
    return "error", None, "github_pagination_limit"


def _summarize_alerts(tool: str, alerts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {severity: 0 for severity in SEVERITIES}
    for alert in alerts:
        counts[_normalize_severity(tool, alert)] += 1
    return {
        "status": "ok",
        "total": len(alerts),
        "by_severity": counts,
    }


def _fleet_record(
    state: str,
    *,
    reason: str | None = None,
    callers: Sequence[Mapping[str, str]] | None = None,
    rejected_pins: Sequence[Mapping[str, str]] | None = None,
    runs: Sequence[Mapping[str, Any]] | None = None,
    findings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "callers": [dict(item) for item in callers or ()],
        "rejected_pins": [dict(item) for item in rejected_pins or ()],
        "runs": [dict(item) for item in runs or ()],
        "findings": [dict(item) for item in findings or ()],
    }


def _github_get(client: Any, endpoint: str, timeout: float) -> github_client.Result:
    try:
        result = client.request("GET", endpoint, timeout=timeout, fresh=True)
    except github_client.GitHubRateLimited as exc:
        return github_client.Result(status=429, error="github_rate_limited", reason=exc.reason)
    except Exception as exc:
        return github_client.Result(status=0, error=f"github_client_error:{type(exc).__name__}")
    if not isinstance(result, github_client.Result):
        return github_client.Result(status=0, error="github_invalid_result")
    return result


def _split_github_slug(repo: FleetRepo) -> tuple[str, str] | None:
    parts = repo.github.split("/")
    if len(parts) != 2 or not all(_SLUG_SEGMENT_RE.fullmatch(part) and part not in {".", ".."} for part in parts):
        return None
    return parts[0], parts[1]


def _repo_numeric_id(document: Mapping[str, Any]) -> str | None:
    repo_id = document.get("id")
    if type(repo_id) is not int or repo_id <= 0:
        return None
    return str(repo_id)


def _load_repo_numeric_id(client: Any, repo: FleetRepo, timeout: float) -> str | None:
    slug = _split_github_slug(repo)
    if slug is None:
        return None
    owner, name = slug
    described = _github_get(client, _repo_api(owner, name, "").rstrip("/"), timeout)
    if described.error or described.status not in {200, 304} or not isinstance(described.value, Mapping):
        return None
    return _repo_numeric_id(described.value)


def _safe_branch(value: Any) -> str | None:
    if not isinstance(value, str) or not _BRANCH_RE.fullmatch(value):
        return None
    if value.startswith("/") or value.endswith("/") or "//" in value or ".." in value.split("/"):
        return None
    return value


def _repo_api(owner: str, name: str, suffix: str) -> str:
    return f"repos/{quote(owner, safe='')}/{quote(name, safe='')}/{suffix}"


def _split_next_link(next_url: str) -> tuple[str, str] | None:
    """Return ``(path, query)`` for a GitHub API link, or None when it must not be followed."""
    if not isinstance(next_url, str) or not next_url or any(ch in next_url for ch in "\\\n\r\t "):
        return None
    try:
        parsed = urlsplit(next_url)
    except ValueError:
        return None
    if parsed.fragment or parsed.scheme != "https" or parsed.netloc != "api.github.com":
        return None
    path = parsed.path.lstrip("/")
    if path.startswith("api/v3/"):
        path = path.removeprefix("api/v3/")
    if not path or ".." in path.split("/"):
        return None
    return path, parsed.query


def _accept_next_link(
    owner: str,
    name: str,
    next_url: str,
    expected_path: str,
    required_repo_id: str | None,
) -> str | None:
    """Return the next endpoint, or None when the link is not this repository's page."""
    split = _split_next_link(next_url)
    if split is None:
        return None
    path, query = split
    suffix = expected_path.removeprefix(f"repos/{owner}/{name}/")
    numeric = _NUMERIC_REPO_PATH_RE.fullmatch(path)
    if numeric:
        if numeric.group(2) != suffix or required_repo_id is None or numeric.group(1) != required_repo_id:
            return None
    elif path != expected_path:
        return None
    return f"{path}?{query}" if query else path


def _collect_pages(
    client: Any,
    endpoint: str,
    *,
    timeout: float,
    owner: str,
    name: str,
    expected_path: str,
    required_repo_id: str | None,
) -> tuple[list[github_client.Result], str | None]:
    pages: list[github_client.Result] = []
    seen: set[str] = set()
    current: str | None = endpoint
    for _ in range(_MAX_PAGES):
        if current is None or current in seen:
            return [], "github_pagination_loop"
        seen.add(current)
        result = _github_get(client, current, timeout)
        if result.error or result.status not in {200, 304}:
            return [], _error_code(result)
        pages.append(result)
        headers = {key.lower(): value for key, value in (result.headers or {}).items()}
        next_url = github_client.next_link(headers)
        if not next_url:
            return pages, None
        current = _accept_next_link(owner, name, next_url, expected_path, required_repo_id)
        if current is None:
            return [], "github_invalid_next_link"
    return [], "github_pagination_limit"


def _oss_severity(raw: str) -> str:
    lowered = raw.strip().lower()
    if lowered in {"info", "style"}:
        return "low"
    return _SEVERITY_MAP.get(lowered, "unknown")


class _UnreadableAnnotation(Exception):
    """An annotation looked like a finding but its location could not be read."""


def _split_licence_target(body: str) -> tuple[str, bool]:
    """Return the target and whether it is unambiguous.

    A package and a path can both contain `` in ``. One separator is the
    workflow's package/target split. More than one is shown in full and is
    not an owned path.
    """
    package, separator, target = body.partition(" in ")
    if not separator or not package or not target:
        raise _UnreadableAnnotation
    return target, " in " not in target


def _line_number(raw: str) -> int:
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 9:
        raise _UnreadableAnnotation
    number = int(raw)
    if number < 1:
        raise _UnreadableAnnotation
    return number


def _finding(
    *,
    scanner: str,
    rule: str,
    severity: str,
    raw_path: str,
    line: int | None,
    message: str,
    repository_file: bool,
) -> dict[str, Any]:
    safe_path = _safe_owned_path(raw_path) if repository_file else None
    location = _sanitize_text(raw_path) or "unknown"
    if line is not None:
        location = f"{location}:{line}"
    return {
        "scanner": scanner,
        "rule": _sanitize_text(rule) or "unknown",
        "severity": severity if severity in SEVERITIES else "unknown",
        "path": safe_path,
        "line": line,
        "location": location,
        "message": _sanitize_text(message),
    }


def _parse_oss_finding(message: Any) -> dict[str, Any] | None:
    """Parse one redacted fleet-security annotation. Unrecognized text is not a finding."""
    if not isinstance(message, str) or not message.strip():
        return None
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in message):
        return None
    text = message.strip()
    matched = re.fullmatch(r"actionlint \[([^\]]+)\] at (.+):([^:\s]+):([^:\s]+)", text)
    if matched:
        _line_number(matched.group(4))
        return _finding(
            scanner="actionlint",
            rule=matched.group(1),
            severity="unknown",
            raw_path=matched.group(2),
            line=_line_number(matched.group(3)),
            message=text,
            repository_file=True,
        )
    matched = re.fullmatch(r"ShellCheck (SC\S+) \(([^)]+)\) at (.+):([^:\s]+):([^:\s]+)", text)
    if matched:
        _line_number(matched.group(5))
        return _finding(
            scanner="shellcheck",
            rule=matched.group(1),
            severity=_oss_severity(matched.group(2)),
            raw_path=matched.group(3),
            line=_line_number(matched.group(4)),
            message=text,
            repository_file=True,
        )
    matched = re.fullmatch(r"zizmor (\S+) severity=(\S+) confidence=\S+ at (.+)", text)
    if matched:
        return _finding(
            scanner="zizmor",
            rule=matched.group(1),
            severity=_oss_severity(matched.group(2)),
            raw_path=matched.group(3),
            line=None,
            message=text,
            repository_file=True,
        )
    matched = re.fullmatch(r"TruffleHog (\S+) verified=(\S+) (.+):([^:\s]+) commit=\S+", text)
    if matched:
        verified = matched.group(2).strip().lower()
        severity = "critical" if verified == "true" else "high" if verified == "false" else "unknown"
        line_raw = matched.group(4)
        return _finding(
            scanner="trufflehog",
            rule=matched.group(1),
            severity=severity,
            raw_path=matched.group(3),
            line=None if line_raw == "?" else _line_number(line_raw),
            message=text,
            repository_file=True,
        )
    matched = re.fullmatch(
        r"Trivy vuln (\S+) \(([^)]+)\) (\S+) (\S*) \(fixed: [^)]*\) in (.+)",
        text,
    )
    if matched:
        return _finding(
            scanner="trivy-fs",
            rule=matched.group(1),
            severity=_oss_severity(matched.group(2)),
            raw_path=matched.group(5),
            line=None,
            message=text,
            repository_file=True,
        )
    matched = re.fullmatch(r"Trivy licence (.+?) \(([^/\s]+)/([^)]+)\) (.+)", text)
    if matched:
        target, own_path = _split_licence_target(matched.group(4))
        return _finding(
            scanner="trivy-fs",
            rule=matched.group(1),
            severity=_oss_severity(matched.group(3)),
            raw_path=target,
            line=None,
            message=text,
            repository_file=own_path,
        )
    matched = re.fullmatch(r"Trivy misconfig (\S+) \(([^)]+)\) in (.+)", text)
    if matched:
        return _finding(
            scanner="trivy-fs",
            rule=matched.group(1),
            severity=_oss_severity(matched.group(2)),
            raw_path=matched.group(3),
            line=None,
            message=text,
            repository_file=True,
        )
    matched = re.fullmatch(r"Trivy IaC (\S+) \(([^)]+)\) at (.+):([^:\s]+)", text)
    if matched:
        return _finding(
            scanner="trivy-iac",
            rule=matched.group(1),
            severity=_oss_severity(matched.group(2)),
            raw_path=matched.group(3),
            line=_line_number(matched.group(4)),
            message=text,
            repository_file=True,
        )
    matched = re.fullmatch(
        r"Trivy image vuln (\S+) \(([^)]+)\) (\S+) (\S*) \(fixed: [^)]*\) in (.+)",
        text,
    )
    if matched:
        return _finding(
            scanner="trivy-image",
            rule=matched.group(1),
            severity=_oss_severity(matched.group(2)),
            raw_path=matched.group(5),
            line=None,
            message=text,
            repository_file=False,
        )
    matched = re.fullmatch(r"Trivy image licence (.+?) \(([^/\s]+)/([^)]+)\) (.+)", text)
    if matched:
        target, _own_path = _split_licence_target(matched.group(4))
        return _finding(
            scanner="trivy-image",
            rule=matched.group(1),
            severity=_oss_severity(matched.group(3)),
            raw_path=target,
            line=None,
            message=text,
            repository_file=False,
        )
    return None


def _is_fleet_job(name: str, job_name: str) -> bool:
    return name == job_name or name.endswith(f" / {job_name}")


def _decode_workflow(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    if document.get("type") != "file" or document.get("encoding") != "base64":
        return None, "workflow_file_unreadable"
    size = document.get("size")
    if type(size) is not int or size < 0:
        return None, "workflow_file_unreadable"
    if size > _MAX_WORKFLOW_BYTES:
        return None, "pagination_limit"
    content = document.get("content")
    if not isinstance(content, str) or len(content) > _MAX_WORKFLOW_BYTES * 2:
        return None, "workflow_file_unreadable"
    try:
        raw = base64.b64decode("".join(content.split()), validate=True)
    except (ValueError, TypeError):
        return None, "workflow_file_unreadable"
    if len(raw) != size:
        return None, "workflow_file_unreadable"
    if len(raw) > _MAX_WORKFLOW_BYTES:
        return None, "pagination_limit"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "workflow_file_unreadable"


def _read_workflow_callers(
    client: Any,
    owner: str,
    name: str,
    branch: str,
    timeout: float,
) -> tuple[list[dict[str, str]], list[dict[str, str]], str | None]:
    """Return ``(callers, rejected_pins, error)``. A 404 directory is an empty caller list."""
    endpoint = _repo_api(owner, name, f"contents/.github/workflows?ref={quote(branch, safe='')}")
    listed = _github_get(client, endpoint, timeout)
    # A 404 from the contents API carries github_http_error. The repository was already
    # readable, so a missing workflows directory is missing enrollment, not an API failure.
    if listed.status == 404 and listed.error != "github_rate_limited":
        return [], [], None
    if listed.error or listed.status not in {200, 304} or not isinstance(listed.value, list):
        reason = _error_code(listed) if listed.error or listed.status not in {200, 304} else "github_invalid_payload"
        return [], [], reason
    files: list[str] = []
    seen: set[str] = set()
    for item in listed.value:
        if not isinstance(item, Mapping):
            return [], [], "github_invalid_payload"
        filename = item.get("name")
        path = item.get("path")
        kind = item.get("type")
        if not isinstance(filename, str) or not isinstance(path, str) or not isinstance(kind, str):
            return [], [], "github_invalid_payload"
        if not _YAML_NAME_RE.search(filename):
            continue
        expected = f".github/workflows/{filename}"
        if filename in seen or not _WORKFLOW_NAME_RE.fullmatch(filename) or kind != "file" or path != expected:
            return [], [], "workflow_file_unreadable"
        seen.add(filename)
        files.append(filename)
    if len(files) > _MAX_WORKFLOW_FILES:
        return [], [], "pagination_limit"

    callers: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    total_decoded = 0
    for filename in files:
        file_endpoint = _repo_api(
            owner,
            name,
            f"contents/.github/workflows/{quote(filename, safe='')}?ref={quote(branch, safe='')}",
        )
        document = _github_get(client, file_endpoint, timeout)
        if document.error or document.status not in {200, 304} or not isinstance(document.value, Mapping):
            return [], [], "workflow_file_unreadable"
        text, decode_error = _decode_workflow(document.value)
        if text is None:
            return [], [], decode_error or "workflow_file_unreadable"
        total_decoded += len(text.encode("utf-8"))
        if total_decoded > _MAX_WORKFLOW_BYTES:
            return [], [], "pagination_limit"
        workflow_path = f".github/workflows/{filename}"
        file_callers, file_rejected, pin_error = _pins_in_workflow(text, workflow_path)
        if pin_error:
            return [], [], pin_error
        for row in file_callers:
            if row not in callers:
                callers.append(row)
        for row in file_rejected:
            if row not in rejected:
                rejected.append(row)
    return callers, rejected, None


def _pins_in_workflow(
    text: str, workflow_path: str
) -> tuple[list[dict[str, str]], list[dict[str, str]], str | None]:
    """Read job-level ``uses`` values. A line inside a script is not a caller."""
    try:
        document = yaml.safe_load(text)
    except Exception:
        return [], [], "workflow_file_unreadable"
    if document is None:
        return [], [], None
    if not isinstance(document, Mapping):
        return [], [], "workflow_file_unreadable"
    jobs = document.get("jobs")
    if jobs is None:
        return [], [], None
    if not isinstance(jobs, Mapping):
        return [], [], "workflow_file_unreadable"
    callers: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    for job in jobs.values():
        if not isinstance(job, Mapping):
            return [], [], "workflow_file_unreadable"
        uses = job.get("uses")
        if uses is None:
            continue
        if not isinstance(uses, str):
            return [], [], "workflow_file_unreadable"
        if not uses.startswith(_FLEET_USES_PREFIX):
            continue
        ref = uses[len(_FLEET_USES_PREFIX) :]
        if _FULL_SHA_RE.fullmatch(ref):
            callers.append({"path": workflow_path, "pin": ref})
        else:
            shown = ref if _REF_RE.fullmatch(ref) else "unparsed-ref"
            rejected.append({"path": workflow_path, "ref": shown})
    return callers, rejected, None


def _workflow_run_path_matches(path: Any, filename: str) -> bool:
    expected = f".github/workflows/{filename}"
    if not isinstance(path, str):
        return False
    if path == expected:
        return True
    prefix = expected + "@"
    if not path.startswith(prefix):
        return False
    return _REF_RE.fullmatch(path[len(prefix) :]) is not None


def _run_record(
    owner: str, name: str, filename: str, run: Mapping[str, Any], branch: str
) -> tuple[dict[str, Any] | None, str | None]:
    run_id = run.get("id")
    if type(run_id) is not int or run_id <= 0:
        return None, "github_invalid_payload"
    if not _workflow_run_path_matches(run.get("path"), filename) or run.get("head_branch") != branch:
        return None, "github_invalid_payload"
    if run.get("status") != "completed":
        return None, "github_invalid_payload"
    event = run.get("event")
    head_repository = run.get("head_repository")
    full_name = head_repository.get("full_name") if isinstance(head_repository, Mapping) else None
    if not isinstance(event, str) or not isinstance(full_name, str):
        return None, "github_invalid_payload"
    if event not in _ALLOWED_RUN_EVENTS or full_name != f"{owner}/{name}":
        return None, "run_not_on_default_branch"
    head_sha = run.get("head_sha")
    safe_sha = head_sha if isinstance(head_sha, str) and _FULL_SHA_RE.fullmatch(head_sha) else None
    html_url = run.get("html_url")
    expected_url = f"https://github.com/{owner}/{name}/actions/runs/{run_id}"
    return {
        "path": f".github/workflows/{filename}",
        "run_id": run_id,
        "head_sha": safe_sha,
        "conclusion": _sanitize_text(run.get("conclusion")) or None,
        "html_url": html_url if html_url == expected_url else None,
    }, None


def _annotation_endpoint(owner: str, name: str, check_run_url: Any) -> str | None:
    if not isinstance(check_run_url, str):
        return None
    matched = _CHECK_RUN_URL_RE.fullmatch(check_run_url)
    if not matched or matched.group(1) != owner or matched.group(2) != name:
        return None
    return _repo_api(owner, name, f"check-runs/{matched.group(3)}/annotations?per_page=100")


def _read_caller_evidence(
    client: Any,
    owner: str,
    name: str,
    branch: str,
    caller: Mapping[str, str],
    timeout: float,
    required_repo_id: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    filename = caller["path"].rsplit("/", 1)[-1]
    runs_endpoint = _repo_api(
        owner,
        name,
        "actions/workflows/"
        f"{quote(filename, safe='')}/runs?branch={quote(branch, safe='')}&status=completed&per_page=1",
    )
    runs_result = _github_get(client, runs_endpoint, timeout)
    if runs_result.error or runs_result.status not in {200, 304} or not isinstance(runs_result.value, Mapping):
        reason = _error_code(runs_result) if runs_result.error or runs_result.status not in {200, 304} else "github_invalid_payload"
        return None, [], reason
    total = runs_result.value.get("total_count")
    workflow_runs = runs_result.value.get("workflow_runs")
    if type(total) is not int or total < 0 or not isinstance(workflow_runs, list):
        return None, [], "github_invalid_payload"
    if total == 0:
        if workflow_runs:
            return None, [], "github_invalid_payload"
        return None, [], "no_completed_run"
    if not workflow_runs or not isinstance(workflow_runs[0], Mapping):
        return None, [], "github_invalid_payload"
    record, run_error = _run_record(owner, name, filename, workflow_runs[0], branch)
    if run_error or record is None:
        return None, [], run_error or "github_invalid_payload"
    run_id = record["run_id"]
    jobs_path = _repo_api(owner, name, f"actions/runs/{run_id}/jobs")
    pages, page_error = _collect_pages(
        client,
        f"{jobs_path}?per_page=100",
        timeout=timeout,
        owner=owner,
        name=name,
        expected_path=jobs_path,
        required_repo_id=required_repo_id,
    )
    if page_error:
        return None, [], page_error
    jobs: list[Mapping[str, Any]] = []
    declared_total: int | None = None
    for page in pages:
        body = page.value
        if not isinstance(body, Mapping) or not isinstance(body.get("jobs"), list):
            return None, [], "github_invalid_payload"
        page_total = body.get("total_count")
        if type(page_total) is not int or page_total < 0:
            return None, [], "github_invalid_payload"
        if declared_total is None:
            declared_total = page_total
        elif declared_total != page_total:
            return None, [], "github_invalid_payload"
        for job in body["jobs"]:
            if not isinstance(job, Mapping):
                return None, [], "github_invalid_payload"
            jobs.append(job)
    if declared_total is None or len(jobs) != declared_total:
        return None, [], "github_invalid_payload"
    seen_job_ids: set[int] = set()
    for job in jobs:
        job_id = job.get("id")
        if type(job_id) is not int or job_id in seen_job_ids:
            return None, [], "github_invalid_payload"
        seen_job_ids.add(job_id)
    gates = [job for job in jobs if isinstance(job.get("name"), str) and _is_fleet_job(job["name"], _FLEET_GATE_JOB)]
    if len(gates) != 1:
        return record, [], "no_fleet_security_jobs"
    gate_conclusion = gates[0].get("conclusion")
    if not isinstance(gate_conclusion, str):
        return None, [], "github_invalid_payload"
    record = {**record, "gate_conclusion": gate_conclusion}
    findings: list[dict[str, Any]] = []
    for job in jobs:
        job_name = job.get("name")
        if not isinstance(job_name, str) or not any(_is_fleet_job(job_name, expected) for expected in _FLEET_JOB_NAMES):
            continue
        # A skipped scanner did not run. The gate conclusion is what says whether that skip was expected.
        if job.get("conclusion") == "skipped":
            continue
        annotations_endpoint = _annotation_endpoint(owner, name, job.get("check_run_url"))
        if annotations_endpoint is None:
            return None, [], "annotations_unreadable"
        expected_annotations = annotations_endpoint.split("?", 1)[0]
        annotation_pages, annotation_error = _collect_pages(
            client,
            annotations_endpoint,
            timeout=timeout,
            owner=owner,
            name=name,
            expected_path=expected_annotations,
            required_repo_id=required_repo_id,
        )
        if annotation_error:
            return None, [], annotation_error
        for annotation_page in annotation_pages:
            rows = annotation_page.value
            if not isinstance(rows, list):
                return None, [], "github_invalid_payload"
            for row in rows:
                if not isinstance(row, Mapping):
                    return None, [], "github_invalid_payload"
                level = row.get("annotation_level", "")
                if not isinstance(level, str):
                    return None, [], "annotations_unreadable"
                if level in {"warning", "notice"}:
                    continue
                try:
                    parsed = _parse_oss_finding(row.get("message"))
                except _UnreadableAnnotation:
                    parsed = None
                if parsed is None:
                    return record, [], "annotations_unreadable"
                findings.append(parsed)
                if len(findings) > _MAX_OSS_FINDINGS:
                    return None, [], "pagination_limit"
    if findings:
        return record, findings, None
    if gate_conclusion != "success":
        return record, [], "run_not_success"
    return record, [], None


def read_fleet_security(client: Any, repo: FleetRepo, *, timeout: float) -> dict[str, Any]:
    """Read one repository's fleet-security enrollment and CI evidence.

    ``missing_enrollment``, ``inaccessible_evidence`` and ``zero_findings`` are
    distinct. A 404 on the repository itself is inaccessible evidence: the
    caller could not be shown to be absent. Findings come only from redacted
    annotations the workflow already emits.
    """
    slug = _split_github_slug(repo)
    if slug is None:
        return _fleet_record("inaccessible_evidence", reason="invalid_repository")
    owner, name = slug
    described = _github_get(client, _repo_api(owner, name, "").rstrip("/"), timeout)
    if described.error or described.status not in {200, 304} or not isinstance(described.value, Mapping):
        reason = _error_code(described) if described.error or described.status not in {200, 304} else "github_invalid_payload"
        return _fleet_record("inaccessible_evidence", reason=reason)
    branch = _safe_branch(described.value.get("default_branch"))
    if branch is None:
        return _fleet_record("inaccessible_evidence", reason="invalid_default_branch")
    repo_id = _repo_numeric_id(described.value)
    callers, rejected, list_error = _read_workflow_callers(client, owner, name, branch, timeout)
    if list_error:
        return _fleet_record("inaccessible_evidence", reason=list_error, rejected_pins=rejected)
    if not callers:
        return _fleet_record("missing_enrollment", rejected_pins=rejected)
    runs: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for caller in callers:
        # Two pins in one workflow file share one Actions run. Read that run once.
        if caller["path"] in seen_paths:
            continue
        seen_paths.add(caller["path"])
        record, caller_findings, evidence_error = _read_caller_evidence(
            client, owner, name, branch, caller, timeout, repo_id
        )
        if evidence_error:
            return _fleet_record(
                "inaccessible_evidence",
                reason=evidence_error,
                callers=callers,
                rejected_pins=rejected,
                runs=[*runs, record] if record is not None else runs,
            )
        if record is not None:
            runs.append(record)
        findings.extend(caller_findings)
        if len(findings) > _MAX_OSS_FINDINGS:
            return _fleet_record(
                "inaccessible_evidence",
                reason="pagination_limit",
                callers=callers,
                rejected_pins=rejected,
                runs=runs,
            )
    if findings:
        return _fleet_record("findings", callers=callers, rejected_pins=rejected, runs=runs, findings=findings)
    return _fleet_record("zero_findings", callers=callers, rejected_pins=rejected, runs=runs)


def scan_fleet_alerts(
    *,
    registry_path: Path | None = None,
    repos: Mapping[str, FleetRepo] | None = None,
    client: Any = None,
    timeout: float = _DEFAULT_TIMEOUT,
    clock: Any = time.time,
) -> dict[str, Any]:
    """Scan open alerts across all non-opted-out repositories in ``fleet_repos.yaml``."""
    checked_timeout = _validate_timeout(timeout)
    catalog = dict(repos) if repos is not None else load_fleet_repos(registry_path)
    gh = client if client is not None else github_client.GitHubClient()
    scanned_epoch = float(clock())
    scanned_at = datetime.fromtimestamp(scanned_epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    repositories: dict[str, dict[str, Any]] = {}
    all_ok = True

    for key, repo in catalog.items():
        if not repo.security_alerts:
            continue
        tools_payload: dict[str, dict[str, Any]] = {}
        repo_ok = True
        for tool in TOOLS:
            status, alerts, err = _fetch_tool_alerts(gh, repo, tool, timeout=checked_timeout)
            if status == "ok" and alerts is not None:
                tools_payload[tool] = _summarize_alerts(tool, alerts)
            elif status == "not_enabled":
                tools_payload[tool] = {
                    "status": "not_enabled",
                    "total": None,
                    "by_severity": None,
                }
            else:
                repo_ok = False
                all_ok = False
                tools_payload[tool] = {
                    "status": "error",
                    "total": None,
                    "by_severity": None,
                    "error": err or "github_error",
                }
        repositories[key] = {
            "github": repo.github,
            "source_ok": repo_ok,
            "tools": tools_payload,
            "fleet_security": read_fleet_security(gh, repo, timeout=checked_timeout),
        }

    return {
        "schema_version": _SCAN_SCHEMA_VERSION,
        "scanned_at": scanned_at,
        "scanned_epoch": scanned_epoch,
        "source_ok": all_ok,
        "repositories": repositories,
    }


def _build_prometheus_registry(scan_result: Mapping[str, Any]) -> CollectorRegistry:
    registry = CollectorRegistry()
    open_gauge = Gauge(
        METRIC_OPEN_ALERTS,
        "Open GitHub security alerts per registered fleet repository, tool, and severity.",
        ("repo", "tool", "severity"),
        registry=registry,
    )
    enabled_gauge = Gauge(
        METRIC_FEATURE_ENABLED,
        "Whether a GitHub security alert feature is enabled (1) or not enabled (0) for a repository.",
        ("repo", "tool"),
        registry=registry,
    )
    source_ok_gauge = Gauge(
        METRIC_SOURCE_OK,
        "Whether all GitHub security alert API queries succeeded for a repository (1=ok, 0=error).",
        ("repo",),
        registry=registry,
    )
    ts_gauge = Gauge(
        METRIC_SCAN_TIMESTAMP,
        "Unix epoch timestamp of the last completed fleet security-alert scan.",
        registry=registry,
    )
    fleet_state_gauge = Gauge(
        METRIC_FLEET_SECURITY_STATE,
        "Fleet-security enrollment state for a repository (1 for the observed state only).",
        ("repo", "state"),
        registry=registry,
    )
    fleet_findings_gauge = Gauge(
        METRIC_FLEET_SECURITY_FINDINGS,
        "OSS findings from the latest fleet-security run. Omitted unless the state is findings.",
        ("repo", "scanner", "severity"),
        registry=registry,
    )
    scanned_epoch = scan_result.get("scanned_epoch")
    if isinstance(scanned_epoch, (int, float)) and not isinstance(scanned_epoch, bool) and math.isfinite(scanned_epoch):
        ts_gauge.set(float(scanned_epoch))

    repositories = scan_result.get("repositories")
    if not isinstance(repositories, Mapping):
        raise SecurityAlertsError("scan result is missing repositories mapping")

    for repo_key, repo_entry in sorted(repositories.items()):
        if not isinstance(repo_entry, Mapping):
            continue
        source_ok_gauge.labels(repo=repo_key).set(1 if repo_entry.get("source_ok") is True else 0)
        tools = repo_entry.get("tools")
        if not isinstance(tools, Mapping):
            continue
        for tool in TOOLS:
            entry = tools.get(tool)
            if not isinstance(entry, Mapping):
                continue
            status = entry.get("status")
            if status == "ok":
                enabled_gauge.labels(repo=repo_key, tool=tool).set(1)
                by_sev = entry.get("by_severity")
                if not isinstance(by_sev, Mapping):
                    continue
                for severity in STANDARD_SEVERITIES:
                    open_gauge.labels(repo=repo_key, tool=tool, severity=severity).set(int(by_sev.get(severity, 0)))
                unknown_count = int(by_sev.get("unknown", 0))
                if unknown_count > 0:
                    open_gauge.labels(repo=repo_key, tool=tool, severity="unknown").set(unknown_count)
            elif status == "not_enabled":
                enabled_gauge.labels(repo=repo_key, tool=tool).set(0)
            # On "error": emit neither open count nor feature_enabled so errors are never read as 0.
        fleet = repo_entry.get("fleet_security")
        if isinstance(fleet, Mapping) and fleet.get("state") in FLEET_SECURITY_STATES:
            fleet_state_gauge.labels(repo=repo_key, state=str(fleet["state"])).set(1)
            if fleet.get("state") == "findings" and isinstance(fleet.get("findings"), list):
                counts: dict[tuple[str, str], int] = {}
                for item in fleet["findings"]:
                    if not isinstance(item, Mapping):
                        continue
                    scanner_name = item.get("scanner")
                    finding_severity = item.get("severity")
                    if not isinstance(scanner_name, str) or scanner_name not in {
                        "actionlint",
                        "shellcheck",
                        "zizmor",
                        "trufflehog",
                        "trivy-fs",
                        "trivy-iac",
                        "trivy-image",
                    }:
                        continue
                    if not isinstance(finding_severity, str) or finding_severity not in SEVERITIES:
                        continue
                    counts[(scanner_name, finding_severity)] = counts.get((scanner_name, finding_severity), 0) + 1
                for (scanner_name, finding_severity), count in sorted(counts.items()):
                    fleet_findings_gauge.labels(
                        repo=repo_key, scanner=scanner_name, severity=finding_severity
                    ).set(count)
    return registry


def render_prometheus_metrics(scan_result: Mapping[str, Any]) -> str:
    """Render Prometheus text 0.0.4 exposition from a ``scan_fleet_alerts`` payload."""
    return generate_latest(_build_prometheus_registry(scan_result)).decode("utf-8")


def write_prometheus_textfile(path: Path | str, scan_result: Mapping[str, Any]) -> Path:
    """Atomically write Prometheus textfile metrics to ``path`` (refuses symlinks)."""
    dest = validate_output_path(path, label="--metrics-file")
    if not dest.parent.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.parent.is_symlink() or not dest.parent.is_dir():
        raise SecurityAlertsError("--metrics-file parent must be a real directory", exit_code=2)
    registry = _build_prometheus_registry(scan_result)
    write_to_textfile(str(dest), registry)
    return dest


def select_security_worker(
    *,
    catalog: dict[str, Any] | None = None,
    health: dict[str, Any] | None = None,
    role: str = DEFAULT_SECURITY_ROLE,
) -> WorkerSelection:
    """Select a security-qualified coding worker purely from the model catalog."""
    try:
        validated = validate_catalog(catalog) if catalog is not None else load_model_catalog()
        resolution = resolve_role(role, catalog=validated, purpose="launch", health=health)
    except ModelCatalogError as exc:
        raise SecurityAlertsError(f"catalog routing failed: {exc}") from exc

    models = validated["models"]
    eligible: list[tuple[int, int, RoleCandidate]] = []
    for index, candidate in enumerate(resolution.candidates):
        if candidate.exclusion_reasons:
            continue
        if candidate.health == "unhealthy":
            continue
        model_spec = models.get(candidate.model_id, {})
        if model_spec.get("lifecycle") != "active":
            continue
        model_roles = set(model_spec.get("roles", ()))
        if not (model_roles & _CODING_ROLES):
            continue
        eligible.append((candidate.health_rank, index, candidate))

    if not eligible:
        raise SecurityAlertsError(f"no eligible security worker route resolved for catalog role {role!r}")

    _, _, picked = min(eligible, key=lambda item: (item[0], item[1]))
    return WorkerSelection(
        role=role,
        seat=picked.seat,
        route_name=picked.route_name,
        model_id=picked.model_id,
        wire_id=picked.wire_id,
        family=picked.family,
        route=picked.route,
        transport=picked.transport,
        effort=picked.effort,
        argv=picked.argv,
        catalog_digest=resolution.catalog_digest,
    )


def _extract_code_scanning_item(alert: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    number = alert.get("number")
    rule = _as_mapping(alert.get("rule"))
    rule_id = _sanitize_text(rule.get("id") or rule.get("name") or "unknown")
    tool = _as_mapping(alert.get("tool"))
    tool_name = _sanitize_text(tool.get("name") or "unknown")
    instance = _as_mapping(alert.get("most_recent_instance"))
    location = _as_mapping(instance.get("location"))
    raw_path = location.get("path")
    owned = _safe_owned_path(raw_path)
    start_line = location.get("start_line")
    line_str = str(start_line) if type(start_line) is int and start_line > 0 else "?"
    display_path = _sanitize_text(raw_path) if isinstance(raw_path, str) and raw_path.strip() else "unknown"
    path_line = f"{display_path}:{line_str}"
    msg_obj = _as_mapping(instance.get("message"))
    message = _sanitize_text(msg_obj.get("text") or rule.get("description") or "")
    item = {
        "number": number if type(number) is int else None,
        "rule": rule_id,
        "severity": _normalize_severity("code_scanning", alert),
        "tool": tool_name,
        "path_line": path_line,
        "message": message,
    }
    return item, owned


def _extract_dependabot_item(alert: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    number = alert.get("number")
    advisory = _as_mapping(alert.get("security_advisory"))
    vuln = _as_mapping(alert.get("security_vulnerability"))
    dep = _as_mapping(alert.get("dependency"))
    pkg = _as_mapping(dep.get("package"))
    rule_id = _sanitize_text(advisory.get("ghsa_id") or advisory.get("cve_id") or "unknown")
    ecosystem = _sanitize_text(pkg.get("ecosystem") or "")
    pkg_name = _sanitize_text(pkg.get("name") or "unknown")
    package_label = f"{ecosystem}/{pkg_name}" if ecosystem else pkg_name
    manifest_raw = dep.get("manifest_path")
    owned = _safe_owned_path(manifest_raw)
    manifest = _sanitize_text(manifest_raw) if isinstance(manifest_raw, str) and manifest_raw.strip() else "unknown"
    patched_obj = _as_mapping(vuln.get("first_patched_version"))
    patched = _sanitize_text(patched_obj.get("identifier") or "none")
    summary = _sanitize_text(advisory.get("summary") or "")
    item = {
        "number": number if type(number) is int else None,
        "rule": rule_id,
        "severity": _normalize_severity("dependabot", alert),
        "package": package_label,
        "manifest_path": manifest,
        "patched_version": patched,
        "summary": summary,
    }
    return item, owned


def _extract_secret_scanning_item(alert: Mapping[str, Any]) -> dict[str, Any]:
    # OPSEC: never read or render the ``secret`` value field.
    number = alert.get("number")
    secret_type = _sanitize_text(alert.get("secret_type_display_name") or alert.get("secret_type") or "unknown")
    validity = _sanitize_text(alert.get("validity") or "unknown")
    return {
        "number": number if type(number) is int else None,
        "rule": secret_type,
        "severity": _normalize_severity("secret_scanning", alert),
        "validity": validity,
    }


def _build_dispatch_argv(
    worker: WorkerSelection,
    *,
    repo_key: str,
    task_id: str,
    prompt_file: str,
    owned_paths: Sequence[str],
) -> tuple[str, ...]:
    argv = list(worker.argv)
    if "--effort" not in argv:
        argv.extend(["--effort", worker.effort])
    argv.extend(
        [
            "--mode",
            "workspace-write",
            "--worktree",
            "--repo",
            repo_key,
            "--task-id",
            task_id,
            "--prompt-file",
            prompt_file,
            "--research-role",
            "implementation",
        ]
    )
    for path in owned_paths:
        argv.extend(["--owned-path", path])
    return tuple(argv)


def _render_fleet_section(fleet: Mapping[str, Any]) -> list[str]:
    state = fleet.get("state")
    lines = ["", "## Fleet Security", ""]
    if state == "missing_enrollment":
        lines.append("State: `missing_enrollment`.")
        lines.append(
            "The default branch has no workflow that calls "
            f"`{_FLEET_SECURITY_WORKFLOW}` at a full 40-character SHA."
        )
        rejected = fleet.get("rejected_pins")
        if isinstance(rejected, list) and rejected:
            lines.append("These refs are not enrollment:")
            for item in rejected:
                if isinstance(item, Mapping):
                    lines.append(
                        f"- `{_sanitize_md_cell(item.get('path'))}` at `{_sanitize_md_cell(item.get('ref'))}`"
                    )
        return lines
    if state == "inaccessible_evidence":
        lines.append("State: `inaccessible_evidence`.")
        lines.append(
            f"Enrollment or CI evidence could not be read (`{_sanitize_md_cell(fleet.get('reason'))}`)."
        )
        callers = fleet.get("callers")
        if isinstance(callers, list):
            for caller in callers:
                if isinstance(caller, Mapping):
                    lines.append(
                        f"- Caller `{_sanitize_md_cell(caller.get('path'))}` pin "
                        f"`{_sanitize_md_cell(caller.get('pin'))}`"
                    )
        return lines
    if state == "zero_findings":
        lines.append("State: `zero_findings`.")
        lines.append(
            "The Fleet security gate on the latest completed default-branch run succeeded and "
            "recorded no OSS finding annotations."
        )
    elif state == "findings":
        lines.append("State: `findings`.")
        lines.append("OSS scanner annotations from the latest completed default-branch run:")
    else:
        lines.append("State: `inaccessible_evidence`.")
        lines.append("The fleet-security record was missing or malformed.")
        return lines
    callers = fleet.get("callers")
    if isinstance(callers, list):
        for caller in callers:
            if isinstance(caller, Mapping):
                lines.append(
                    f"- Caller `{_sanitize_md_cell(caller.get('path'))}` pin `{_sanitize_md_cell(caller.get('pin'))}`"
                )
    runs = fleet.get("runs")
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, Mapping):
                continue
            url = run.get("html_url")
            url_text = f" {url}" if isinstance(url, str) and url.startswith("https://github.com/") else ""
            lines.append(
                f"- Run `{run.get('run_id')}` gate `{_sanitize_md_cell(run.get('gate_conclusion'))}` "
                f"head `{_sanitize_md_cell(run.get('head_sha'))}`{url_text}"
            )
    finding_rows = fleet.get("findings") if state == "findings" else []
    if state == "findings":
        lines.extend(
            [
                "",
                "| Scanner | Rule | Severity | Location | Message |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        if isinstance(finding_rows, list):
            for item in finding_rows:
                if not isinstance(item, Mapping):
                    continue
                lines.append(
                    f"| `{_sanitize_md_cell(item.get('scanner'))}` | `{_sanitize_md_cell(item.get('rule'))}` | "
                    f"`{_sanitize_md_cell(item.get('severity'))}` | `{_sanitize_md_cell(item.get('location'))}` | "
                    f"{_sanitize_md_cell(item.get('message'))} |"
                )
    return lines


def _render_brief_markdown(
    *,
    repo: FleetRepo,
    generated_at: str,
    worker: WorkerSelection,
    alerts_payload: Mapping[str, Any],
    fleet_security: Mapping[str, Any],
    owned_paths: Sequence[str],
    dispatch_command: str,
) -> str:
    cs = alerts_payload["code_scanning"]
    dep = alerts_payload["dependabot"]
    sec = alerts_payload["secret_scanning"]

    routing_line = (
        f"Catalog routing: role=`{worker.role}`, seat=`{worker.seat}/{worker.route_name}`, "
        f"model=`{worker.model_id}` (wire `{worker.wire_id}`, family `{worker.family}`, "
        f"route `{worker.route}`, effort `{worker.effort}`)"
    )
    lines: list[str] = [
        f"# Security Alert Fix Brief — `{repo.key}` (`{repo.github}`)",
        "",
        f"Generated: `{generated_at}`",
        routing_line,
        f"Catalog digest: `{worker.catalog_digest}`",
        "",
        "## Live Alert Summary",
        "",
        "| Tool | Status | Open alerts |",
        "| --- | --- | --- |",
    ]
    for tool_name, entry in (("code_scanning", cs), ("dependabot", dep), ("secret_scanning", sec)):
        count_text = str(entry["total"]) if entry["status"] == "ok" else entry["status"]
        lines.append(f"| `{tool_name}` | `{entry['status']}` | {count_text} |")

    lines.extend(["", "## Code Scanning Alerts", ""])
    if cs["status"] != "ok":
        lines.append(f"Feature status: `{cs['status']}` (not zero).")
    elif not cs["items"]:
        lines.append("No open code-scanning alerts.")
    else:
        lines.extend(
            [
                "| Alert | Rule | Severity | Location (`path:line`) | Tool | Message |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in cs["items"]:
            num = item["number"] if item["number"] is not None else "?"
            lines.append(
                f"| #{num} | `{_sanitize_md_cell(item['rule'])}` | `{item['severity']}` | "
                f"`{_sanitize_md_cell(item['path_line'])}` | `{_sanitize_md_cell(item['tool'])}` | "
                f"{_sanitize_md_cell(item['message'])} |"
            )

    lines.extend(["", "## Dependabot Alerts", ""])
    if dep["status"] != "ok":
        lines.append(f"Feature status: `{dep['status']}` (not zero).")
    elif not dep["items"]:
        lines.append("No open Dependabot alerts.")
    else:
        lines.extend(
            [
                "| Alert | Advisory | Severity | Package | Manifest | Patched | Summary |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in dep["items"]:
            num = item["number"] if item["number"] is not None else "?"
            lines.append(
                f"| #{num} | `{_sanitize_md_cell(item['rule'])}` | `{item['severity']}` | "
                f"`{_sanitize_md_cell(item['package'])}` | `{_sanitize_md_cell(item['manifest_path'])}` | "
                f"`{_sanitize_md_cell(item['patched_version'])}` | {_sanitize_md_cell(item['summary'])} |"
            )

    lines.extend(["", "## Secret Scanning Alerts", ""])
    if sec["status"] != "ok":
        lines.append(f"Feature status: `{sec['status']}` (not zero).")
    elif not sec["items"]:
        lines.append("No open secret-scanning alerts.")
    else:
        lines.extend(
            [
                "| Alert | Secret type | Severity | Validity |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in sec["items"]:
            num = item["number"] if item["number"] is not None else "?"
            lines.append(
                f"| #{num} | `{_sanitize_md_cell(item['rule'])}` | `{item['severity']}` | "
                f"`{_sanitize_md_cell(item['validity'])}` |"
            )
        opsec_note = (
            "OPSEC: Never print, commit, or paste secret values. Follow `docs/runbooks/secret-scanning.md` "
            "and escalate real exposed credentials to the operator via the private channel; agents never "
            "rotate, revoke, or rewrite Git history."
        )
        lines.extend(["", opsec_note])

    lines.extend(_render_fleet_section(fleet_security))

    lines.extend(["", "## Suggested Owned Paths", ""])
    if owned_paths:
        for path in owned_paths:
            lines.append(f"- `{_sanitize_md_cell(path)}`")
    else:
        lines.append("- None derived from open alerts (confirm scope before dispatch).")

    rule_root_cause = (
        "- Fix the underlying vulnerability at its root cause first. Never silence a real defect with "
        "`# nosemgrep`, `# noqa`, ignore patterns, or a false-positive triage row."
    )
    rule_fleet = (
        "- For a fleet-security finding, fix the cited file at the cited line. Do not weaken the scanner "
        "or add a blanket ignore. A missing caller and an unreadable run are not a clean scan."
    )
    rule_dependabot = "- For Dependabot alerts, upgrade or pin past the patched version and update lockfiles cleanly."
    rule_secret = (
        "- For secret-scanning alerts, stop on any possible real credential and escalate counts and alert "
        "numbers to the operator through the private channel per `docs/runbooks/secret-scanning.md`."
    )
    rule_triage = (
        "- Only write a triage row when positive SARIF and code-flow evidence proves an alert is a false "
        "positive, test-only fixture, or accepted won't-fix."
    )
    rule_verify = (
        "- Run targeted verification checks and Ruff, push the head, run cross-family `review-loop` with "
        "`--risk critical`, wait for green CI Gate on the same head, land through the merge queue, and "
        "complete closeout."
    )
    triage_intro = (
        f"When a code-scanning alert is genuinely not a bug, add or revise exactly one four-column Markdown "
        f"table row in `{_TRIAGE_RUNBOOK_PATH}` in the same PR:"
    )
    preflight_col13 = (
        "- Column 1 is the plain integer alert number (no `#`), column 2 is the backticked `rule.id`, "
        "column 3 is the backticked `path:start_line` matching every open branch instance."
    )
    preflight_col4 = (
        "- Column 4 must start with `False positive.` (for reasons `false positive` and `used in tests`; "
        "`used in tests` also requires column 3 to start with `` `tests/ ``) or `Won't fix.` (for `won't fix`)."
    )
    preflight_diff = (
        "- The PR diff must add or modify that exact row; moving an unchanged row or duplicating an alert "
        "number is refused."
    )
    preflight_seat = (
        "- Workers and reviewers cannot run `code-scanning-dismiss`; after the triage PR merges, an "
        "interactive seat runs `code-scanning-dismiss` citing `Alert #N`, a prose explanation, and the "
        "single canonical same-repository merged PR URL."
    )

    lines.extend(
        [
            "",
            "## Root-Cause Fix Rules",
            "",
            rule_root_cause,
            rule_fleet,
            rule_dependabot,
            rule_secret,
            rule_triage,
            rule_verify,
            "",
            "## Code-Scanning Dismissal Triage Row Format (`code-scanning-dismiss`)",
            "",
            triage_intro,
            "",
            "```markdown",
            "| <alert_number> | `<rule_id>` | `<path>:<start_line>` | False positive. <concrete SARIF/code-flow evidence> |",
            "| <alert_number> | `<rule_id>` | `<path>:<start_line>` | Won't fix. <concrete risk and containment rationale> |",
            "```",
            "",
            "Publisher preflight rules (`lu_machinery.publish code-scanning-dismiss`):",
            preflight_col13,
            preflight_col4,
            preflight_diff,
            preflight_seat,
            "",
            "## Dispatch Command (Not Executed)",
            "",
            "```bash",
            dispatch_command,
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def build_fix_brief(
    repo_key: str,
    *,
    registry_path: Path | None = None,
    repos: Mapping[str, FleetRepo] | None = None,
    client: Any = None,
    catalog: dict[str, Any] | None = None,
    health: dict[str, Any] | None = None,
    role: str = DEFAULT_SECURITY_ROLE,
    task_id: str | None = None,
    prompt_file: str = "<brief-file>",
    include_opted_out: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
    clock: Any = time.time,
) -> BriefResult:
    """Build a routed fix brief and dispatch command from one repository's live alerts.

    Never dispatches. Fails closed on unknown/opted-out repositories, GitHub API
    errors, or missing catalog worker routes.
    """
    checked_timeout = _validate_timeout(timeout)
    repo_catalog = dict(repos) if repos is not None else load_fleet_repos(registry_path)
    resolved_key = canonical_repo_key(repo_key, repo_catalog)
    repo = repo_catalog[resolved_key]
    if not repo.security_alerts and not include_opted_out:
        raise SecurityAlertsError(
            f"repository {resolved_key!r} has security_alerts opted out in fleet_repos.yaml "
            "(pass --include-opted-out to override)",
            exit_code=2,
        )

    safe_prompt_file = _validate_prompt_file_arg(prompt_file)
    worker = select_security_worker(catalog=catalog, health=health, role=role)
    gh = client if client is not None else github_client.GitHubClient()
    generated_epoch = float(clock())
    generated_dt = datetime.fromtimestamp(generated_epoch, tz=UTC)
    generated_at = generated_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    effective_task_id = _validate_task_id(
        task_id if task_id is not None else f"sec-{resolved_key}-{generated_dt.strftime('%Y%m%d')}"
    )

    alerts_payload: dict[str, Any] = {}
    owned: set[str] = set()
    errors: list[str] = []

    for tool in TOOLS:
        status, raw_alerts, err = _fetch_tool_alerts(gh, repo, tool, timeout=checked_timeout)
        if status == "error":
            errors.append(f"{tool}={err or 'github_error'}")
            continue
        if status == "not_enabled":
            alerts_payload[tool] = {"status": "not_enabled", "total": None, "items": []}
            continue
        assert raw_alerts is not None
        items: list[dict[str, Any]] = []
        if tool == "code_scanning":
            for alert in raw_alerts:
                item, path = _extract_code_scanning_item(alert)
                items.append(item)
                if path:
                    owned.add(path)
            if items:
                owned.add(_TRIAGE_RUNBOOK_PATH)
        elif tool == "dependabot":
            for alert in raw_alerts:
                item, path = _extract_dependabot_item(alert)
                items.append(item)
                if path:
                    owned.add(path)
        elif tool == "secret_scanning":
            for alert in raw_alerts:
                items.append(_extract_secret_scanning_item(alert))
        items.sort(key=lambda entry: (entry.get("number") is None, entry.get("number") or 0))
        alerts_payload[tool] = {"status": "ok", "total": len(items), "items": items}

    if errors:
        raise SecurityAlertsError(
            f"refusing to generate fix brief for {resolved_key!r}: alert API error ({', '.join(errors)})"
        )

    fleet_security = read_fleet_security(gh, repo, timeout=checked_timeout)
    if fleet_security.get("state") == "findings":
        for item in fleet_security.get("findings", ()):
            path = item.get("path") if isinstance(item, Mapping) else None
            if isinstance(path, str):
                owned.add(path)

    owned_paths = tuple(sorted(owned))
    dispatch_argv = _build_dispatch_argv(
        worker,
        repo_key=resolved_key,
        task_id=effective_task_id,
        prompt_file=safe_prompt_file,
        owned_paths=owned_paths,
    )
    dispatch_command = shlex.join(dispatch_argv)
    brief_markdown = _render_brief_markdown(
        repo=repo,
        generated_at=generated_at,
        worker=worker,
        alerts_payload=alerts_payload,
        fleet_security=fleet_security,
        owned_paths=owned_paths,
        dispatch_command=dispatch_command,
    )
    return BriefResult(
        repo_key=resolved_key,
        github=repo.github,
        generated_at=generated_at,
        worker=worker,
        alerts=alerts_payload,
        fleet_security=fleet_security,
        owned_paths=owned_paths,
        dispatch_argv=dispatch_argv,
        dispatch_command=dispatch_command,
        brief_markdown=brief_markdown,
    )


def write_brief_file(path: Path | str, content: str) -> Path:
    """Atomically write a rendered fix brief to ``path`` (refuses symlinks and directories)."""
    dest = validate_output_path(path, label="--brief-out")
    if not dest.parent.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.parent.is_symlink() or not dest.parent.is_dir():
        raise SecurityAlertsError("--brief-out parent must be a real directory", exit_code=2)
    fd, raw_tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return dest


__all__ = [
    "DEFAULT_SECURITY_ROLE",
    "FLEET_SECURITY_STATES",
    "METRIC_FEATURE_ENABLED",
    "METRIC_FLEET_SECURITY_FINDINGS",
    "METRIC_FLEET_SECURITY_STATE",
    "METRIC_OPEN_ALERTS",
    "METRIC_SCAN_TIMESTAMP",
    "METRIC_SOURCE_OK",
    "SEVERITIES",
    "STANDARD_SEVERITIES",
    "TOOLS",
    "BriefResult",
    "FleetRepoError",
    "SecurityAlertsError",
    "WorkerSelection",
    "build_fix_brief",
    "render_prometheus_metrics",
    "scan_fleet_alerts",
    "select_security_worker",
    "write_brief_file",
    "write_prometheus_textfile",
]
