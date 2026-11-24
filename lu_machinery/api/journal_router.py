"""Loopback-only read API for the append-only project journal (JOURNAL-1124)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from lu_machinery.api import panel_values
from lu_machinery.api.observer_presence import _direct_loopback_peer
from lu_machinery.common import json_depth
from lu_machinery.orchestration.fleet_repos import FleetRepo, FleetRepoError, load_fleet_repos

router = APIRouter()

RESPONSE_SCHEMA_VERSION = "journal.v1"
DRIVER_STATE_CLI_ENV = "LU_DRIVER_STATE_CLI"
JOURNAL_CLI_TIMEOUT_S = 10.0
JOURNAL_FRESHNESS_BOUND_S = 60.0
_JSON_MAX_DEPTH = 32
_INT64_MAX = (1 << 63) - 1
_MAX_OFFSET = 2_147_483_647
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100
_DEFAULT_SINCE = "7d"

JOURNAL_TYPES: tuple[str, ...] = (
    "decision",
    "landed",
    "applied",
    "started",
    "stopped",
    "incident",
    "escalation",
    "correction",
    "note",
)
_JOURNAL_TYPE_CANONICAL: dict[str, str] = {name: name for name in JOURNAL_TYPES}
_SINCE_WINDOWS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_ASCII_UINT_RE = re.compile(r"^[0-9]+$")
_PORT_RE = re.compile(r"^[1-9][0-9]{0,4}$")
_ORIGIN_SCHEME_RE = re.compile(r"^(?:http|https)://(.+)$", re.IGNORECASE)
_RFC3339_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(?:([Zz])|([+-])([0-9]{2}):([0-9]{2}))$"
)
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_loopback_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    # ipaddress.{IPv4,IPv6}Address.is_loopback is a boolean property, not a method.
    return address.is_loopback or bool(mapped and mapped.is_loopback)  # nosemgrep: python.lang.maintainability.is-function-without-parentheses


def _valid_port(raw_port: str) -> bool:
    return bool(_PORT_RE.fullmatch(raw_port)) and int(raw_port) <= 65535


def _is_loopback_authority(raw: str) -> bool:
    """Validate one raw HTTP authority (``host`` or ``host:port``) as strictly loopback."""
    if not raw or any(ord(ch) <= 0x20 or ord(ch) >= 0x7F or ch in "/?#@\\%" for ch in raw):
        return False
    if raw.startswith("["):
        end_bracket = raw.find("]")
        if end_bracket <= 1:
            return False
        host_part = raw[1:end_bracket]
        rest = raw[end_bracket + 1 :]
        if rest and (not rest.startswith(":") or not _valid_port(rest[1:])):
            return False
        try:
            address = ipaddress.IPv6Address(host_part)
        except ValueError:
            return False
        if address.scope_id is not None:
            return False
        return _is_loopback_ip(address)

    if "]" in raw:
        return False
    if ":" in raw:
        host_part, sep, port_part = raw.partition(":")
        if not sep or ":" in port_part or not _valid_port(port_part):
            return False
    else:
        host_part = raw

    if host_part.lower() == "localhost":
        return True
    try:
        ipv4 = ipaddress.IPv4Address(host_part)
    except ValueError:
        return False
    return _is_loopback_ip(ipv4)


def _host_is_allowed(request: Request) -> bool:
    """Require exactly one valid loopback Host header (never rely on Starlette's server fallback)."""
    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        return False
    return _is_loopback_authority(hosts[0])


def _origin_is_allowed(request: Request) -> bool:
    """Allow requests with no Origin header or a single strict loopback http(s) Origin."""
    origins = request.headers.getlist("origin")
    if not origins:
        return True
    if len(origins) != 1:
        return False
    match = _ORIGIN_SCHEME_RE.fullmatch(origins[0])
    if match is None:
        return False
    return _is_loopback_authority(match.group(1))


def _request_is_allowed(request: Request) -> bool:
    return _direct_loopback_peer(request) and _host_is_allowed(request) and _origin_is_allowed(request)


def _is_private_repo(repo: FleetRepo) -> bool:
    if repo.visibility is not None:
        return repo.visibility != "public"
    return not repo.role.startswith("public-")


def _project_catalog() -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = [{"key": "fleet", "label": "Fleet", "private": False}]
    try:
        repos = load_fleet_repos()
    except FleetRepoError:
        return projects
    for repo in repos.values():
        projects.append(
            {
                "key": repo.key,
                "label": repo.local_name,
                "private": _is_private_repo(repo),
            }
        )
    return projects


def _parse_rfc3339(value: str) -> datetime | None:
    match = _RFC3339_RE.fullmatch(value)
    if match is None:
        return None
    year_s, month_s, day_s, hour_s, minute_s, second_s, frac_s, z_flag, sign, tz_h_s, tz_m_s = match.groups()
    if z_flag is None:
        tz_h, tz_m = int(tz_h_s), int(tz_m_s)
        if tz_h > 23 or tz_m > 59:
            return None
        tz_part = f"{sign}{tz_h_s}:{tz_m_s}"
    else:
        tz_part = "+00:00"
    frac_part = ""
    if frac_s is not None:
        frac_part = "." + frac_s[:6].ljust(6, "0")
    canonical = f"{year_s}-{month_s}-{day_s}T{hour_s}:{minute_s}:{second_s}{frac_part}{tz_part}"
    try:
        parsed = datetime.fromisoformat(canonical)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _parse_bounded_int(
    raw: str | None, *, default: int | None, minimum: int, maximum: int
) -> tuple[bool, int | None]:
    if raw is None:
        return True, default
    if not _ASCII_UINT_RE.fullmatch(raw):
        return False, None
    try:
        value = int(raw)
    except ValueError:
        return False, None
    if value < minimum or value > maximum:
        return False, None
    return True, value


def _resolve_cli() -> str | None:
    raw = os.environ.get(DRIVER_STATE_CLI_ENV)
    if not raw or not raw.strip() or raw != raw.strip():
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    return shutil.which(raw)


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"unexpected JSON constant: {token}")


