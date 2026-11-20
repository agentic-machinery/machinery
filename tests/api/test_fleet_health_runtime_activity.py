"""Fleet header jobs chip must name its ledger and see runtime/delegate work (#7079).

The fleet.html header chip derives its "jobs in the last 24h" count from the
fleet-comms ``authority_jobs`` table only. Runtime and delegate work is
recorded in a separate usage ledger, so a zero authority window is idle-now,
not zero-today. The health payload exposes both ledgers and the chip names
them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import lu_machinery.api.fleet_router as fleet_router
from lu_machinery.api.monitor_context import production_context
from lu_machinery.fleet_comms.migrations import apply_migrations


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.state.ctx = production_context()
    app.include_router(fleet_router.router, prefix="/api/fleet")
    return TestClient(app)


@pytest.fixture()
def fleet_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "fleet-comms" / "v1"
    root.mkdir(parents=True)
    connection = sqlite3.connect(root / "comms.sqlite3")
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")
    return root


def _runtime_records(*timestamps: str) -> dict[str, list[dict[str, str]]]:
    return {
        "records": [
            {
                "ts": ts,
                "source": "operator",
                "agent": "kimi",
                "via": "dispatch",
                "entrypoint": "dispatch",
                "model": "kimi",
                "outcome": "ok",
                "source_provenance": "explicit",
                "source_task_id": "task-7079",
            }
            for ts in timestamps
        ]
    }


def test_health_names_both_ledgers_when_authority_window_is_empty(
    client: TestClient, fleet_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idle authority window + runtime/delegate work must not read as zero-today."""
    now = fleet_router.datetime.now(fleet_router.UTC)
    inside = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    outside = (now - fleet_router.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        fleet_router,
        "recent_runtime_records",
        lambda *, limit, ctx=None: _runtime_records(inside, inside, outside),
    )
    # A deliberate expire-stale cleanup inside the window is not a failure.
    with sqlite3.connect(fleet_root / "comms.sqlite3") as connection:
        connection.execute(
            "INSERT INTO authority_dead_letters VALUES ('cleanup', NULL, NULL, 'expired_by_cleanup', ?)",
            (inside,),
        )

    health = client.get("/api/fleet/health").json()

    authority = health["authority_health"]
    assert authority["state"] == "idle"
    assert authority["dead_letters"] == 0
    assert authority["jobs"] == {"total": 0, "by_state": {}}
    runtime = health["runtime_activity"]
    assert runtime["ledger"] == "runtime_delegate_usage"
    assert runtime["availability"] == "available"
    assert runtime["records_in_window"] == 2


def test_overview_non_integer_migration_versions_fall_back_to_diagnostic_reader(
    client: TestClient, fleet_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-integer SQLite migration versions must fall back instead of rounding or raising OverflowError."""
    monkeypatch.delenv("LEARN_UKRAINIAN_CP_AUTHORITY", raising=False)
    monkeypatch.delenv("LEARN_UKRAINIAN_CP_FLEET_COMMS_AUTHORITY", raising=False)
    db_path = fleet_root / "comms.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        original_rows = connection.execute(
            "SELECT version, name, checksum, applied_at FROM comms_schema_migrations ORDER BY version"
        ).fetchall()
        connection.execute("DROP TABLE comms_schema_migrations")
        connection.execute(
            """CREATE TABLE comms_schema_migrations (
                version,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )"""
        )
        connection.commit()

        cases: list[tuple[object, str | None]] = [
            (0.9999999999999999, "schema_incompatible"),
            (1.0, None),
            (float("inf"), "sqlite_probe_failed"),
            (float("-inf"), "sqlite_probe_failed"),
            (None, "schema_incompatible"),
            ("1", None),
            (b"1", None),
        ]
        for malformed_version, expected_db_error in cases:
            connection.execute("DELETE FROM comms_schema_migrations")
            modified = [
                (malformed_version, original_rows[0][1], original_rows[0][2], original_rows[0][3]),
                *original_rows[1:],
            ]
            connection.executemany(
                "INSERT INTO comms_schema_migrations VALUES (?, ?, ?, ?)",
                modified,
            )
            connection.commit()

            assert fleet_router._sqlite_overview_snapshot(fleet_root) is None
            response = client.get("/api/fleet/overview")
            assert response.status_code == 200
            schema = response.json()["health"]["schema"]
            expected = fleet_router.read_plane_status(root=fleet_root, recent_limit=0)["schema"]
            assert schema["db_error"] == expected_db_error == expected.get("db_error")
            assert schema["applied_version"] == expected["applied_version"]

        connection.execute("DELETE FROM comms_schema_migrations")
        connection.executemany(
            "INSERT INTO comms_schema_migrations VALUES (?, ?, ?, ?)",
            original_rows,
        )
        connection.commit()
    finally:
        connection.close()

    assert fleet_router._sqlite_overview_snapshot(fleet_root) is not None
    restored = client.get("/api/fleet/overview")
    assert restored.status_code == 200
    assert restored.json()["health"]["schema"]["db_error"] is None
