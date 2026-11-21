"""GitHub security-alert watcher and routed fix-brief generator.

Reads the fleet repository registry (``fleet_repos.yaml``), queries the three
GitHub security alert APIs strictly through ``lu_machinery.common.github_client``,
emits JSON and Prometheus textfile metrics, and renders a fix brief + dispatch
command routed through the model catalog without ever dispatching.
"""

from __future__ import annotations

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
from urllib.parse import quote, urlsplit

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
    "secret_scanning": "secret-scanning/alerts?state=open&per_page=100",
}

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


def _safe_owned_path(raw: Any) -> str | None:
    """Validate one alert-derived repository-relative path for ``--owned-path``."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.startswith(("/", "\\", "-")) or "\\" in text or "\x00" in text:
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
        endpoint: str | None = _repo_endpoint(repo, tool)
    except SecurityAlertsError:
        return "error", None, "invalid_repository_slug"
    expected_path = endpoint.split("?", 1)[0]

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
        parsed_next = urlsplit(next_url)
        next_path = parsed_next.path.lstrip("/")
        if next_path.startswith("api/v3/"):
            next_path = next_path.removeprefix("api/v3/")
        if next_path != expected_path:
            return "error", None, "github_invalid_next_link"
        endpoint = next_url
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
    rule = alert.get("rule") if isinstance(alert.get("rule"), Mapping) else {}
    rule_id = _sanitize_text(rule.get("id") or rule.get("name") or "unknown")
    tool = alert.get("tool") if isinstance(alert.get("tool"), Mapping) else {}
    tool_name = _sanitize_text(tool.get("name") or "unknown")
    instance = alert.get("most_recent_instance") if isinstance(alert.get("most_recent_instance"), Mapping) else {}
    location = instance.get("location") if isinstance(instance.get("location"), Mapping) else {}
    raw_path = location.get("path")
    owned = _safe_owned_path(raw_path)
    start_line = location.get("start_line")
    line_str = str(start_line) if type(start_line) is int and start_line > 0 else "?"
    display_path = _sanitize_text(raw_path) if isinstance(raw_path, str) and raw_path.strip() else "unknown"
    path_line = f"{display_path}:{line_str}"
    msg_obj = instance.get("message") if isinstance(instance.get("message"), Mapping) else {}
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
    advisory = alert.get("security_advisory") if isinstance(alert.get("security_advisory"), Mapping) else {}
    vuln = alert.get("security_vulnerability") if isinstance(alert.get("security_vulnerability"), Mapping) else {}
    dep = alert.get("dependency") if isinstance(alert.get("dependency"), Mapping) else {}
    pkg = dep.get("package") if isinstance(dep.get("package"), Mapping) else {}
    rule_id = _sanitize_text(advisory.get("ghsa_id") or advisory.get("cve_id") or "unknown")
    ecosystem = _sanitize_text(pkg.get("ecosystem") or "")
    pkg_name = _sanitize_text(pkg.get("name") or "unknown")
    package_label = f"{ecosystem}/{pkg_name}" if ecosystem else pkg_name
    manifest_raw = dep.get("manifest_path")
    owned = _safe_owned_path(manifest_raw)
    manifest = _sanitize_text(manifest_raw) if isinstance(manifest_raw, str) and manifest_raw.strip() else "unknown"
    patched_obj = vuln.get("first_patched_version") if isinstance(vuln.get("first_patched_version"), Mapping) else {}
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


def _render_brief_markdown(
    *,
    repo: FleetRepo,
    generated_at: str,
    worker: WorkerSelection,
    alerts_payload: Mapping[str, Any],
    owned_paths: Sequence[str],
    dispatch_command: str,
) -> str:
    cs = alerts_payload["code_scanning"]
    dep = alerts_payload["dependabot"]
    sec = alerts_payload["secret_scanning"]

    lines: list[str] = [
        f"# Security Alert Fix Brief — `{repo.key}` (`{repo.github}`)",
        "",
        f"Generated: `{generated_at}`",
        (
            f"Catalog routing: role=`{worker.role}`, seat=`{worker.seat}/{worker.route_name}`, "
            f"model=`{worker.model_id}` (wire `{worker.wire_id}`, family `{worker.family}`, "
            f"route `{worker.route}`, effort `{worker.effort}`)"
        ),
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
        lines.extend(
            [
                "",
                "OPSEC: Never print, commit, or paste secret values. Follow `docs/runbooks/secret-scanning.md` "
                "and escalate real exposed credentials to the operator via the private channel; agents never "
                "rotate, revoke, or rewrite Git history.",
            ]
        )

    lines.extend(["", "## Suggested Owned Paths", ""])
    if owned_paths:
        for path in owned_paths:
            lines.append(f"- `{path}`")
    else:
        lines.append("- None derived from open alerts (confirm scope before dispatch).")

    lines.extend(
        [
            "",
            "## Root-Cause Fix Rules",
            "",
            "- Fix the underlying vulnerability at its root cause first. Never silence a real defect with "
            "`# nosemgrep`, `# noqa`, ignore patterns, or a false-positive triage row.",
            "- For Dependabot alerts, upgrade or pin past the patched version and update lockfiles cleanly.",
            "- For secret-scanning alerts, stop on any possible real credential and escalate counts and alert "
            "numbers to the operator through the private channel per `docs/runbooks/secret-scanning.md`.",
            "- Only write a triage row when positive SARIF and code-flow evidence proves an alert is a false "
            "positive, test-only fixture, or accepted won't-fix.",
            "- Run targeted verification checks and Ruff, push the head, run cross-family `review-loop` with "
            "`--risk critical`, wait for green CI Gate on the same head, land through the merge queue, and "
            "complete closeout.",
            "",
            "## Code-Scanning Dismissal Triage Row Format (`code-scanning-dismiss`)",
            "",
            f"When a code-scanning alert is genuinely not a bug, add or revise exactly one four-column Markdown "
            f"table row in `{_TRIAGE_RUNBOOK_PATH}` in the same PR:",
            "",
            "```markdown",
            "| <alert_number> | `<rule_id>` | `<path>:<start_line>` | False positive. <concrete SARIF/code-flow evidence> |",
            "| <alert_number> | `<rule_id>` | `<path>:<start_line>` | Won't fix. <concrete risk and containment rationale> |",
            "```",
            "",
            "Publisher preflight rules (`lu_machinery.publish code-scanning-dismiss`):",
            "- Column 1 is the plain integer alert number (no `#`), column 2 is the backticked `rule.id`, "
            "column 3 is the backticked `path:start_line` matching every open branch instance.",
            "- Column 4 must start with `False positive.` (for reasons `false positive` and `used in tests`; "
            "`used in tests` also requires column 3 to start with `` `tests/ ``) or `Won't fix.` (for `won't fix`).",
            "- The PR diff must add or modify that exact row; moving an unchanged row or duplicating an alert "
            "number is refused.",
            "- Workers and reviewers cannot run `code-scanning-dismiss`; after the triage PR merges, an "
            "interactive seat runs `code-scanning-dismiss` citing `Alert #N`, a prose explanation, and the "
            "single canonical same-repository merged PR URL.",
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
        owned_paths=owned_paths,
        dispatch_command=dispatch_command,
    )
    return BriefResult(
        repo_key=resolved_key,
        github=repo.github,
        generated_at=generated_at,
        worker=worker,
        alerts=alerts_payload,
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
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=dest.parent,
            prefix=f".{dest.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return dest


__all__ = [
    "DEFAULT_SECURITY_ROLE",
    "METRIC_FEATURE_ENABLED",
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