def _is_utf8_str(value: Any) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validate_safe_url(value: Any) -> str | None:
    if not _is_utf8_str(value) or not value or value != value.strip():
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        return None
    return value


def _is_positive_int(value: Any) -> TypeGuard[int]:
    return type(value) is int and 1 <= value <= _INT64_MAX


def _is_non_negative_int(value: Any) -> TypeGuard[int]:
    return type(value) is int and 0 <= value <= _INT64_MAX


def _validate_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    entry_id = raw.get("id")
    occurred_at = panel_values.timestamp(raw.get("occurred_at"))
    recorded_at = panel_values.timestamp(raw.get("recorded_at"))
    project = raw.get("project")
    entry_type = raw.get("type")
    headline = raw.get("headline")
    body = raw.get("body")
    links_raw = raw.get("links")
    author_seat = raw.get("author_seat")
    source_key = raw.get("source_key")
    source_item_id = raw.get("source_item_id")
    corrects_id = raw.get("corrects_id")

    if not _is_positive_int(entry_id):
        return None
    if occurred_at is None or recorded_at is None:
        return None
    if not _is_utf8_str(project) or not project or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in project):
        return None
    if not isinstance(entry_type, str) or entry_type not in _JOURNAL_TYPE_CANONICAL:
        return None
    if not _is_utf8_str(headline) or not headline:
        return None
    if not _is_utf8_str(body):
        return None
    if not isinstance(links_raw, list):
        return None
    links: list[dict[str, str]] = []
    for item in links_raw:
        if not isinstance(item, dict):
            return None
        label = item.get("label")
        url = _validate_safe_url(item.get("url"))
        if not _is_utf8_str(label) or not label or url is None:
            return None
        links.append({"label": label, "url": url})
    if not _is_utf8_str(author_seat) or not author_seat:
        return None
    if source_key is not None and not _is_utf8_str(source_key):
        return None
    if source_item_id is not None and not _is_utf8_str(source_item_id):
        return None
    if corrects_id is not None and not _is_positive_int(corrects_id):
        return None

    return {
        "id": entry_id,
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "project": project,
        "type": _JOURNAL_TYPE_CANONICAL[entry_type],
        "headline": headline,
        "body": body,
        "links": links,
        "author_seat": author_seat,
        "source_key": source_key,
        "source_item_id": source_item_id,
        "corrects_id": corrects_id,
    }


