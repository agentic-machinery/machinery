"""`claude-cto` is the one durable CTO recipient; session identities only claim and acknowledge."""

from __future__ import annotations

import io
import json

import pytest

from lu_machinery.fleet_comms.authority import AuthorityRecipientRefused, AuthorityService, AuthorityServiceError
from lu_machinery.fleet_comms.cli import EXIT_ERROR, EXIT_NOT_FOUND, EXIT_OK, main

SESSION = "claude-cto-27b21508"
LEGACY = "claude-cto-d465aa4b"


@pytest.fixture
def plane(tmp_path, monkeypatch):
    root = tmp_path / "plane"
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    monkeypatch.setenv("AB_DB_PATH", str(tmp_path / "unused-legacy.db"))
    return root


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    """A dispatch task store; ``tasks(task_id, status)`` writes or rewrites one record."""
    root = tmp_path / "tasks"
    root.mkdir()
    monkeypatch.setenv("LU_TASKS_DIR", str(root))

    def write(task_id, status):
        record = {"schema_version": 2, "task_id": task_id, "run_nonce": "n", "status": status}
        (root / f"{task_id}.json").write_text(json.dumps(record))
        return task_id

    return write


def run(capsys, *args, expected=EXIT_OK):
    assert main(list(args)) == expected
    return json.loads(capsys.readouterr().out)


def backlog(root):
    with AuthorityService(root=root) as service:
        return service.store.connection.execute(
            "SELECT recipient, state FROM authority_deliveries ORDER BY created_at"
        ).fetchall()


def delivery_recipients(service, message):
    rows = service.store.connection.execute(
        "SELECT recipient FROM authority_deliveries WHERE message_id = ?", (message.message_id,)
    ).fetchall()
    return [row["recipient"] for row in rows]


def escalate(recipient):
    return main([
        "channel", "publish", "cto", "-", "--sender", "claude/cto-7923-demo", "--source", "worker",
        "--recipient", recipient, "--kind", "request", "--idempotency-key", f"esc-{recipient}",
    ])


def test_worker_escalation_lands_at_claude_cto_and_the_session_claims_it(plane, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("escalation body"))
    run(capsys, "channel", "create", "cto")
    monkeypatch.setattr("sys.stdin", io.StringIO("escalation body"))
    assert escalate("claude-cto") == EXIT_OK
    published = json.loads(capsys.readouterr().out)
    assert [tuple(row) for row in backlog(plane)] == [("claude-cto", "queued")]

    claimed = run(capsys, "deliveries", "claim", "--recipient", "claude-cto", "--worker-id", SESSION)
    assert claimed["message_id"] == published["message_id"]
    assert claimed["lease_owner"] == SESSION
    acked = run(
        capsys, "deliveries", "ack", claimed["delivery_id"],
        "--worker-id", SESSION, "--fence-token", str(claimed["fence_token"]),
    )
    assert acked["state"] == "acknowledged"
    run(capsys, "deliveries", "claim", "--recipient", "claude-cto", "--worker-id", SESSION,
        expected=EXIT_NOT_FOUND)


def test_publishing_to_a_session_specific_cto_name_is_refused_at_the_authority(plane, capsys):
    with AuthorityService(root=plane) as service:
        service.create_channel("cto")
        for name in (SESSION, "claude-cto-d465aa4b", " claude-cto-x "):
            with pytest.raises(AuthorityServiceError, match=r"session_cto_recipient_refused.*claude-cto"):
                service.publish_message(sender="codex", body="b", channel="cto", recipients=(name,))
            with pytest.raises(AuthorityServiceError, match="session_cto_recipient_refused"):
                service.publish_message(sender="codex", body="b", recipients=("claude-cto", name))
            with pytest.raises(AuthorityServiceError, match="session_cto_recipient_refused"):
                service.subscribe("cto", (name,))
        with pytest.raises(AuthorityServiceError, match="session_cto_recipient_refused"):
            service.create_channel("other", subscribers=(SESSION,))
    assert backlog(plane) == []


def test_cli_refusal_carries_the_pointer_and_writes_nothing(plane, capsys, monkeypatch):
    run(capsys, "channel", "create", "cto")
    monkeypatch.setattr("sys.stdin", io.StringIO("body"))
    assert escalate(SESSION) == EXIT_ERROR
    assert "session_cto_recipient_refused: address claude-cto," in capsys.readouterr().err
    assert backlog(plane) == []


def test_legacy_session_names_stay_claimable_and_stale_subscribers_reach_claude_cto(plane):
    with AuthorityService(root=plane) as service:
        # A delivery queued before the rule, as the legacy import replays it.
        with service._write_transaction():
            legacy = service._publish_message_tx(
                sender="codex", body="old escalation", channel=None, targets=None, kind="request",
                imported_recipients=(LEGACY,), imported_source="test-legacy",
                idempotency_key="legacy-1",
            )
        # A stored subscription under an old session name, replayed by the legacy import.
        service.create_channel("cto", subscribers=(LEGACY, "claude-epics"), legacy_import=True)
        fresh = service.publish_message(sender="codex", body="new", channel="cto")
        own = service.publish_message(sender=SESSION, body="note", channel="cto")

        assert sorted(delivery_recipients(service, fresh)) == ["claude-cto", "claude-epics"]
        assert delivery_recipients(service, own) == ["claude-epics"]
        lease = service.claim_next_delivery(LEGACY, SESSION)
        assert lease is not None and lease.delivery.message_id == legacy.message_id
        assert lease.delivery.fence_token == 1
        service.acknowledge_delivery(
            lease.delivery.delivery_id, worker_id=SESSION, fence_token=lease.delivery.fence_token
        )
        assert service.claim_next_delivery(LEGACY, SESSION) is None


