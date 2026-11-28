"""Sol PR-K: read-only Monitor surfaces for session streams.

Exposes stream health, status and digest without any cutover mutation. The
markdown-handoff mirror (dual-write status and projection drift) was retired in
#512: a lane's handoff content is its DB note.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from lu_machinery.session_streams.handoff import diagnose_handoff
from lu_machinery.session_streams.model import entry_as_dict, validate_stream_id
from lu_machinery.session_streams.store import NotFoundError, SessionStreamStore

from .epics_router import _response_token, _safe_entry
from .monitor_context import MonitorContext, get_ctx
from .repository_authority import build_repository_identity

router = APIRouter(tags=["session-streams"])


def _store(ctx: MonitorContext) -> SessionStreamStore:
    db_path = ctx.roots.session_streams_db_path
    if db_path is None or not db_path.is_file():
        raise FileNotFoundError("session-stream database is unavailable")
    store = ctx.stores.session_streams_store
    if store is None:
        raise FileNotFoundError("session-stream database is unavailable")
    return store


def _valid_stream_id(stream_id: str) -> str:
    try:
        return validate_stream_id(stream_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# A holder's identity in its lease envelope. Monitor never serves the envelope's random lease_id; these are
# withheld too, as defense in depth, like the epics lease projection (machinery-infra#1659).
_HOLDER_IDENTITY_FIELDS = ("session_id", "holder_instance_id", "holder_process_id")
# The holder labels that stay public pass the epics API's token rule.
_HOLDER_LABEL_FIELDS = ("holder_agent", "holder_harness")


def _public_status(status: dict[str, Any]) -> dict[str, Any]:
    """Withhold the holder's identity and project its labels through the epics token rule.

    ``reason`` quotes the holder's labels, so it is withheld whenever a label is.
    """
    public = {key: value for key, value in status.items() if key not in _HOLDER_IDENTITY_FIELDS}
    for field in _HOLDER_LABEL_FIELDS:
        if public[field] is not None:
            public[field] = _response_token(public[field])
    if any(public[field] != status[field] for field in _HOLDER_LABEL_FIELDS):
        public["reason"] = "[redacted]"
    return public


def _public_entry(entry: Any) -> dict[str, Any]:
    """The epics API's safe entry projection, without the writer's session id."""
    return {**_safe_entry(entry_as_dict(entry)), "session_id": "[redacted]"}


def _store_health(ctx: MonitorContext) -> dict[str, Any]:
    """Return store reachability and migration versions without its path."""
    connection = None
    try:
        if (
            ctx.stores.session_streams_database is None
            or ctx.roots.session_streams_db_path is None
            or not ctx.roots.session_streams_db_path.is_file()
        ):
            return {"reachable": False, "schema_versions": []}
        connection = ctx.stores.session_streams_database.connect(read_only=True)
        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        return {"reachable": True, "schema_versions": versions}
    except Exception:
        return {"reachable": False, "schema_versions": []}
    finally:
        if connection is not None:
            connection.close()


@router.get("/v1/health")
def session_streams_health(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """Liveness for the session-streams monitor surface (no cutover)."""
    root = ctx.roots.live_repo_root
    return {
        "ok": True,
        "repo": build_repository_identity(root),
        "store": _store_health(ctx),
        "cutover": "operator-gated",
    }


@router.get("/v1/status/{stream_id:path}")
def session_stream_status(
    stream_id: str,
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Handoff/lease diagnosis for one stream (read-only; no claim)."""
    stream_id = _valid_stream_id(stream_id)
    try:
        status = diagnose_handoff(_store(ctx), stream_id)
    except (NotFoundError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive API boundary
        raise HTTPException(status_code=500, detail="status_failed") from exc
    return _public_status(status.as_dict())


@router.get("/v1/digest/{stream_id:path}")
def session_stream_digest(
    stream_id: str,
    limit: int = Query(default=20, ge=0, le=500),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Pinned entries plus last N non-pinned entries (bounded), through the epics API's safe entry projection."""
    stream_id = _valid_stream_id(stream_id)
    try:
        digest = _store(ctx).load_digest(stream_id, limit=limit)
    except (NotFoundError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail="digest_failed") from exc
    return {
        "stream_id": stream_id,
        "limit": limit,
        "pinned": [_public_entry(e) for e in digest.pinned],
        "recent": [_public_entry(e) for e in digest.recent],
        "pinned_count": len(digest.pinned),
        "recent_count": len(digest.recent),
    }


@router.get("/v1/plane-continuity")
def plane_continuity_bundle(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """One-shot continuity board: health plus the plane-status pointer.

    Does not mutate streams or flip message-plane defaults.
    """
    health = session_streams_health(ctx=ctx)
    return {
        "session_streams": health,
        "message_plane": {
            "status_path": "/api/comms/v1/plane-status",
            "default_cutover": "off — operator-gated",
        },
        "cutover": "operator-gated",
    }
