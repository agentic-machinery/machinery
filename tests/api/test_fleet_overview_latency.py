"""Regression tests for GET /api/fleet/overview latency, SWR caching, and guards (#939)."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import fleet_router, state_helpers
from lu_machinery.api.monitor_context import MonitorContext, fixture_context, get_ctx
from lu_machinery.fleet_comms import message_plane, migrations
from lu_machinery.fleet_comms import paths as fleet_paths


@pytest.fixture(autouse=True)
def _isolated_fleet_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep tests independent of ambient PG, shadow, telemetry, or cache state."""
    for name in (
        "LEARN_UKRAINIAN_CP_AUTHORITY",
        "LEARN_UKRAINIAN_CP_AUTHORITY_FLEET_COMMS",
        "LEARN_UKRAINIAN_CP_PG_DSN",
        "FLEET_COMMS_ROOT",
        "FLEET_COMMS_MESSAGE_PLANE",
        "FLEET_COMMS_PLANE_TELEMETRY",
        "FLEET_COMMS_ALLOW_LOCAL_SHADOW",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY", "sqlite")
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")
    state_helpers.cache_invalidate()
    yield
    state_helpers.cache_invalidate()


def _seed_plane_db(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "comms.sqlite3"
    with sqlite3.connect(db_path) as conn:
        migrations.apply_migrations(conn)
    return db_path


def _make_app(tmp_path: Path, *, state_root: Path | None = None) -> tuple[FastAPI, MonitorContext]:
    ctx = fixture_context(tmp_path / "monitor")
    if state_root is not None:
        ctx = ctx.with_roots(state_root=state_root)
    ctx = replace(ctx, stores=replace(ctx.stores, epics_store=None))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(fleet_router.router, prefix="/api/fleet")
    return app, ctx


def _insert_message(conn: sqlite3.Connection, message_id: str, kind: str) -> None:
    now = "2026-11-19T12:00:00Z"
    conn.execute(
        """
        INSERT INTO comms_messages (
            message_id, conversation_id, kind, sender, recipient,
            body_inline, created_at
        ) VALUES (?, 'conv-1', ?, 'sender', 'recipient', 'body', ?)
        """,
        (message_id, kind, now),
    )


def _seed_conversation(conn: sqlite3.Connection) -> None:
    now = "2026-11-19T12:00:00Z"
    conn.execute(
        "INSERT OR IGNORE INTO conversations (conversation_id, created_at, source, title) "
        "VALUES ('conv-1', ?, 'test', 'title')",
        (now,),
    )


@pytest.mark.parametrize("mode", ["authority", "shadow"])
def test_sqlite_single_row_overview_matches_per_table_for_edge_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Defect class: SQLite JSON aggregation diverges from per-table SQL on edge labels or health window."""
    plane_root = tmp_path / "plane"
    db_path = _seed_plane_db(plane_root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_root))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", mode)

    now = datetime.now(UTC)
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past_deadline = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    long_prefix = "k" * 160

    with sqlite3.connect(db_path) as conn:
        _seed_conversation(conn)
        # Edge-case message kinds: quotes, backslash, newline, Unicode, whitespace, colliding >160-char prefix.
        edge_kinds = [
            "normal",
            "normal",
            'quote"and\\slash\nline',
            "привіт 🇺🇦",
            "",
            "   \t\n",
            long_prefix + "a",
            long_prefix + "b",
            long_prefix + "b",
        ]
        for idx, kind in enumerate(edge_kinds):
            _insert_message(conn, f"msg-{idx}", kind)
        conn.execute(
            """
            INSERT INTO requests (
                request_id, request_message_id, requested_recipient, resolved_recipient,
                state, expires_at, created_at, updated_at
            ) VALUES ('req-1', 'msg-0', 'cto', 'cto', 'queued', ?, ?, ?)
            """,
            (recent, recent, recent),
        )
        conn.execute(
            """
            INSERT INTO artifacts (artifact_id, sha256, bytes, producer, retention_class, created_at)
            VALUES ('art-1', 'sha-art-1', 10, 'test', 'short', ?)
            """,
            (recent,),
        )
        conn.execute(
            """
            INSERT INTO formal_review_jobs (
                review_id, repository, pr_number, head_sha, gate_kind, state, created_at
            ) VALUES ('rev-1', 'org/repo', 1, 'deadbeef', 'code', 'done', ?)
            """,
            (recent,),
        )
        conn.execute(
            """
            INSERT INTO authority_jobs (
                job_id, job_kind, subject_id, payload_artifact_id, state,
                deadline_at, attempt_count, idempotency_key, created_at, updated_at
            ) VALUES
                ('job-1', 'request', 'msg-0', 'art-1', 'running', ?, 1, 'ik-1', ?, ?),
                ('job-2', 'formal_review', 'rev-1', 'art-1', 'failed', NULL, 1, 'ik-2', ?, ?),
                ('job-3', 'request', 'msg-1', 'art-1', 'complete', NULL, 1, 'ik-3', ?, ?)
            """,
            (past_deadline, recent, recent, recent, recent, old, old),
        )
        conn.execute(
            """
            INSERT INTO authority_dead_letters (dead_letter_id, delivery_id, job_id, reason_code, created_at)
            VALUES
                ('dl-cleanup', NULL, 'job-1', 'expired_by_cleanup', ?),
                ('dl-real', NULL, 'job-2', 'delivery_failed', ?)
            """,
            (recent, recent),
        )
        conn.execute(
            """
            INSERT INTO dead_letters (dead_letter_id, request_id, delivery_id, reason, created_at)
            VALUES ('legacy-dl-1', 'req-1', NULL, 'expired', ?)
            """,
            (recent,),
        )
        conn.execute(
            """
            INSERT INTO acp_conversations (
                conversation_id, task_digest, correlation_digest, idempotency_digest,
                rounds_requested, participants_json, created_at, deadline_at,
                token_budget, content_budget_bytes
            ) VALUES ('conv-1', 'td', 'cd', 'id', 1, '[]', ?, ?, 1000, 1000)
            """,
            (recent, recent),
        )

    app, ctx = _make_app(tmp_path)
    status = fleet_router._safe_plane_status(ctx, plane_root=plane_root)

    def _template() -> dict[str, Any]:
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

    with fleet_router._read_connection(ctx, pg_supported=True, plane_root=plane_root) as (conn, _):
        assert conn is not None
        fast, fast_complete = fleet_router._populate_fleet_overview_sqlite(_template(), status, conn)
        slow, slow_complete = fleet_router._populate_fleet_overview_per_table(_template(), status, conn)

    assert fast_complete is True
    assert slow_complete is True
    # Normalize window timestamps to avoid 1-second boundary drift between the two calls.
    if fast.get("authority_health") and slow.get("authority_health"):
        fast["authority_health"]["window"] = slow["authority_health"]["window"]
    assert fast == slow

    with TestClient(app) as client:
        resp = client.get("/api/fleet/overview")
        assert resp.status_code == 200
        assert resp.headers["X-Snapshot-State"] == "fresh"
        body = resp.json()
        assert body["counts"]["messages"]["total"] == slow["counts"]["messages"]["total"] == 7
        assert body["counts"]["messages"]["by_kind"]["unknown"] == 1
        assert body["counts"]["messages"]["by_kind"]["привіт 🇺🇦"] == 1
        assert body["counts"]["messages"]["by_kind"][long_prefix] == 2


def test_overview_caches_plane_root_without_repeated_git_rev_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class: GET /api/fleet/overview spawns git rev-parse twice per request when FLEET_COMMS_ROOT is unset."""
    state_repo = tmp_path / "state-repo"
    state_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(state_repo)], check=True)
    plane_root = state_repo / "batch_state" / "fleet-comms" / "v1"
    _seed_plane_db(plane_root)

    calls: list[Path | None] = []
    real_default = message_plane.default_plane_root

    def _counting_default(*, repo_root: Path | None = None, allow_non_git: bool = False) -> Path:
        calls.append(repo_root)
        return real_default(repo_root=repo_root, allow_non_git=allow_non_git)

    monkeypatch.setattr(message_plane, "default_plane_root", _counting_default)

    app, _ctx = _make_app(tmp_path, state_root=state_repo)
    with TestClient(app) as client:
        r1 = client.get("/api/fleet/overview")
        assert r1.status_code == 200
        assert len(calls) == 1

        # Even after cache invalidation (a second cold overview collect), plane_root stays cached per context.
        state_helpers.cache_invalidate()
        r2 = client.get("/api/fleet/overview")
        assert r2.status_code == 200
        assert len(calls) == 1


