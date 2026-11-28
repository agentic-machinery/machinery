"""FastAPI router for local telemetry persistence.

This router owns per-tool timing telemetry from local hooks. Module-build token telemetry
(``/api/telemetry/module-builds*``) is a product route in
``scripts.project_api.module_builds`` under the same prefix (Monitor split,
C-4687).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lu_machinery.lib.readonly_sqlite import SQLiteConnection

from .monitor_context import MonitorContext, get_ctx, resolve_context
from .project_state_router import BodyTooLargeError, read_json_body_bounded
from .runtime_router import local_body_read_refusal_response

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])
_WINDOWS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
# Rows older than the widest read window serve no query, so every write prunes them.
RETENTION = max(_WINDOWS.values())
# The table never holds more rows than this; a write drops the oldest beyond it.
MAX_STORED_ROWS = 200_000
# One hook payload is a few hundred bytes; anything far larger is refused unread.
MAX_BODY_BYTES = 4096
MAX_TEXT_LENGTH = 256
# A week, in milliseconds: far above any real tool call and well inside SQLite INTEGER.
MAX_DURATION_MS = 7 * 86_400_000


class ToolTimingIngest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    tool_name: str = Field(..., min_length=1, max_length=MAX_TEXT_LENGTH)
    duration_ms: int = Field(..., ge=0, le=MAX_DURATION_MS)
    tool_use_id: str | None = Field(None, max_length=MAX_TEXT_LENGTH)
    session_id: str | None = Field(None, max_length=MAX_TEXT_LENGTH)
    failed: bool = False


def _tool_timings_path(ctx: MonitorContext | None = None) -> Path:
    return resolve_context(ctx).roots.state_root / "data" / "telemetry" / "tool_timings.db"



def _open_telemetry_db(ctx: MonitorContext, path: Path) -> SQLiteConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return ctx._open_db(path)


def _isoformat_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _now_z() -> str:
    return _isoformat_z(datetime.now(UTC))


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _required_text(value: str | None, field: str) -> str:
    cleaned = _clean_text(value)
    if cleaned is None:
        raise HTTPException(status_code=422, detail=f"{field} must not be blank")
    return cleaned


def _init_db(ctx: MonitorContext | None = None, db_path: Path | None = None) -> None:
    resolved = resolve_context(ctx)
    path = db_path or _tool_timings_path(resolved)
    with closing(_open_telemetry_db(resolved, path)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tool_timings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                tool_name   TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                tool_use_id TEXT,
                session_id  TEXT,
                failed      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_tt_ts ON tool_timings(ts);
            CREATE INDEX IF NOT EXISTS idx_tt_name_ts ON tool_timings(tool_name, ts);
        """)
        conn.commit()


def _percentile_ms(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]

    rank = (len(values) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    fraction = rank - lower
    return round(values[lower] + (values[upper] - values[lower]) * fraction)


def _window_start(window: str) -> str:
    delta = _WINDOWS.get(window)
    if delta is None:
        allowed = ", ".join(_WINDOWS)
        raise HTTPException(status_code=422, detail=f"window must be one of: {allowed}")
    return _isoformat_z(datetime.now(UTC) - delta)


@router.post("/tool-timings")
async def ingest_tool_timing(
    request: Request,
    ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """Store one hook timing from a local caller; the body is read only after the local guard passes."""
    refusal = local_body_read_refusal_response(request)
    if refusal is not None:
        return refusal
    no_store = {"Cache-Control": "no-store"}
    try:
        payload = ToolTimingIngest.model_validate(await read_json_body_bounded(request, max_bytes=MAX_BODY_BYTES))
    except BodyTooLargeError:
        return JSONResponse(status_code=413, content={"detail": "request body too large"}, headers=no_store)
    except (ValueError, ValidationError):
        return JSONResponse(status_code=422, content={"detail": "invalid tool timing"}, headers=no_store)
    _store_tool_timing(ctx, payload)
    return JSONResponse(content={"ok": True}, headers=no_store)


def _store_tool_timing(ctx: MonitorContext, payload: ToolTimingIngest) -> None:
    _init_db(ctx)
    with closing(_open_telemetry_db(ctx, _tool_timings_path(ctx))) as conn:
        conn.execute(
            """
            INSERT INTO tool_timings (ts, tool_name, duration_ms, tool_use_id, session_id, failed)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _isoformat_z(payload.ts),
                payload.tool_name,
                int(payload.duration_ms),
                payload.tool_use_id,
                payload.session_id,
                1 if payload.failed else 0,
            ),
        )
        conn.execute("DELETE FROM tool_timings WHERE ts < ?", (_isoformat_z(datetime.now(UTC) - RETENTION),))
        conn.execute(
            """
            DELETE FROM tool_timings WHERE id <= (
                SELECT id FROM tool_timings ORDER BY id DESC LIMIT 1 OFFSET ?
            )
            """,
            (MAX_STORED_ROWS,),
        )
        conn.commit()


@router.get("/tool-timings")
def read_tool_timings(
    window: str = Query("1h"),
    tool: str | None = Query(None, max_length=MAX_TEXT_LENGTH),
    ctx: MonitorContext = Depends(get_ctx),
) -> list[dict[str, Any]]:
    """Summarize stored timings per tool. A read never creates the database."""
    path = _tool_timings_path(ctx)
    start = _window_start(window)
    if not path.is_file():
        return []
    conditions = ["ts >= ?"]
    params: list[Any] = [start]
    if tool:
        conditions.append("tool_name = ?")
        params.append(tool)

    where = " AND ".join(conditions)
    # A table written before retention existed can exceed the cap: read at most the newest rows.
    params.append(MAX_STORED_ROWS)
    with closing(ctx._open_db(path, read_only=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT tool_name, duration_ms, failed FROM (
                SELECT tool_name, duration_ms, failed
                FROM tool_timings
                WHERE {where}
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY tool_name, duration_ms
            """,
            params,
        ).fetchall()

    durations: dict[str, list[int]] = defaultdict(list)
    failures: dict[str, int] = defaultdict(int)
    for row in rows:
        tool_name = str(row["tool_name"])
        durations[tool_name].append(int(row["duration_ms"]))
        failures[tool_name] += int(row["failed"])

    results = []
    for tool_name, values in durations.items():
        count = len(values)
        results.append({
            "tool_name": tool_name,
            "count": count,
            "p50_ms": _percentile_ms(values, 0.50),
            "p95_ms": _percentile_ms(values, 0.95),
            "p99_ms": _percentile_ms(values, 0.99),
            "mean_ms": round(sum(values) / count),
            "failure_count": failures[tool_name],
        })

    return sorted(results, key=lambda item: (-item["count"], item["tool_name"]))


