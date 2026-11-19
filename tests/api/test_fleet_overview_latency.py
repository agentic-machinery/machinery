"""Behavioral tests for GET /api/fleet/overview latency, SQLite parity, and SWR caching (#939)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from lu_machinery.api import fleet_router, state_helpers
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.fleet_comms.migrations import apply_migrations
from lu_machinery.fleet_comms.paths import RETIRED_LOCAL_MARKER, PlaneRootAnchorError


def _make_plane_db(root: Path, *, populate: bool = True) -> Path:
    plane = root / "fleet-comms" / "v1"
    plane.mkdir(parents=True, exist_ok=True)
    db_path = plane / "comms.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        apply_migrations(conn)
        if populate:
            now = datetime.now(UTC)
            inside = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            outside = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
            past_deadline = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            future_deadline = (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

            conn.execute(
                "INSERT INTO conversations(conversation_id, created_at, source, title) "
                "VALUES ('conv-1', ?, 'cto', 't')",
                (inside,),
            )
            messages = [
                ("m1", "conv-1", "message", "a", "b", inside),
                ("m2", "conv-1", "message", "a", "b", inside),
                ("m3", "conv-1", 'quote"and\\slash', "a", "b", inside),
                ("m4", "conv-1", "укр-вид", "a", "b", inside),
                ("m5", "conv-1", "request", "a", "b", outside),
            ]
            for mid, cid, kind, sender, recipient, ts in messages:
                conn.execute(
                    "INSERT INTO comms_messages(message_id, conversation_id, kind, sender, recipient, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mid, cid, kind, sender, recipient, ts),
                )
            conn.execute(
                "INSERT INTO requests(request_id, request_message_id, requested_recipient, resolved_recipient, "
                "state, completion_state, expires_at, created_at, updated_at) "
                "VALUES ('r1', 'm5', 'a', 'a', 'queued', 'pending', ?, ?, ?)",
                (future_deadline, inside, inside),
            )
            conn.execute(
                "INSERT INTO formal_review_jobs(review_id, repository, pr_number, head_sha, gate_kind, state, created_at) "
                "VALUES ('rev-1', 'learn-ukrainian/machinery', 939, 'abc123', 'code', 'complete', ?)",
                (inside,),
            )
            jobs = [
                ("j1", "request", "s1", "complete", None, inside),
                ("j2", "request", "s2", "queued", past_deadline, inside),
                ("j3", "discussion", "s3", "complete", None, inside),
                ("j4", "formal_review", "s4", "failed", None, outside),
                ("j5", "request", "s5", "running", future_deadline, inside),
            ]
            for jid, jkind, subj, state, deadline, updated in jobs:
                conn.execute(
                    "INSERT INTO authority_jobs("
                    "job_id, job_kind, subject_id, payload_artifact_id, state, deadline_at, "
                    "idempotency_key, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (jid, jkind, subj, f"art-{jid}", state, deadline, f"idem-{jid}", updated, updated),
                )
            dead_letters = [
                ("dl1", "j1", "d1", "expired_by_cleanup", inside),
                ("dl2", "j2", "d2", "timeout", inside),
                ("dl3", "j4", "d3", "timeout", outside),
            ]
            for dlid, jid, did, reason, ts in dead_letters:
                conn.execute(
                    "INSERT INTO authority_dead_letters(dead_letter_id, job_id, delivery_id, reason_code, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (dlid, jid, did, reason, ts),
                )
            conn.execute(
                "INSERT INTO dead_letters(dead_letter_id, delivery_id, reason, created_at) "
                "VALUES ('ldl1', 'del1', 'expired', ?)",
                (inside,),
            )
            conn.execute(
                "INSERT INTO acp_conversations("
                "conversation_id, task_digest, correlation_digest, idempotency_digest, "
                "rounds_requested, participants_json, created_at, deadline_at, token_budget, content_budget_bytes) "
                "VALUES ('acp-1', 'td', 'cd', 'id', 1, '[]', ?, ?, 1000, 4096)",
                (inside, future_deadline),
            )
            conn.commit()
    finally:
        conn.close()
    return plane


def _overview_skeleton(status: dict[str, Any]) -> dict[str, Any]:
    return {
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


@pytest.mark.parametrize("mode", ["authority", "shadow"])
def test_sqlite_single_row_overview_matches_granular_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Defect class: SQLite single-row overview query diverging from granular queries on counts,
    job_kind grouping, placeholder ordering, expired_by_cleanup exclusion, or legacy mode."""
    plane = _make_plane_db(tmp_path)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", mode)
    ctx = fixture_context(tmp_path / "monitor")
    status = fleet_router._safe_plane_status(ctx, plane_root=plane)

    with fleet_router._read_connection(ctx, pg_supported=True, plane_root=plane) as (conn, avail):
        assert conn is not None and avail == "available"
        fast = fleet_router._populate_fleet_overview_sqlite(conn, _overview_skeleton(status), status)
        assert fast is not None
        granular = fleet_router._populate_fleet_overview_queries(conn, _overview_skeleton(status), status)

    if mode == "authority":
        # Normalize second-precision window timestamps if a clock second rolled over between calls.
        fast["authority_health"]["window"] = granular["authority_health"]["window"]
        assert fast["counts"]["requests"] == {
            "total": 3,
            "by_state": {"complete": 1, "queued": 1, "running": 1},
            "source": "authority_jobs",
        }
        assert fast["counts"]["authority_jobs"] == {
            "total": 5,
            "by_state": {"complete": 2, "failed": 1, "queued": 1, "running": 1},
        }
        assert fast["authority_health"]["overdue_nonterminal"] == 1
        assert fast["authority_health"]["dead_letters"] == 1
        assert fast["counts"]["dead_letters"]["total"] == 3
        assert fast["counts"]["messages"]["by_kind"]['quote"and\\slash'] == 1
        assert fast["counts"]["messages"]["by_kind"]["укр-вид"] == 1
    else:
        assert fast["authority_health"] is None
        assert fast["counts"]["requests"]["source"] == "legacy_requests"
        assert fast["counts"]["dead_letters"]["total"] == 1

    assert fast == granular


