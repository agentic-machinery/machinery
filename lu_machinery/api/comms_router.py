"""
Comms API router — Fleet Comms metrics, plane status and inbox counts.

Mounted at /api/comms/ in main.py. ``/api/comms/batch-progress*`` (curriculum
batch progress) is a product route in ``scripts.project_api.batch_progress``
under the same prefix (Monitor split, C-4687).

Endpoints:
  GET /api/comms/v1/plane-status       Message-plane mode + schema + parity (read-only)
  GET /api/comms/live-activity         Module-level live feed
  GET /api/comms/inbox                 Queued Fleet Comms deliveries for one agent (body-free)
  GET /api/comms/v1/metrics            Authority failure metrics

Retired with the legacy broker and channel stores (#515), each a 410 naming
its replacement: the /channels/{name} reads, /by-module, POST /cleanup,
POST /acknowledge/{id} and POST /channels/{name}/post.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import MachineryConfigError
from lu_machinery.control_plane.storage import ControlPlaneError
from lu_machinery.fleet_comms.agent_success_metrics import collect_agent_success
from lu_machinery.fleet_comms.efficiency_metrics import (
    collect_efficiency_metrics_authority,
)
from lu_machinery.fleet_comms.message_plane import read_plane_status
from lu_machinery.fleet_comms.opsec_store import (
    COMMS_RESPONSE_SCHEMA_VERSION,
    comms_plane_store,
)
from lu_machinery.fleet_comms.paths import PlaneRootAnchorError, default_plane_root

from . import panel_values
from .monitor_context import MonitorContext, get_ctx
from .state_helpers import (
    cache_get,
    cache_get_or_compute_async,
    cache_get_stored,
    ctx_scoped_ttl_key,
    schedule_cache_refresh,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["comms"])

# Roots are read from MonitorContext via Depends(get_ctx) (#7269 step 5): this
# module keeps no module-global Path seams.

# ==================== HEALTH ====================


@router.get("/v1/plane-status")
def message_plane_status(ctx: MonitorContext = Depends(get_ctx)):
    """Read-only fleet message-plane status (PR-K-ish / #5512 parity surface).

    Reports ``FLEET_COMMS_MESSAGE_PLANE`` mode (default off), schema migration
    version when ``batch_state/fleet-comms/v1/comms.sqlite3`` exists, and a
    recent parity telemetry summary when the optional JSONL file is present.
    Never writes or migrates.
    """
    return read_plane_status(repo_root=ctx.roots.state_root)


# ==================== LIVE ACTIVITY ====================


def _scan_live_activity(ctx: MonitorContext, minutes: int = 15) -> list[dict]:
    """Scan legacy and v6 state files for live module-level activity."""
    now = time.time()
    cutoff = now - (minutes * 60)
    activities = []
    curriculum_root = ctx.roots.curriculum_root

    # 1. Scan all orchestration state files modified recently
    if not curriculum_root.is_dir():
        return activities

    for track_dir in curriculum_root.iterdir():
        if not track_dir.is_dir():
            continue
        orch_dir = track_dir / "orchestration"
        if not orch_dir.exists():
            continue
        track = track_dir.name
        for module_dir in orch_dir.iterdir():
            if not module_dir.is_dir():
                continue
            state_files = [
                module_dir / "state.json",
                module_dir / "state-v3.json",
            ]
            recent_state_file = None
            recent_mtime = 0.0
            for state_file in state_files:
                if not state_file.exists():
                    continue
                mtime = state_file.stat().st_mtime
                if mtime < cutoff or mtime <= recent_mtime:
                    continue
                recent_state_file = state_file
                recent_mtime = mtime
            if recent_state_file is None:
                continue

            try:
                state = json_depth.loads(recent_state_file.read_text(), max_depth=512)
            except (json.JSONDecodeError, OSError):
                continue

            slug = state.get("slug", module_dir.name)
            phases = state.get("phases", {})

            # Find the latest phase
            latest_phase = None
            latest_ts = ""
            for phase_key, phase_data in phases.items():
                ts = phase_data.get("ts", "")
                if ts > latest_ts:
                    latest_ts = ts
                    latest_phase = phase_key

            if not latest_phase:
                continue

            phase_data = phases[latest_phase]
            phase_status = phase_data.get("status", "unknown")

            # Check if there are recently modified files in the module dir
            # (indicates active work beyond just the state file)
            recent_files = []
            for f in module_dir.iterdir():
                if f.is_file() and f.stat().st_mtime > cutoff:
                    recent_files.append(f.name)

            activities.append(
                {
                    "track": track,
                    "slug": slug,
                    "phase": latest_phase.replace("v3-", "Phase "),
                    "phase_status": phase_status,
                    "timestamp": latest_ts,
                    "seconds_ago": int(now - recent_mtime),
                    "task_id": phase_data.get("task_id", ""),
                    "mode": phase_data.get("mode", ""),
                    "recent_files": recent_files,
                }
            )

    # Sort by most recent first
    activities.sort(key=lambda a: a["seconds_ago"])
    return activities


def _scan_recent_completions(ctx: MonitorContext, minutes: int = 60) -> list[dict]:
    """Scan research files created recently for a completion feed."""
    now = time.time()
    cutoff = now - (minutes * 60)
    completions = []
    curriculum_root = ctx.roots.curriculum_root

    if not curriculum_root.is_dir():
        return completions

    for track_dir in curriculum_root.iterdir():
        if not track_dir.is_dir():
            continue
        research_dir = track_dir / "research"
        if not research_dir.exists():
            continue
        track = track_dir.name
        for rf in research_dir.glob("*-research.md"):
            mtime = rf.stat().st_mtime
            if mtime < cutoff:
                continue
            completions.append(
                {
                    "track": track,
                    "slug": rf.stem.replace("-research", ""),
                    "type": "research",
                    "seconds_ago": int(now - mtime),
                    "size_kb": round(rf.stat().st_size / 1024, 1),
                    "timestamp": datetime.fromtimestamp(mtime, tz=UTC).isoformat(),
                }
            )

    completions.sort(key=lambda c: c["seconds_ago"])
    return completions


@router.get("/live-activity")
async def live_activity(
    minutes: int = Query(15, ge=1, le=120),
    limit: int = Query(30, ge=1, le=500, description="Echoed as dispatch_limit; the broker dispatch feed is removed"),
    ctx: MonitorContext = Depends(get_ctx),
):
    """Module-level live feed: in-progress modules, recent completions, recent dispatches."""
    acts, completions = await asyncio.gather(
        asyncio.to_thread(_scan_live_activity, ctx, minutes),
        asyncio.to_thread(_scan_recent_completions, ctx, 60),
    )

    # The broker dispatch feed is removed with the legacy store (#515); its keys stay empty.
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "in_progress": acts,
        "recent_completions": completions[:30],
        "recent_dispatches": [],
        "dispatch_limit": limit,
        "dispatch_next_cursor": None,
    }


# ==================== ACTIONS ====================


@router.post("/cleanup")
async def cleanup_zombies(max_age_hours: float = Query(24.0)):
    """Retired legacy mutation endpoint."""
    _ = max_age_hours
    return JSONResponse(
        status_code=410,
        content={"error": "legacy comms writes retired; use fleet-comms authority tooling"},
    )


@router.post("/acknowledge/{message_id}")
async def acknowledge_message(message_id: int):
    """Retired legacy mutation endpoint."""
    _ = message_id
    return JSONResponse(
        status_code=410,
        content={"error": "legacy acknowledge retired; use fleet-comms delivery receipts"},
    )


# ══════════════════════════════════════════════════════════════════
# Retired legacy channel routes (#1190 Phase B.3, removed in #515)
# ══════════════════════════════════════════════════════════════════
#
# Every route answers 410 naming its replacement; authority writes enter
# through fleet-comms and the Monitor API stays read-only.


class ChannelPostRequest(BaseModel):
    """Pydantic body for POST /channels/{name}/post."""

    body: str
    to_agents: list[str] = []
    parent_id: str | None = None
    correlation_id: str | None = None
    from_agent: str = "user"
    auto_snapshot: bool = True


def _retired_read(replacement: str) -> JSONResponse:
    """A legacy store read removed with the broker and channel stores (#515)."""
    return JSONResponse(
        status_code=410,
        content={"error": "retired: the legacy broker and channel stores are removed (#515)", "replacement": replacement},
    )


_CHANNEL_READ_REPLACEMENT = "GET /api/fleet/acp/conversations; fleet_comms channel"


@router.get("/channels/{name}")
@router.get("/channels/{name}/messages")
@router.get("/channels/{name}/deliveries")
def retired_channel_reads(name: str):
    """Retired legacy channel store reads."""
    _ = name
    return _retired_read(_CHANNEL_READ_REPLACEMENT)


@router.get("/channels/{name}/threads/{thread_id}")
def retired_channel_thread(name: str, thread_id: str):
    """Retired legacy channel thread read."""
    _ = (name, thread_id)
    return _retired_read(_CHANNEL_READ_REPLACEMENT)


@router.post("/channels/{name}/post")
async def post_to_channel(name: str, req: ChannelPostRequest, request: Request):
    """Retired browser channel mutation endpoint."""
    _ = (name, req, request)
    return JSONResponse(
        status_code=410,
        content={"error": "browser channel writes retired; the Monitor API is read-only"},
    )

@router.get("/by-module/{track}/{slug}")
def retired_messages_by_module(track: str, slug: str):
    """Retired legacy broker trail for a module."""
    _ = (track, slug)
    return _retired_read("GET /api/fleet/requests")


# ==================== INBOX (#1309) ====================


@router.get("/inbox")
def comms_inbox(
    agent: str = Query(..., description="Target agent slot (e.g. 'claude-cto', 'codex-infra')."),
    limit: int = Query(10, ge=1, le=50, description="Max delivery message ids to return."),
    ctx: MonitorContext = Depends(get_ctx),
):
    """Queued Fleet Comms deliveries for one agent: count and message ids, never bodies.

    The cheap cold-start "do I have work waiting" check (GH #1309), read from
    the authority plane (``pending_delivery_snapshot``). ``probe`` is ``ok``
    only when the plane was read; otherwise ``count`` is null (unknown, never
    zero). Read the deliveries with ``fleet_comms deliveries receive``.
    """
    from lu_machinery.fleet_comms.authority import (  # noqa: PLC0415 — authority stack on demand
        pending_delivery_snapshot,
    )

    try:
        plane_root = default_plane_root(repo_root=ctx.roots.state_root)
    except (PlaneRootAnchorError, MachineryConfigError, OSError, ValueError):
        # Same fail-open rule as the metrics route: an unanchored plane is unknown, never a 500.
        snapshot: dict = {"probe": "unknown"}
    else:
        snapshot = pending_delivery_snapshot(agent, root=plane_root, limit=limit)
    return {
        "agent": agent,
        "count": snapshot.get("count"),
        "ids": list(snapshot.get("ids") or []),
        "truncated": bool(snapshot.get("truncated", False)),
        "limit": limit,
        "probe": snapshot.get("probe", "unknown"),
        "content_included": False,
        "source": "authority",
    }


def _authority_plane_db(ctx: MonitorContext) -> Path:
    # EXACT mirror of fleet_router._plane_db_path (#7505 CF r1: the two
    # surfaces must never resolve the plane differently).
    return default_plane_root(repo_root=ctx.roots.state_root) / "comms.sqlite3"


def _authority_metrics_payload(
    collector, ctx: MonitorContext, empty_fields: dict | None = None, **kwargs
) -> dict:
    """#7486 (plan v3.1 item 5): metric routes follow the storage/plane switch.

    Fail-open like the fleet facade: absent plane db → db_missing; collector
    errors → typed db_error code (never exception text); the control-plane
    interlock's typed refusal under pg surfaces the same way.
    """
    zero = dict(empty_fields or {})
    try:
        db_path = _authority_plane_db(ctx)
    except Exception:
        # Fail-open parity with the fleet facade: an unresolvable plane root
        # (retired local plane, non-git fixture) reads as missing, never a 500.
        return {
            "response_schema_version": COMMS_RESPONSE_SCHEMA_VERSION,
            "content_included": False,
            "source": "authority",
            "store": comms_plane_store(reachable=False),
            "db_missing": True,
            **zero,
        }
    base = {
        "response_schema_version": COMMS_RESPONSE_SCHEMA_VERSION,
        "content_included": False,
        "source": "authority",
        "store": comms_plane_store(reachable=db_path.is_file()),
    }
    if not db_path.is_file():
        # #7505 CF r1: keep the legacy zero-value shape fields so consumers
        # of the configured-default authority mode see a stable schema.
        return {**base, "db_missing": True, **zero}
    try:
        payload = collector(db_path, **kwargs)
    except (OSError, sqlite3.Error, ValueError, ControlPlaneError) as exc:
        logger.warning("comms authority collector unavailable: %s", type(exc).__name__)
        return {**base, "db_error": type(exc).__name__, **zero}
    payload.update(base)
    return payload


_METRIC_STATES = frozenset({
    "pending", "processing", "dispatched", "delivered", "failed", "queued", "leased",
    "running", "done", "complete", "completed", "succeeded", "cancelled", "expired", "dead_lettered",
    "dead_letter", "acknowledged", "sent", "received", "open", "closed", "resolved", "unknown",
})


def _metric_counts(raw: object, allowed: frozenset[str]) -> dict[str, int | float]:
    """Preserve the denominator, folding arbitrary database labels into unknown."""
    out: dict[str, int | float] = {}
    for key, value in panel_values.mapping(raw).items():
        count = panel_values.number(value)
        if count is None or count < 0:
            continue
        label = panel_values.choice(key, allowed, "unknown")
        out[label] = min(1e15, out.get(label, 0) + count)
    return out


def _public_agent_success(raw: object) -> dict:
    # Summary field names are authored here, not copied from task identities.
    from lu_machinery.fleet_comms import agent_success_metrics as metrics  # noqa: PLC0415

    fields = frozenset({
        "tasks", "roles", "review", "authoring", "other", "model_sources", "resolved", "requested",
        "status", "success", "terminal_success", "no_deliverable_reasons", "failure_classes",
        "median_wall_seconds", "reviews", "verdicts", "changes_requested", "rounds_to_approval",
        "findings", "finding_severities", "confirmed_findings", "confirmation_rate", "unobserved_finding_reviews",
        "tokens", "pr_opened", "pr_merged", "merge_group_ci_failures", "cost_usd", "value", "observed",
        "total", "coverage", "numerator", "denominator", "rate", "percent", "n", "count", "median", "rubric_agreement",
        "hot_records", "archive_records", "excluded_legacy", "excluded_dry_run", "invalid_records", "undated_records",
        "outside_window", "duplicate_runs", "unreadable_directories", "model_catalog_unavailable", "unknown",
    }) | set(metrics.STATUSES) | set(metrics.REASONS) | set(metrics.ERROR_CLASSES) | set(metrics.SEVERITIES) | set(metrics.TOKEN_ALIASES) | set(metrics.VERDICTS.values())

    def summary(value: object, depth: int = 0):
        if depth > 8:
            return None
        if isinstance(value, dict):
            return {key: summary(child, depth + 1) for key, child in value.items() if key in fields}
        return "unknown" if value == "unknown" else panel_values.number(value)

    row = panel_values.mapping(raw)
    out = {"schema_version": "agent-success.v1", "content_included": False, "source": "dispatch_v2",
           **panel_values.scalars(row, numbers=("tasks",), flags=("available", "complete")),
           "coverage": summary(row.get("coverage")),
           "window": {**panel_values.scalars(panel_values.mapping(row.get("window")), times=("since", "until")),
                      "cohort": "started_at", "bounds": "[since,until)"}}
    for dimension in ("by_agent", "by_model"):
        groups = panel_values.mapping(row.get(dimension))
        out[dimension] = {
            key: summary(value) for key, value in groups.items()
            if key == "unknown" or (key in panel_values.AGENTS if dimension == "by_agent" else panel_values.model(key) == key)
        }
    scores = panel_values.mapping(row.get("review_scores"))
    out["review_scores"] = {dimension: {key: summary(value) for key, value in panel_values.mapping(scores.get(dimension)).items()
                                       if key == "unknown" or panel_values.model(key) == key}
                            for dimension in ("by_reviewer_model", "by_author_model")}
    return out


_FAILURE_METRICS = ("dead_letters", "failed_deliveries", "failed_jobs")
_PUBLIC_RECIPIENTS = panel_values.AGENTS | {"claude-cto", "kimi", "authority-job"}


def _public_failure_windows(raw: object) -> dict:
    out: dict[str, dict] = {}
    for label in ("24h", "7d"):
        window = panel_values.mapping(panel_values.mapping(raw).get(label))
        out[label] = {}
        for metric in _FAILURE_METRICS:
            if metric not in window:
                continue
            row = panel_values.mapping(window[metric])
            grouped: dict[str, int | float] = {}
            for recipient, count in panel_values.mapping(row.get("by_recipient")).items():
                count = panel_values.number(count)
                if count is None or count < 0:
                    continue
                bucket = recipient if recipient in _PUBLIC_RECIPIENTS else "other"
                grouped[bucket] = grouped.get(bucket, 0) + count
            out[label][metric] = {"total": panel_values.number(row.get("total")), "by_recipient": grouped}
    return out


def _public_comms_metrics(raw: object) -> dict:
    row = panel_values.mapping(raw)
    out = {"response_schema_version": COMMS_RESPONSE_SCHEMA_VERSION, "content_included": False,
           **panel_values.scalars(row, numbers=("dead_letters",), flags=("db_missing",))}
    if "failure_windows" in row:
        out["failure_windows"] = _public_failure_windows(row["failure_windows"])
    if "failure_lifetime" in row:
        out["failure_lifetime"] = panel_values.scalars(panel_values.mapping(row["failure_lifetime"]), numbers=_FAILURE_METRICS)
    if "source" in row:
        out["source"] = panel_values.choice(row["source"], {"authority"}, "unknown")
    if row.get("db_error"):
        out["db_error"] = "collector_unavailable"
        out["reason_code"] = "collector_unavailable"
    store = panel_values.mapping(row.get("store"))
    out["store"] = {"kind": panel_values.choice(store.get("kind"), {"comms-plane"}, "unknown"),
                    "reachable": store.get("reachable") is True}
    for key in ("deliveries", "jobs", "requests"):
        if key in row:
            out[key] = _metric_counts(row[key], _METRIC_STATES)
    if "retired_endpoint_pending" in row:
        out["retired_endpoint_pending"] = panel_values.scalars(
            panel_values.mapping(row["retired_endpoint_pending"]), numbers=("gemini",))
    if "latency_seconds" in row:
        latency = panel_values.mapping(row["latency_seconds"])
        out["latency_seconds"] = {
            key: panel_values.scalars(panel_values.mapping(latency[key]), numbers=("n", "avg", "min", "max"))
            for key in ("delivery_dispatch_to_done", "delivery_created_to_done") if key in latency}
    if "agent_success" in row:
        out["agent_success"] = _public_agent_success(row["agent_success"])
    return out


COMMS_METRICS_TTL_S = 30.0


def _collect_panel_metrics(ctx: MonitorContext, include_agent_success: bool) -> tuple[str, dict]:
    observed_at = datetime.now(UTC).isoformat()
    extra = {}
    if include_agent_success:
        extra["agent_success"] = collect_agent_success(tasks_dir=ctx.roots.batch_state_dir / "tasks")
    payload = _authority_metrics_payload(collect_efficiency_metrics_authority, ctx)
    payload.update(extra)
    return observed_at, _public_comms_metrics(payload)


def _metrics_cache_key(ctx: MonitorContext, include_agent_success: bool = False) -> str:
    return ctx_scoped_ttl_key(ctx, "comms-metrics", ctx.roots.state_root,
                              ctx.roots.batch_state_dir, os.environ.get("FLEET_COMMS_ROOT", ""),
                              include_agent_success)


def warm_panel_metrics(ctx: MonitorContext) -> None:
    """Refresh off-loop, sharing the dashboard's cache and single flight."""
    key = _metrics_cache_key(ctx)
    schedule_cache_refresh(
        key, COMMS_METRICS_TTL_S, lambda: _collect_panel_metrics(ctx, False),
        should_refresh=lambda: cache_get(key, COMMS_METRICS_TTL_S) is None,
    )


def cached_panel_metrics(ctx: MonitorContext) -> tuple[dict, float] | None:
    """Read the shared snapshot without collecting or waiting on its writer."""
    stored = cache_get_stored(_metrics_cache_key(ctx))
    if stored is None:
        return None
    (observed_at, payload), age = cast(tuple[tuple[str, dict], float], stored)
    # A slow aggregation must not make an old observation look newly sampled.
    return payload, max(age, panel_values.observation_age_s(observed_at) or 0.0)


async def refresh_panel_metrics_periodically(ctx: MonitorContext) -> None:
    """Keep exporter data warm even when nobody visits the dashboard."""
    while True:
        warm_panel_metrics(ctx)
        await asyncio.sleep(COMMS_METRICS_TTL_S)


@router.get("/v1/metrics")
async def comms_v1_metrics(ctx: MonitorContext = Depends(get_ctx), include_agent_success: bool = False) -> JSONResponse:
    """Local durable metrics, cached for 30 s; warm reads never wait for a scan."""
    key = await asyncio.to_thread(_metrics_cache_key, ctx, include_agent_success)

    def compute() -> tuple[str, dict]:
        return _collect_panel_metrics(ctx, include_agent_success)

    stored = cache_get_stored(key)
    if stored is None:
        observed_at, payload = await cache_get_or_compute_async(key, COMMS_METRICS_TTL_S, compute)
    else:
        value, age = stored
        observed_at, payload = cast(tuple[str, dict], value)
        if age >= COMMS_METRICS_TTL_S:
            schedule_cache_refresh(key, COMMS_METRICS_TTL_S, compute)
    return JSONResponse(content=payload, headers=panel_values.snapshot_headers(
        # Same request-driven cadence as PRs: two page cycles plus collection
        # margin belong in the display bound, without increasing scan frequency.
        panel_values.observation_age_s(observed_at), COMMS_METRICS_TTL_S + 90.0,
        unavailable=bool(payload.get("db_missing") or payload.get("db_error")),
    ))
