"""Contract tests for body-free legacy comms route telemetry (#6106)."""

from __future__ import annotations

import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import comms_router, telemetry_router
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.api.telemetry import legacy_comms


@pytest.fixture()
def telemetry_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data" / "telemetry" / "legacy_comms_routes.db"
    monkeypatch.setattr(legacy_comms, "_default_db_path", lambda: path)
    legacy_comms._reset_initialized_paths_for_tests()
    return path


@pytest.fixture()
def client(tmp_path: Path, telemetry_db: Path):
    # comms_router resolves its broker DB from the app's MonitorContext
    # (#7269 step 5); pin the context at a missing broker DB exactly like
    # the old MESSAGE_DB patch did.
    base = fixture_context(tmp_path)
    ctx = base.with_roots(message_db_path=tmp_path / "missing-broker.db")
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(comms_router.router, prefix="/api/comms")
    app.include_router(telemetry_router.router)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _rows(path: Path) -> list[tuple]:
    with sqlite3.connect(str(path)) as connection:
        return connection.execute(
            """
            SELECT hour_utc, route_id, method, caller_class, status_class,
                   count, first_seen, last_seen
            FROM legacy_comms_route_usage
            ORDER BY route_id, caller_class, status_class
            """
        ).fetchall()


def test_route_matcher_is_exact_and_normalizes_parameters() -> None:
    assert legacy_comms.match_legacy_route("POST", "/api/comms/acknowledge/42") == "acknowledge"
    assert legacy_comms.match_legacy_route("GET", "/api/comms/acknowledge/42") is None
    assert legacy_comms.match_legacy_route("POST", "/api/comms/acknowledge/42/extra") is None
    assert legacy_comms.match_legacy_route("GET", "/api/comms/channels/private/messages") is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Learn-Uk-Caller": "canary"}, "canary"),
        ({"X-Learn-Uk-Caller": "not-allowlisted", "User-Agent": "Mozilla/5.0"}, "browser"),
        ({"User-Agent": "curl/8.0"}, "cli"),
        ({"User-Agent": "python-httpx/0.28"}, "programmatic"),
        ({"User-Agent": "opaque-client"}, "unknown"),
    ],
)
def test_caller_classification_returns_only_allowlisted_classes(headers, expected) -> None:
    assert legacy_comms.classify_caller(headers) == expected


def test_all_legacy_routes_and_validation_failures_are_counted(
    client: TestClient,
    telemetry_db: Path,
) -> None:
    assert client.post("/api/comms/acknowledge/1").status_code == 410

    payload = legacy_comms.legacy_comms_summary("1h", db_path=telemetry_db)
    assert payload["total"] == 1
    assert payload["by_caller"] == {"test": 1}
    assert payload["by_status"] == {"4xx": 1}
    assert payload["scope_note"].startswith("Direct ask-* CLI calls bypass")
    assert {item["route_id"]: item["count"] for item in payload["routes"]} == {"acknowledge": 1}
    assert telemetry_db.exists()


def test_route_writes_land_in_the_serving_context_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixture-built app must not write the process-wide checkout's store (#8542)."""
    process_default = tmp_path / "process-checkout" / "data" / "telemetry" / "legacy_comms_routes.db"
    monkeypatch.setattr(legacy_comms, "_default_db_path", lambda: process_default)
    legacy_comms._reset_initialized_paths_for_tests()
    ctx_root = tmp_path / "ctx-root"
    app = FastAPI()
    app.state.ctx = fixture_context(ctx_root)
    app.include_router(comms_router.router, prefix="/api/comms")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        assert test_client.post("/api/comms/acknowledge/1").status_code == 410
        summary = legacy_comms.legacy_comms_summary(
            "1h", db_path=legacy_comms.db_path_for_root(ctx_root.resolve())
        )

    assert summary["total"] == 1
    assert _rows(legacy_comms.db_path_for_root(ctx_root.resolve()))[0][1] == "acknowledge"
    assert not process_default.exists()