def test_overview_reuses_cached_plane_root_and_respects_overrides_and_retired_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: repeated default_plane_root / git rev-parse spawns per overview request,
    env-override blindness, or cached plane root bypassing a newly placed retired marker."""
    plane = _make_plane_db(tmp_path)
    monkeypatch.delenv("FLEET_COMMS_ROOT", raising=False)
    monkeypatch.delenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW", raising=False)
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")

    calls: list[Path | None] = []

    def fake_default_plane_root(*, repo_root: Path | None = None, allow_non_git: bool = False) -> Path:
        calls.append(repo_root)
        return plane

    monkeypatch.setattr(fleet_router.message_plane, "default_plane_root", fake_default_plane_root)
    ctx = fixture_context(tmp_path / "monitor")

    first = fleet_router._compute_fleet_overview(ctx)
    second = fleet_router._compute_fleet_overview(ctx)
    assert first["availability"] == "available"
    assert second["counts"] == first["counts"]
    assert len(calls) == 1

    # Explicit FLEET_COMMS_ROOT override must not use the cached default root.
    other_plane = _make_plane_db(tmp_path / "other", populate=False)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(other_plane))
    monkeypatch.setattr(
        fleet_router.message_plane,
        "default_plane_root",
        lambda *, repo_root=None, allow_non_git=False: other_plane,
    )
    overridden = fleet_router._compute_fleet_overview(ctx)
    assert overridden["availability"] == "empty"

    # Unsetting FLEET_COMMS_ROOT returns to the cached root, which must still fail closed if retired.
    monkeypatch.delenv("FLEET_COMMS_ROOT", raising=False)
    (plane / RETIRED_LOCAL_MARKER).write_text("retired\n", encoding="utf-8")
    with pytest.raises(PlaneRootAnchorError):
        fleet_router._plane_root(ctx)


def test_overview_swr_serves_warm_hits_without_executor_and_refreshes_on_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: warm /api/fleet/overview requests waiting on a saturated executor,
    expired reads blocking on recompute, or dormant reads failing to refresh."""
    state_helpers.cache_invalidate()
    now = [1000.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: now[0])

    plane = _make_plane_db(tmp_path)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")

    ctx = replace(fixture_context(tmp_path / "monitor"), stores=replace(fixture_context(tmp_path / "monitor").stores, epics_store=None))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(fleet_router.router, prefix="/api/fleet")

    real_compute = fleet_router._compute_fleet_overview
    calls: list[int] = []
    refreshing = threading.Event()
    release = threading.Event()

    def counted_compute(target_ctx: Any) -> dict[str, Any]:
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            refreshing.set()
            assert release.wait(5)
        payload = real_compute(target_ctx)
        payload["counts"]["acp_conversations"]["total"] = len(calls)
        return payload

    monkeypatch.setattr(fleet_router, "_compute_fleet_overview", counted_compute)

    with TestClient(app) as client:
        cold = client.get("/api/fleet/overview")
        assert cold.status_code == 200
        assert cold.headers["X-Snapshot-State"] == "fresh"
        assert cold.headers["X-Snapshot-Age-S"] == "0.000"
        assert cold.json()["counts"]["acp_conversations"]["total"] == 1
        assert calls == [1]

        # Warm hit stays on the event loop even if the default threadpool executor is saturated.
        async def check_warm_under_saturated_executor() -> None:
            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            loop.set_default_executor(executor)
            started = threading.Event()
            unblock = threading.Event()
            blocked = None
            try:
                def hog() -> None:
                    started.set()
                    unblock.wait(timeout=5)

                blocked = loop.run_in_executor(None, hog)
                assert started.wait(2)
                resp = Response()
                payload = await asyncio.wait_for(
                    fleet_router.fleet_overview(response=resp, ctx=ctx), timeout=0.5
                )
                assert payload["counts"]["acp_conversations"]["total"] == 1
                assert resp.headers["X-Snapshot-State"] == "fresh"
            finally:
                unblock.set()
                if blocked is not None:
                    await blocked
                executor.shutdown(wait=True)

        asyncio.run(check_warm_under_saturated_executor())
        assert calls == [1]

        # Expired read returns stale snapshot immediately without waiting for the held refresh.
        now[0] += fleet_router.FACADE_TTL_S + 2.0
        try:
            for _ in range(3):
                stale = client.get("/api/fleet/overview")
                assert stale.status_code == 200
                assert stale.headers["X-Snapshot-State"] == "stale"
                assert stale.json()["counts"]["acp_conversations"]["total"] == 1
            assert refreshing.wait(5)
        finally:
            release.set()

        # Wait for the single background refresh flight to publish.
        scope = ctx.runtime._derived[f"swr-key-scope:fleet-overview:{ctx.roots!r}"]
        key = fleet_router._overview_cache_key(scope)
        refreshed = state_helpers.cache_get_or_compute(
            key, fleet_router.FACADE_TTL_S, lambda: pytest.fail("unexpected second refresh")
        )
        assert isinstance(refreshed, dict)
        assert refreshed["counts"]["acp_conversations"]["total"] == 2
        assert calls == [1, 2]

        # Dormant read (past TTL + DEMAND_WINDOW_S) refreshes and returns fresh data.
        now[0] += fleet_router.FACADE_TTL_S + state_helpers.DEMAND_WINDOW_S + 1.0
        dormant = client.get("/api/fleet/overview")
        assert dormant.status_code == 200
        assert dormant.headers["X-Snapshot-State"] == "fresh"
        assert dormant.json()["counts"]["acp_conversations"]["total"] == 3
        assert calls == [1, 2, 3]


