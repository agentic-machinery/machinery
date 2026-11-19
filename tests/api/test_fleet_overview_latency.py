"""Behavioral tests for GET /api/fleet/overview latency, SQLite parity, and SWR caching (#939)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import fleet_router, state_helpers
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.fleet_comms.migrations import apply_migrations
from lu_machinery.fleet_comms.paths import RETIRED_LOCAL_MARKER, PlaneRootAnchorError


@pytest.fixture(autouse=True)
def _pin_sqlite_fixture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class (F6): SQLite overview tests inheriting ambient PG authority or telemetry."""
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY", "sqlite")
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_FLEET_COMMS", "sqlite")
    monkeypatch.delenv("FLEET_COMMS_PLANE_TELEMETRY", raising=False)
    monkeypatch.delenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW", raising=False)


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


def _overview_app(tmp_path: Path) -> tuple[FastAPI, Any]:
    ctx = fixture_context(tmp_path)
    ctx = replace(ctx, stores=replace(ctx.stores, epics_store=None))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(fleet_router.router, prefix="/api/fleet")
    return app, ctx


@pytest.mark.parametrize("mode", ["authority", "shadow"])
def test_sqlite_single_row_overview_matches_granular_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Defect class (F1): SQLite single-row overview query diverging from granular queries on
    counts, job_kind grouping, colliding raw display labels (NULL/empty/whitespace/truncated),
    placeholder ordering, expired_by_cleanup exclusion, or legacy mode."""
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

    # Colliding raw states (NULL, empty, whitespace-only, collapsed whitespace, >160-char prefix)
    # must match the granular path's exact ordered raw-state projection in every mode.
    colliding_plane = _make_plane_db(tmp_path / "colliding", populate=False)
    conn_rw = sqlite3.connect(colliding_plane / "comms.sqlite3")
    try:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn_rw.execute("DROP TABLE authority_jobs")
        conn_rw.execute(
            "CREATE TABLE authority_jobs ("
            "job_id TEXT PRIMARY KEY, job_kind TEXT NOT NULL, subject_id TEXT NOT NULL, "
            "state TEXT, deadline_at TEXT, updated_at TEXT NOT NULL)"
        )
        prefix = "x" * 160
        rows = [
            ("c1", "request", "s1", None),
            ("c2", "request", "s2", ""),
            ("c3", "request", "s3", ""),
            ("c4", "request", "s4", "   "),
            ("c5", "request", "s5", "   "),
            ("c6", "request", "s6", "   "),
            ("c7", "discussion", "s7", "   "),
            ("c8", "request", "s8", "a  b"),
            ("c9", "request", "s9", "a b"),
            ("c10", "request", "s10", prefix + "1"),
            ("c11", "request", "s11", prefix + "2"),
        ]
        for jid, jkind, subj, raw_state in rows:
            conn_rw.execute(
                "INSERT INTO authority_jobs(job_id, job_kind, subject_id, state, deadline_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (jid, jkind, subj, raw_state, now),
            )
        conn_rw.commit()
    finally:
        conn_rw.close()

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(colliding_plane))
    colliding_status = fleet_router._safe_plane_status(ctx, plane_root=colliding_plane)
    with fleet_router._read_connection(ctx, pg_supported=True, plane_root=colliding_plane) as (
        conn2,
        avail2,
    ):
        assert conn2 is not None and avail2 == "available"
        fast2 = fleet_router._populate_fleet_overview_sqlite(
            conn2, _overview_skeleton(colliding_status), colliding_status
        )
        assert fast2 is not None
        granular2 = fleet_router._populate_fleet_overview_queries(
            conn2, _overview_skeleton(colliding_status), colliding_status
        )
    if mode == "authority":
        fast2["authority_health"]["window"] = granular2["authority_health"]["window"]
        assert fast2["counts"]["requests"]["by_state"]["unknown"] == 3
        assert fast2["counts"]["authority_jobs"]["by_state"]["unknown"] == 4
    else:
        assert fast2["counts"]["authority_jobs"]["by_state"]["unknown"] == 4
    assert fast2 == granular2


def test_overview_reuses_cached_plane_root_and_respects_overrides_and_retired_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class (F4, F5): repeated default_plane_root / git rev-parse spawns, env-override
    pollution during an in-flight compute, or cached snapshots served from a retired plane."""
    state_helpers.cache_invalidate()
    now = [2000.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: now[0])

    plane = _make_plane_db(tmp_path / "default")
    monkeypatch.delenv("FLEET_COMMS_ROOT", raising=False)
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")

    calls: list[Path | None] = []

    def fake_default_plane_root(*, repo_root: Path | None = None, allow_non_git: bool = False) -> Path:
        calls.append(repo_root)
        return plane

    monkeypatch.setattr(fleet_router.message_plane, "default_plane_root", fake_default_plane_root)
    ctx = fixture_context(tmp_path / "monitor-root")

    first = fleet_router._compute_fleet_overview(ctx)
    second = fleet_router._compute_fleet_overview(ctx)
    assert first["availability"] == "available"
    assert second["counts"] == first["counts"]
    assert len(calls) == 1

    # F4: Primed HTTP cache must refuse a newly retired plane at warm (1s), stale (20s), and dormant (200s) ages.
    monkeypatch.undo()
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY", "sqlite")
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_FLEET_COMMS", "sqlite")
    monkeypatch.delenv("FLEET_COMMS_PLANE_TELEMETRY", raising=False)
    monkeypatch.delenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW", raising=False)
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: now[0])

    retired_plane = _make_plane_db(tmp_path / "retire")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(retired_plane))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")
    retire_app, _ = _overview_app(tmp_path / "monitor-retire")

    with TestClient(retire_app, raise_server_exceptions=True) as client:
        primed = client.get("/api/fleet/overview")
        assert primed.status_code == 200
        assert primed.headers["X-Snapshot-State"] == "fresh"

        (retired_plane / RETIRED_LOCAL_MARKER).write_text("retired\n", encoding="utf-8")
        for delta_s in (1.0, 19.0, 180.0):
            now[0] += delta_s
            with pytest.raises(PlaneRootAnchorError):
                client.get("/api/fleet/overview")

        # Explicit local-shadow override stays permitted.
        monkeypatch.setenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW", "1")
        shadow_ok = client.get("/api/fleet/overview")
        assert shadow_ok.status_code == 200
        assert shadow_ok.json()["availability"] == "available"

    # F5: Switching FLEET_COMMS_ROOT while a compute is in flight must not publish plane B under plane A's key.
    monkeypatch.delenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW", raising=False)
    plane_a = _make_plane_db(tmp_path / "plane-a", populate=False)
    plane_b = _make_plane_db(tmp_path / "plane-b", populate=True)
    race_app, _ = _overview_app(tmp_path / "monitor-race")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_a))

    entered = threading.Event()
    proceed = threading.Event()
    real_checked = fleet_router._compute_fleet_overview_checked

    def paused_checked(target_ctx: Any, *, routing: Any = None) -> tuple[dict[str, Any], bool]:
        if not entered.is_set():
            entered.set()
            assert proceed.wait(5)
        return real_checked(target_ctx, routing=routing)

    monkeypatch.setattr(fleet_router, "_compute_fleet_overview_checked", paused_checked)

    with TestClient(race_app) as race_client:
        result_holder: list[Any] = []

        def run_first() -> None:
            result_holder.append(race_client.get("/api/fleet/overview"))

        worker = threading.Thread(target=run_first)
        worker.start()
        assert entered.wait(5)
        monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_b))
        proceed.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result_holder[0].status_code == 200
        assert result_holder[0].json()["counts"]["messages"]["total"] == 0

        monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_a))
        second_a = race_client.get("/api/fleet/overview")
        assert second_a.status_code == 200
        assert second_a.headers["X-Snapshot-State"] == "fresh"
        assert second_a.json()["counts"]["messages"]["total"] == 0