def test_overview_swr_fresh_stale_and_dormant_headers_and_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class: overview bypasses SWR caching, serves wrong freshness headers, or fails to refresh after expiry/dormancy."""
    plane_root = tmp_path / "plane"
    db_path = _seed_plane_db(plane_root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_root))

    clock = [1000.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: clock[0])

    refresh_done = threading.Event()
    collect_count = 0
    real_collect = fleet_router._collect_fleet_overview

    def _tracked_collect(ctx: MonitorContext, *, plane_root: Path | None = None) -> tuple[dict[str, Any], bool]:
        nonlocal collect_count
        collect_count += 1
        out = real_collect(ctx, plane_root=plane_root)
        if collect_count >= 2:
            refresh_done.set()
        return out

    monkeypatch.setattr(fleet_router, "_collect_fleet_overview", _tracked_collect)

    app, _ctx = _make_app(tmp_path)
    with TestClient(app) as client:
        # 1. Cold miss -> fresh, 1 collect.
        r1 = client.get("/api/fleet/overview")
        assert r1.status_code == 200
        assert r1.headers["X-Snapshot-State"] == "fresh"
        assert r1.json()["counts"]["messages"]["total"] == 0
        assert collect_count == 1

        # Insert a message while cached.
        with sqlite3.connect(db_path) as conn:
            _seed_conversation(conn)
            _insert_message(conn, "msg-1", "update")

        # 2. Warm hit (+5s < 15s TTL) -> serves cached without re-collecting.
        clock[0] += 5.0
        r2 = client.get("/api/fleet/overview")
        assert r2.status_code == 200
        assert r2.headers["X-Snapshot-State"] == "fresh"
        assert r2.json()["counts"]["messages"]["total"] == 0
        assert collect_count == 1

        # 3. Stale hit (+20s >= 15s TTL, < 135s dormant) -> serves stale immediately and refreshes in background.
        clock[0] += 15.0
        r3 = client.get("/api/fleet/overview")
        assert r3.status_code == 200
        assert r3.headers["X-Snapshot-State"] == "stale"
        assert r3.json()["counts"]["messages"]["total"] == 0
        assert refresh_done.wait(timeout=5.0), "background SWR refresh did not complete"
        assert collect_count == 2

        # Next read sees refreshed count as fresh.
        r4 = client.get("/api/fleet/overview")
        assert r4.status_code == 200
        assert r4.headers["X-Snapshot-State"] == "fresh"
        assert r4.json()["counts"]["messages"]["total"] == 1

        # Insert a second message and jump past dormant window (TTL + DEMAND_WINDOW_S = 135s).
        with sqlite3.connect(db_path) as conn:
            _insert_message(conn, "msg-2", "update")
        clock[0] += fleet_router.FACADE_TTL_S + state_helpers.DEMAND_WINDOW_S + 10.0

        # 4. Dormant read -> refreshes inline within SWR_DORMANT_READ_WAIT_S and returns fresh updated data.
        r5 = client.get("/api/fleet/overview")
        assert r5.status_code == 200
        assert r5.headers["X-Snapshot-State"] == "fresh"
        assert r5.json()["counts"]["messages"]["total"] == 2
        assert collect_count == 3


def test_overview_missing_required_table_is_not_cached_and_recovers_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class (F3): partial counts from a missing required table get cached for the full TTL."""
    plane_root = tmp_path / "plane"
    db_path = _seed_plane_db(plane_root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_root))

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE comms_messages")

    app, _ctx = _make_app(tmp_path)
    with TestClient(app) as client:
        r1 = client.get("/api/fleet/overview")
        assert r1.status_code == 200
        assert r1.headers["X-Snapshot-State"] == "unavailable"
        assert r1.json()["counts"]["messages"] == {"total": 0, "by_kind": {}}

        # Restore schema and insert a message; immediate next GET must observe it without waiting for TTL.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE comms_messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT,
                    body_inline TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            _seed_conversation(conn)
            _insert_message(conn, "msg-restored", "recovered")

        r2 = client.get("/api/fleet/overview")
        assert r2.status_code == 200
        assert r2.headers["X-Snapshot-State"] == "fresh"
        assert r2.json()["counts"]["messages"] == {"total": 1, "by_kind": {"recovered": 1}}


