"""Mutation guards for the remote epic TTL/fencing store seam (#7178)."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.session_streams.db import SessionStreamDatabase
from lu_machinery.session_streams.model import EntryType, LeaseHolder, utc_now
from lu_machinery.session_streams.store import (
    ContentRejectedError,
    LeaseConflictError,
    SessionStreamStore,
)
from tests.epics_monitor_stub import isolated_monitor_state as isolated_monitor_state

pytestmark = pytest.mark.usefixtures("isolated_monitor_state")


def _store(tmp_path: Path) -> SessionStreamStore:
    return SessionStreamStore(SessionStreamDatabase(tmp_path / "remote.sqlite3"))


def test_monitor_ttl_successor_fences_predecessor_heartbeat_and_write(tmp_path: Path, monkeypatch) -> None:
    from lu_machinery.session_supervisor.remote import (
        RemoteEpicClient,
        RemoteLeaseLostError,
        RemoteSupervisorError,
    )
    from tests.epics_monitor_stub import epics_monitor_stub

    store = _store(tmp_path)
    clock = utc_now()
    monkeypatch.setattr("lu_machinery.session_streams.store.utc_now", lambda: clock)
    monkeypatch.setattr("lu_machinery.api.epics_router.utc_now", lambda: clock)

    def forbidden_probe(_process_id: int) -> bool:
        pytest.fail("Monitor TTL/CAS recovery must not probe the holder process")

    store._process_probe = forbidden_probe
    first = LeaseHolder("codex", "codex-cli", "predecessor", process_id=os.getpid())
    second = replace(first, agent="claude", harness="claude-code", instance_id="successor")
    with epics_monitor_stub(store) as base:
        monitor = RemoteEpicClient(base=base)
        predecessor, _ = monitor.claim(
            stream_id="epic:999992",  # allow-hardcoded-epic: synthetic Monitor TTL/CAS fixture
            holder=first, lineage_id="ttl-predecessor", ttl_seconds=30,
            session_id="predecessor-session", lease_id="predecessor-lease",
        )
        # Stop renewing: before TTL, a different successor still cannot claim.
        clock += timedelta(seconds=29)
        with pytest.raises(RemoteSupervisorError, match="already has live session"):
            monitor.claim(
                stream_id=predecessor.stream_id, holder=second, lineage_id="ttl-successor", ttl_seconds=30,
                session_id="successor-session", lease_id="successor-lease",
            )
        clock += timedelta(seconds=2)
        successor, receipt = monitor.claim(
            stream_id=predecessor.stream_id, holder=second, lineage_id="ttl-successor", ttl_seconds=30,
            session_id="successor-session", lease_id="successor-lease",
        )
        assert receipt["outcome"] == "recovered"
        assert successor.generation == predecessor.generation + 1
        assert successor.fencing_token == predecessor.fencing_token + 1
        monitor.handoff(successor, entry_type="state", body="Successor holds write authority.", idempotency_key="current")
        before = store.dump_stream(successor.stream_id)
        with pytest.raises(RemoteLeaseLostError):
            monitor.heartbeat(predecessor)
        with pytest.raises(RemoteLeaseLostError):
            monitor.handoff(predecessor, entry_type="note", body="Stale write.", idempotency_key="stale")
        assert store.dump_stream(successor.stream_id) == before


def _holder(agent: str, instance: str, host: str) -> LeaseHolder:
    return LeaseHolder(
        agent=agent,
        harness=f"{agent}-harness",
        instance_id=instance,
        process_id=1234,
        host_id=host,
    )


@pytest.mark.parametrize("expired", [False, True])
def test_remote_supervisor_recovery_reports_monitor_expiry(tmp_path: Path, monkeypatch, expired: bool) -> None:
    from lu_machinery.session_supervisor import SessionSupervisor, SupervisorError

    store = _store(tmp_path)
    now = utc_now()
    holder = LeaseHolder("codex", "codex-cli", "remote-holder", process_id=os.getpid())
    lease, _ = store.claim_remote_session(
        stream_id="epic:999993",  # allow-hardcoded-epic: synthetic supervisor diagnosis fixture
        holder=holder, lineage_id="diagnosis", ttl_seconds=30,
        session_id="diagnosis-session", lease_id="diagnosis-lease", now=now,
    )
    observed = now + timedelta(seconds=30 if expired else 29)
    monkeypatch.setattr("lu_machinery.session_streams.handoff.utc_now", lambda: observed)
    reads = []

    def read_stream(stream_id):
        reads.append(stream_id)
        return {"lease": {"holder": {"agent": holder.agent, "harness": holder.harness}, "expires_at": lease.expires_at}}

    # Only the read capability is supplied; no claim, release or process probe.
    supervisor = SessionSupervisor(None, repo_root=tmp_path, remote=SimpleNamespace(stream=read_stream))
    before = store.dump_stream(lease.stream_id)
    with pytest.raises(SupervisorError) as error:
        supervisor.recover_expired_driver(role="driver", stream_id=lease.stream_id, holder=holder)
    reason = str(error.value)
    assert "local PID recovery refused" in reason
    if expired:
        assert "claim through Monitor TTL/CAS now" in reason
    else:
        assert f"{holder.agent}/{holder.harness}" in reason
        assert f"retry the Monitor claim after {lease.expires_at}" in reason
    assert reads == [lease.stream_id]
    assert store.dump_stream(lease.stream_id) == before


def test_claim_heartbeat_handoff_release_replay_conflict_and_fencing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _holder("codex", "first", "alpha")
    second = _holder("gemini", "second", "beta")
    now = utc_now()

    lease, outcome = store.claim_remote_session(
        stream_id="epic:7178",
        holder=first,
        lineage_id="lineage-first",
        ttl_seconds=30,
        session_id="session-first",
        lease_id="lease-first",
        now=now,
    )
    assert outcome == "claimed"
    replay, outcome = store.claim_remote_session(
        stream_id="epic:7178",
        holder=first,
        lineage_id="lineage-first",
        ttl_seconds=30,
        session_id="session-first",
        lease_id="lease-first",
        now=now + timedelta(seconds=1),
    )
    assert outcome == "replayed"
    assert replay == lease

    with pytest.raises(LeaseConflictError, match="live holder"):
        store.claim_remote_session(
            stream_id="epic:7178",
            holder=second,
            lineage_id="lineage-second",
            ttl_seconds=30,
            session_id="session-second",
            lease_id="lease-second",
            now=now + timedelta(seconds=2),
        )

    renewed = store.heartbeat(lease, now=now + timedelta(seconds=3))
    entry = store.append_entry(
        renewed,
        entry_type=EntryType.STATE,
        body="working",
        idempotency_key="state-1",
        now=now + timedelta(seconds=4),
    ).entry
    assert entry.type is EntryType.STATE
    # The reply is the stored lease as the release left it, and a replay returns the same one.
    released = store.release_remote_session(renewed, now=now + timedelta(seconds=5))
    assert released == replace(renewed, version=renewed.version + 1)
    assert store.release_remote_session(renewed, now=now + timedelta(seconds=6)) == released

    # A closed exact lease is fenced from a later claim, but clean release is idempotent.
    successor, outcome = store.claim_remote_session(
        stream_id="epic:7178",
        holder=second,
        lineage_id="lineage-second",
        ttl_seconds=30,
        session_id="session-second",
        lease_id="lease-second",
        now=now + timedelta(seconds=7),
    )
    assert outcome == "claimed"
    assert (successor.generation, successor.fencing_token) == (2, 2)
    with pytest.raises(LeaseConflictError):
        store.heartbeat(renewed, now=now + timedelta(seconds=8))
    with pytest.raises(LeaseConflictError):
        store.append_entry(
            renewed,
            entry_type=EntryType.NOTE,
            body="late",
            idempotency_key="late-note",
            now=now + timedelta(seconds=8),
        )
    with pytest.raises(LeaseConflictError):
        store.release_remote_session(renewed, now=now + timedelta(seconds=8))


def test_expired_claim_recovery_is_one_cas_receipt_and_old_lease_is_expired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = utc_now()
    old, _ = store.claim_remote_session(
        stream_id="epic:7000",
        holder=_holder("codex", "old", "alpha"),
        lineage_id="old-lineage",
        ttl_seconds=5,
        session_id="old-session",
        lease_id="old-lease",
        now=now,
    )
    new, outcome = store.claim_remote_session(
        stream_id="epic:7000",
        holder=_holder("codex", "new", "beta"),
        lineage_id="new-lineage",
        ttl_seconds=5,
        session_id="new-session",
        lease_id="new-lease",
        now=now + timedelta(seconds=6),
    )
    assert outcome == "recovered"
    assert new.generation == old.generation + 1
    assert new.fencing_token == old.fencing_token + 1
    assert (
        store.session_state("epic:7000", "old-session").value == "expired"
    )  # allow-hardcoded-epic: synthetic expiry recovery stream fixture
    with store._read_snapshot() as connection:
        recovery = connection.execute(
            "SELECT proof_json FROM lease_events WHERE stream_id = ? AND event_type = 'recovered'",
            ("epic:7000",),
        ).fetchone()
    assert recovery is not None
    assert "old-session" in str(recovery["proof_json"])
    assert "new-session" in str(recovery["proof_json"])


def test_late_heartbeat_before_successor_revives_exact_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = utc_now()
    lease, _ = store.claim_remote_session(
        stream_id="epic:7001",
        holder=_holder("codex", "late", "alpha"),
        lineage_id="late-lineage",
        ttl_seconds=5,
        session_id="late-session",
        lease_id="late-lease",
        now=now,
    )
    revived = store.heartbeat(lease, now=now + timedelta(seconds=6))
    assert revived.expires_at > lease.expires_at
    with pytest.raises(LeaseConflictError):
        store.claim_remote_session(
            stream_id="epic:7001",
            holder=_holder("gemini", "competitor", "beta"),
            lineage_id="competitor-lineage",
            ttl_seconds=5,
            session_id="competitor-session",
            lease_id="competitor-lease",
            now=now + timedelta(seconds=7),
        )


@pytest.mark.parametrize(
    "body",
    ["contact 203.0.113.7", "write /Users/alice/private.txt", "ssh atlas-runner", "connect example.com"],
)
def test_remote_entry_body_rejects_opsec_tokens(tmp_path: Path, body: str) -> None:
    store = _store(tmp_path)
    lease, _ = store.claim_remote_session(
        stream_id="epic:7002",
        holder=_holder("codex", "opsec", "alpha"),
        lineage_id="opsec-lineage",
        ttl_seconds=30,
        session_id="opsec-session",
        lease_id="opsec-lease",
    )
    with pytest.raises(ContentRejectedError):
        store.append_entry(lease, entry_type=EntryType.NOTE, body=body, idempotency_key="opsec")


def test_holder_host_is_part_of_the_fence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lease, _ = store.claim_remote_session(
        stream_id="epic:7003",
        holder=_holder("codex", "same", "alpha"),
        lineage_id="host-lineage",
        ttl_seconds=30,
        session_id="host-session",
        lease_id="host-lease",
    )
    forged = replace(lease, holder=replace(lease.holder, host_id="beta"))
    with pytest.raises(LeaseConflictError):
        store.heartbeat(forged)


@pytest.mark.parametrize("host_id", [None, "synthetic-host"])
@pytest.mark.parametrize("expired", [False, True])
def test_local_pid_recovery_cannot_close_remote_lease(tmp_path: Path, host_id, expired) -> None:
    from lu_machinery.session_streams.handoff import diagnose_handoff

    store = _store(tmp_path)
    now = utc_now()
    holder = replace(_holder("codex", "remote", "alpha"), host_id=host_id)
    lease, _ = store.claim_remote_session(
        stream_id="epic:7178", holder=holder, lineage_id="remote-lineage",
        ttl_seconds=30, session_id="remote-session", lease_id="remote-lease", now=now,
    )
    # Heartbeats must not erase the acquisition's recovery mode.
    store.heartbeat(lease, now=now + timedelta(seconds=1))
    store._process_probe = lambda pid: pid == 5678
    observed = now + timedelta(seconds=60 if expired else 2)
    before = store.dump_stream(lease.stream_id)
    status = diagnose_handoff(store, lease.stream_id, now=observed)
    assert status.claimable_force_close is False
    assert status.holder_process_alive is None
    candidate = LeaseHolder(agent="codex", harness="cli", instance_id="local", process_id=5678)
    with pytest.raises(LeaseConflictError, match=r"remote.*TTL"):
        store.force_close_expired_session(
            stream_id=lease.stream_id, session_id=lease.session_id, candidate=candidate, now=observed,
        )
    assert store.dump_stream(lease.stream_id) == before


def test_dead_local_process_recovers_but_same_dead_pid_is_not_remote_authority(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    from lu_machinery.session_supervisor import SessionSupervisor

    store = _store(tmp_path)
    supervisor = SessionSupervisor(store, repo_root=tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        holder = LeaseHolder("codex", "cli", "local-predecessor", process_id=child.pid)
        local = supervisor.open_driver(role="driver", stream_id="epic:7178", holder=holder, lineage_id="local", ttl_seconds=900)
        remote, _ = store.claim_remote_session(
            stream_id="epic:7179", holder=replace(holder, instance_id="remote-predecessor"),
            lineage_id="remote", ttl_seconds=900, session_id="remote", lease_id="remote-lease",
        )
    finally:
        child.communicate(timeout=10)
    candidate = LeaseHolder("codex", "cli", "local-successor", process_id=os.getpid())
    successor = supervisor.open_driver(role="driver", stream_id=local.stream_id, holder=candidate, lineage_id="local", ttl_seconds=900)
    assert successor.generation == local.generation + 1
    before = store.dump_stream(remote.stream_id)
    with pytest.raises(LeaseConflictError, match="remote lease requires TTL"):
        store.force_close_expired_session(stream_id=remote.stream_id, session_id=remote.session_id, candidate=candidate)
    assert store.dump_stream(remote.stream_id) == before
    assert supervisor.close_driver(role="driver", lease=successor) == "closed"


def test_remote_epic_status_has_no_per_request_connection_fan_out(tmp_path: Path, monkeypatch) -> None:
    """Defect class: per-request connection fan-out (#12627).

    The epic status read opened one connection for each part of the response.
    Every connection repeats its setup statements and each statement hands the
    GIL back, so under CPU-bound API work the read stalled for seconds. One
    status read now opens exactly one read-only connection.
    """
    from lu_machinery.api import epics_router
    from lu_machinery.session_streams.model import EntryType

    stream_id = "epic:4707"  # allow-hardcoded-epic: fixture stream, not a live epic
    store = SessionStreamStore(SessionStreamDatabase(tmp_path / "streams.sqlite3"), _process_probe=lambda _pid: True)
    holder = LeaseHolder(agent="codex", harness="codex", instance_id="runtime-1", process_id=41001, task_id="task-1")
    session = store.open_session(
        stream_id=stream_id,
        holder=holder,
        lineage_id="lineage-epic",
        ttl_seconds=60,
        session_id="session-epic",
        lease_id="lease-epic",
    )
    store.append_entry(session, entry_type=EntryType.STATE, body="working", idempotency_key="state-1")
    monkeypatch.setattr(epics_router, "_REGISTRY_SNAPSHOT_SHA256", "a" * 64)
    opened: list[bool] = []
    real_connect = store.database.connect

    def counting_connect(*args, **kwargs):
        opened.append(bool(kwargs.get("read_only", False)))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store.database, "connect", counting_connect)
    response = epics_router._stream_response(store, stream_id, 20)

    assert response["lease"] is not None
    assert [entry["body"] for entry in response["digest"]["recent"]] == ["working"]
    assert opened == [True]


def _turn(instance: str = "turn-run") -> LeaseHolder:
    from lu_machinery.session_streams.store import TURN_HOLDER_HARNESS

    return LeaseHolder("claude", TURN_HOLDER_HARNESS, instance, process_id=os.getpid(), task_id=f"turn-{instance}")


def _claim(store: SessionStreamStore, holder: LeaseHolder, name: str, now, *, stream: str = "epic:7300", ttl: int = 30):
    return store.claim_remote_session(
        stream_id=stream, holder=holder, lineage_id=f"{name}-lineage", ttl_seconds=ttl,
        session_id=f"{name}-session", lease_id=f"{name}-lease", now=now,
    )


@pytest.mark.parametrize("first_is_turn", [True, False])
@pytest.mark.parametrize("after_ttl", [False, True])
def test_a_turn_and_a_legacy_claim_exclude_each_other_in_both_orders(tmp_path: Path, first_is_turn, after_ttl) -> None:
    """Defect class T1: a legacy launcher and a dispatch turn both hold one stream, in either order, before or after
    the holder's TTL; or exclusion leaks into legacy-to-legacy TTL recovery."""
    from lu_machinery.session_streams.store import TurnLeaseUnclearedError

    store = _store(tmp_path)
    now = utc_now()
    turn, legacy = _turn(), _holder("codex", "legacy", "alpha")
    first, second = (turn, legacy) if first_is_turn else (legacy, turn)
    held, outcome = _claim(store, first, "first", now)
    assert outcome == "claimed"
    before = store.dump_stream(held.stream_id)
    with pytest.raises(TurnLeaseUnclearedError, match=r"^turn_lease_uncleared: "):
        _claim(store, second, "second", now + timedelta(seconds=31 if after_ttl else 5))
    assert store.dump_stream(held.stream_id) == before
    # Two legacy holders keep TTL recovery.
    other, _ = _claim(store, legacy, "legacy-a", now, stream="epic:7301")
    _successor, recovered = _claim(store, _holder("gemini", "legacy-b", "beta"), "legacy-b", now + timedelta(seconds=31),
                                   stream="epic:7301")
    assert recovered == "recovered" and other.generation == 1


def test_exactly_one_of_many_concurrent_claims_wins(tmp_path: Path) -> None:
    """Defect class T1: two claimants of one free stream, turns and legacy launchers at once, both win the CAS."""
    import threading

    store = _store(tmp_path)
    store.claim_remote_session(  # creates the schema before the race
        stream_id="epic:7399", holder=_holder("codex", "warm", "alpha"), lineage_id="warm", ttl_seconds=30,
        session_id="warm-session", lease_id="warm-lease",
    )
    holders = [_turn(f"run-{n}") for n in range(4)] + [_holder("codex", f"legacy-{n}", "alpha") for n in range(4)]
    gate, results = threading.Barrier(len(holders)), []

    def claim(index: int, holder: LeaseHolder) -> None:
        gate.wait(timeout=60)
        try:
            results.append(_claim(store, holder, f"c{index}", utc_now(), stream="epic:7302")[1])
        except LeaseConflictError as exc:
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=claim, args=pair) for pair in enumerate(holders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert results.count("claimed") == 1 and len(results) == len(holders), results
    assert all(result in {"claimed", "LeaseConflictError", "TurnLeaseUnclearedError"} for result in results), results


def test_an_expired_turn_lease_stays_until_an_exact_or_attributed_release(tmp_path: Path) -> None:
    """Defect class T2: a turn lease past its TTL, with no proof the turn stopped, is superseded by a claim or freed by
    a dead-PID force close; or its exact holder, or an attributed operator, can no longer release it."""
    from lu_machinery.session_streams.store import TurnLeaseUnclearedError

    store = _store(tmp_path)
    now = utc_now()
    lease, _ = _claim(store, _turn(), "turn", now)
    late = now + timedelta(days=2)
    before = store.dump_stream(lease.stream_id)
    for holder in (_holder("codex", "legacy", "alpha"), _turn("other-run")):
        with pytest.raises(TurnLeaseUnclearedError):
            _claim(store, holder, holder.instance_id, late)
    store._process_probe = lambda pid: pid == 5678  # the turn's recorded process is dead; the candidate lives
    candidate = LeaseHolder("codex", "cli", "local", process_id=5678)
    with pytest.raises(TurnLeaseUnclearedError):
        store.force_close_expired_session(stream_id=lease.stream_id, session_id=lease.session_id, candidate=candidate,
                                          now=late)
    assert store.dump_stream(lease.stream_id) == before
    # A turn lease opened without Monitor's claim (no remote provenance) is refused the PID close all the same.
    local = store.open_session(stream_id="epic:7303", holder=replace(_turn("local-run"), process_id=4321),
                               lineage_id="local", ttl_seconds=30, now=now)
    with pytest.raises(TurnLeaseUnclearedError):
        store.force_close_expired_session(stream_id=local.stream_id, session_id=local.session_id, candidate=candidate,
                                          now=late)
    # The exact holder closes it after expiry, and the stream is free again.
    assert store.release_remote_session(lease, now=late) == replace(lease, version=lease.version + 1)
    successor, outcome = _claim(store, _holder("codex", "legacy", "alpha"), "after-release", late)
    assert outcome == "claimed" and successor.generation == lease.generation + 1
    # An attributed operator force release clears an expired turn lease too.
    turn, _ = _claim(store, _turn("third-run"), "third", now, stream="epic:7304")
    store.force_release_remote_session(stream_id=turn.stream_id, actor_agent="claude-cto", actor_host_id="host-a",
                                       reason="scope proven gone", now=late)
    assert _claim(store, _turn("fourth-run"), "fourth", late, stream="epic:7304")[1] == "claimed"
