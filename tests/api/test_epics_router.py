"""Loopback router contract for remote epic lifecycle v1."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lu_machinery.api import epics_router
from lu_machinery.api.config import LIVE_REPO_ROOT
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.dispatcher import host_admission
from lu_machinery.session_streams.model import SessionState
from lu_machinery.session_streams.store import LifecycleError, SessionStreamStore
from tests.epics_monitor_stub import epics_app_for_store


def _client_and_store(tmp_path: Path, monkeypatch) -> tuple[TestClient, SessionStreamStore]:
    del monkeypatch
    live = Path(LIVE_REPO_ROOT)
    ctx_root = tmp_path / "ctx"
    ctx_root.mkdir()
    ctx = fixture_context(ctx_root)
    store = ctx.stores.epics_store
    assert store is not None
    epics_router.seed_manifest_inventory(live, store=store, handoff_root=live)
    app = epics_app_for_store(store, ctx_root, live_repo_root=live)
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 8765)), store


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    client, _store = _client_and_store(tmp_path, monkeypatch)
    return client


def _claim(
    client: TestClient,
    stream_id: str = "epic:7178",
    *,
    session_id: str = "api-session",
    lease_id: str = "api-lease",
) -> dict:
    response = client.post(
        f"/api/epics/v1/{stream_id}/claim",
        json={
            "session_id": session_id,
            "lease_id": lease_id,
            "lineage_id": "api-lineage",
            "agent": "codex",
            "harness": "codex-cli",
            "instance_id": "api-instance",
            "process_id": 1234,
            "host_id": "api-host",
            "ttl_seconds": 900,
            "digest_limit": 5,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_router_claim_heartbeat_handoff_release_round_trip(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    claimed = _claim(client)
    lease = claimed["lease"]
    assert claimed["outcome"] == "claimed"
    assert _claim(client)["outcome"] == "replayed"

    heartbeat = client.post("/api/epics/v1/epic:7178/heartbeat", json=lease)
    assert heartbeat.status_code == 200
    exact = heartbeat.json()["lease"]
    handoff = client.post(
        "/api/epics/v1/epic:7178/handoff",
        json={**exact, "type": "state", "body": "working", "idempotency_key": "api-state-1"},
    )
    assert handoff.status_code == 200
    assert handoff.json()["entry"]["type"] == "state"

    released = client.post("/api/epics/v1/epic:7178/release", json=exact)
    assert released.status_code == 200
    assert released.json()["outcome"] == "released"
    assert (
        client.post("/api/epics/v1/epic:7178/release", json=exact).status_code == 200
    )  # allow-hardcoded-epic: remote lifecycle route fixture


def test_lease_without_timestamps_renders_unknown_not_epoch(tmp_path: Path, monkeypatch) -> None:
    """Defect class: a missing lease timestamp is published as a 1970-01-01 date.

    The remote client releases with an identity envelope that carries no timestamps.
    The reply must show the stored lease, and the dispatcher must still accept it.
    """
    client = _client(tmp_path, monkeypatch)
    claimed = _claim(client)["lease"]
    identity = {
        key: claimed[key]
        for key in ("stream_id", "session_id", "lease_id", "generation", "fencing_token", "holder")
    }

    parsed = epics_router._lease_from_payload(identity)
    assert (parsed.heartbeat_at, parsed.expires_at) == ("unknown", "unknown")

    released = client.post("/api/epics/v1/epic:7178/release", json=identity)

    assert released.status_code == 200, released.text
    assert "1970-" not in released.text
    reply = released.json()["lease"]
    stored = ("heartbeat_at", "expires_at", "ttl_seconds")
    assert {key: reply[key] for key in stored} == {key: claimed[key] for key in stored}
    assert 0 <= reply["age_seconds"] < 300
    assert (reply["state"], reply["session_state"]) == ("released", "closed")
    assert host_admission._valid_projection_lease(reply, identity["stream_id"], match_ttl=False)
    # The release transaction's own lease: what the store holds afterwards, also on a replay.
    current = client.get("/api/epics/v1/epic:7178").json()["lease"]
    replayed = client.post("/api/epics/v1/epic:7178/release", json=identity).json()["lease"]
    for key in (*stored, "version"):
        assert reply[key] == current[key] == replayed[key], key


def test_router_heartbeat_reads_session_state_on_the_write_connection(tmp_path: Path, monkeypatch) -> None:
    """Defect class: per-request connection fan-out on heartbeat (#935).

    The handler opened a second connection to re-read state and session_state
    the write transaction already had. Each connection repeats its setup, and
    every setup statement yields the GIL, so the route stalled for seconds
    while the API process was busy.
    """
    client, store = _client_and_store(tmp_path, monkeypatch)
    lease = _claim(client)["lease"]
    opened: list[bool] = []
    real_connect = store.database.connect

    def counting_connect(*args, **kwargs):
        opened.append(bool(kwargs.get("read_only", False)))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store.database, "connect", counting_connect)
    heartbeat = client.post("/api/epics/v1/epic:7178/heartbeat", json=lease)
    assert heartbeat.status_code == 200, heartbeat.text
    body = heartbeat.json()["lease"]
    assert body["state"] == "active"
    assert body["session_state"] == SessionState.OPEN.value
    assert opened == [False]

    renewed = epics_router._lease_from_payload(body)
    assert (
        store.transition_session(renewed, to_state=SessionState.ROLLING, reason="bridge")
        == SessionState.ROLLING
    )
    opened.clear()
    rolling = client.post("/api/epics/v1/epic:7178/heartbeat", json=body)
    assert rolling.status_code == 200, rolling.text
    rolled = rolling.json()["lease"]
    assert rolled["state"] == "active"
    assert rolled["session_state"] == SessionState.ROLLING.value
    assert opened == [False]


def test_router_handoff_accepts_idempotency_key_header_alias(tmp_path: Path, monkeypatch) -> None:
    """The Idempotency-Key HTTP header is accepted as an alias for body.idempotency_key."""
    client = _client(tmp_path, monkeypatch)
    lease = _claim(client)["lease"]
    handoff = client.post(
        "/api/epics/v1/epic:7178/handoff",
        json={**lease, "type": "state", "body": "working"},
        headers={"Idempotency-Key": "hdr-state-1"},
    )
    assert handoff.status_code == 200, handoff.text
    assert handoff.json()["entry"]["type"] == "state"
    assert "Idempotent-Replayed" not in handoff.headers


def test_router_handoff_header_body_idempotency_key_conflict_is_400(tmp_path: Path, monkeypatch) -> None:
    """A header/body idempotency-key mismatch is refused rather than silently preferring one."""
    client = _client(tmp_path, monkeypatch)
    lease = _claim(client)["lease"]
    handoff = client.post(
        "/api/epics/v1/epic:7178/handoff",
        json={**lease, "type": "state", "body": "working", "idempotency_key": "body-key"},
        headers={"Idempotency-Key": "header-key"},
    )
    assert handoff.status_code == 400


def test_router_handoff_replay_sets_idempotent_replayed_header(tmp_path: Path, monkeypatch) -> None:
    """Replaying the exact same handoff key/content marks the response as a replay."""
    client = _client(tmp_path, monkeypatch)
    lease = _claim(client)["lease"]
    first = client.post(
        "/api/epics/v1/epic:7178/handoff",
        json={**lease, "type": "state", "body": "working", "idempotency_key": "replay-key-1"},
    )
    assert first.status_code == 200, first.text
    assert "Idempotent-Replayed" not in first.headers

    replay = client.post(
        "/api/epics/v1/epic:7178/handoff",
        json={**lease, "type": "state", "body": "working"},
        headers={"Idempotency-Key": "replay-key-1"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.json()["entry"]["type"] == "state"


def test_router_handoff_missing_stream_is_lease_lost_not_store_error(tmp_path: Path, monkeypatch) -> None:
    """A handoff against a missing stream is 409 LEASE LOST, not a digest-probe 5xx (#7488)."""
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/epics/v1/epic:999999999/handoff",
        json={
            "stream_id": "epic:999999999",
            "session_id": "missing-session",
            "lease_id": "missing-lease",
            "generation": 1,
            "fencing_token": 1,
            "agent": "codex",
            "harness": "codex-cli",
            "instance_id": "missing-instance",
            "process_id": 1234,
            "host_id": "api-host",
            "ttl_seconds": 900,
            "type": "state",
            "body": "working",
            "idempotency_key": "missing-stream-1",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "LEASE LOST"


def test_router_rejects_live_holder_force_without_actor_and_opsec_body(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _claim(client)
    competing = client.post(
        "/api/epics/v1/epic:7178/claim",
        json={
            "session_id": "other-session",
            "lease_id": "other-lease",
            "lineage_id": "other-lineage",
            "agent": "gemini",
            "harness": "agy",
            "instance_id": "other-instance",
            "process_id": 5678,
            "host_id": "other-host",
        },
    )
    assert competing.status_code == 409
    assert (
        client.post("/api/epics/v1/epic:7178/release", json={"force": True}).status_code == 403
    )  # allow-hardcoded-epic: remote lifecycle route fixture

    bad = client.post(
        "/api/epics/v1/epic:7178/handoff",
        json={**_claim(client)["lease"], "type": "note", "body": "203.0.113.1", "idempotency_key": "bad"},
    )
    # The claim above replays the first lease, so the body is rejected by the API hygiene gate.
    assert bad.status_code == 400


def test_router_live_claim_conflict_names_holder_and_expiry(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _claim(client)
    competing = client.post(
        "/api/epics/v1/epic:7178/claim",
        json={
            "session_id": "other-session",
            "lease_id": "other-lease",
            "lineage_id": "other-lineage",
            "agent": "gemini",
            "harness": "agy",
            "instance_id": "other-instance",
            "process_id": 5678,
            "host_id": "other-host",
        },
    )
    assert competing.status_code == 409
    assert "current holder=codex/codex-cli" in competing.text
    assert f"lease_digest={client.get('/api/epics/v1/epic:7178').json()['lease']['lease_digest']}" in competing.text
    assert "api-instance" not in competing.text and "instance_id" not in competing.text
    assert "expires_at=" in competing.text


_HOLDER_SECRETS = ("api-session", "api-lease", "api-instance")


def _assert_no_holder_secrets(lease: dict) -> None:
    assert not {"session_id", "lease_id"} & set(lease), lease
    assert not {"instance_id", "process_id"} & set(lease["holder"]), lease
    assert len(lease["lease_digest"]) == 64


def test_status_never_lets_a_reader_rebuild_and_use_the_holders_lease(tmp_path: Path, monkeypatch) -> None:
    """Defect class: lease takeover by an unauthorized local caller. Any loopback process reads stream status;
    when status, listing or a 409 published the holder's exact envelope, the reader could renew, hand off or
    release that holder's lease. Status carries lease_digest instead; holder operations still match exactly."""
    from lu_machinery.session_supervisor.remote import envelope_digest

    client, store = _client_and_store(tmp_path, monkeypatch)
    claimed = _claim(client)["lease"]
    assert claimed["session_id"] == "api-session"  # the claimer's own reply keeps the envelope it chose
    assert claimed["lease_digest"] == envelope_digest(claimed)

    status = client.get("/api/epics/v1/epic:7178").json()["lease"]
    listed = next(row for row in client.get("/api/epics/v1").json()["streams"] if row["stream_id"] == "epic:7178")
    competing = client.post(
        "/api/epics/v1/epic:7178/claim",
        json={"session_id": "s2", "lease_id": "l2", "lineage_id": "x", "agent": "gemini", "harness": "agy",
              "instance_id": "other", "process_id": 5678},
    )
    assert competing.status_code == 409
    for lease in (status, listed["lease"]):
        _assert_no_holder_secrets(lease)
        assert lease["lease_digest"] == claimed["lease_digest"]
        assert (lease["generation"], lease["fencing_token"]) == (claimed["generation"], claimed["fencing_token"])
    for body in (json.dumps(status), json.dumps(listed), competing.text):
        assert not any(secret in body for secret in _HOLDER_SECRETS), body

    # The strongest reader: it also knows the holder's instance id and pid from elsewhere and guesses the ids.
    rebuilt = {
        **status,
        "session_id": "turn-session-api-instance",
        "lease_id": "turn-lease-api-instance",
        "holder": {**status["holder"], "instance_id": "api-instance", "process_id": 1234},
    }
    before = store.dump_stream("epic:7178")
    assert client.post("/api/epics/v1/epic:7178/heartbeat", json=rebuilt).status_code == 409
    handoff = {**rebuilt, "type": "state", "body": "taken", "idempotency_key": "rebuilt-1"}
    assert client.post("/api/epics/v1/epic:7178/handoff", json=handoff).status_code == 409
    assert client.post("/api/epics/v1/epic:7178/release", json=rebuilt).status_code == 409
    assert store.dump_stream("epic:7178") == before
    # Paired control: the holder's exact envelope still renews and releases its lease.
    assert client.post("/api/epics/v1/epic:7178/heartbeat", json=claimed).status_code == 200
    released = client.post("/api/epics/v1/epic:7178/release", json=claimed)
    assert released.status_code == 200 and released.json()["lease"]["lease_id"] == "api-lease"


@pytest.mark.parametrize("turn_first", [True, False])
def test_router_turn_conflict_has_its_stable_detail_prefix(tmp_path: Path, monkeypatch, turn_first: bool) -> None:
    """Defect class T1: a turn/legacy conflict reaches the dispatcher as a bare 409 it cannot tell from an invariant
    failure, so a held stream reads as unknown, or an invariant 409 reads as a held stream.
    Also verifies the 409 detail is built from fixed text, never echoed from TurnLeaseUnclearedError text."""
    from lu_machinery.session_streams.store import (
        TURN_CONFLICT_PREFIX,
        TURN_HOLDER_HARNESS,
        TurnLeaseUnclearedError,
    )

    client, store = _client_and_store(tmp_path, monkeypatch)
    turn = {"harness": TURN_HOLDER_HARNESS, "instance_id": "a" * 32, "task_id": "turn-a"}
    first, second = ({**turn}, {}) if turn_first else ({}, {**turn})
    held = client.post("/api/epics/v1/epic:7178/claim", json={
        "session_id": "first-session", "lease_id": "first-lease", "lineage_id": "first", "agent": "claude",
        "harness": "codex-cli", "instance_id": "first-instance", "process_id": 1234, "ttl_seconds": 1, **first})
    assert held.status_code == 200, held.text
    later = epics_router.parse_timestamp("2099-01-01T00:00:00Z")  # long past the holder's TTL
    monkeypatch.setattr(epics_router, "utc_now", lambda: later)
    monkeypatch.setattr("lu_machinery.session_streams.store.utc_now", lambda: later)
    claim_body = {
        "session_id": "other-session", "lease_id": "other-lease", "lineage_id": "other", "agent": "claude",
        "harness": "codex-cli", "instance_id": "other-instance", "process_id": 5678, **second}
    competing = client.post("/api/epics/v1/epic:7178/claim", json=claim_body)
    assert competing.status_code == 409
    detail = competing.json()["detail"]
    assert detail.startswith(f"{TURN_CONFLICT_PREFIX}: "), detail
    holder = TURN_HOLDER_HARNESS if turn_first else "codex-cli"
    assert f"current holder=claude/{holder}" in detail and "expires_at=" in detail

    def _raise_with_sentinel(**_kwargs):
        raise TurnLeaseUnclearedError("internal-exception-sentinel")

    monkeypatch.setattr(store, "claim_remote_session", _raise_with_sentinel)
    masked = client.post("/api/epics/v1/epic:7178/claim", json=claim_body)
    assert masked.status_code == 409
    masked_detail = masked.json()["detail"]
    assert masked_detail.startswith(f"{TURN_CONFLICT_PREFIX}: "), masked_detail
    assert "internal-exception-sentinel" not in masked.text
    assert f"current holder=claude/{holder}" in masked_detail and "expires_at=" in masked_detail


def test_router_responses_have_no_paths_or_host_network_tokens(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    response = _claim(client)
    response_text = json.dumps(response, sort_keys=True)
    assert "/Users/" not in response_text
    assert "/home/" not in response_text
    assert "127.0.0.1" not in response_text
    assert "atlas-runner" not in response_text
    status = client.get("/api/epics/v1/epic:7178")
    assert status.status_code == 200
    assert "/Users/" not in status.text
    assert "/home/" not in status.text


@pytest.mark.parametrize("failure", [sqlite3.IntegrityError("foreign key"), LifecycleError("unsafe lifecycle")])
def test_router_claim_invariant_failures_are_logged_and_not_store_unavailable(
    tmp_path: Path, monkeypatch, caplog, failure: Exception
) -> None:
    ctx_root = tmp_path / "ctx"
    ctx_root.mkdir()
    ctx = fixture_context(ctx_root)
    store = ctx.stores.epics_store
    assert store is not None

    def fail_claim(**_kwargs):
        raise failure

    monkeypatch.setattr(store, "claim_remote_session", fail_claim)
    app = epics_app_for_store(store, ctx_root, live_repo_root=Path(LIVE_REPO_ROOT))

    with caplog.at_level(logging.ERROR, logger=epics_router.__name__):
        response = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 8765)).post(
            "/api/epics/v1/epic:7178/claim",
            json={
                "session_id": "failed-session",
                "lease_id": "failed-lease",
                "lineage_id": "failed-lineage",
                "agent": "codex",
                "harness": "codex-cli",
                "instance_id": "failed-instance",
                "process_id": 1234,
                "host_id": "failed-host",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "epic claim rejected by a session-stream invariant"
    assert not response.json()["detail"].startswith("turn_lease_uncleared")
    assert "store unavailable" not in response.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("force", [True, False, "true"])
def test_router_refuses_force_release_so_a_local_caller_cannot_take_over_a_lease(
    tmp_path: Path, monkeypatch, force: object
) -> None:
    """Defect class: lease takeover by an unauthorized local caller. Any loopback process could post an asserted
    operator identity and force-release another holder's live lease; force release is operator-only, on the
    Monitor host's store through the session supervisor CLI."""
    client, store = _client_and_store(tmp_path, monkeypatch)
    lease = _claim(client)["lease"]
    before = store.dump_stream("epic:7178")
    response = client.post(
        "/api/epics/v1/epic:7178/release",
        json={
            **lease,
            "force": force,
            "actor_agent": "operator",
            "actor_host_id": "operator-host",
            "reason": "operator recovery",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == epics_router.FORCE_RELEASE_REFUSAL
    assert store.dump_stream("epic:7178") == before
    assert client.get("/api/epics/v1/epic:7178").json()["lease"]["state"] == "active"


def test_router_rejects_shared_stream_aliases(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/epics/v1/shared:legacy/claim",
        json={"session_id": "session", "lease_id": "lease", "lineage_id": "lineage"},
    )
    assert response.status_code == 400


def test_router_redacts_legacy_uri_references() -> None:
    payload = {
        "entry_id": 1,
        "stream": "epic:7178",
        "session_id": "session",
        "agent": "codex",
        "harness": "codex-cli",
        "idempotency_key": "key",
        "body": "safe",
        "refs": [{"kind": "source", "uri": "/Users/alice/private.txt", "target_entry_id": None}],
    }
    sanitized = epics_router._safe_entry(payload)
    assert "/Users/" not in json.dumps(sanitized)
    assert sanitized["stream"] == "epic:7178"  # allow-hardcoded-epic: remote lifecycle response fixture
    assert sanitized["refs"] == []


def test_epics_graph_endpoint_contract_and_structure(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    mock_audit = {
        "generated_at": 1700000000,
        "membership_complete": True,
        "incomplete_nodes": [],
        "effective_membership": {
            "7269": {"epics": [7919], "streams": ["monitor"], "via": "native", "unique_stream": True},  # allow-hardcoded-epic: mock audit data
            "7270": {"epics": [7919], "streams": ["monitor"], "via": "native", "unique_stream": True},  # allow-hardcoded-epic: mock audit data
            "7271": {"epics": [7919], "streams": ["monitor"], "via": "native", "unique_stream": True},  # allow-hardcoded-epic: mock audit data
            "7100": {"epics": [7919], "streams": ["monitor"], "via": "native", "unique_stream": True},  # allow-hardcoded-epic: mock audit data
        },
        "open_issue_numbers": [7269, 7270, 7271],
        "open_issue_titles": {
            "7269": "Epics graph endpoint",
            "7270": "Dashboard map view",
            "7271": "Verification gates",
        },
    }

    monkeypatch.setattr(epics_router.audit, "read_cache", lambda max_age_s: mock_audit if max_age_s == 3600 else None)
    monkeypatch.setattr(
        epics_router.audit,
        "read_refresh_state",
        lambda: {"phase": "idle", "last_outcome": "succeeded", "last_outcome_at": 1700000000},
    )

    response = client.get("/api/epics/graph/v1")
    assert response.status_code == 200
    data = response.json()

    assert data["schema"] == "epics-graph.v1"
    assert "generated_at" in data
    assert data["refreshing"] is False
    assert data["refresh"]["phase"] == "idle"
    # AC-02 (#8870): a complete cache yields the same payload as before the
    # completeness gate — no membership_complete/incomplete_nodes markers.
    assert "membership_complete" not in data
    assert "incomplete_nodes" not in data

    # Verify nodes.areas
    areas = {a["id"]: a for a in data["nodes"]["areas"]}
    assert "area:monitor" in areas
    assert areas["area:monitor"]["stream_id"] == "monitor"
    assert areas["area:monitor"]["epic_count"] == 1
    assert "Ops API + UI" in areas["area:monitor"]["title"]

    # Verify nodes.epics
    epics = {e["id"]: e for e in data["nodes"]["epics"]}
    assert "epic:7919" in epics  # allow-hardcoded-epic: graph contract check
    monitor_epic = epics["epic:7919"]  # allow-hardcoded-epic: graph contract check
    assert monitor_epic["number"] == 7919  # allow-hardcoded-epic: graph contract check
    assert monitor_epic["area_id"] == "monitor"
    assert monitor_epic["open_issue_count"] == 3
    assert monitor_epic["closed_issue_count"] == 1
    assert "last_state" in monitor_epic
    assert "last_decision" in monitor_epic
    assert "last_next_action" in monitor_epic

    # Verify edges
    edge_pairs = {(e["from"], e["to"]) for e in data["edges"]}
    assert ("area:monitor", "epic:7919") in edge_pairs  # allow-hardcoded-epic: graph edge check

    # Verify issues_by_epic
    assert "7919" in data["issues_by_epic"]  # allow-hardcoded-epic: issues_by_epic check
    epic_issues = data["issues_by_epic"]["7919"]  # allow-hardcoded-epic: issues_by_epic check
    assert epic_issues["total_open"] == 3
    assert epic_issues["truncated"] is False
    assert len(epic_issues["items"]) == 3

    first_item = epic_issues["items"][0]
    assert first_item["number"] == 7269
    assert first_item["title"] == "Epics graph endpoint"
    assert first_item["state"] == "open"
    assert first_item["url"] == "https://github.com/learn-ukrainian/learn-ukrainian.github.io/issues/7269"


def test_epics_graph_stale_and_no_cache_fallbacks(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    stale_audit = {
        "generated_at": 1600000000,
        "effective_membership": {},
        "open_issue_numbers": [],
        "open_issue_titles": {},
    }

    # Case 1: Fresh cache miss, stale cache hit
    monkeypatch.setattr(
        epics_router.audit,
        "read_cache",
        lambda max_age_s: stale_audit if max_age_s > 3600 else None,
    )
    scheduled = []
    monkeypatch.setattr(
        epics_router.audit,
        "schedule_refresh",
        lambda force=False: scheduled.append(force) or {"phase": "scheduled", "requested_at": 1700000000},
    )

    resp_stale = client.get("/api/epics/graph/v1")
    assert resp_stale.status_code == 200
    stale_data = resp_stale.json()
    assert stale_data["stale"] is True
    assert stale_data["refreshing"] is True
    assert scheduled == [False]

    # Case 2: Complete cache miss (no fresh, no stale)
    monkeypatch.setattr(epics_router.audit, "read_cache", lambda max_age_s: None)
    scheduled.clear()
    resp_no_cache = client.get("/api/epics/graph/v1")
    assert resp_no_cache.status_code == 200
    no_cache_data = resp_no_cache.json()
    assert no_cache_data["status"] == "no-cache"
    assert no_cache_data["ok"] is None
    assert scheduled == [False]

    # Case 3: Explicit fresh=true param
    scheduled.clear()
    resp_fresh = client.get("/api/epics/graph/v1?fresh=true")
    assert resp_fresh.status_code == 200
    assert scheduled == [True]


def test_epics_graph_incomplete_cache_drops_membership(tmp_path: Path, monkeypatch) -> None:
    """AC-01 (#8870): an explicitly incomplete traversal must not be shown as membership."""
    client = _client(tmp_path, monkeypatch)

    incomplete_audit = {
        "generated_at": 1700000000,
        "membership_complete": False,
        "incomplete_nodes": [7100],
        "effective_membership": {
            "7269": {"epics": [7919], "streams": ["monitor"], "via": "native", "unique_stream": True},  # allow-hardcoded-epic: mock audit data
        },
        "open_issue_numbers": [7269],
        "open_issue_titles": {"7269": "Epics graph endpoint"},
    }
    monkeypatch.setattr(epics_router.audit, "read_cache", lambda max_age_s: incomplete_audit)
    monkeypatch.setattr(epics_router.audit, "read_refresh_state", lambda: {"phase": "idle"})

    response = client.get("/api/epics/graph/v1")
    assert response.status_code == 200
    data = response.json()

    assert data["membership_complete"] is False
    assert data["incomplete_nodes"] == [7100]  # allow-hardcoded-epic: mock audit data

    monitor_epic = {e["id"]: e for e in data["nodes"]["epics"]}[
        "epic:7919"
    ]  # allow-hardcoded-epic: graph contract check
    assert monitor_epic["open_issue_count"] is None
    assert monitor_epic["closed_issue_count"] is None
    epic_issues = data["issues_by_epic"]["7919"]  # allow-hardcoded-epic: graph contract check
    assert epic_issues["total_open"] is None
    assert epic_issues["items"] is None
    assert epic_issues["truncated"] is None


def test_epics_graph_denied_audit_spawn_returns_no_cache_200(tmp_path: Path, monkeypatch) -> None:
    """merge_group has no host audit cache; denied Popen must stay 200 no-cache."""
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(epics_router.audit, "read_cache", lambda max_age_s: None)

    def _denied(*, force: bool = False) -> dict:
        del force
        raise AssertionError("OPSEC sweep fixture forbids subprocess execution")

    monkeypatch.setattr(epics_router.audit, "schedule_refresh", _denied)
    response = client.get("/api/epics/graph/v1")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "no-cache"
    assert data["ok"] is None
    assert data["refreshing"] is False
    assert data["refresh"]["phase"] == "idle"


def test_epics_graph_store_lease_and_decision_passthrough(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    claimed = _claim(client, "epic:7919")  # allow-hardcoded-epic: remote lifecycle route fixture
    lease = claimed["lease"]

    # Append a state entry and a decision entry
    client.post(
        "/api/epics/v1/epic:7919/handoff",  # allow-hardcoded-epic: remote lifecycle route fixture
        json={**lease, "type": "state", "body": "working on graph", "idempotency_key": "graph-state-1"},
    )
    client.post(
        "/api/epics/v1/epic:7919/handoff",  # allow-hardcoded-epic: remote lifecycle route fixture
        json={**lease, "type": "decision", "body": "design approved", "idempotency_key": "graph-dec-1"},
    )

    monkeypatch.setattr(epics_router.audit, "read_cache", lambda max_age_s: {"generated_at": 1700000000})
    monkeypatch.setattr(epics_router.audit, "read_refresh_state", lambda: {"phase": "idle"})

    response = client.get("/api/epics/graph/v1")
    assert response.status_code == 200
    data = response.json()

    epics_by_id = {e["id"]: e for e in data["nodes"]["epics"]}
    assert "epic:7919" in epics_by_id  # allow-hardcoded-epic: store passthrough check
    epic_7919 = epics_by_id["epic:7919"]  # allow-hardcoded-epic: store passthrough check
    assert epic_7919["lease"] is not None
    _assert_no_holder_secrets(epic_7919["lease"])
    assert epic_7919["lease"]["lease_digest"] == lease["lease_digest"]
    assert epic_7919["last_state"]["body"] == "working on graph"
    assert epic_7919["last_decision"]["body"] == "design approved"


def test_epic_listing_ignores_history_on_other_streams(tmp_path: Path) -> None:
    """Latest typed entries are an index lookup per epic, not a rank of every row."""
    from lu_machinery.session_streams.db import SessionStreamDatabase
    from lu_machinery.session_streams.model import EntryType, LeaseHolder
    from lu_machinery.session_streams.store import SessionStreamStore

    store = SessionStreamStore(
        SessionStreamDatabase(tmp_path / "streams.sqlite3"),
        _process_probe=lambda _process_id: True,
    )
    holder = LeaseHolder(
        agent="codex",
        harness="codex",
        instance_id="runtime-1",
        process_id=41001,
        task_id="task-runtime-1",
    )
    epic = store.open_session(
        stream_id="epic:4707",  # allow-hardcoded-epic: listing fixture, not a live epic
        holder=holder,
        lineage_id="lineage-epic",
        ttl_seconds=60,
        session_id="session-epic",
        lease_id="lease-epic",
    )
    other = store.open_session(
        stream_id="shared:notes",
        holder=holder,
        lineage_id="lineage-other",
        ttl_seconds=60,
        session_id="session-other",
        lease_id="lease-other",
    )
    for index in range(30):
        store.append_entry(
            other,
            entry_type=EntryType.STATE,
            body=f"other history {index}",
            idempotency_key=f"other-{index}",
        )
    store.append_entry(epic, entry_type=EntryType.STATE, body="older state", idempotency_key="epic-state-1")
    store.append_entry(epic, entry_type=EntryType.STATE, body="newer state", idempotency_key="epic-state-2")

    bundle = store.load_remote_epic_listing(snapshot_sha256=None)
    stream_ids = {str(row["stream_id"]) for row in bundle["projections"]}
    assert stream_ids == {"epic:4707"}  # allow-hardcoded-epic: listing fixture, not a live epic
    latest = bundle["latest_entries"]["epic:4707"][EntryType.STATE.value]  # allow-hardcoded-epic: listing fixture
    assert latest.body == "newer state"
    assert "shared:notes" not in bundle["latest_entries"]