def dead_letter_reasons(service):
    rows = service.store.connection.execute(
        """SELECT d.recipient, l.reason_code FROM authority_dead_letters l
           JOIN authority_deliveries d USING (delivery_id) ORDER BY d.recipient"""
    ).fetchall()
    return [tuple(row) for row in rows]


def test_unregistered_recipient_admitted_publish_refuses_names_no_drain_reads(plane, tasks):
    """Defect class: an unregistered or settled recipient is admitted, and its mail is never read (#594)."""
    live = tasks("cto-1-i1-live", "running")
    settled = tasks("cto-2-i2-done", "done")
    with AuthorityService(root=plane) as service:
        service.create_channel("cto")
        for name, cause in (
            ("codex", "recipient_not_registered"),
            ("claude/cto-7b96-i12394-opus-b", "recipient_not_registered"),
            (settled, "recipient_settled"),
        ):
            with pytest.raises(AuthorityRecipientRefused, match=f"^{cause}: {name} ") as refused:
                service.publish_message(sender="sre-timers", body="b", channel="cto", recipients=(name,))
            assert (refused.value.recipient, refused.value.cause) == (name, cause)
            with pytest.raises(AuthorityRecipientRefused, match=cause):
                service.subscribe("cto", (name,))
        service.publish_message(sender="sre-timers", body="b", recipients=("claude-cto", live))
        service.publish_message(sender="sre-timers", body="b", recipients=("claude-epics",))
    assert sorted(tuple(row) for row in backlog(plane)) == [
        ("claude-cto", "queued"), ("claude-epics", "queued"), (live, "queued"),
    ]


def test_settled_task_delivery_left_queued_expires_on_the_next_claim_and_hygiene_clears_the_rest(
    plane, tasks, capsys,
):
    """Defect class: a delivery to a task that has settled stays queued until it dead-letters as unread (#594)."""
    worker = tasks("cto-3-i3-worker", "running")
    with AuthorityService(root=plane) as service:
        service.publish_message(sender="claude-cto", body="apply option B", recipients=(worker,))
        with service._write_transaction():
            # Rows queued before the rule, as the legacy import replays them; Kimi is a retired seat.
            for name in ("codex", "kimi"):
                service._publish_message_tx(
                    sender="sre-timers", body="old report", channel=None, targets=None, kind="message",
                    imported_recipients=(name,), imported_source="test-legacy", idempotency_key=f"legacy-{name}",
                )
    tasks(worker, "done")

    dry = run(capsys, "deliveries", "expire-stale", "--root", str(plane))
    assert dry["unreadable"] == {
        "deliveries": 3, "recipient_not_registered": 2, "recipient_settled": 1, "subscriptions": 0,
    }
    assert dry["unreadable_expired"] is None
    assert sorted(tuple(row) for row in backlog(plane)) == [
        ("codex", "queued"), (worker, "queued"), ("kimi", "queued"),
    ]

    with AuthorityService(root=plane) as service:
        assert service.claim_next_delivery("claude-cto", "seat-1") is not None  # the CTO's settled-recipient notice
        assert dead_letter_reasons(service) == [(worker, "recipient_settled")]
    assert sorted(tuple(row) for row in backlog(plane) if row["recipient"] != "claude-cto") == [
        ("codex", "queued"), (worker, "expired"), ("kimi", "queued"),
    ]

    applied = run(capsys, "deliveries", "expire-stale", "--root", str(plane), "--apply")
    assert applied["unreadable_expired"] == {
        "deliveries": 2, "recipient_not_registered": 2, "recipient_settled": 0, "subscriptions": 0,
    }
    with AuthorityService(root=plane) as service:
        assert dead_letter_reasons(service) == [
            ("codex", "recipient_not_registered"), (worker, "recipient_settled"), ("kimi", "recipient_not_registered"),
        ]
        assert service.count_unreadable()["deliveries"] == 0


def test_dead_subscription_still_receiving_broadcasts_ends_when_its_task_settles(plane, tasks):
    """Defect class: a settled task stays subscribed, so every broadcast still fans out to it (#594)."""
    worker = tasks("cto-4-i4-worker", "running")
    with AuthorityService(root=plane) as service:
        service.create_channel("alerts", subscribers=("claude-cto",))
        service.subscribe("alerts", (worker,))
        before = service.publish_message(sender="alertmanager", body="disk 91%", channel="alerts")
        assert sorted(delivery_recipients(service, before)) == sorted(["claude-cto", worker])
        tasks(worker, "failed")
        after = service.publish_message(sender="alertmanager", body="disk 95%", channel="alerts")
        assert delivery_recipients(service, after) == ["claude-cto"]
        assert service.get_channel("alerts").subscribers == ("claude-cto",)
