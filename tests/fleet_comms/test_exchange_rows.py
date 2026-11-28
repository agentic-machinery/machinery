"""Tests for read-only Fleet Comms exchange rows (machinery-infra #1619).

One test case per row class in the #1619 denominator:
- Delivery states: queued, running, acknowledged, expired, dead_lettered
- Reply link cases: valid, missing parent, other conversation
- Corrupt row isolation: typed per-row error while sibling rows still print
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from lu_machinery.fleet_comms.authority import AuthorityMessage, AuthorityService
from lu_machinery.fleet_comms.cli import EXIT_ERROR, EXIT_OK, main

pytestmark = pytest.mark.usefixtures("placeholder_seats_registered")

BODY_SENTINEL = "confidential exchange body sentinel"
SINCE_TS = "2026-11-28T00:00:00Z"


@pytest.fixture
def plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "plane"
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    monkeypatch.delenv("LEARN_UKRAINIAN_CP_AUTHORITY", raising=False)
    monkeypatch.delenv("LEARN_UKRAINIAN_CP_AUTHORITY_FLEET_COMMS", raising=False)
    return root


def _message_dict(message: AuthorityMessage) -> dict[str, Any]:
    payload = asdict(message)
    payload["delivery_ids"] = list(message.delivery_ids)
    return payload


def _expected_delivery_dict(root: Path, delivery_id: str) -> dict[str, Any]:
    with AuthorityService(root=root) as service:
        row = service._conn.execute(
            "SELECT * FROM authority_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        assert row is not None
        return asdict(service._delivery_from_row(row))


def _expected_message_dict(root: Path, message_id: str) -> dict[str, Any]:
    with AuthorityService(root=root) as service:
        return _message_dict(service.get_message(message_id))


def _run_exchange(
    capsys: pytest.CaptureFixture[str],
    root: Path,
    *,
    recipient: str = "codex",
    since: str = SINCE_TS,
    expected_rc: int = EXIT_OK,
) -> dict[str, Any]:
    rc = main(["fleet", "exchange", "--recipient", recipient, "--since", since, "--root", str(root)])
    output = capsys.readouterr()
    assert rc == expected_rc, f"rc={rc}, stdout={output.out!r}, stderr={output.err!r}"
    assert output.err == ""
    assert BODY_SENTINEL not in output.out
    payload = json.loads(output.out)
    assert payload["content_included"] is False
    assert payload["read_only"] is True
    return payload


def test_queued_delivery_omits_claim_and_terminal_times_and_matches_normalizers(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: queued delivery fabricates lifecycle times, ignores --since, or diverges from normalizers."""
    with AuthorityService(root=plane) as service:
        old_msg = service.publish_message(
            sender="operator",
            body=f"old {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-27T23:59:59Z",
            idempotency_key="queued-old",
        )
        msg = service.publish_message(
            sender="operator",
            body=f"queued {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="queued-in-window",
        )
        service.publish_message(
            sender="operator",
            body=f"other {BODY_SENTINEL}",
            recipients=("claude",),
            created_at="2026-11-28T01:05:00Z",
            idempotency_key="queued-other-recipient",
        )

    delivery_id = msg.delivery_ids[0]
    expected_delivery = _expected_delivery_dict(plane, delivery_id)
    expected_message = _expected_message_dict(plane, msg.message_id)
    db_bytes_before = (plane / "comms.sqlite3").read_bytes()

    payload = _run_exchange(capsys, plane, recipient="codex", since="2026-11-28T02:00:00+02:00")
    assert (plane / "comms.sqlite3").read_bytes() == db_bytes_before
    assert payload["recipient"] == "codex"
    assert payload["since"] == SINCE_TS
    assert payload["delivery_count"] == 1
    assert payload["reply_count"] == 0
    assert payload["error_count"] == 0
    assert all(row["message_id"] != old_msg.message_id for row in payload["deliveries"])

    row = payload["deliveries"][0]
    assert row["ok"] is True
    assert row["error"] is None
    assert row["delivery_id"] == delivery_id
    assert row["message_id"] == msg.message_id
    assert row["delivery"] == expected_delivery
    assert row["message"] == expected_message
    assert row["delivery"]["state"] == "queued"
    assert row["delivery"]["attempt_count"] == 0
    assert row["claimed_at"] is None
    assert row["acked_at"] is None
    assert row["expired_at"] is None
    assert row["dead_lettered_at"] is None
    assert row["dead_letter_reason"] is None


