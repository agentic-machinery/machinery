"""Unified, read-only observer for the durable fleet communications plane.

Health and overview read the resolved authority through read-only storage
handles. SQLite-shaped detail routes refuse PostgreSQL explicitly. No read
initializes schema, writes a receipt, invokes an agent, or retrieves artifacts.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from lu_machinery.agent_runtime.accounting import recorded_model_accounting_fields
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.control_plane import storage
from lu_machinery.control_plane.health import read_failure_code
from lu_machinery.fleet_comms import message_plane
from lu_machinery.fleet_comms.endpoints import load_endpoint_registry
from lu_machinery.fleet_comms.message_plane import read_plane_status
from lu_machinery.fleet_comms.migrations import MIGRATIONS
from lu_machinery.fleet_comms.paths import (
    RETIRED_LOCAL_PLANE_MESSAGE,
    PlaneRootAnchorError,
    local_plane_is_retired,
    validate_plane_root,
)
from lu_machinery.guardrails.worktree_containment import (
    NotAGitRepositoryError,
    resolve_main_root_without_git,
)
from lu_machinery.lib.readonly_sqlite import SQLiteConnection, is_sqlite_connection, open_readonly

from .monitor_context import MonitorContext, get_ctx, resolve_context
from .panel_values import snapshot_headers
from .product_contribution import fleet_batches
from .runtime_router import get_acp_conversation, list_acp_conversations, recent_runtime_records
from .state_helpers import reports_failure, swr_route

_DATABASE_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error,)
_pg_conninfo_to_dict: Callable[[str], dict[str, Any]] | None = None
try:
    from psycopg import Error as PostgresError
    from psycopg.conninfo import conninfo_to_dict as _pg_conninfo_to_dict
except ModuleNotFoundError as exc:
    if exc.name != "psycopg":
        raise
else:
    _DATABASE_ERRORS += (PostgresError,)
_OVERVIEW_ERRORS: tuple[type[Exception], ...] = (*_DATABASE_ERRORS, storage.ControlPlaneError)

router = APIRouter(tags=["fleet"])
# Fleet read models served stale-while-revalidate (``state_helpers.swr_route``):
# a read gets the last projection with its true age and an older one queues
# one refresh. Each projection reads the whole plane or task store.
FACADE_TTL_S = 15.0
logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_DETAIL_ROWS = 100
MAX_ACP_SCAN = 500
MAX_ACTIVITY_SCAN = 500
MAX_OPERATIONS_ITEMS = 50
AUTHORITY_HEALTH_WINDOW_HOURS = 24

_BATCH_HEALTH = frozenset({"complete", "healthy", "stalled", "dead", "unknown"})
_TRACK_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_PROBE_ERROR = re.compile(r"^ps: [A-Za-z][A-Za-z0-9_]{0,63}(?: \d{1,6})?$")
_SAFE_FAILURE_PHASES = frozenset(
    {"admission", "transport", "provider", "result_parse", "postprocess"}
)
_SAFE_FAILURE_CODES = frozenset(
    {
        "adapter_refused",
        "conversation_state_missing",
        "primary_cwd_rejected",
        "protocol_output_limit",
        "provider_unavailable",
        "rate_limited",
        "result_invalid",
        "route_effort_conflict",
        "route_model_conflict",
        "timeout",
        "transport_error",
        "unknown",
    }
)

_SAFE_METADATA_KEYS = frozenset(
    {
        "agent",
        "completion_state",
        "harness",
        "legacy_message_id",
        "model",
        "plane_mode",
        "pr_number",
        "source",
        "source_agent",
        "stream_epic",
        "task_family",
        "task_id",
        "transport",
        "transport_mode",
        "via",
    }
)
_PROVENANCE_KEY_ALIASES = {"source": "Source", "agent": "Agent", "via": "Via"}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)\b(\s*[:=]\s*)([^,;\s]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_LITERAL = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,})\b")




def _plane_root(ctx: MonitorContext | None = None) -> Path:
    """Resolve the existing durable plane root without creating it."""
    resolved_ctx = resolve_context(ctx)
    if not os.environ.get(message_plane.ENV_ROOT):
        try:
            base = resolve_main_root_without_git(resolved_ctx.roots.state_root)
        except NotAGitRepositoryError:
            pass
        else:
            try:
                root = validate_plane_root(base / message_plane.DEFAULT_ROOT_REL)
                if local_plane_is_retired(root=root):
                    raise PlaneRootAnchorError(RETIRED_LOCAL_PLANE_MESSAGE)
            except PlaneRootAnchorError:
                raise
            except (OSError, RuntimeError):
                raise PlaneRootAnchorError("plane_root_unresolvable") from None
            return root
    return message_plane.default_plane_root(repo_root=resolved_ctx.roots.state_root)


def _plane_db_path(ctx: MonitorContext | None = None) -> Path:
    return _plane_root(ctx) / "comms.sqlite3"


@contextmanager
def _read_connection_with_authority(
    ctx: MonitorContext | None = None,
    *,
    pg_supported: bool = False,
    plane_root: Path | None = None,
) -> Iterator[tuple[Any | None, str, storage.Authority]]:
    """Yield a query-only connection, availability, and resolved authority."""
    resolved_ctx = resolve_context(ctx)
    db_path = (
        plane_root / "comms.sqlite3"
        if plane_root is not None
        else _plane_db_path(resolved_ctx)
    )
    authority = storage.resolve_authority(storage.StoreId.FLEET_COMMS)
    if authority is storage.Authority.PG and not pg_supported:
        # Detail routes still use SQLite-shaped SQL. Do not read a stale
        # local file or mistake this component limit for database absence.
        yield None, "authority_unsupported_component", authority
        return
    if authority is not storage.Authority.PG and not db_path.is_file():
        yield None, "db_missing", authority
        return

    connection = None
    availability = "available"
    try:
        connection = (
            storage.connect(storage.StoreId.FLEET_COMMS, path=db_path, read_only=True, timeout=0.25)
            if authority is storage.Authority.PG
            else open_readonly(db_path, timeout=0.25)
        )
        if authority is storage.Authority.PG:
            connection.autocommit = True
            connection.execute("SET statement_timeout = 2000")
        else:
            connection.row_factory = sqlite3.Row
    except Exception as exc:
        availability = read_failure_code(exc, authority)
    try:
        yield connection if availability == "available" else None, availability, authority
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.warning("Fleet observer read-only connection close failed")


@contextmanager
def _read_connection(
    ctx: MonitorContext | None = None,
    *,
    pg_supported: bool = False,
    plane_root: Path | None = None,
) -> Iterator[tuple[Any | None, str]]:
    """Yield a query-only connection, never creating a database or schema."""
    with _read_connection_with_authority(
        ctx,
        pg_supported=pg_supported,
        plane_root=plane_root,
    ) as (connection, availability, _authority):
        yield connection, availability


def _table_exists(connection: Any, table: str) -> bool:
    query = (
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
        if is_sqlite_connection(connection)
        else "SELECT 1 FROM pg_class WHERE oid = to_regclass(%s) AND relkind IN ('r', 'p')"
    )
    row = connection.execute(query, (table,)).fetchone()
    return row is not None


def _normalize_time(value: str | None, label: str) -> str | None:
    """Normalize an ISO-8601 filter to lexical UTC form used by fleet rows."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_clauses(
    column: str,
    *,
    since: str | None,
    until: str | None,
    clauses: list[str],
    params: list[Any],
) -> None:
    if since is not None:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until is not None:
        clauses.append(f"{column} <= ?")
        params.append(until)


def _safe_text(value: Any, *, limit: int = 160, fallback: str = "unknown") -> str:
    """Return a compact display label without reflecting arbitrary multiline data."""
    if value is None:
        return fallback
    text = " ".join(str(value).split())
    if not text:
        return fallback
    return text[:limit]