def test_telemetry_never_persists_paths_queries_bodies_credentials_or_raw_headers(
    client: TestClient,
    telemetry_db: Path,
) -> None:
    secrets = {
        "path-secret": "private-task-67890",
        "query-secret": "query-token-67890",
        "body-secret": "body-token-67890",
        "auth-secret": "auth-token-67890",
        "cookie-secret": "cookie-token-67890",
        "header-secret": "header-token-67890",
    }
    headers = {
        "Authorization": f"Bearer {secrets['auth-secret']}",
        "Cookie": f"session={secrets['cookie-secret']}",
        "X-Learn-Uk-Caller": secrets["header-secret"],
        "User-Agent": "curl/8.0",
    }
    assert client.post(
        f"/api/comms/acknowledge/1?token={secrets['query-secret']}&note={secrets['path-secret']}&body={secrets['body-secret']}",
        headers=headers,
    ).status_code == 410

    persisted = json.dumps(_rows(telemetry_db))
    assert all(secret not in persisted for secret in secrets.values())
    assert '"acknowledge"' in persisted


def test_summary_marks_window_incomplete_until_coverage_is_old_enough(
    telemetry_db: Path,
) -> None:
    now = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)
    legacy_comms.initialize_legacy_comms_telemetry(telemetry_db, now=now)
    payload = legacy_comms.legacy_comms_summary("7d", db_path=telemetry_db, now=now)
    assert payload["total"] == 0
    assert payload["window_fully_observed"] is False
    assert len(payload["routes"]) == 1
    assert payload["routes"][0]["route_id"] == "acknowledge"


def test_retention_cleanup_is_bounded_and_idempotent(telemetry_db: Path) -> None:
    now = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)
    legacy_comms.initialize_legacy_comms_telemetry(telemetry_db, now=now - timedelta(days=100))
    with sqlite3.connect(str(telemetry_db)) as connection:
        connection.execute(
            """
            INSERT INTO legacy_comms_route_usage(
                hour_utc, route_id, method, caller_class, status_class,
                count, first_seen, last_seen
            ) VALUES (?, 'acknowledge', 'POST', 'unknown', '4xx', 1, ?, ?)
            """,
            (
                "2026-04-01T00:00:00Z",
                "2026-04-01T00:00:00Z",
                "2026-04-01T00:00:00Z",
            ),
        )
        connection.commit()

    for _ in range(2):
        legacy_comms.record_legacy_route_usage(
            "acknowledge",
            "POST",
            "canary",
            410,
            db_path=telemetry_db,
            now=now,
        )

    rows = _rows(telemetry_db)
    assert len(rows) == 1
    assert rows[0][1:6] == ("acknowledge", "POST", "canary", "4xx", 2)


def test_concurrent_writers_increment_one_atomic_bucket(telemetry_db: Path) -> None:
    now = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)

    def record(_index: int) -> None:
        legacy_comms.record_legacy_route_usage(
            "acknowledge",
            "POST",
            "automation",
            410,
            db_path=telemetry_db,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(40)))

    assert _rows(telemetry_db)[0][5] == 40


def test_existing_database_permissions_are_migrated_to_owner_only(telemetry_db: Path) -> None:
    telemetry_db.parent.mkdir(parents=True)
    telemetry_db.touch(mode=0o644)
    telemetry_db.chmod(0o644)
    legacy_comms.initialize_legacy_comms_telemetry(telemetry_db)
    assert stat.S_IMODE(telemetry_db.stat().st_mode) == 0o600


def test_storage_failure_does_not_break_legacy_route(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args, **_kwargs) -> None:
        raise OSError("synthetic telemetry outage")

    monkeypatch.setattr(legacy_comms, "record_legacy_route_usage", fail)
    response = client.post("/api/comms/acknowledge/1")
    assert response.status_code == 410
