"""Fleet header jobs chip must name its ledger and see runtime/delegate work (#7079).

The fleet.html header chip derives its "jobs in the last 24h" count from the
fleet-comms ``authority_jobs`` table only. Runtime and delegate work is
recorded in a separate usage ledger, so a zero authority window is idle-now,
not zero-today. The health payload exposes both ledgers and the chip names
them.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import lu_machinery.api.fleet_router as fleet_router
from lu_machinery import agent_identity
from lu_machinery.api.monitor_context import fixture_context, production_context
from lu_machinery.fleet_comms.migrations import apply_migrations
from lu_machinery.ops import driver_lanes
from lu_machinery.orchestration import launcher_aliases
from lu_machinery.session_streams import inventory
from lu_machinery.session_streams.model import LeaseHolder, isoformat_z


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
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_FLEET_COMMS", "sqlite")
    monkeypatch.delenv("LEARN_UKRAINIAN_CP_AUTHORITY", raising=False)
    monkeypatch.delenv("LEARN_UKRAINIAN_CP_PG_DSN", raising=False)
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


@pytest.fixture()
def fleet_panels(tmp_path, monkeypatch, fleet_root):
    app = FastAPI()
    ctx = fixture_context(tmp_path)
    app.state.ctx = ctx
    app.include_router(fleet_router.router, prefix="/api/fleet")
    registry_path = tmp_path / "driver_lanes.json"
    lane = {
        "kind": "claude", "goal": "driver.md", "launcher": "launch.sh",
        "model": "test-model", "effort": "high",
    }
    registry_path.write_text(json.dumps({
        "schema_version": 1, "guard_sender": "guard",
        **dict.fromkeys(driver_lanes.SETTINGS, 1),
        "lanes": [
            dict(lane, session="cto", name="cto", epic="lead-selector", sender="claude-epics", lead=True),
            dict(lane, session="test", name="driver", epic="test-stream", sender="driver-sender"),
            dict(lane, session="unresolved", name="unresolved", epic="missing-stream", sender="unresolved-sender"),
        ],
    }))
    monkeypatch.setattr(driver_lanes, "DATA_FILE", registry_path)
    aliases = tmp_path / "launcher_stream_aliases.tsv"
    aliases.write_text("lead-selector\tepics\tcto\n")
    monkeypatch.setattr(launcher_aliases, "ALIASES_PATH", aliases)
    stream_registry = tmp_path / "issue_streams.yaml"
    stream_registry.write_text("streams: {epics: {epics: [10453, 7]}, test-stream: {epics: [42]}}\n")
    monkeypatch.setattr(inventory, "DEFAULT_STREAMS_YAML", stream_registry)
    roster = tmp_path / "area_assignments.yaml"
    roster.write_text("external_recipients: [eggbot, mac-agent]\n")
    monkeypatch.setattr(agent_identity, "ASSIGNMENTS_PATH", roster)
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_FLEET_COMMS", "sqlite")
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_SESSION_STREAMS", "sqlite")
    return TestClient(app), ctx, roster, fleet_root / "comms.sqlite3"


def _add_delivery(db, index, recipient, created, *, state="queued", completed=None,
                  sender="unlisted-sender", kind="message", imported=False, missing=False, message_id=None):
    message_id = message_id or f"message_{index:032x}"
    with closing(sqlite3.connect(db)) as conn, conn:
        if not missing:
            conn.execute(
                """
                INSERT INTO comms_messages
                    (message_id, conversation_id, sender, kind, body_inline, body_artifact_id, metadata_json, created_at)
                VALUES (?, 'conversation', ?, ?, 'body-fixture-sentinel', 'artifact-fixture-sentinel',
                        '{"note":"metadata-fixture-sentinel"}', ?)
                """,
                (message_id, sender, kind, created),
            )
        if imported:
            conn.execute(
                "INSERT INTO authority_message_metadata VALUES (?, NULL, 'thread', ?, '{}', '{}', 'legacy', ?)",
                (message_id, "correlation-fixture-sentinel", created),
            )
        conn.execute(
            """
            INSERT INTO authority_deliveries
                (delivery_id, message_id, recipient, state, created_at, updated_at, completed_at, attempt_count)
            VALUES (?, ?, ?, ?, ?, '2026-11-22T21:01:00Z', ?, 1)
            """,
            (f"delivery-{index}", message_id, recipient, state, created, completed),
        )
    return message_id


def test_fleet_seats_uses_exact_mail_and_live_stream_activity(fleet_panels):
    """Defect class: alias mail, imports or terminal states are mistaken for a seat's activity."""
    client, ctx, _roster, db = fleet_panels
    now = datetime.now(UTC).replace(microsecond=0)
    cto_heartbeat = now - timedelta(minutes=2)
    store = ctx.stores.session_streams_store
    assert store is not None
    for stream, heartbeat in (("epic:10453", cto_heartbeat), ("epic:42", now - timedelta(hours=1))):
        store.open_session(
            stream_id=stream, holder=LeaseHolder("claude", "test", "instance", process_id=1),
            lineage_id="lineage", ttl_seconds=600, now=heartbeat,
        )
    old = isoformat_z(now - timedelta(days=12))
    earlier = isoformat_z(now - timedelta(minutes=20))
    ack = isoformat_z(now - timedelta(minutes=10))
    recent = isoformat_z(now - timedelta(minutes=1))
    _add_delivery(db, 1, "claude-cto", old)
    _add_delivery(db, 2, "claude-epics", earlier)
    _add_delivery(db, 3, "claude-cto-session", old)
    _add_delivery(db, 4, "cto", old)
    _add_delivery(db, 5, "driver-sender", earlier, state="acknowledged", completed=ack)
    _add_delivery(db, 6, "claude-cto", old, state="acknowledged", completed=ack)
    _add_delivery(db, 7, "claude-epics", old, state="acknowledged", completed=recent, imported=True)
    _add_delivery(db, 8, "driver-sender", old, state="expired", completed=recent)
    _add_delivery(db, 9, "driver-sender", old, state="failed", completed=recent)
    _add_delivery(db, 10, "eggbot", earlier)
    _add_delivery(db, 11, "eggbot", old, state="acknowledged", completed=ack)

    response = client.get("/api/fleet/seats")
    assert response.status_code == 200
    assert response.headers["X-Snapshot-State"] == "fresh"
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    seats = {row["seat"]: row for row in payload["seats"]}
    assert payload["total"] == len(seats) == 5
    assert set(seats) == {"cto", "driver", "unresolved", "eggbot", "mac-agent"}
    assert seats["cto"] == {
        "seat": "cto", "kind": "cto", "epic": "lead-selector", "queued": 2,
        "oldest_queued_at": old, "last_activity_at": isoformat_z(cto_heartbeat),
        "last_activity_source": "lease", "lease_state": "live", "lease_reason": None,
    }
    assert seats["driver"] == {
        "seat": "driver", "kind": "driver", "epic": "test-stream", "queued": 0,
        "oldest_queued_at": None, "last_activity_at": ack, "last_activity_source": "ack",
        "lease_state": "no_lease", "lease_reason": None,
    }
    assert seats["eggbot"] == {
        "seat": "eggbot", "kind": "external", "epic": None, "queued": 1,
        "oldest_queued_at": earlier, "last_activity_at": ack, "last_activity_source": "ack",
        "lease_state": "unknown", "lease_reason": None,
    }
    assert seats["unresolved"]["lease_reason"] == "stream_unresolved"
    assert seats["unresolved"]["lease_state"] == "unknown"