def _safe_metadata(value: Any) -> dict[str, Any]:
    """Allowlist terse metadata fields rather than exposing arbitrary JSON."""
    if not isinstance(value, str) or not value:
        return {}
    try:
        raw = json_depth.loads(value, max_depth=512)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    metadata: dict[str, Any] = {}
    for key in sorted(_SAFE_METADATA_KEYS):
        alias = _PROVENANCE_KEY_ALIASES.get(key)
        item = raw.get(alias) if alias in raw else raw.get(key)
        if isinstance(item, str):
            metadata[key] = _safe_text(item, limit=120, fallback="")
        elif isinstance(item, (int, bool)):
            metadata[key] = item
    return metadata


def _first_label(*values: Any, fallback: str) -> str:
    for value in values:
        text = _safe_text(value, fallback="")
        if text:
            return text
    return fallback


def _provenance(
    *,
    metadata: dict[str, Any] | None = None,
    source: Any = None,
    agent: Any = None,
    via: Any = None,
) -> dict[str, str]:
    """Normalize the Source / Agent / Via triplet used throughout the observer."""
    metadata = metadata or {}
    return {
        "source": _first_label(
            source,
            metadata.get("source"),
            metadata.get("source_agent"),
            fallback="fleet-comms",
        ),
        "agent": _first_label(metadata.get("agent"), agent, fallback="unknown"),
        "via": _first_label(
            metadata.get("via"),
            metadata.get("transport_mode"),
            metadata.get("transport"),
            metadata.get("harness"),
            via,
            fallback="fleet-comms",
        ),
    }


def _collection(
    key: str,
    items: list[dict[str, Any]],
    *,
    total: int,
    limit: int,
    offset: int,
    availability: str,
    filters: dict[str, Any],
) -> dict[str, Any]:
    next_offset = offset + len(items) if offset + len(items) < total else None
    return {
        "read_only": True,
        "availability": availability,
        "db_exists": availability not in {"db_missing", "db_unavailable"},
        key: items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "filters": filters,
    }


def _empty_collection(
    key: str,
    *,
    limit: int,
    offset: int,
    availability: str,
    filters: dict[str, Any],
) -> dict[str, Any]:
    return _collection(
        key,
        [],
        total=0,
        limit=limit,
        offset=offset,
        availability=availability,
        filters=filters,
    )