def test_overview_swr_serves_warm_hits_without_executor_and_refreshes_on_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class (F2): warm HTTP /api/fleet/overview blocking on AnyIO or asyncio worker pools,
    expired reads blocking on recompute, or dormant reads exceeding the 0.5 s cap when the
    default executor is saturated."""
    state_helpers.cache_invalidate()
    now = [1000.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: now[0])

    plane = _make_plane_db(tmp_path)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")

    app, ctx = _overview_app(tmp_path / "monitor")

    real_checked = fleet_router._compute_fleet_overview_checked
    calls: list[int] = []
    refreshing = threading.Event()
    release = threading.Event()

    def counted_checked(target_ctx: Any, *, routing: Any = None) -> tuple[dict[str, Any], bool]:
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            refreshing.set()
            assert release.wait(5)
        payload, ok = real_checked(target_ctx, routing=routing)
        payload["counts"]["acp_conversations"]["total"] = len(calls)
        return payload, ok

    monkeypatch.setattr(fleet_router, "_compute_fleet_overview_checked", counted_checked)

    with TestClient(app) as client:
        cold = client.get("/api/fleet/overview")
        assert cold.status_code == 200
        assert cold.headers["X-Snapshot-State"] == "fresh"
        assert cold.headers["X-Snapshot-Age-S"] == "0.000"
        assert cold.json()["counts"]["acp_conversations"]["total"] == 1
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

        scope = ctx.runtime._derived[f"swr-key-scope:fleet-overview:{ctx.roots!r}"]
        routing = fleet_router._capture_overview_routing(plane)
        key = fleet_router._overview_cache_key(scope, routing)
        refreshed = state_helpers.cache_get_or_compute(
            key, fleet_router.FACADE_TTL_S, lambda: pytest.fail("unexpected second refresh")
        )
        assert isinstance(refreshed, dict)
        assert refreshed["counts"]["acp_conversations"]["total"] == 2
        assert calls == [1, 2]

        # Dormant read (past TTL + DEMAND_WINDOW_S) with free workers refreshes and returns fresh data.
        now[0] += fleet_router.FACADE_TTL_S + state_helpers.DEMAND_WINDOW_S + 1.0
        dormant = client.get("/api/fleet/overview")
        assert dormant.status_code == 200
        assert dormant.headers["X-Snapshot-State"] == "fresh"
        assert dormant.json()["counts"]["acp_conversations"]["total"] == 3
        assert calls == [1, 2, 3]

    # Exercise BOTH AnyIO worker pool and asyncio default executor saturation through real ASGI HTTP GETs.
    async def check_http_under_saturated_pools() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            # 1. Occupy the single AnyIO worker token; warm HTTP GET must still return fresh quickly.
            limiter = anyio.to_thread.current_default_thread_limiter()
            prev_tokens = limiter.total_tokens
            limiter.total_tokens = 1
            anyio_started = threading.Event()
            anyio_release = threading.Event()

            def block_anyio() -> None:
                anyio_started.set()
                anyio_release.wait(timeout=5)

            anyio_holder = asyncio.create_task(anyio.to_thread.run_sync(block_anyio))
            try:
                assert await asyncio.to_thread(anyio_started.wait, 2)
                warm_anyio = await asyncio.wait_for(
                    async_client.get("/api/fleet/overview"), timeout=0.5
                )
                assert warm_anyio.status_code == 200
                assert warm_anyio.headers["X-Snapshot-State"] == "fresh"
                assert warm_anyio.json()["counts"]["acp_conversations"]["total"] == 3
            finally:
                anyio_release.set()
                await anyio_holder
                limiter.total_tokens = prev_tokens

            # 2. Occupy the single asyncio default executor worker; both warm AND dormant HTTP GETs
            # must answer within the 1 s budget (dormant bounded by SWR_DORMANT_READ_WAIT_S = 0.5 s).
            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            loop.set_default_executor(executor)
            exec_started = threading.Event()
            exec_release = threading.Event()
            blocked = None
            try:
                def block_executor() -> None:
                    exec_started.set()
                    exec_release.wait(timeout=5)

                blocked = loop.run_in_executor(None, block_executor)
                assert exec_started.wait(2)

                warm_exec = await asyncio.wait_for(
                    async_client.get("/api/fleet/overview"), timeout=0.5
                )
                assert warm_exec.status_code == 200
                assert warm_exec.headers["X-Snapshot-State"] == "fresh"

                now[0] += fleet_router.FACADE_TTL_S + state_helpers.DEMAND_WINDOW_S + 5.0
                t0 = time.monotonic()
                dormant_exec = await asyncio.wait_for(
                    async_client.get("/api/fleet/overview"), timeout=0.95
                )
                elapsed = time.monotonic() - t0
                assert dormant_exec.status_code == 200
                assert dormant_exec.headers["X-Snapshot-State"] == "stale"
                assert elapsed < 0.95
            finally:
                exec_release.set()
                if blocked is not None:
                    await blocked
                if fleet_router._OVERVIEW_DORMANT_TASKS:
                    await asyncio.gather(*list(fleet_router._OVERVIEW_DORMANT_TASKS), return_exceptions=True)
                executor.shutdown(wait=True)

    asyncio.run(check_http_under_saturated_pools())


def test_overview_swr_never_stores_probe_or_schema_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class (F3): transient db_missing / probe_failed / schema_read_failed or partial
    count-query failures being cached as fresh and hiding subsequent repair."""
    state_helpers.cache_invalidate()
    missing_plane = tmp_path / "fleet-comms" / "v1"
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(missing_plane))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")

    app, _ = _overview_app(tmp_path / "monitor")

    with TestClient(app) as client:
        failed = client.get("/api/fleet/overview")
        assert failed.status_code == 200
        assert failed.json()["availability"] == "db_missing"
        assert failed.headers["X-Snapshot-State"] == "unavailable"

        # Once the database exists, the next read immediately observes healthy data.
        plane = _make_plane_db(tmp_path)
        recovered = client.get("/api/fleet/overview")
        assert recovered.status_code == 200
        assert recovered.json()["availability"] == "available"
        assert recovered.headers["X-Snapshot-State"] == "fresh"

    # F3: A broken count column (e.g. renamed formal_review_jobs.state) returns partial granular
    # counts without caching them, so repairing the column is visible on the very next GET.
    state_helpers.cache_invalidate()
    repair_plane = _make_plane_db(tmp_path / "repair")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(repair_plane))
    repair_app, _ = _overview_app(tmp_path / "monitor-repair")
    with sqlite3.connect(repair_plane / "comms.sqlite3") as conn:
        conn.execute("ALTER TABLE formal_review_jobs RENAME COLUMN state TO broken_state")
        conn.commit()

    with TestClient(repair_app) as repair_client:
        partial = repair_client.get("/api/fleet/overview")
        assert partial.status_code == 200
        assert partial.json()["availability"] == "available"
        assert partial.json()["counts"]["reviews"]["total"] == 0
        assert partial.headers["X-Snapshot-State"] == "unavailable"

        with sqlite3.connect(repair_plane / "comms.sqlite3") as conn:
            conn.execute("ALTER TABLE formal_review_jobs RENAME COLUMN broken_state TO state")
            conn.commit()

        repaired = repair_client.get("/api/fleet/overview")
        assert repaired.status_code == 200
        assert repaired.json()["counts"]["reviews"]["total"] == 1
        assert repaired.headers["X-Snapshot-State"] == "fresh"

    assert plane.is_dir()
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

    overview, cacheable = fleet_router._compute_fleet_overview_checked(ctx)
    assert overview["availability"] == "available"
    assert overview["counts"]["dead_letters"]["total"] == 1
    assert overview["authority_health"]["availability"] == "schema_read_failed"
    assert cacheable is False
