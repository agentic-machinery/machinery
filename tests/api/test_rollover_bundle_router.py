"""Historical store reads, schema constraints and lease fencing survive transport retirement."""
from __future__ import annotations
import json
import sqlite3
import pytest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from lu_machinery.session_streams.db import SessionStreamDatabase
from lu_machinery.session_streams.model import LeaseHolder
from lu_machinery.session_streams.store import SessionStreamStore


def _history_row(database, *, agent, lineage, generation, status, prepared_at, rollover_id, digest):
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO rollover_bundles(stream_id, agent, lineage_id, generation, rollover_id, status, "
            "prepared_at, bundle_sha256, manifest_json, blob, uploaded_at, uploaded_by_lease_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("epic:7178", agent, lineage, generation, rollover_id, status, prepared_at,
             digest * 64, json.dumps({"agent": agent, "generation": generation, "status": status,
             "prepared_at": prepared_at, "rollover_id": rollover_id}),
             rollover_id.encode(), prepared_at, "historical-lease"),
        )


def test_bundle_latest_without_agent_selects_highest_order_across_agents(tmp_path: Path, monkeypatch) -> None:
    """Historical selection preserves generation, status and preparation ordering across agents."""
    database = SessionStreamDatabase(tmp_path / "history.sqlite3")
    store = SessionStreamStore(database)
    store.open_session(stream_id="epic:7178",
        holder=LeaseHolder(agent="codex", harness="codex-cli", instance_id="history", process_id=1234),
        lineage_id="history-lineage", ttl_seconds=900)
    _history_row(database, agent="grok-infra", lineage="grok-lineage", generation=4, status="pending_start",
                 prepared_at="2026-08-24T10:00:00Z", rollover_id="rollover-generation", digest="a")
    _history_row(database, agent="claude-infra", lineage="claude-lineage", generation=3, status="confirmed",
                 prepared_at="2026-08-25T10:00:00Z", rollover_id="rollover-older-generation", digest="b")
    assert store.latest_rollover_bundle("epic:7178")["manifest"]["rollover_id"] == "rollover-generation"
    _history_row(database, agent="grok-infra", lineage="grok-lineage", generation=4, status="resumed",
                 prepared_at="2026-08-24T09:00:00Z", rollover_id="rollover-resumed", digest="c")
    assert store.latest_rollover_bundle("epic:7178")["manifest"]["rollover_id"] == "rollover-resumed"
    _history_row(database, agent="grok-infra", lineage="grok-lineage", generation=4, status="confirmed",
                 prepared_at="2026-08-24T08:00:00Z", rollover_id="rollover-confirmed", digest="d")
    assert store.latest_rollover_bundle("epic:7178")["manifest"]["rollover_id"] == "rollover-confirmed"
    _history_row(database, agent="grok-infra", lineage="grok-lineage", generation=4, status="confirmed",
                 prepared_at="2026-08-24T11:00:00Z", rollover_id="rollover-latest", digest="e")
    latest = store.latest_rollover_bundle("epic:7178")
    assert latest["manifest"]["rollover_id"] == "rollover-latest"
    assert latest["manifest"]["upload_seq"] == latest["upload_seq"] == 5
    assert latest["blob"] == b"rollover-latest"
    assert store.rollover_bundle_by_upload_seq("epic:7178", 5) == latest
    assert [row["upload_seq"] for row in store.list_rollover_bundles("epic:7178", agent="grok-infra")] == [5, 4, 3, 1]
    assert store.latest_rollover_bundle("epic:7178", agent="claude-infra")["manifest"]["rollover_id"] == "rollover-older-generation"