def _paged_query(
    connection: SQLiteConnection,
    *,
    key: str,
    select_sql: str,
    from_sql: str,
    clauses: list[str],
    params: list[Any],
    order_sql: str,
    limit: int,
    offset: int,
    filters: dict[str, Any],
    transform: Callable[[sqlite3.Row], dict[str, Any]],
) -> dict[str, Any]:
    where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        total_row = connection.execute(f"SELECT COUNT(*){from_sql}{where_sql}", params).fetchone()
        total = int(total_row[0]) if total_row is not None else 0
        query = f"{select_sql}{from_sql}{where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?"
        rows = connection.execute(query, [*params, limit, offset]).fetchall()
    except sqlite3.Error:
        logger.warning("Fleet observer query failed")
        return _empty_collection(
            key,
            limit=limit,
            offset=offset,
            availability="db_unavailable",
            filters=filters,
        )
    return _collection(
        key,
        [transform(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        availability="available" if total else "empty",
        filters=filters,
    )


def _safe_plane_status(
    ctx: MonitorContext | None = None,
    *,
    plane_root: Path | None = None,
) -> dict[str, Any]:
    """Project the existing plane read model without paths or raw telemetry rows."""
    resolved_ctx = resolve_context(ctx)
    status = (
        read_plane_status(root=plane_root, recent_limit=0)
        if plane_root is not None
        else read_plane_status(repo_root=resolved_ctx.roots.state_root, recent_limit=0)
    )
    mode = _safe_text(status.get("mode"), fallback="invalid")
    authority_active = mode == "authority"
    schema = status.get("schema") if isinstance(status.get("schema"), dict) else {}
    telemetry = (
        status.get("parity_telemetry")
        if isinstance(status.get("parity_telemetry"), dict)
        else {}
    )
    return {
        "mode": mode,
        "enabled": bool(status.get("enabled")),
        "read_only": True,
        "response_schema_version": status.get("response_schema_version"),
        "store": status.get("store"),
        "authority": (
            "fleet_comms_authoritative" if authority_active else "file_handoffs_authoritative"
        ),
        "cutover": "authority_active" if authority_active else "pre_flip_operator_gated",
        "schema": {
            "known_version": schema.get("known_version"),
            "applied_version": schema.get("applied_version"),
            "applied_name": schema.get("applied_name"),
            "db_exists": schema.get("db_exists"),
            "authority": schema.get("authority"),
            "db_error": schema.get("db_error"),
            "store": schema.get("store"),
        },
        "parity_telemetry": {
            "store": telemetry.get("store"),
            "exists": bool(telemetry.get("exists")),
            "event_count": int(telemetry.get("event_count") or 0),
            "parity_ok_count": int(telemetry.get("parity_ok_count") or 0),
            "parity_fail_count": int(telemetry.get("parity_fail_count") or 0),
        },
    }








# The bottleneck slice is lifecycle statistics over every task record: a few seconds
# old is fine, and a cold scan of today's ledger outlasts the request timeout. Serve it
# stale-while-revalidate; the stored copy is refreshed by one background flight.
BOARD_BOTTLENECK_TTL_S = 60.0
# A cold process waits this long for the first scan, then reports the probe as warming.
BOARD_BOTTLENECK_WAIT_S = 2.0
# ``gh pr list`` is a network round trip on every board; the five newest PRs may lag this much.
BOARD_PR_LIST_TTL_S = 30.0








def _count_by_state(connection: Any, table: str, column: str) -> dict[str, int]:
    if not _table_exists(connection, table):
        return {}
    try:
        rows = connection.execute(
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} ORDER BY {column}"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {_safe_text(row[0], fallback="unknown"): int(row[1]) for row in rows}


def _count_by_state_where(
    connection: Any,
    table: str,
    column: str,
    *,
    where: str,
    params: tuple[Any, ...] = (),
) -> dict[str, int]:
    if not _table_exists(connection, table):
        return {}
    rows = connection.execute(
        f"SELECT {column}, COUNT(*) FROM {table} WHERE {where} "
        f"GROUP BY {column} ORDER BY {column}",
        params,
    ).fetchall()
    return {_safe_text(row[0], fallback="unknown"): int(row[1]) for row in rows}


def _authority_health_window(now: datetime | None = None) -> dict[str, Any]:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    since = observed_at - timedelta(hours=AUTHORITY_HEALTH_WINDOW_HOURS)
    return {
        "hours": AUTHORITY_HEALTH_WINDOW_HOURS,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "until": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _authority_health_payload(
    states: dict[str, int],
    *,
    overdue: int,
    dead_letters: int,
    window: dict[str, Any],
) -> dict[str, Any]:
    total = sum(states.values())
    failures = sum(
        states.get(name, 0) for name in ("failed", "expired", "dead_lettered")
    )
    if failures or overdue or dead_letters:
        state = "degraded"
    elif total:
        state = "healthy"
    else:
        state = "idle"
    return {
        "state": state,
        "ok": state in {"healthy", "idle"},
        "availability": "available",
        "window": window,
        "jobs": {"total": total, "by_state": states},
        "overdue_nonterminal": overdue,
        "dead_letters": dead_letters,
    }


def _authority_health_snapshot(
    connection: Any | None,
    *,
    now: datetime | None = None,
    availability: str = "available",
) -> dict[str, Any]:
    """Derive current health only from a fixed authority evidence window."""
    window = _authority_health_window(now)
    unavailable = {
        "state": "unavailable",
        "ok": False,
        "availability": availability,
        "window": window,
        "jobs": {"total": 0, "by_state": {}},
        "overdue_nonterminal": 0,
        "dead_letters": 0,
    }
    if connection is None:
        return unavailable
    placeholder = "?" if is_sqlite_connection(connection) else "%s"
    try:
        if not _table_exists(connection, "authority_jobs"):
            return {**unavailable, "availability": "schema_read_failed"}
        states = _count_by_state_where(
            connection,
            "authority_jobs",
            "state",
            where=f"updated_at >= {placeholder}",
            params=(window["since"],),
        )
        overdue = int(
            connection.execute(
                f"""SELECT COUNT(*) FROM authority_jobs
                   WHERE state IN ('queued', 'running')
                     AND deadline_at IS NOT NULL AND deadline_at < {placeholder}""",
                (window["until"],),
            ).fetchone()[0]
        )
        dead_letters = 0
        if _table_exists(connection, "authority_dead_letters"):
            dead_letters = int(
                connection.execute(
                    # A deliberate expire-stale cleanup is not a failure (authority.CLEANUP_EXPIRY_REASON).
                    "SELECT COUNT(*) FROM authority_dead_letters"
                    f" WHERE created_at >= {placeholder} AND reason_code <> {placeholder}",
                    (window["since"], "expired_by_cleanup"),
                ).fetchone()[0]
            )
    except _DATABASE_ERRORS:
        logger.warning("Fleet observer could not derive authority health")
        return {**unavailable, "availability": "schema_read_failed"}
    return _authority_health_payload(
        states,
        overdue=overdue,
        dead_letters=dead_letters,
        window=window,
    )


def _runtime_activity_snapshot(since: str, ctx: MonitorContext | None = None) -> dict[str, Any]:
    """Count runtime/delegate ledger records inside the authority evidence window.

    The authority health window counts only fleet-comms ``authority_jobs``.
    Runtime and delegate work is recorded in a separate usage ledger, so a
    "0 jobs" authority window must not be read as "no fleet work today".
    """
    try:
        records = recent_runtime_records(limit=MAX_ACTIVITY_SCAN, ctx=ctx).get("records", [])
    except Exception:
        logger.warning("Fleet observer could not derive runtime activity")
        return {
            "ledger": "runtime_delegate_usage",
            "records_in_window": None,
            "availability": "unavailable",
        }
    total = sum(
        1
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("ts"), str)
        and record["ts"] >= since
    )
    return {
        "ledger": "runtime_delegate_usage",
        "records_in_window": total,
        "availability": "available",
    }


def _non_negative_int(value: Any) -> int:
    """Normalize legacy numeric fields without reflecting arbitrary values."""
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _non_negative_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, round(number, 1))


def _safe_probe_errors(raw: Any) -> list[str]:
    """Keep process-probe failures that name the failure class, not a path or secret."""
    if not isinstance(raw, list):
        return []
    errors: list[str] = []
    for item in raw[:8]:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if _PROBE_ERROR.fullmatch(text):
            errors.append(text)
    return errors


def _safe_batch_projection(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "availability": "unavailable",
            "total": 0,
            "returned": 0,
            "limit": MAX_OPERATIONS_ITEMS,
            "truncated": False,
            "running_processes": 0,
            "by_health": {},
            "tracks": [],
        }

    source = raw.get("tracks")
    source_tracks = source if isinstance(source, dict) else {}
    tracks: list[dict[str, Any]] = []
    by_health: Counter[str] = Counter()
    for raw_track, entry in sorted(source_tracks.items(), key=lambda item: str(item[0])):
        if not isinstance(entry, dict):
            continue
        track = str(raw_track)
        if not _TRACK_LABEL.fullmatch(track):
            track = "unknown"
        health = entry.get("health")
        if health not in _BATCH_HEALTH:
            health = "unknown"
        by_health[health] += 1
        if len(tracks) >= MAX_OPERATIONS_ITEMS:
            continue
        tracks.append(
            {
                "track": track,
                "health": health,
                "total_expected": _non_negative_int(entry.get("total_expected")),
                "research_done": _non_negative_int(entry.get("research_done")),
                "remaining": _non_negative_int(entry.get("remaining")),
                "recent_30min": _non_negative_int(entry.get("recent_30min")),
                "throughput_per_hour": _non_negative_float(
                    entry.get("throughput_per_hour")
                ),
            }
        )

    errors = _safe_probe_errors(raw.get("errors"))
    probe_failed = raw.get("running_processes", 0) is None or bool(errors)
    projected = {
        "availability": "degraded" if probe_failed else "available",
        "total": sum(by_health.values()),
        "returned": len(tracks),
        "limit": MAX_OPERATIONS_ITEMS,
        "truncated": sum(by_health.values()) > len(tracks),
        "running_processes": (
            None
            if raw.get("running_processes", 0) is None
            else _non_negative_int(raw.get("running_processes"))
        ),
        "by_health": dict(sorted(by_health.items())),
        "tracks": tracks,
    }
    if errors:
        projected["errors"] = errors
    return projected


async def _product_batch_snapshot(ctx: MonitorContext) -> dict[str, Any] | None:
    """The product's raw curriculum batch snapshot, or ``None`` when no product supplies one."""
    collect = fleet_batches()
    if collect is None:
        return None
    return await asyncio.to_thread(collect, ctx)


@router.get("/operations")
async def fleet_operations(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """Sanitized curriculum batch operations projection.

    The legacy broker and zombie sections are removed with the legacy store (#515).
    """
    try:
        batch_result = await _product_batch_snapshot(ctx)
    except Exception:  # a failed product collector reads as unavailable, never a 500
        logger.warning("Fleet operations could not collect the product batch snapshot")
        batch_result = None
    batches = _safe_batch_projection(batch_result)
    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "read_only": True,
        "writes_enabled": False,
        "availability": batches["availability"],
        "batches": batches,
    }


@router.get("/health")
def fleet_health(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """Read-only health, mode, schema, and current authority posture."""
    status = _safe_plane_status(ctx)
    with _read_connection(ctx, pg_supported=True) as (connection, availability):
        authority_health = (
            _authority_health_snapshot(connection, availability=availability)
            if status["mode"] == "authority"
            else {
                "state": "not_applicable",
                "ok": True,
                "availability": availability,
                "window": None,
                "jobs": {"total": 0, "by_state": {}},
                "overdue_nonterminal": 0,
                "dead_letters": 0,
            }
        )
    window = authority_health.get("window") or {}
    since = window.get("since")
    if not isinstance(since, str):
        since = (datetime.now(UTC) - timedelta(hours=AUTHORITY_HEALTH_WINDOW_HOURS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    runtime_activity = _runtime_activity_snapshot(since, ctx)
    return {
        "ok": (
            bool(status["enabled"]) and bool(authority_health["ok"])
            and status["store"]["reachable"] is True
            and not status["schema"].get("db_error")
        ),
        "observer": "fleet-comms-v1",
        "read_only": True,
        "writes_enabled": False,
        "authority_health": authority_health,
        "runtime_activity": runtime_activity,
        **status,
    }


OVERVIEW_CACHE_TTL_S = FACADE_TTL_S
_OVERVIEW_CACHE_MAX_ENTRIES = 16
_overview_clock: Callable[[], float] = time.monotonic
_overview_cache_lock = threading.Lock()
# Keyed by live routing state (a routing change produces a distinct key):
# no invalidation, no in-flight join, no waiters.
_overview_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

_SQLITE_OVERVIEW_REQUIRED_TABLE_COUNT = 7
_SQLITE_OVERVIEW_TABLES_SQL = """
SELECT COUNT(*) FROM sqlite_master
WHERE type = 'table' AND name IN (
    'requests',
    'comms_messages',
    'formal_review_jobs',
    'authority_jobs',
    'authority_dead_letters',
    'dead_letters',
    'acp_conversations'
)
"""
_SQLITE_OVERVIEW_COUNTS_SQL = """
SELECT
    (SELECT json_group_array(json_array(k, c))
     FROM (SELECT state AS k, COUNT(*) AS c FROM requests GROUP BY state ORDER BY state)),
    (SELECT json_group_array(json_array(k, c))
     FROM (SELECT state AS k, COUNT(*) AS c FROM authority_jobs WHERE job_kind = 'request' GROUP BY state ORDER BY state)),
    (SELECT json_group_array(json_array(k, c))
     FROM (SELECT kind AS k, COUNT(*) AS c FROM comms_messages GROUP BY kind ORDER BY kind)),
    (SELECT json_group_array(json_array(k, c))
     FROM (SELECT state AS k, COUNT(*) AS c FROM formal_review_jobs GROUP BY state ORDER BY state)),
    (SELECT json_group_array(json_array(k, c))
     FROM (SELECT state AS k, COUNT(*) AS c FROM authority_jobs GROUP BY state ORDER BY state)),
    (SELECT COUNT(*) FROM authority_dead_letters),
    (SELECT COUNT(*) FROM dead_letters),
    (SELECT COUNT(*) FROM acp_conversations),
    (SELECT json_group_array(json_array(k, c))
     FROM (SELECT state AS k, COUNT(*) AS c FROM authority_jobs WHERE updated_at >= ? GROUP BY state ORDER BY state)),
    (SELECT COUNT(*) FROM authority_jobs
     WHERE state IN ('queued', 'running') AND deadline_at IS NOT NULL AND deadline_at < ?),
    (SELECT COUNT(*) FROM authority_dead_letters
     WHERE created_at >= ? AND reason_code <> 'expired_by_cleanup')
"""


def _file_stat_key(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _pg_routing_digest() -> str:
    raw_dsn = (os.environ.get("LEARN_UKRAINIAN_CP_PG_DSN") or "").strip()
    pg_env = tuple(sorted((key, value) for key, value in os.environ.items() if key.startswith("PG")))
    if not raw_dsn and not pg_env:
        return ""
    payload = json.dumps({"dsn": raw_dsn, "pg_env": pg_env}, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _overview_mode_spec() -> tuple[Any, ...]:
    env = os.environ.get(message_plane.ENV_MODE)
    if env is not None and env.strip():
        value = env.strip().lower()
        if value in {"0", "false", "off", "disabled"}:
            return ("env", "off")
        if value in {"dual-write", "dualwrite"}:
            return ("env", "dual_write")
        return ("env", value)
    cfg_path = machinery_path("config/fleet_communications.yaml")
    return ("config", str(cfg_path), _file_stat_key(cfg_path))


def _overview_routing_key(
    ctx: MonitorContext,
    plane_root: Path,
) -> tuple[tuple[Any, ...], storage.Authority]:
    authority = storage.resolve_authority(storage.StoreId.FLEET_COMMS)
    pg_digest = _pg_routing_digest() if authority is storage.Authority.PG else ""
    telemetry_path = message_plane.default_parity_telemetry_path(plane_root)
    allow_shadow = os.environ.get("FLEET_COMMS_ALLOW_LOCAL_SHADOW", "").strip() == "1"
    key = (
        repr(ctx.roots),
        str(plane_root),
        authority.value,
        pg_digest,
        _overview_mode_spec(),
        str(telemetry_path),
        _file_stat_key(telemetry_path),
        allow_shadow,
    )
    return key, authority


def _current_plane_mode() -> str:
    try:
        return str(message_plane.resolve_plane_mode())
    except ValueError:
        return "invalid"


def _overview_is_storable(result: dict[str, Any]) -> bool:
    if reports_failure(result):
        return False
    if result.get("availability") not in {"available", "empty"}:
        return False
    health = result.get("health")
    if not isinstance(health, dict):
        return False
    if health.get("mode") == "invalid":
        return False
    store = health.get("store")
    if not isinstance(store, dict) or store.get("reachable") is not True:
        return False
    schema = health.get("schema")
    return isinstance(schema, dict) and not schema.get("db_error")


def _overview_cache_get(key: tuple[Any, ...]) -> tuple[dict[str, Any], float] | None:
    now = _overview_clock()
    with _overview_cache_lock:
        entry = _overview_cache.get(key)
        if entry is None:
            return None
        age = now - entry[0]
        if age >= OVERVIEW_CACHE_TTL_S:
            return None
        return copy.deepcopy(entry[1]), max(0.0, age)


def _pg_connection_matches_dsn(connection: Any, raw_dsn: str) -> bool:
    info = getattr(connection, "info", None)
    if info is None or not raw_dsn:
        return True
    if _pg_conninfo_to_dict is None:
        return False
    try:
        expected = _pg_conninfo_to_dict(raw_dsn)
    except Exception:
        return False
    for field in ("host", "port", "dbname", "user"):
        want = expected.get(field)
        if want is None:
            continue
        actual = str(getattr(info, field, ""))
        if field in {"host", "port"}:
            allowed = {part.strip() for part in str(want).split(",")}
            if actual not in allowed:
                return False
        elif actual != str(want):
            return False
    return True


def _overview_cache_put(key: tuple[Any, ...], started: float, payload: dict[str, Any]) -> bool:
    now = _overview_clock()
    if (now - started) >= OVERVIEW_CACHE_TTL_S:
        return False
    with _overview_cache_lock:
        expired = [k for k, (ts, _) in _overview_cache.items() if (now - ts) >= OVERVIEW_CACHE_TTL_S]
        for k in expired:
            _overview_cache.pop(k, None)
        existing = _overview_cache.get(key)
        if existing is not None and started < existing[0]:
            return True
        if key not in _overview_cache and len(_overview_cache) >= _OVERVIEW_CACHE_MAX_ENTRIES:
            oldest = min(_overview_cache, key=lambda item: _overview_cache[item][0])
            _overview_cache.pop(oldest, None)
        _overview_cache[key] = (started, copy.deepcopy(payload))
    return True


def _decode_json_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, str) or not raw:
        return {}
    pairs = json_depth.loads(raw, max_depth=8)
    if not isinstance(pairs, list):
        raise ValueError("overview count payload is not a JSON list")
    counts: dict[str, int] = {}
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("overview count entry is not a key/count pair")
        counts[_safe_text(pair[0], fallback="unknown")] = int(pair[1])
    return counts


def _assign_overview_counts(
    result: dict[str, Any],
    *,
    request_states: dict[str, int],
    request_source: str,
    legacy_request_states: dict[str, int],
    message_kinds: dict[str, int],
    review_states: dict[str, int],
    authority_job_states: dict[str, int],
    dead_letters_total: int,
    acp_conversations_total: int,
    authority_health: dict[str, Any] | None,
) -> dict[str, Any]:
    counts = {key: dict(value) for key, value in result["counts"].items()}
    counts["requests"] = {
        "total": sum(request_states.values()),
        "by_state": request_states,
        "source": request_source,
    }
    counts["legacy_requests"] = {
        "total": sum(legacy_request_states.values()),
        "by_state": legacy_request_states,
        "excluded_from_authority_health": True,
    }
    counts["messages"] = {"total": sum(message_kinds.values()), "by_kind": message_kinds}
    counts["reviews"] = {"total": sum(review_states.values()), "by_state": review_states}
    counts["authority_jobs"] = {
        "total": sum(authority_job_states.values()),
        "by_state": authority_job_states,
    }
    counts["dead_letters"]["total"] = dead_letters_total
    counts["acp_conversations"]["total"] = acp_conversations_total
    result["counts"] = counts
    result["availability"] = "available" if any(
        section["total"] for section in counts.values()
    ) else "empty"
    result["authority_health"] = authority_health
    return result


def _populate_fleet_overview_sqlite(
    connection: Any,
    result: dict[str, Any],
    status: dict[str, Any],
) -> bool:
    """Fast single-row SQLite collector avoiding per-row GIL handoffs."""
    table_count_row = connection.execute(_SQLITE_OVERVIEW_TABLES_SQL).fetchone()
    if table_count_row is None or int(table_count_row[0]) != _SQLITE_OVERVIEW_REQUIRED_TABLE_COUNT:
        return False
    window = _authority_health_window()
    row = connection.execute(
        _SQLITE_OVERVIEW_COUNTS_SQL,
        (window["since"], window["until"], window["since"]),
    ).fetchone()
    if row is None:
        return False
    legacy_request_states = _decode_json_counts(row[0])
    if status["mode"] == "authority":
        request_states = _decode_json_counts(row[1])
        request_source = "authority_jobs"
        dead_letters_total = int(row[5] or 0)
        authority_health: dict[str, Any] | None = _authority_health_payload(
            _decode_json_counts(row[8]),
            overdue=int(row[9] or 0),
            dead_letters=int(row[10] or 0),
            window=window,
        )
    else:
        request_states = legacy_request_states
        request_source = "legacy_requests"
        dead_letters_total = int(row[6] or 0)
        authority_health = None
    _assign_overview_counts(
        result,
        request_states=request_states,
        request_source=request_source,
        legacy_request_states=legacy_request_states,
        message_kinds=_decode_json_counts(row[2]),
        review_states=_decode_json_counts(row[3]),
        authority_job_states=_decode_json_counts(row[4]),
        dead_letters_total=dead_letters_total,
        acp_conversations_total=int(row[7] or 0),
        authority_health=authority_health,
    )
    return True


@router.get("/overview")
def fleet_overview(
    response: Response,
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Compact durable-plane counts for the consolidated fleet dashboard."""
    resolved_ctx = resolve_context(ctx)
    plane_root = _plane_root(resolved_ctx)
    routing_key, expected_authority = _overview_routing_key(resolved_ctx, plane_root)
    expected_pg_dsn = (
        (os.environ.get("LEARN_UKRAINIAN_CP_PG_DSN") or "").strip()
        if expected_authority is storage.Authority.PG
        else ""
    )
    cached = _overview_cache_get(routing_key)
    if cached is not None:
        payload, age = cached
        response.headers.update(snapshot_headers(age, OVERVIEW_CACHE_TTL_S))
        return payload

    started = _overview_clock()
    status = _safe_plane_status(resolved_ctx, plane_root=plane_root)
    result: dict[str, Any] = {
        "read_only": True,
        "authority": status["authority"],
        "cutover": status["cutover"],
        "health": status,
        "counts": {
            "requests": {"total": 0, "by_state": {}},
            "legacy_requests": {"total": 0, "by_state": {}},
            "messages": {"total": 0, "by_kind": {}},
            "reviews": {"total": 0, "by_state": {}},
            "dead_letters": {"total": 0},
            "acp_conversations": {"total": 0},
            "authority_jobs": {"total": 0, "by_state": {}},
        },
        "availability": "db_missing",
    }
    conn_authority: storage.Authority | None = None
    pg_conn_ok = True
    try:
        result, conn_authority, pg_conn_ok = _populate_fleet_overview(
            result,
            status,
            resolved_ctx,
            plane_root=plane_root,
            expected_pg_dsn=expected_pg_dsn,
        )
    except (*_OVERVIEW_ERRORS, ValueError, json.JSONDecodeError):
        # Never expose a driver exception or leave partial counts looking complete.
        result["availability"] = "schema_read_failed"

    stored = False
    if _overview_is_storable(result) and conn_authority is expected_authority and pg_conn_ok:
        status_authority = status.get("schema", {}).get("authority")
        if status_authority == expected_authority.value and status.get("mode") == _current_plane_mode():
            try:
                post_root = _plane_root(resolved_ctx)
                post_key, _ = _overview_routing_key(resolved_ctx, post_root)
            except (PlaneRootAnchorError, storage.ControlPlaneError):
                post_key = ()
            if post_key == routing_key:
                stored = _overview_cache_put(routing_key, started, result)

    age = max(0.0, _overview_clock() - started)
    response.headers.update(snapshot_headers(age, OVERVIEW_CACHE_TTL_S, unavailable=not stored))
    return result


def _populate_fleet_overview(
    result: dict[str, Any],
    status: dict[str, Any],
    ctx: MonitorContext,
    *,
    plane_root: Path | None = None,
    expected_pg_dsn: str = "",
) -> tuple[dict[str, Any], storage.Authority, bool]:
    with _read_connection_with_authority(
        ctx,
        pg_supported=True,
        plane_root=plane_root,
    ) as (connection, availability, authority):
        if connection is None:
            result["availability"] = availability
            return result, authority, True
        pg_conn_ok = (
            _pg_connection_matches_dsn(connection, expected_pg_dsn)
            if authority is storage.Authority.PG
            else True
        )
        if is_sqlite_connection(connection) and _populate_fleet_overview_sqlite(connection, result, status):
            return result, authority, pg_conn_ok
        legacy_request_states = _count_by_state(connection, "requests", "state")
        if status["mode"] == "authority":
            request_states = _count_by_state_where(
                connection,
                "authority_jobs",
                "state",
                where="job_kind = 'request'",
            )
            request_source = "authority_jobs"
        else:
            request_states = legacy_request_states
            request_source = "legacy_requests"
        dead_letter_table = (
            "authority_dead_letters" if status["mode"] == "authority" else "dead_letters"
        )
        dead_letters_total = (
            int(connection.execute(f"SELECT COUNT(*) FROM {dead_letter_table}").fetchone()[0])
            if _table_exists(connection, dead_letter_table)
            else 0
        )
        acp_conversations_total = (
            int(connection.execute("SELECT COUNT(*) FROM acp_conversations").fetchone()[0])
            if _table_exists(connection, "acp_conversations")
            else 0
        )
        _assign_overview_counts(
            result,
            request_states=request_states,
            request_source=request_source,
            legacy_request_states=legacy_request_states,
            message_kinds=_count_by_state(connection, "comms_messages", "kind"),
            review_states=_count_by_state(connection, "formal_review_jobs", "state"),
            authority_job_states=_count_by_state(connection, "authority_jobs", "state"),
            dead_letters_total=dead_letters_total,
            acp_conversations_total=acp_conversations_total,
            authority_health=(
                _authority_health_snapshot(connection)
                if status["mode"] == "authority"
                else None
            ),
        )
    return result, authority, pg_conn_ok


def _registered_endpoints(connection: SQLiteConnection | None) -> dict[str, dict[str, Any]]:
    if connection is None or not _table_exists(connection, "agent_endpoints"):
        return {}
    try:
        rows = connection.execute(
            """
            SELECT canonical_name, registry_version, state, successor, created_at
            FROM agent_endpoints
            ORDER BY canonical_name ASC
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        _safe_text(row["canonical_name"]): {
            "registry_version": row["registry_version"],
            "state": _safe_text(row["state"]),
            "successor": _safe_text(row["successor"], fallback="") or None,
            "created_at": row["created_at"],
        }
        for row in rows
    }


@router.get("/agents")
@router.get("/endpoints")
def fleet_agents(
    agent: str | None = Query(default=None),
    state: str | None = Query(default=None),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Configured and durable endpoint inventory with configuration secrets omitted."""
    with _read_connection(ctx) as (connection, availability):
        registered = _registered_endpoints(connection)
    try:
        registry = load_endpoint_registry()
    except (OSError, ValueError):
        logger.warning("Fleet observer could not load the endpoint registry")
        registry = None

    endpoints: list[dict[str, Any]] = []
    if registry is not None:
        for endpoint in registry.endpoints:
            durable = registered.pop(endpoint.name, None)
            item = {
                "agent": endpoint.name,
                "aliases": list(endpoint.aliases),
                "state": durable["state"] if durable else endpoint.state,
                "successor": durable["successor"] if durable else endpoint.successor,
                "registry_version": registry.version,
                "transports": list(endpoint.transports),
                "completion_evidence": list(endpoint.completion_evidence),
                "default_ttl_seconds": endpoint.default_ttl_seconds,
                "concurrency_limit": endpoint.concurrency_limit,
                "formal_review_eligible": endpoint.formal_review_eligible,
                "durable_registration": durable is not None,
                "created_at": durable["created_at"] if durable else None,
                "read_only": True,
            }
            endpoints.append(item)
    for name, durable in registered.items():
        endpoints.append(
            {
                "agent": name,
                "aliases": [],
                "state": durable["state"],
                "successor": durable["successor"],
                "registry_version": durable["registry_version"],
                "transports": [],
                "completion_evidence": [],
                "default_ttl_seconds": None,
                "concurrency_limit": None,
                "formal_review_eligible": False,
                "durable_registration": True,
                "created_at": durable["created_at"],
                "read_only": True,
            }
        )
    if agent is not None:
        endpoints = [
            item for item in endpoints if agent == item["agent"] or agent in item["aliases"]
        ]
    if state is not None:
        endpoints = [item for item in endpoints if state == item["state"]]
    endpoints.sort(key=lambda item: str(item["agent"]))
    return {
        "read_only": True,
        "availability": availability if registry is None else "available",
        "registry_version": registry.version if registry is not None else None,
        "endpoints": endpoints,
        "total": len(endpoints),
        "filters": {"agent": agent, "state": state},
        "configuration_policy": "configuration_json_omitted",
    }


def _request_item(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    metadata = _safe_metadata(data.get("invocation_spec_json"))
    authority_provenance = _safe_metadata(data.get("authority_provenance_json"))
    metadata = {**metadata, **authority_provenance}
    provenance = _provenance(
        metadata=metadata,
        source=authority_provenance.get("source") or data.get("conversation_source"),
        agent=data.get("sender") or data.get("resolved_recipient"),
    )
    return {
        "request_id": data["request_id"],
        "request_message_id": data["request_message_id"],
        "conversation_id": data.get("conversation_id"),
        "requested_recipient": data["requested_recipient"],
        "resolved_recipient": data["resolved_recipient"],
        "state": data["state"],
        "completion_state": data["completion_state"],
        "expires_at": data["expires_at"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "metadata": metadata,
        "read_only": True,
        **provenance,
    }


@router.get("/requests")
def fleet_requests(
    kind: str | None = Query(default=None),
    state: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    source: str | None = Query(default=None),
    conversation: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Queued, in-flight, and terminal request projection without request bodies."""
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "kind": kind,
        "state": state,
        "agent": agent,
        "source": source,
        "conversation": conversation,
        "since": since_value,
        "until": until_value,
    }
    with _read_connection(ctx) as (connection, availability):
        if connection is None:
            return _empty_collection(
                "requests", limit=limit, offset=offset, availability=availability, filters=filters
            )
        if not _table_exists(connection, "requests"):
            return _empty_collection(
                "requests", limit=limit, offset=offset, availability="table_missing", filters=filters
            )
        has_messages = _table_exists(connection, "comms_messages")
        has_conversations = _table_exists(connection, "conversations")
        has_authority_metadata = _table_exists(connection, "authority_message_metadata")
        select_extras = [
            "NULL AS conversation_id",
            "NULL AS sender",
            "NULL AS conversation_source",
            "NULL AS authority_provenance_json",
        ]
        from_sql = " FROM requests AS request"
        if has_messages:
            select_extras[:2] = [
                "message.conversation_id AS conversation_id",
                "message.sender AS sender",
            ]
            from_sql += " LEFT JOIN comms_messages AS message ON message.message_id = request.request_message_id"
        if has_messages and has_conversations:
            select_extras[2] = "conversation.source AS conversation_source"
            from_sql += " LEFT JOIN conversations AS conversation ON conversation.conversation_id = message.conversation_id"
        if has_messages and has_authority_metadata:
            select_extras[3] = "authority_meta.provenance_json AS authority_provenance_json"
            from_sql += (
                " LEFT JOIN authority_message_metadata AS authority_meta"
                " ON authority_meta.message_id = message.message_id"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("request.state = ?")
            params.append(state)
        if kind is not None:
            if has_messages:
                clauses.append("message.kind = ?")
                params.append(kind)
            elif kind != "request":
                clauses.append("1 = 0")
        if agent is not None:
            agent_clause = "request.requested_recipient = ? OR request.resolved_recipient = ?"
            params.extend([agent, agent])
            if has_messages:
                agent_clause += " OR message.sender = ? OR message.recipient = ?"
                params.extend([agent, agent])
            if has_messages and has_authority_metadata:
                agent_clause += " OR json_extract(authority_meta.provenance_json, '$.Agent') = ?"
                params.append(agent)
            clauses.append(f"({agent_clause})")
        if source is not None:
            if has_messages and has_conversations:
                source_expression = "conversation.source"
                if has_authority_metadata:
                    source_expression = (
                        "COALESCE(json_extract(authority_meta.provenance_json, '$.Source'), "
                        f"{source_expression})"
                    )
                clauses.append(f"{source_expression} = ?")
                params.append(source)
            else:
                clauses.append("1 = 0")
        if conversation is not None:
            if has_messages:
                clauses.append("message.conversation_id = ?")
                params.append(conversation)
            else:
                clauses.append("1 = 0")
        _time_clauses(
            "request.created_at",
            since=since_value,
            until=until_value,
            clauses=clauses,
            params=params,
        )
        return _paged_query(
            connection,
            key="requests",
            select_sql=(
                "SELECT request.request_id, request.request_message_id, request.requested_recipient, "
                "request.resolved_recipient, request.state, request.expires_at, request.completion_state, "
                "request.invocation_spec_json, request.created_at, request.updated_at, "
                + ", ".join(select_extras)
            ),
            from_sql=from_sql,
            clauses=clauses,
            params=params,
            order_sql="request.created_at DESC, request.request_id ASC",
            limit=limit,
            offset=offset,
            filters=filters,
            transform=_request_item,
        )


def _authority_job_item(row: sqlite3.Row) -> dict[str, Any]:
    """Project a durable authority job without its payload or result artifacts."""
    data = dict(row)
    metadata = _safe_metadata(data.get("authority_provenance_json"))
    job_kind = _safe_text(data.get("job_kind"), fallback="unknown")
    default_source = {
        "request": "authority",
        "discussion": "authority-discussion",
        "formal_review": "formal-review",
    }.get(job_kind, "authority")
    default_agent = "review-gate" if job_kind == "formal_review" else "authority-service"
    conversation_id = data.get("conversation_id")
    if conversation_id is None and job_kind == "discussion":
        conversation_id = data.get("subject_id")
    provenance = _provenance(
        metadata=metadata,
        source=metadata.get("source") or data.get("conversation_source") or default_source,
        agent=data.get("message_sender") or default_agent,
        via="queue",
    )
    failure = _safe_authority_failure(data.get("terminal_event_metadata_json"))
    return {
        "job_id": data["job_id"],
        "kind": job_kind,
        "subject_id": data["subject_id"],
        "conversation_id": conversation_id,
        "review_id": data.get("review_id"),
        "pr_number": data.get("pr_number"),
        "state": data["state"],
        "deadline_at": data.get("deadline_at"),
        "attempt_count": int(data.get("attempt_count") or 0),
        "task_id": metadata.get("task_id"),
        "failure": failure,
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "completed_at": data.get("completed_at"),
        "payload_content": "omitted",
        "result_content": "omitted",
        "read_only": True,
        **provenance,
    }


def _safe_authority_failure(value: Any) -> dict[str, Any] | None:
    """Project only the closed body-free failure record from a terminal event."""
    if not isinstance(value, str) or not value:
        return None
    try:
        event_metadata = json_depth.loads(value, max_depth=512)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(event_metadata, dict):
        return None
    failure = event_metadata.get("failure")
    if not isinstance(failure, dict) or set(failure) != {"phase", "code", "retryable"}:
        return None
    phase = failure.get("phase")
    code = failure.get("code")
    retryable = failure.get("retryable")
    if (
        phase not in _SAFE_FAILURE_PHASES
        or code not in _SAFE_FAILURE_CODES
        or not isinstance(retryable, bool)
    ):
        return None
    return {"phase": phase, "code": code, "retryable": retryable}


@router.get("/authority/jobs")
def fleet_authority_jobs(
    kind: str | None = Query(default=None),
    state: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    source: str | None = Query(default=None),
    pr: int | None = Query(default=None, ge=1),
    conversation: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Read-only authority queue projection; payload and result blobs remain sealed."""
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "kind": kind,
        "state": state,
        "agent": agent,
        "source": source,
        "pr": pr,
        "conversation": conversation,
        "since": since_value,
        "until": until_value,
    }
    with _read_connection(ctx) as (connection, availability):
        if connection is None:
            return _empty_collection(
                "jobs", limit=limit, offset=offset, availability=availability, filters=filters
            )
        if not _table_exists(connection, "authority_jobs"):
            return _empty_collection(
                "jobs", limit=limit, offset=offset, availability="table_missing", filters=filters
            )
        has_messages = _table_exists(connection, "comms_messages")
        has_metadata = _table_exists(connection, "authority_message_metadata")
        has_conversations = _table_exists(connection, "conversations")
        has_reviews = _table_exists(connection, "formal_review_jobs")
        has_job_events = _table_exists(connection, "authority_job_events")
        from_sql = " FROM authority_jobs AS job"
        message_sender_select = "NULL AS message_sender"
        conversation_select = "NULL AS conversation_id"
        conversation_source_select = "NULL AS conversation_source"
        provenance_select = "NULL AS authority_provenance_json"
        review_select = "NULL AS review_id, NULL AS pr_number"
        terminal_metadata_select = "NULL AS terminal_event_metadata_json"
        if has_messages:
            from_sql += (
                " LEFT JOIN comms_messages AS message"
                " ON (job.job_kind = 'request' AND message.message_id = job.subject_id)"
                " OR (job.job_kind = 'discussion'"
                " AND message.conversation_id = job.subject_id"
                " AND message.kind = 'discussion')"
            )
            message_sender_select = "message.sender AS message_sender"
            conversation_select = "message.conversation_id AS conversation_id"
        if has_metadata and has_messages:
            from_sql += (
                " LEFT JOIN authority_message_metadata AS authority_meta"
                " ON authority_meta.message_id = message.message_id"
            )
            provenance_select = "authority_meta.provenance_json AS authority_provenance_json"
        if has_conversations:
            conversation_join = "(job.job_kind = 'discussion' AND conversation.conversation_id = job.subject_id)"
            if has_messages:
                conversation_join = (
                    "conversation.conversation_id = message.conversation_id OR "
                    "(job.job_kind = 'discussion' AND conversation.conversation_id = job.subject_id)"
                )
            from_sql += (
                " LEFT JOIN conversations AS conversation ON " + conversation_join
            )
            conversation_select = "conversation.conversation_id AS conversation_id"
            if has_messages:
                conversation_select = (
                    "COALESCE(message.conversation_id, conversation.conversation_id) AS conversation_id"
                )
            conversation_source_select = "conversation.source AS conversation_source"
        if has_reviews:
            from_sql += (
                " LEFT JOIN formal_review_jobs AS review"
                " ON job.job_kind = 'formal_review' AND review.review_id = job.subject_id"
            )
            review_select = "review.review_id AS review_id, review.pr_number AS pr_number"
        if has_job_events:
            terminal_metadata_select = (
                "(SELECT event.metadata_json FROM authority_job_events AS event "
                "WHERE event.job_id = job.job_id AND event.event_type = 'finished' "
                "ORDER BY event.created_at DESC, event.rowid DESC LIMIT 1) "
                "AS terminal_event_metadata_json"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("job.job_kind = ?")
            params.append(kind)
        if state is not None:
            clauses.append("job.state = ?")
            params.append(state)
        if agent is not None:
            default_agent = (
                "CASE WHEN job.job_kind = 'formal_review' THEN 'review-gate' "
                "ELSE 'authority-service' END"
            )
            agent_expression = default_agent
            if has_messages:
                agent_expression = f"COALESCE(message.sender, {default_agent})"
            if has_metadata and has_messages:
                agent_expression = (
                    "COALESCE(json_extract(authority_meta.provenance_json, '$.Agent'), "
                    f"{agent_expression})"
                )
            agent_clause = f"{agent_expression} = ?"
            params.append(agent)
            if has_messages:
                agent_clause += " OR message.recipient = ?"
                params.append(agent)
            clauses.append(f"({agent_clause})")
        if source is not None:
            default_source = (
                "CASE WHEN job.job_kind = 'formal_review' THEN 'formal-review' "
                "WHEN job.job_kind = 'discussion' THEN 'authority-discussion' ELSE 'authority' END"
            )
            source_expression = default_source
            if has_conversations:
                source_expression = f"COALESCE(conversation.source, {default_source})"
            if has_metadata and has_messages:
                source_expression = (
                    "COALESCE(json_extract(authority_meta.provenance_json, '$.Source'), "
                    f"{source_expression})"
                )
            clauses.append(f"{source_expression} = ?")
            params.append(source)
        if pr is not None:
            if has_reviews:
                clauses.append("review.pr_number = ?")
                params.append(pr)
            else:
                clauses.append("1 = 0")
        if conversation is not None:
            if has_conversations:
                conversation_expression = "conversation.conversation_id"
                if has_messages:
                    conversation_expression = "COALESCE(message.conversation_id, conversation.conversation_id)"
                clauses.append(f"{conversation_expression} = ?")
                params.append(conversation)
            elif has_messages:
                clauses.append("message.conversation_id = ?")
                params.append(conversation)
            else:
                clauses.append("1 = 0")
        _time_clauses(
            "job.created_at",
            since=since_value,
            until=until_value,
            clauses=clauses,
            params=params,
        )
        return _paged_query(
            connection,
            key="jobs",
            select_sql=(
                "SELECT job.job_id, job.job_kind, job.subject_id, job.state, job.deadline_at, "
                "job.attempt_count, job.created_at, job.updated_at, job.completed_at, "
                f"{message_sender_select}, {conversation_select}, {conversation_source_select}, "
                f"{provenance_select}, {review_select}, {terminal_metadata_select}"
            ),
            from_sql=from_sql,
            clauses=clauses,
            params=params,
            order_sql="job.created_at DESC, job.job_id ASC",
            limit=limit,
            offset=offset,
            filters=filters,
            transform=_authority_job_item,
        )









def _review_item(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "review_id": data["review_id"],
        "repository": data["repository"],
        "pr_number": data["pr_number"],
        "head_sha": data["head_sha"],
        "gate_kind": data["gate_kind"],
        "state": data["state"],
        "created_at": data["created_at"],
        "attempt_count": int(data.get("attempt_count") or 0),
        "latest_attempt_state": data.get("latest_attempt_state"),
        "publication_count": int(data.get("publication_count") or 0),
        "sealed_verdict_available": bool(data.get("sealed_verdict_artifact_id")),
        "source": "formal-review",
        "agent": "review-gate",
        "via": "fleet-comms",
        "read_only": True,
    }


def fleet_reviews(
    kind: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    repository: Annotated[
        str | None,
        Query(
            description="Exact repository identity (owner/name). Applied in SQL before count/pagination.",
        ),
    ] = None,
    pr: Annotated[int | None, Query(ge=1)] = None,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Formal-review jobs, attempts, and publication counts without sealed blobs.

    Optional ``repository`` is an exact match on ``formal_review_jobs.repository``
    and participates in COUNT / LIMIT / OFFSET so foreign rows cannot consume the
    page budget of a repository-scoped consumer (e.g. public Work projection).
    """
    ctx = resolve_context(ctx)
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "kind": kind,
        "state": state,
        "source": source,
        "repository": repository,
        "pr": pr,
        "since": since_value,
        "until": until_value,
    }
    with _read_connection(ctx) as (connection, availability):
        if connection is None:
            return _empty_collection(
                "reviews", limit=limit, offset=offset, availability=availability, filters=filters
            )
        if not _table_exists(connection, "formal_review_jobs"):
            return _empty_collection(
                "reviews", limit=limit, offset=offset, availability="table_missing", filters=filters
            )
        has_attempts = _table_exists(connection, "formal_review_attempts")
        has_publications = _table_exists(connection, "github_publications")
        attempt_count = "0 AS attempt_count"
        latest_attempt = "NULL AS latest_attempt_state"
        publication_count = "0 AS publication_count"
        if has_attempts:
            attempt_count = (
                "(SELECT COUNT(*) FROM formal_review_attempts AS attempt "
                "WHERE attempt.review_id = review.review_id) AS attempt_count"
            )
            latest_attempt = (
                "(SELECT completion_state FROM formal_review_attempts AS latest_attempt "
                "WHERE latest_attempt.review_id = review.review_id "
                "ORDER BY latest_attempt.attempt_number DESC LIMIT 1) AS latest_attempt_state"
            )
        if has_publications:
            publication_count = (
                "(SELECT COUNT(*) FROM github_publications AS publication "
                "WHERE publication.review_id = review.review_id) AS publication_count"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("review.gate_kind = ?")
            params.append(kind)
        if state is not None:
            clauses.append("review.state = ?")
            params.append(state)
        if source is not None and source != "formal-review":
            clauses.append("1 = 0")
        if repository is not None:
            # Exact match only — no LIKE / suffix / casefold — privacy boundary.
            clauses.append("review.repository = ?")
            params.append(repository)
        if pr is not None:
            clauses.append("review.pr_number = ?")
            params.append(pr)
        _time_clauses(
            "review.created_at",
            since=since_value,
            until=until_value,
            clauses=clauses,
            params=params,
        )
        return _paged_query(
            connection,
            key="reviews",
            select_sql=(
                "SELECT review.review_id, review.repository, review.pr_number, review.head_sha, review.gate_kind, "
                "review.state, review.sealed_verdict_artifact_id, review.created_at, "
                f"{attempt_count}, {latest_attempt}, {publication_count}"
            ),
            from_sql=" FROM formal_review_jobs AS review",
            clauses=clauses,
            params=params,
            order_sql="review.created_at DESC, review.review_id ASC",
            limit=limit,
            offset=offset,
            filters=filters,
            transform=_review_item,
        )





@router.get("/migrations")
def fleet_migrations(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """Schema migration status from the existing plane, without applying a migration."""
    known = [{"version": item.version, "name": item.name} for item in MIGRATIONS]
    with _read_connection(ctx) as (connection, availability):
        if connection is None:
            return {
                "read_only": True,
                "availability": availability,
                "known_migrations": known,
                "migrations": [],
                "applied_version": None,
            }
        if not _table_exists(connection, "comms_schema_migrations"):
            return {
                "read_only": True,
                "availability": "table_missing",
                "known_migrations": known,
                "migrations": [],
                "applied_version": None,
            }
        try:
            rows = connection.execute(
                "SELECT version, name, applied_at FROM comms_schema_migrations ORDER BY version ASC"
            ).fetchall()
        except sqlite3.Error:
            rows = []
            availability = "db_unavailable"
    migrations = [
        {"version": int(row["version"]), "name": row["name"], "applied_at": row["applied_at"]}
        for row in rows
    ]
    return {
        "read_only": True,
        "availability": "available" if migrations else availability,
        "known_migrations": known,
        "migrations": migrations,
        "applied_version": migrations[-1]["version"] if migrations else None,
    }


def _acp_item(item: dict[str, Any]) -> dict[str, Any]:
    participants = item.get("participants") if isinstance(item.get("participants"), list) else []
    agent = " / ".join(_safe_text(value, fallback="") for value in participants if value) or "unknown"
    return {
        **item,
        "source": "acpx",
        "agent": agent,
        "via": "fleet-comms",
        "read_only": True,
    }


@router.get("/acp/conversations")
@swr_route("fleet-acp-conversations", FACADE_TTL_S)
def fleet_acp_conversations(
    state: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    source: str | None = Query(default=None),
    conversation: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Reuse the ACP read model for body-free conversation and round observability."""
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "state": state,
        "agent": agent,
        "source": source,
        "conversation": conversation,
        "since": since_value,
        "until": until_value,
    }
    payload = list_acp_conversations(limit=MAX_ACP_SCAN, ctx=ctx)
    availability = _safe_text(payload.get("availability"), fallback="unavailable")
    records = payload.get("conversations") if isinstance(payload.get("conversations"), list) else []
    filtered: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = _acp_item(raw)
        created_at = record.get("created_at")
        if state is not None and state not in {record.get("classification"), record.get("current_state")}:
            continue
        if agent is not None and agent not in record.get("participants", []):
            continue
        if source is not None and source != "acpx":
            continue
        if conversation is not None and conversation != record.get("conversation_id"):
            continue
        if since_value is not None and (not isinstance(created_at, str) or created_at < since_value):
            continue
        if until_value is not None and (not isinstance(created_at, str) or created_at > until_value):
            continue
        filtered.append(record)
    return _collection(
        "conversations",
        filtered[offset : offset + limit],
        total=len(filtered),
        limit=limit,
        offset=offset,
        availability=availability,
        filters=filters,
    )


@router.get("/acp/conversations/{conversation_id}")
def fleet_acp_conversation_detail(
    conversation_id: str,
    ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """Reuse the existing body-free ACP event timeline for one conversation."""
    record = get_acp_conversation(conversation_id, ctx=ctx)
    if record is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JSONResponse(
        content={
            "read_only": True,
            "body_policy": "events_only_no_transcript_or_artifact_content",
            "conversation": _acp_item(record),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/activity")
def fleet_activity(
    state: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    source: str | None = Query(default=None),
    via: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Recent runtime provenance records projected under the unified observer."""
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "state": state,
        "agent": agent,
        "source": source,
        "via": via,
        "since": since_value,
        "until": until_value,
    }
    raw_records = recent_runtime_records(limit=MAX_ACTIVITY_SCAN, ctx=ctx).get("records", [])
    records: list[dict[str, Any]] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        record = {
            "ts": raw.get("ts"),
            "source": _safe_text(raw.get("source")),
            "agent": _safe_text(raw.get("agent")),
            "via": _safe_text(raw.get("via") or raw.get("entrypoint")),
            "model": _safe_text(raw.get("model"), fallback="") or None,
            **recorded_model_accounting_fields(raw),
            "outcome": _safe_text(raw.get("outcome"), fallback="") or None,
            "duration_s": raw.get("duration_s"),
            "source_provenance": _safe_text(raw.get("source_provenance"), fallback="unknown"),
            "source_task_id": _safe_text(raw.get("source_task_id"), fallback="") or None,
            "read_only": True,
        }
        timestamp = record["ts"]
        if state is not None and state != record["outcome"]:
            continue
        if agent is not None and agent != record["agent"]:
            continue
        if source is not None and source != record["source"]:
            continue
        if via is not None and via != record["via"]:
            continue
        if since_value is not None and (not isinstance(timestamp, str) or timestamp < since_value):
            continue
        if until_value is not None and (not isinstance(timestamp, str) or timestamp > until_value):
            continue
        records.append(record)
    return _collection(
        "records",
        records[offset : offset + limit],
        total=len(records),
        limit=limit,
        offset=offset,
        availability="available" if records else "empty",
        filters=filters,
    )