@pytest.mark.parametrize(("case", "availability"), [
    ("missing", "db_missing"), ("failed", "db_unavailable"),
    ("unsupported", "authority_unsupported_component"),
])
def test_fleet_seats_reports_unknown_lease_when_source_is_unavailable(fleet_panels, monkeypatch, case, availability):
    """Defect class: an unavailable lease source is displayed as an idle driver."""
    client, ctx, _roster, _db = fleet_panels
    if case == "unsupported":
        monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_SESSION_STREAMS", "pg")
    elif case == "failed":
        path = ctx.roots.session_streams_db_path
        path.parent.mkdir(parents=True)
        path.write_text("broken store")
    payload = client.get("/api/fleet/seats").json()
    cto = next(row for row in payload["seats"] if row["seat"] == "cto")
    assert payload["lease_availability"] == availability
    assert cto["lease_state"] == "unknown"
    assert cto["lease_reason"] == availability
    assert cto["last_activity_at"] is None
    assert cto["queued"] == 0


@pytest.mark.parametrize(("config", "expected"), [
    ("{}\n", "available"), ("external_recipients: []\n", "available"),
    (None, "unavailable"), ("[]\n", "unavailable"),
    ("external_recipients: eggbot\n", "unavailable"),
    ("external_recipients: [eggbot, 12]\n", "unavailable"),
    ("external_recipients: [eggbot, " + "1" * 4301 + "]\n", "unavailable"),
    ("external_recipients: [eggbot, eggbot]\n", "unavailable"),
    ("external_recipients: [' eggbot']\n", "unavailable"),
])
def test_fleet_seats_distinguishes_empty_and_unavailable_external_rosters(fleet_panels, config, expected):
    """Defect class: malformed roster input is treated as a shorter or empty external list."""
    client, ctx, roster, db = fleet_panels
    assert ctx.stores.session_streams_database is not None
    ctx.stores.session_streams_database.connect().close()
    if config is None:
        roster.unlink()
    else:
        roster.write_text(config)
    _add_delivery(db, 1, "claude-cto", isoformat_z(datetime.now(UTC)))
    response = client.get("/api/fleet/seats")
    assert response.headers["X-Snapshot-State"] == ("fresh" if expected == "available" else "unavailable")
    assert response.json()["external_roster"] == expected
    assert {row["seat"] for row in response.json()["seats"]} == {"cto", "driver", "unresolved"}
    assert response.json()["total"] == (3 if expected == "available" else None)
    delivery_response = client.get("/api/fleet/deliveries")
    assert delivery_response.headers["X-Snapshot-State"] == response.headers["X-Snapshot-State"]
    assert delivery_response.json()["external_roster"] == expected
    assert delivery_response.json()["availability"] == (
        "available" if expected == "available" else "roster_unavailable"
    )
    assert delivery_response.json()["total"] == (1 if expected == "available" else None)
    queue = client.get("/api/fleet/deliveries", params={"seat": "cto", "state": "queued"}).json()
    cto = next(row for row in response.json()["seats"] if row["seat"] == "cto")
    assert queue["total"] == cto["queued"] == 1


