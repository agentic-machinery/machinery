"""`claude-cto` is the one durable CTO recipient; session identities only claim and acknowledge."""

from __future__ import annotations

import io
import json

import pytest
import yaml

from lu_machinery.fleet_comms.authority import AuthorityRecipientRefused, AuthorityService, AuthorityServiceError
from lu_machinery.fleet_comms.cli import EXIT_ERROR, EXIT_OK, main

SESSION = "claude-cto-27b21508"
MISSING = object()
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
        if status is MISSING:
            del record["status"]
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
    empty = run(capsys, "deliveries", "claim", "--recipient", "claude-cto", "--worker-id", SESSION)
    assert empty["delivery"] is None


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


def test_declared_external_recipients_are_admitted_and_unknown_names_still_refused(plane, tasks, monkeypatch, tmp_path):
    """Defect class: an external agent declared for an external drain (eggbot, dot, mac-agent, jean) is refused as unregistered."""
    from lu_machinery import agent_identity

    external = ("eggbot", "dot", "mac-agent", "jean")
    with AuthorityService(root=plane) as service:
        service.publish_message(sender="claude-cto", body="b", recipients=external)
        for unknown in ("eggbot-2", "jean-2"):
            with pytest.raises(AuthorityRecipientRefused, match=rf"^recipient_not_registered: {unknown} "):
                service.publish_message(sender="claude-cto", body="b", recipients=(unknown,))
    assert sorted(tuple(row) for row in backlog(plane)) == sorted((name, "queued") for name in external)

    # Admission comes from the declared list, not a name in code: without it, the same name is refused.
    data = yaml.safe_load(agent_identity.ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
    data["external_recipients"] = []
    stripped = tmp_path / "area_assignments.yaml"
    stripped.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(agent_identity, "ASSIGNMENTS_PATH", stripped)
    with AuthorityService(root=plane) as service:
        for name in external:
            with pytest.raises(AuthorityRecipientRefused, match=rf"^recipient_not_registered: {name} "):
                service.publish_message(sender="claude-cto", body="b", recipients=(name,))


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
        "roster_complete": True,
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
        "roster_complete": True,
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


def open_state(service, name):
    """The delivery states and subscriptions a recipient still holds."""
    states = [row["state"] for row in service.store.connection.execute(
        "SELECT state FROM authority_deliveries WHERE recipient = ? ORDER BY created_at", (name,)
    ).fetchall()]
    return states, name in service.get_channel("alerts").subscribers


@pytest.mark.parametrize(("status", "settled"), [
    (MISSING, False), (None, False), ("", False), ("paused", False), (["done"], False), ({"s": "done"}, False),
    ("spawning", False), ("running", False),
    *((terminal, True) for terminal in (
        "done", "failed", "timeout", "rate_limited", "cancelled", "crashed", "no_deliverable",
    )),
])
def test_damaged_task_status_never_expires_mail_or_ends_a_subscription(plane, tasks, status, settled):
    """Defect class: a missing, null or unrecognized task status classifies as settled and destroys mail (#594)."""
    worker = tasks("cto-5-i5-worker", "running")
    with AuthorityService(root=plane) as service:
        service.create_channel("alerts", subscribers=("claude-cto",))
        service.subscribe("alerts", (worker,))
        service.publish_message(sender="claude-cto", body="apply option B", recipients=(worker,))
    tasks(worker, status)
    with AuthorityService(root=plane) as service:
        service.claim_next_delivery("claude-cto", "seat-1")
        service.expire_unreadable()
        broadcast = service.publish_message(sender="alertmanager", body="disk 95%", channel="alerts")
        if settled:
            assert open_state(service, worker) == (["expired"], False)
            assert delivery_recipients(service, broadcast) == ["claude-cto"]
        else:
            # Standing unknown or live: mail and subscription stay; a direct publish is admitted.
            service.publish_message(sender="claude-cto", body="again", recipients=(worker,))
            assert open_state(service, worker) == (["queued", "queued", "queued"], True)
            assert dead_letter_reasons(service) == []


@pytest.mark.parametrize("unavailable", ["slots", "lanes"])
def test_incomplete_roster_proves_no_name_unregistered_and_removes_nothing(
    plane, tasks, monkeypatch, tmp_path, unavailable,
):
    """Defect class: with one roster source unreadable, registered or unproven names lose mail and subscriptions (#594)."""
    from lu_machinery import agent_identity
    from lu_machinery.fleet_comms import authority
    from lu_machinery.ops import driver_lanes

    slots = set(agent_identity._load_registry_slots(agent_identity.ASSIGNMENTS_PATH))
    lane = next(item.sender for item in driver_lanes.load_registry().lanes if item.sender not in slots)
    worker = tasks("cto-6-i6-worker", "running")
    with AuthorityService(root=plane) as service:
        service.create_channel("alerts", subscribers=("claude-cto", lane))
        service.subscribe("alerts", (worker,))
        service.publish_message(sender="claude-cto", body="b", recipients=(lane, worker))
        with service._write_transaction():
            service._publish_message_tx(
                sender="sre-timers", body="old report", channel=None, targets=None, kind="message",
                imported_recipients=("codex",), imported_source="test-legacy", idempotency_key="legacy-codex",
            )
    tasks(worker, "done")
    target = (agent_identity, "ASSIGNMENTS_PATH") if unavailable == "slots" else (driver_lanes, "DATA_FILE")
    monkeypatch.setattr(*target, tmp_path / "unavailable")
    assert authority._roster_complete() is False

    with AuthorityService(root=plane) as service:
        service.claim_next_delivery("claude-cto", "seat-1")
        assert service.count_unreadable() == {
            "deliveries": 0, "recipient_not_registered": 0, "recipient_settled": 0, "subscriptions": 0,
            "roster_complete": False,
        }
        assert service.expire_unreadable()["roster_complete"] is False
        broadcast = service.publish_message(sender="alertmanager", body="disk 95%", channel="alerts")
        assert sorted(delivery_recipients(service, broadcast)) == sorted(["claude-cto", lane, worker])
        service.publish_message(sender="sre-timers", body="b", recipients=("codex",))  # admitted, not dropped
        assert dead_letter_reasons(service) == []
        assert sorted(service.get_channel("alerts").subscribers) == sorted(["claude-cto", lane, worker])
    assert sorted(tuple(row) for row in backlog(plane) if row["recipient"] != "claude-cto") == sorted([
        ("codex", "queued"), ("codex", "queued"), (lane, "queued"), (lane, "queued"),
        (worker, "queued"), (worker, "queued"),
    ])


@pytest.mark.parametrize("doubt", [
    "malformed_area", "unknown_roster_key", "task_store_unreadable", "record_not_readable", "hot_entry_not_a_file",
    "dangling_record_link", "dangling_tasks_dir_link",
])
def test_partly_unreadable_roster_or_task_store_keeps_mail_queued_through_expire_stale_apply(
    plane, tasks, monkeypatch, tmp_path, capsys, doubt,
):
    """Defect class: a reader that cannot read part of its input answers as if it had, so live mail is refused or dead-lettered (#1567)."""
    from lu_machinery import agent_identity
    from lu_machinery.dispatcher import store
    from lu_machinery.fleet_comms import authority

    # One area holds the only registration of its slot, as a dedicated slot area does.
    data = yaml.safe_load(agent_identity.ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
    data["assignments"]["doubt-probe"] = {"driver_agent_type": "codex", "slots": ["codex-doubt-probe"]}
    roster = tmp_path / "area_assignments.yaml"
    roster.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(agent_identity, "ASSIGNMENTS_PATH", roster)
    worker = tasks("cto-7-i7-worker", "running")
    victim = {"malformed_area": "codex-doubt-probe", "unknown_roster_key": "eggbot"}.get(doubt, worker)
    with AuthorityService(root=plane) as service:
        service.publish_message(sender="claude-cto", body="b", recipients=(victim,))
    tasks_dir = tmp_path / "tasks"
    hot = tasks_dir / f"{worker}.json"
    moved = tmp_path / "tasks-moved"
    try:
        if doubt == "malformed_area":
            tasks(victim, "done")  # a settled task may share the slot's name
            data["assignments"]["doubt-probe"]["slots"] = victim  # a scalar, not a list
            roster.write_text(yaml.safe_dump(data), encoding="utf-8")
            assert authority._roster_complete() is False
            with AuthorityService(root=plane) as service:
                service.publish_message(sender="claude-cto", body="b", recipients=(victim,))  # admitted
        elif doubt == "unknown_roster_key":
            data["external_recipient"] = data.pop("external_recipients")  # a misspelt key the contract refuses
            roster.write_text(yaml.safe_dump(data), encoding="utf-8")
            assert authority._roster_complete() is False
        else:
            if doubt == "task_store_unreadable":
                tasks_dir.chmod(0o600)  # no search permission: every record stat fails with EACCES
            elif doubt == "record_not_readable":
                hot.chmod(0)  # found by stat, refused at open
            elif doubt == "dangling_tasks_dir_link":
                tasks_dir.rename(moved)
                tasks_dir.symlink_to(tmp_path / "unmounted")
            else:
                # An older terminal copy in the archive must not answer for an unusable hot entry.
                archived = json.loads(hot.read_text()) | {"status": "done"}
                (tasks_dir / "archive").mkdir()
                (tasks_dir / "archive" / hot.name).write_text(json.dumps(archived))
                hot.unlink()
                hot.mkdir() if doubt == "hot_entry_not_a_file" else hot.symlink_to(tmp_path / "gone.json")
            with pytest.raises(store.TaskRecordError, match="TASK_RECORD_UNREADABLE"):
                store.read(worker, missing_ok=True)
            assert authority._task_standing(worker) == "unknown"
        applied = run(capsys, "deliveries", "expire-stale", "--root", str(plane), "--apply")
    finally:
        if doubt == "record_not_readable":
            hot.chmod(0o600)
        if moved.exists():
            tasks_dir.unlink()
            moved.rename(tasks_dir)
        tasks_dir.chmod(0o700)
    assert applied["unreadable_expired"]["deliveries"] == 0
    with AuthorityService(root=plane) as service:
        assert dead_letter_reasons(service) == []
    assert {tuple(row) for row in backlog(plane)} == {(victim, "queued")}
    assert store.read("cto-0-i0-absent", missing_ok=True) is None  # proven absence still reads as no record
    tasks("a_b", "running")
    with pytest.raises(ValueError, match="task id must contain only"):
        store.read("a/b", missing_ok=True, tasks_root=tasks_dir)  # an explicit store still validates the id


def test_unreadable_alias_roster_leaves_an_alias_unverified_never_refused(plane, monkeypatch, tmp_path):
    """Defect class: a failed alias read answers "canonical", so a minted alias such as cursor-monitor is refused (#1567)."""
    from lu_machinery import agent_identity
    from lu_machinery.fleet_comms import authority

    AuthorityService(root=plane).close()  # an existing, empty store
    roster = tmp_path / "area_assignments.yaml"
    roster.write_text(agent_identity.ASSIGNMENTS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(agent_identity, "ASSIGNMENTS_PATH", roster)
    assert agent_identity.resolve_recipient_alias("cursor-monitor", strict=True) == "cursor"
    assert authority._roster_complete() is True  # read and cached before the alias read fails
    roster.chmod(0)  # mtime and size unchanged
    try:
        assert authority._roster_complete() is False  # the cached read is not reused for an unreadable file
        with pytest.raises(ValueError, match="Cannot read"):
            agent_identity.resolve_recipient_alias("cursor-monitor", strict=True)
        # A name no roster can make an alias needs no read.
        assert [agent_identity.resolve_recipient_alias(name, strict=True) for name in ("eggbot", "codex")] == [
            "eggbot", "codex",
        ]
        snapshot = authority.pending_delivery_snapshot("cursor-monitor", root=plane)
    finally:
        roster.chmod(0o600)
    assert (snapshot["count"], snapshot["reason"]) == (None, "recipient_standing_unverified")
    assert authority.pending_delivery_snapshot("cursor-monitor", root=plane)["probe"] == "ok"