def _parse_cli_payload(stdout: str) -> tuple[int, int | None, list[dict[str, Any]]] | None:
    try:
        parsed = json_depth.loads(stdout, max_depth=_JSON_MAX_DEPTH, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, RecursionError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("schema") != RESPONSE_SCHEMA_VERSION:
        return None
    total = parsed.get("total")
    latest_id = parsed.get("latest_id")
    raw_entries = parsed.get("entries")
    if not _is_non_negative_int(total):
        return None
    if latest_id is not None and not _is_positive_int(latest_id):
        return None
    if not isinstance(raw_entries, list):
        return None
    if len(raw_entries) > total:
        return None
    if raw_entries and latest_id is None:
        return None

    entries: list[dict[str, Any]] = []
    for item in raw_entries:
        validated = _validate_entry(item)
        if validated is None:
            return None
        entries.append(validated)
    return total, latest_id, entries


def _build_response(
    *,
    availability: str,
    observed_at: str,
    total: int | None,
    latest_id: int | None,
    limit: int,
    offset: int,
    filters: dict[str, Any],
    projects: list[dict[str, Any]],
    entries: list[dict[str, Any]],
) -> JSONResponse:
    unavailable = availability != "available"
    if unavailable:
        total = None
        latest_id = None
        entries = []
        next_offset = None
    else:
        next_offset = offset + len(entries) if entries and total is not None and offset + len(entries) < total else None

    payload: dict[str, Any] = {
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "availability": availability,
        "observed_at": observed_at,
        "total": total,
        "latest_id": latest_id,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "filters": filters,
        "projects": projects,
        "entries": entries,
    }
    headers = panel_values.snapshot_headers(
        None if unavailable else 0.0,
        JOURNAL_FRESHNESS_BOUND_S,
        unavailable=unavailable,
    )
    return JSONResponse(content=payload, headers=headers)


@router.get("")
def get_journal(
    request: Request,
    project: str | None = Query(default=None),
    type: str | None = Query(default=None),
    since: str | None = Query(default=None),
    after_id: str | None = Query(default=None),
    limit: str | None = Query(default=None),
    offset: str | None = Query(default=None),
) -> JSONResponse:
    """Read newest-first project journal entries via the configured driver-state CLI."""
    if not _request_is_allowed(request):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"}, headers=_NO_STORE_HEADERS)

    for param_name in ("project", "type", "since", "after_id", "limit", "offset"):
        if len(request.query_params.getlist(param_name)) > 1:
            return JSONResponse(
                status_code=422,
                content={"detail": f"duplicate_{param_name}"},
                headers=_NO_STORE_HEADERS,
            )

    projects = _project_catalog()
    allowed_projects: dict[str, str] = {item["key"]: item["key"] for item in projects}

    canonical_project: str | None = None
    if project is not None:
        canonical_project = allowed_projects.get(project)
        if canonical_project is None:
            return JSONResponse(status_code=422, content={"detail": "unknown_project"}, headers=_NO_STORE_HEADERS)

    canonical_type: str | None = None
    if type is not None:
        canonical_type = _JOURNAL_TYPE_CANONICAL.get(type)
        if canonical_type is None:
            return JSONResponse(status_code=422, content={"detail": "unknown_type"}, headers=_NO_STORE_HEADERS)

    now = datetime.now(UTC)
    observed_at = _isoformat_z(now)

    since_filter: str
    since_cli_arg: str | None
    if since is None:
        since_filter = _DEFAULT_SINCE
        since_cli_arg = _isoformat_z(now - _SINCE_WINDOWS[_DEFAULT_SINCE])
    elif since == "all":
        since_filter = "all"
        since_cli_arg = None
    elif since in _SINCE_WINDOWS:
        since_filter = since
        since_cli_arg = _isoformat_z(now - _SINCE_WINDOWS[since])
    else:
        parsed_since = _parse_rfc3339(since)
        if parsed_since is None:
            return JSONResponse(status_code=422, content={"detail": "invalid_since"}, headers=_NO_STORE_HEADERS)
        since_filter = _isoformat_z(parsed_since)
        since_cli_arg = since_filter

    ok_after, parsed_after_id = _parse_bounded_int(after_id, default=None, minimum=0, maximum=_INT64_MAX)
    if not ok_after:
        return JSONResponse(status_code=422, content={"detail": "invalid_after_id"}, headers=_NO_STORE_HEADERS)
    ok_limit, parsed_limit = _parse_bounded_int(limit, default=_DEFAULT_LIMIT, minimum=1, maximum=_MAX_LIMIT)
    if not ok_limit or parsed_limit is None:
        return JSONResponse(status_code=422, content={"detail": "invalid_limit"}, headers=_NO_STORE_HEADERS)
    ok_offset, parsed_offset = _parse_bounded_int(offset, default=0, minimum=0, maximum=_MAX_OFFSET)
    if not ok_offset or parsed_offset is None:
        return JSONResponse(status_code=422, content={"detail": "invalid_offset"}, headers=_NO_STORE_HEADERS)

    filters = {
        "project": canonical_project,
        "type": canonical_type,
        "since": since_filter,
        "after_id": parsed_after_id,
    }

    cli = _resolve_cli()
    if cli is None:
        return _build_response(
            availability="cli_unavailable",
            observed_at=observed_at,
            total=None,
            latest_id=None,
            limit=parsed_limit,
            offset=parsed_offset,
            filters=filters,
            projects=projects,
            entries=[],
        )

    cmd = [cli, "journal", "list", "--json", "--limit", str(parsed_limit), "--offset", str(parsed_offset)]
    if canonical_project is not None:
        cmd.extend(["--project", canonical_project])
    if canonical_type is not None:
        cmd.extend(["--type", canonical_type])
    if since_cli_arg is not None:
        cmd.extend(["--since", since_cli_arg])
    if parsed_after_id is not None:
        cmd.extend(["--after-id", str(parsed_after_id)])

    try:
        completed = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=JOURNAL_CLI_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return _build_response(
            availability="cli_unavailable",
            observed_at=observed_at,
            total=None,
            latest_id=None,
            limit=parsed_limit,
            offset=parsed_offset,
            filters=filters,
            projects=projects,
            entries=[],
        )

    if completed.returncode == 3:
        return _build_response(
            availability="db_unavailable",
            observed_at=observed_at,
            total=None,
            latest_id=None,
            limit=parsed_limit,
            offset=parsed_offset,
            filters=filters,
            projects=projects,
            entries=[],
        )

    if completed.returncode != 0:
        return _build_response(
            availability="cli_unavailable",
            observed_at=observed_at,
            total=None,
            latest_id=None,
            limit=parsed_limit,
            offset=parsed_offset,
            filters=filters,
            projects=projects,
            entries=[],
        )

    parsed_payload = _parse_cli_payload(completed.stdout)
    if parsed_payload is None:
        return _build_response(
            availability="invalid_payload",
            observed_at=observed_at,
            total=None,
            latest_id=None,
            limit=parsed_limit,
            offset=parsed_offset,
            filters=filters,
            projects=projects,
            entries=[],
        )

    total, latest_id, entries = parsed_payload
    return _build_response(
        availability="available",
        observed_at=observed_at,
        total=total,
        latest_id=latest_id,
        limit=parsed_limit,
        offset=parsed_offset,
        filters=filters,
        projects=projects,
        entries=entries,
    )