def test_fleet_deliveries_is_body_free_and_queued_totals_include_older_and_orphan_rows(fleet_panels, monkeypatch):
    """Defect class: the delivery join leaks message fields or drops rows counted in the queued backlog."""
    client, _ctx, _roster, db = fleet_panels
    now = datetime.now(UTC)
    old = isoformat_z(now - timedelta(days=12))
    recent = isoformat_z(now - timedelta(minutes=10))
    orphan_id = _add_delivery(db, 1, "claude-cto", old, missing=True)
    listed_id = _add_delivery(db, 2, "claude-epics", recent, sender="claude-cto-session")
    _add_delivery(db, 3, "claude-epics", recent, kind="unlisted-kind", message_id="unlisted-id")
    _add_delivery(db, 4, "claude-cto-session", recent)
    _add_delivery(db, 5, "driver-sender", old, state="acknowledged", completed=isoformat_z(now - timedelta(days=11)),
                  sender="eggbot", kind="request", imported=True)

    trace = []
    open_readonly = fleet_router.open_readonly

    def observe_sql(*args, **kwargs):
        conn = open_readonly(*args, **kwargs)
        conn.set_trace_callback(trace.append)
        return conn

    monkeypatch.setattr(fleet_router, "open_readonly", observe_sql)
    queue = client.get("/api/fleet/deliveries", params={"seat": "cto", "state": "queued", "limit": 2})
    assert queue.status_code == 200
    assert queue.headers["X-Snapshot-State"] == "fresh"
    assert queue.headers["X-Snapshot-Age-S"] == "0.000"
    assert queue.json()["total"] == 3
    assert queue.json()["next_offset"] == 2
    assert queue.json()["filters"] == {"seat": "cto", "state": "queued", "since": "all"}
    first, second = queue.json()["deliveries"]
    assert first == {
        "message_id": listed_id, "sender": "cto", "recipient": "cto", "kind": "message", "state": "queued",
        "attempt_count": 1, "created_at": recent, "completed_at": None, "waited_s": first["waited_s"],
    }
    assert first["waited_s"] >= 600
    assert second["message_id"] is None
    assert second["kind"] == second["sender"] == "other"

    page = client.get("/api/fleet/deliveries", params={"seat": "cto", "state": "queued", "since": "all", "offset": 2})
    assert page.json()["total"] == 3
    assert page.json()["next_offset"] is None
    assert len(page.json()["deliveries"]) == 1
    orphan = page.json()["deliveries"][0]
    assert orphan["message_id"] == orphan_id
    assert orphan["kind"] == orphan["sender"] == "other"
    assert client.get("/api/fleet/deliveries", params={"seat": "cto"}).json()["total"] == 2
    ack = client.get("/api/fleet/deliveries", params={"seat": "driver", "state": "acknowledged", "since": "all"})
    assert ack.json()["deliveries"][0]["waited_s"] == 86400
    assert ack.json()["deliveries"][0]["sender"] == "eggbot"
    assert ack.json()["deliveries"][0]["kind"] == "request"

    assert any("LEFT JOIN comms_messages" in sql for sql in trace)
    for token in ("body_inline", "body_artifact_id", "metadata_json", "correlation_id"):
        assert all(token not in sql for sql in trace)
    assert all("fixture-sentinel" not in response.text for response in (queue, page, ack))
    cto = next(row for row in client.get("/api/fleet/seats").json()["seats"] if row["seat"] == "cto")
    assert queue.json()["total"] == page.json()["total"] == cto["queued"]

    # Both lanes observe their exact configured recipient. A shared address
    # has no unambiguous delivery label even though both counts are known.
    lane_data = json.loads(driver_lanes.DATA_FILE.read_text())
    lane_data["lanes"][1]["sender"] = "claude-cto"
    driver_lanes.DATA_FILE.write_text(json.dumps(lane_data))
    shared = client.get("/api/fleet/deliveries", params={"seat": "driver", "state": "queued"}).json()
    seats = {row["seat"]: row for row in client.get("/api/fleet/seats").json()["seats"]}
    assert seats["driver"]["queued"] == shared["total"] == 1
    assert shared["deliveries"][0]["recipient"] == "other"
    assert seats["cto"]["queued"] == 3

    trace.clear()
    for name, value, detail in (
        ("seat", "claude-epics", "unknown_seat"),
        ("seat", "cto' OR 1=1 --", "unknown_seat"),
        ("state", "not-a-state", "unknown_state"),
        ("since", "14d", "unknown_since"),
    ):
        response = client.get("/api/fleet/deliveries", params={name: value})
        assert response.status_code == 422
        assert response.json() == {"detail": detail}
    assert trace == []