def test_rollover_schema_has_both_unique_indexes_and_raw_digest_constraint(
    tmp_path: Path,
) -> None:
    database = SessionStreamDatabase(tmp_path / "api.sqlite3")
    store = SessionStreamStore(database)
    lease = store.open_session(
        stream_id="epic:7178",
        holder=LeaseHolder(agent="codex", harness="codex-cli", instance_id="raw", process_id=1234),
        lineage_id="raw-lineage",
        ttl_seconds=900,
    )
    connection = database.connect()
    try:
        unique_indexes = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list('rollover_bundles')").fetchall()
            if int(row[2]) == 1
        }
        assert "rollover_bundles_bundle_sha256_unique" in unique_indexes
        assert "rollover_bundles_identity_unique" in unique_indexes
        foreign_keys = {
            (str(row[2]), str(row[3]), str(row[4]))
            for row in connection.execute("PRAGMA foreign_key_list('rollover_bundles')").fetchall()
        }
        assert foreign_keys == {("streams", "stream_id", "stream_id")}

        manifest_json = json.dumps(
            {
                "schema": "rollover-bundle.v1",
                "status": "pending_start",
                "prepared_at": "2026-08-24T16:00:00Z",
            }
        )
        row = (
            "epic:7178",
            "claude-infra",
            "raw-lineage",
            3,
            "rollover-raw-a",
            "pending_start",
            "2026-08-24T16:00:00Z",
            "a" * 64,
            manifest_json,
            b"raw",
            "2026-08-24T16:00:00Z",
            lease.lease_id,
        )
        columns = (
            "stream_id, agent, lineage_id, generation, rollover_id, status, prepared_at, "
            "bundle_sha256, manifest_json, blob, uploaded_at, uploaded_by_lease_id"
        )
        connection.execute(f"INSERT INTO rollover_bundles({columns}) VALUES ({','.join('?' for _ in row)})", row)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO rollover_bundles({columns}) VALUES ({','.join('?' for _ in row)})",
                (*row[:4], "rollover-raw-b", *row[5:]),
            )
    finally:
        connection.close()


def test_claim_replaces_released_lease_with_historical_bundle_token(tmp_path: Path) -> None:
    database = SessionStreamDatabase(tmp_path / "reclaim.sqlite3")
    store = SessionStreamStore(database)
    base_time = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    old_lease, _ = store.claim_remote_session(
        stream_id="epic:7178",
        holder=LeaseHolder(agent="claude", harness="claude-cli", instance_id="old", process_id=1234),
        lineage_id="historical-lineage",
        ttl_seconds=900,
        session_id="historical-session",
        lease_id="historical-lease",
        now=base_time,
    )
    store.release_remote_session(old_lease, now=base_time + timedelta(seconds=1))

    connection = database.connect()
    try:
        connection.execute(
            "INSERT INTO rollover_bundles("
            "stream_id, agent, lineage_id, generation, rollover_id, status, prepared_at, "
            "bundle_sha256, manifest_json, blob, uploaded_at, uploaded_by_lease_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "epic:7178",
                "claude-infra",
                "historical-lineage",
                1,
                "historical-rollover",
                "pending_start",
                "2026-08-25T10:00:00Z",
                "a" * 64,
                json.dumps({"status": "pending_start", "prepared_at": "2026-08-25T10:00:00Z"}),
                b"historical bundle",
                "2026-08-25T10:00:00Z",
                old_lease.lease_id,
            ),
        )
    finally:
        connection.close()

    successor, outcome = store.claim_remote_session(
        stream_id="epic:7178",
        holder=LeaseHolder(agent="grok", harness="grok-cli", instance_id="new", process_id=5678),
        lineage_id="successor-lineage",
        ttl_seconds=900,
        session_id="successor-session",
        lease_id="successor-lease",
        now=base_time + timedelta(seconds=2),
    )

    assert successor.lease_id == "successor-lease"
    assert outcome in {"claimed", "recovered"}
    with database.connect(read_only=True) as connection:
        bundle = connection.execute(
            "SELECT uploaded_by_lease_id FROM rollover_bundles WHERE rollover_id = ?",
            ("historical-rollover",),
        ).fetchone()
    assert bundle["uploaded_by_lease_id"] == old_lease.lease_id