def test_running_delivery_reports_active_lease_and_claim_time_without_modifying_lease(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: running delivery drops lease/claimed_at metadata or mutates lease state on read."""
    with AuthorityService(root=plane) as service:
        msg = service.publish_message(
            sender="operator",
            body=f"running {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="running-1",
        )
        lease = service.claim_next_delivery(
            "codex", "worker-run", lease_seconds=300, now="2026-11-28T01:05:00Z",
        )
        assert lease is not None

    delivery_id = lease.delivery.delivery_id
    expected_delivery = _expected_delivery_dict(plane, delivery_id)
    expected_message = _expected_message_dict(plane, msg.message_id)

    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["error_count"] == 0

    row = payload["deliveries"][0]
    assert row["ok"] is True
    assert row["error"] is None
    assert row["delivery"] == expected_delivery
    assert row["message"] == expected_message
    assert row["delivery"]["state"] == "running"
    assert row["delivery"]["lease_owner"] == "worker-run"
    assert row["delivery"]["attempt_count"] == 1
    assert row["claimed_at"] == "2026-11-28T01:05:00Z"
    assert row["acked_at"] is None
    assert row["expired_at"] is None
    assert row["dead_lettered_at"] is None
    assert row["dead_letter_reason"] is None
    # Reading exchange rows must not modify fence token, attempt count, or lease expiry.
    assert _expected_delivery_dict(plane, delivery_id) == expected_delivery


def test_acknowledged_delivery_preserves_claim_time_and_reports_acked_at(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: acknowledged delivery loses claimed_at after ack overwrites updated_at."""
    with AuthorityService(root=plane) as service:
        msg = service.publish_message(
            sender="operator",
            body=f"acked {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="acked-1",
        )
        lease = service.claim_next_delivery(
            "codex", "worker-ack", lease_seconds=300, now="2026-11-28T01:02:00Z",
        )
        assert lease is not None
        service.acknowledge_delivery(
            lease.delivery.delivery_id,
            worker_id="worker-ack",
            fence_token=lease.fence_token,
            now="2026-11-28T01:04:00Z",
        )

    delivery_id = lease.delivery.delivery_id
    expected_delivery = _expected_delivery_dict(plane, delivery_id)
    expected_message = _expected_message_dict(plane, msg.message_id)

    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["error_count"] == 0

    row = payload["deliveries"][0]
    assert row["ok"] is True
    assert row["error"] is None
    assert row["delivery"] == expected_delivery
    assert row["message"] == expected_message
    assert row["delivery"]["state"] == "acknowledged"
    assert row["claimed_at"] == "2026-11-28T01:02:00Z"
    assert row["acked_at"] == "2026-11-28T01:04:00Z"
    assert row["expired_at"] is None
    assert row["dead_lettered_at"] is None
    assert row["dead_letter_reason"] is None


def test_expired_delivery_reports_expired_at_and_dead_letter_reason(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: expired delivery drops expired_at or deadline_expired dead-letter reason."""
    with AuthorityService(root=plane) as service:
        msg = service.publish_message(
            sender="operator",
            body=f"expired {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            deadline_at="2026-11-28T01:10:00Z",
            idempotency_key="expired-1",
        )
        reclaimed = service.reclaim_expired_deliveries(now="2026-11-28T01:15:00Z")
        assert reclaimed == 1

    delivery_id = msg.delivery_ids[0]
    expected_delivery = _expected_delivery_dict(plane, delivery_id)
    expected_message = _expected_message_dict(plane, msg.message_id)

    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["error_count"] == 0

    row = payload["deliveries"][0]
    assert row["ok"] is True
    assert row["error"] is None
    assert row["delivery"] == expected_delivery
    assert row["message"] == expected_message
    assert row["delivery"]["state"] == "expired"
    assert row["claimed_at"] is None
    assert row["acked_at"] is None
    assert row["expired_at"] == "2026-11-28T01:15:00Z"
    assert row["dead_letter_reason"] == "deadline_expired"
    assert row["dead_lettered_at"] is not None


def test_dead_lettered_delivery_reports_exhausted_attempts_and_dlq_metadata(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: dead-lettered delivery loses attempt_count, claimed_at, or dead_letter_reason."""
    with AuthorityService(root=plane) as service:
        msg = service.publish_message(
            sender="operator",
            body=f"dlq {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="dlq-1",
        )
        lease = service.claim_next_delivery(
            "codex", "worker-dlq", lease_seconds=60, max_attempts=1, now="2026-11-28T01:01:00Z",
        )
        assert lease is not None
        reclaimed = service.reclaim_expired_deliveries(max_attempts=1, now="2026-11-28T01:03:00Z")
        assert reclaimed == 1

    delivery_id = lease.delivery.delivery_id
    expected_delivery = _expected_delivery_dict(plane, delivery_id)
    expected_message = _expected_message_dict(plane, msg.message_id)

    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["error_count"] == 0

    row = payload["deliveries"][0]
    assert row["ok"] is True
    assert row["error"] is None
    assert row["delivery"] == expected_delivery
    assert row["message"] == expected_message
    assert row["delivery"]["state"] == "dead_lettered"
    assert row["delivery"]["attempt_count"] == 1
    assert row["claimed_at"] == "2026-11-28T01:01:00Z"
    assert row["acked_at"] is None
    assert row["expired_at"] is None
    assert row["dead_letter_reason"] == "attempts_exhausted"
    assert row["dead_lettered_at"] is not None


def test_valid_reply_matches_get_message_and_links_to_parent_delivery(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: valid reply fails to join to delivery message or diverges from get_message."""
    with AuthorityService(root=plane) as service:
        parent = service.publish_message(
            sender="operator",
            body=f"parent {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="reply-valid-parent",
        )
        reply = service.publish_message(
            sender="codex",
            body=f"reply {BODY_SENTINEL}",
            recipients=("claude-cto",),
            in_reply_to=parent.message_id,
            created_at="2026-11-28T01:02:00Z",
            idempotency_key="reply-valid-child",
        )
        # Unrelated reply to an older message before --since must not leak into this window.
        old_parent = service.publish_message(
            sender="operator",
            body=f"old parent {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-27T20:00:00Z",
            idempotency_key="reply-old-parent",
        )
        service.publish_message(
            sender="codex",
            body=f"old reply {BODY_SENTINEL}",
            recipients=("claude-cto",),
            in_reply_to=old_parent.message_id,
            created_at="2026-11-28T01:03:00Z",
            idempotency_key="reply-old-child",
        )

    expected_reply = _expected_message_dict(plane, reply.message_id)
    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["reply_count"] == 1
    assert payload["error_count"] == 0

    row = payload["replies"][0]
    assert row["ok"] is True
    assert row["error"] is None
    assert row["reply_link_status"] == "valid"
    assert row["message_id"] == reply.message_id
    assert row["in_reply_to"] == parent.message_id
    assert row["conversation_id"] == parent.conversation_id
    assert row["parent_conversation_id"] == parent.conversation_id
    assert row["parent_delivery_ids"] == list(parent.delivery_ids)
    assert row["message"] == expected_reply


def test_reply_with_missing_parent_emits_typed_reply_parent_not_found_error(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: orphan reply with missing parent crashes or is silently dropped instead of typed error."""
    missing_parent_id = "message_" + ("0" * 32)
    with AuthorityService(root=plane) as service:
        parent = service.publish_message(
            sender="operator",
            body=f"delivered {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="missing-parent-base",
        )
        orphan = service.publish_message(
            sender="codex",
            body=f"orphan {BODY_SENTINEL}",
            recipients=("claude-cto",),
            conversation_id=parent.conversation_id,
            created_at="2026-11-28T01:02:00Z",
            idempotency_key="missing-parent-orphan",
        )

    # Point orphan.in_reply_to at a nonexistent parent while bypassing immutability/FK guards in fixture.
    conn = sqlite3.connect(str(plane / "comms.sqlite3"))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER IF EXISTS authority_comms_message_immutable_update")
        conn.execute(
            "UPDATE comms_messages SET in_reply_to = ? WHERE message_id = ?",
            (missing_parent_id, orphan.message_id),
        )
        conn.commit()
    finally:
        conn.close()

    expected_orphan = _expected_message_dict(plane, orphan.message_id)
    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["reply_count"] == 1
    assert payload["error_count"] == 1

    row = payload["replies"][0]
    assert row["ok"] is False
    assert row["error"] == "reply_parent_not_found"
    assert row["reply_link_status"] == "missing_parent"
    assert row["message_id"] == orphan.message_id
    assert row["in_reply_to"] == missing_parent_id
    assert row["parent_conversation_id"] is None
    assert row["message"] == expected_orphan


def test_reply_in_other_conversation_emits_typed_reply_thread_mismatch_error(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: cross-conversation reply passes as valid instead of emitting reply_thread_mismatch."""
    with AuthorityService(root=plane) as service:
        parent = service.publish_message(
            sender="operator",
            body=f"parent conv1 {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="other-conv-parent",
        )
        other_conv_seed = service.publish_message(
            sender="codex",
            body=f"reply conv2 {BODY_SENTINEL}",
            recipients=("claude-cto",),
            created_at="2026-11-28T01:02:00Z",
            idempotency_key="other-conv-reply",
        )
        assert other_conv_seed.conversation_id != parent.conversation_id

    conn = sqlite3.connect(str(plane / "comms.sqlite3"))
    try:
        conn.execute("DROP TRIGGER IF EXISTS authority_comms_message_immutable_update")
        conn.execute(
            "UPDATE comms_messages SET in_reply_to = ? WHERE message_id = ?",
            (parent.message_id, other_conv_seed.message_id),
        )
        conn.commit()
    finally:
        conn.close()

    expected_reply = _expected_message_dict(plane, other_conv_seed.message_id)
    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["reply_count"] == 1
    assert payload["error_count"] == 1

    row = payload["replies"][0]
    assert row["ok"] is False
    assert row["error"] == "reply_thread_mismatch"
    assert row["reply_link_status"] == "other_conversation"
    assert row["message_id"] == other_conv_seed.message_id
    assert row["in_reply_to"] == parent.message_id
    assert row["conversation_id"] == other_conv_seed.conversation_id
    assert row["parent_conversation_id"] == parent.conversation_id
    assert row["parent_delivery_ids"] == list(parent.delivery_ids)
    assert row["message"] == expected_reply


def test_corrupt_row_emits_typed_error_while_sibling_rows_still_print(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: one corrupt row aborts the batch instead of typing its error while siblings print."""
    with AuthorityService(root=plane) as service:
        good_msg = service.publish_message(
            sender="operator",
            body=f"good {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="corrupt-sibling-good",
        )
        bad_msg = service.publish_message(
            sender="operator",
            body=f"bad {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:00Z",
            idempotency_key="corrupt-sibling-bad",
        )
        good_reply = service.publish_message(
            sender="codex",
            body=f"good reply {BODY_SENTINEL}",
            recipients=("claude-cto",),
            in_reply_to=good_msg.message_id,
            created_at="2026-11-28T01:02:00Z",
            idempotency_key="corrupt-sibling-good-reply",
        )

    # Corrupt bad_msg's provenance_json so AuthorityService.get_message refuses it with provenance_corrupt.
    conn = sqlite3.connect(str(plane / "comms.sqlite3"))
    try:
        conn.execute("DROP TRIGGER IF EXISTS authority_message_metadata_immutable_update")
        conn.execute(
            "UPDATE authority_message_metadata SET provenance_json = ? WHERE message_id = ?",
            ("{not-valid-json", bad_msg.message_id),
        )
        conn.commit()
    finally:
        conn.close()

    expected_good_delivery = _expected_delivery_dict(plane, good_msg.delivery_ids[0])
    expected_good_message = _expected_message_dict(plane, good_msg.message_id)
    expected_bad_delivery = _expected_delivery_dict(plane, bad_msg.delivery_ids[0])
    expected_good_reply = _expected_message_dict(plane, good_reply.message_id)

    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 2
    assert payload["reply_count"] == 1
    assert payload["error_count"] == 1

    by_mid = {row["message_id"]: row for row in payload["deliveries"]}
    good_row = by_mid[good_msg.message_id]
    bad_row = by_mid[bad_msg.message_id]

    assert good_row["ok"] is True
    assert good_row["error"] is None
    assert good_row["delivery"] == expected_good_delivery
    assert good_row["message"] == expected_good_message

    assert bad_row["ok"] is False
    assert bad_row["error"] == "provenance_corrupt"
    assert bad_row["delivery"] == expected_bad_delivery
    assert bad_row["message"] is None

    assert payload["replies"][0]["ok"] is True
    assert payload["replies"][0]["message"] == expected_good_reply

    # Boundary refusals still fail closed with typed JSON errors and exit 3.
    bad_since = _run_exchange(capsys, plane, recipient="codex", since="not-a-date", expected_rc=EXIT_ERROR)
    assert bad_since["error"] == "invalid_since"
    missing_store = _run_exchange(
        capsys, plane.parent / "absent-plane", recipient="codex", expected_rc=EXIT_ERROR,
    )
    assert missing_store["error"] == "exchange_store_unavailable"