@pytest.mark.parametrize(("case", "availability"), [
    ("pg", "authority_unsupported_component"), ("missing", "db_missing"),
    ("root", "plane_root_unavailable"), ("read", "db_unavailable"), ("table", "db_unavailable"),
])
def test_fleet_panels_failed_plane_reads_have_null_totals(fleet_panels, monkeypatch, case, availability):
    """Defect class: failed or unsupported reads masquerade as empty results or expose the driver error."""
    client, _ctx, _roster, db = fleet_panels
    if case == "pg":
        monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_FLEET_COMMS", "pg")
    elif case == "missing":
        db.unlink()
    elif case == "root":
        monkeypatch.delenv("FLEET_COMMS_ROOT")
    elif case == "read":
        db.write_text("unreadable-store-sentinel")
    else:
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("DROP TABLE authority_deliveries")
    for route in ("seats", "deliveries"):
        response = client.get(f"/api/fleet/{route}")
        assert response.status_code == 200
        assert response.headers["X-Snapshot-State"] == "unavailable"
        payload = response.json()
        assert payload["availability"] == availability
        assert payload["total"] is None
        assert "unreadable-store-sentinel" not in response.text
        if route == "seats":
            assert payload["seats"]
            assert all(row["queued"] is None for row in payload["seats"])
        else:
            assert payload["deliveries"] == []