def test_overview_corrupt_column_falls_back_without_caching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class: SQLite fast path crashes on a corrupt column instead of falling back without caching."""
    plane_root = tmp_path / "plane"
    db_path = _seed_plane_db(plane_root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_root))

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE comms_messages")
        conn.execute("CREATE TABLE comms_messages (message_id TEXT PRIMARY KEY)")

    app, _ctx = _make_app(tmp_path)
    with TestClient(app) as client:
        r1 = client.get("/api/fleet/overview")
        assert r1.status_code == 200
        assert r1.headers["X-Snapshot-State"] == "unavailable"
        assert r1.json()["counts"]["messages"] == {"total": 0, "by_kind": {}}


def test_overview_retirement_blocks_counts_store_open_and_cached_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class (F4): local retirement after status probe opens counts DB or serves cached snapshot."""
    plane_root = tmp_path / "plane"
    _seed_plane_db(plane_root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_root))

    # Case 1: Retirement marker created immediately after read_plane_status returns -> zero open_readonly calls.
    open_calls = 0
    real_open = fleet_router.open_readonly
    real_status = fleet_router.read_plane_status
    marker = plane_root / fleet_paths.RETIRED_LOCAL_MARKER

    def _retiring_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = real_status(*args, **kwargs)
        marker.write_text("retired\n", encoding="utf-8")
        return out

    def _counting_open(*args: Any, **kwargs: Any) -> Any:
        nonlocal open_calls
        open_calls += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(fleet_router, "read_plane_status", _retiring_status)
    monkeypatch.setattr(fleet_router, "open_readonly", _counting_open)

    app, _ctx = _make_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        r1 = client.get("/api/fleet/overview")
        assert r1.status_code == 500
        assert open_calls == 0

        # Shadow override permits reading the retired local fixture.
        monkeypatch.setattr(fleet_router, "read_plane_status", real_status)
        monkeypatch.setenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW", "1")
        r2 = client.get("/api/fleet/overview")
        assert r2.status_code == 200
        assert r2.headers["X-Snapshot-State"] == "fresh"

        # Case 2: Unsetting shadow override refuses immediately even while the cache entry is warm.
        monkeypatch.delenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW")
        r3 = client.get("/api/fleet/overview")
        assert r3.status_code == 500