def test_overview_swr_never_stores_probe_or_schema_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: transient db_missing / probe_failed / schema_read_failed responses
    being cached as fresh and hiding recovery."""
    state_helpers.cache_invalidate()
    missing_plane = tmp_path / "fleet-comms" / "v1"
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(missing_plane))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")

    ctx = fixture_context(tmp_path / "monitor")
    ctx = replace(ctx, stores=replace(ctx.stores, epics_store=None))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(fleet_router.router, prefix="/api/fleet")

    with TestClient(app) as client:
        failed = client.get("/api/fleet/overview")
        assert failed.status_code == 200
        assert failed.json()["availability"] == "db_missing"
        assert failed.headers["X-Snapshot-State"] == "unavailable"

        # Once the database exists, the next read immediately observes healthy data.
        _make_plane_db(tmp_path)
        recovered = client.get("/api/fleet/overview")
        assert recovered.status_code == 200
        assert recovered.json()["availability"] == "available"
        assert recovered.headers["X-Snapshot-State"] == "fresh"

    for bad_availability in (
        "db_missing",
        "schema_read_failed",
        "sqlite_probe_failed",
        "pg_probe_failed",
        "pg_dsn_missing",
        "pg_privilege_denied",
        "authority_unsupported_component",
    ):
        assert not fleet_router._is_storable_overview(
            {"availability": bad_availability, "health": {"schema": {}}, "authority_health": None}
        )
    assert not fleet_router._is_storable_overview(
        {
            "availability": "available",
            "health": {"schema": {"db_error": "schema_incompatible"}},
            "authority_health": {"availability": "available"},
        }
    )
    assert not fleet_router._is_storable_overview(
        {
            "availability": "available",
            "health": {"schema": {}},
            "authority_health": {"availability": "schema_read_failed"},
        }
    )


def test_overview_sqlite_falls_back_on_partial_corrupt_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: a broken column on one optional table failing the whole combined SQLite
    query instead of preserving the granular per-table fallback semantics."""
    plane = tmp_path / "fleet-comms" / "v1"
    plane.mkdir(parents=True)
    db_path = plane / "comms.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        apply_migrations(conn)
        conn.execute("DROP TABLE authority_dead_letters")
        conn.execute("CREATE TABLE authority_dead_letters(dead_letter_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO authority_dead_letters VALUES ('dl-broken')")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")
    ctx = fixture_context(tmp_path / "monitor")

    overview = fleet_router._compute_fleet_overview(ctx)
    assert overview["availability"] == "available"
    assert overview["counts"]["dead_letters"]["total"] == 1
    assert overview["authority_health"]["availability"] == "schema_read_failed"