def test_overview_cache_key_isolates_root_mode_telemetry_authority_and_hashes_pg_dsn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class: routing env changes collide in SWR cache or raw PG DSN leaks into cache keys."""
    root_a = tmp_path / "plane-a"
    root_b = tmp_path / "plane-b"
    db_a = _seed_plane_db(root_a)
    db_b = _seed_plane_db(root_b)
    with sqlite3.connect(db_a) as conn:
        _seed_conversation(conn)
        _insert_message(conn, "m-a", "from-a")
    with sqlite3.connect(db_b) as conn:
        _seed_conversation(conn)
        _insert_message(conn, "m-b1", "from-b")
        _insert_message(conn, "m-b2", "from-b")

    app, ctx = _make_app(tmp_path)
    with TestClient(app) as client:
        monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_a))
        ra = client.get("/api/fleet/overview")
        assert ra.json()["counts"]["messages"]["total"] == 1

        monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_b))
        rb = client.get("/api/fleet/overview")
        assert rb.json()["counts"]["messages"]["total"] == 2

        monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "shadow")
        r_shadow = client.get("/api/fleet/overview")
        assert r_shadow.json()["health"]["mode"] == "shadow"
        assert r_shadow.json()["authority_health"] is None

    # PG DSN must be hashed, never stored verbatim in cache keys.
    secret_dsn = "dbname=synthetic_fleet_a user=synthetic_reader_a"
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_FLEET_COMMS", "pg")
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_PG_DSN", secret_dsn)

    key_pg_1 = asyncio.run(fleet_router._overview_cache_key(ctx, root_a))
    assert secret_dsn not in key_pg_1
    assert "synthetic_reader_a" not in key_pg_1

    monkeypatch.setenv("LEARN_UKRAINIAN_CP_PG_DSN", "dbname=synthetic_fleet_b user=synthetic_reader_b")
    key_pg_2 = asyncio.run(fleet_router._overview_cache_key(ctx, root_a))
    assert key_pg_1 != key_pg_2


def test_overview_preserves_dependency_override_and_background_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class (F7): custom dependency solving drops BackgroundTasks registered by get_ctx overrides."""
    plane_root = tmp_path / "plane"
    _seed_plane_db(plane_root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_root))

    app, ctx = _make_app(tmp_path)
    ran: list[str] = []

    def _sub_dep(background_tasks: BackgroundTasks) -> str:
        background_tasks.add_task(ran.append, "override-ran")
        return "ok"

    def _override_ctx(_tag: str = Depends(_sub_dep)) -> MonitorContext:
        return ctx

    app.dependency_overrides[get_ctx] = _override_ctx
    with TestClient(app) as client:
        resp = client.get("/api/fleet/overview")
        assert resp.status_code == 200
    assert ran == ["override-ran"]
