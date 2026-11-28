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


def _reject_non_finite(token: str) -> Any:
    raise ValueError(f"non-finite JSON token: {token}")


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
    root: Path | str | None,
    *,
    recipient: str = "codex",
    since: str = SINCE_TS,
    expected_rc: int = EXIT_OK,
) -> dict[str, Any]:
    argv = ["fleet", "exchange", "--recipient", recipient, "--since", since]
    if root is not None:
        argv.extend(["--root", str(root)])
    rc = main(argv)
    output = capsys.readouterr()
    assert rc == expected_rc, f"rc={rc}, stdout={output.out!r}, stderr={output.err!r}"
    assert output.err == ""
    assert BODY_SENTINEL not in output.out
    payload = json.loads(output.out, parse_constant=_reject_non_finite)
    assert isinstance(payload, dict)
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
    """Defect class: valid reply fails to join to delivery message or leaks replies to out-of-scope parents."""
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
        # Unrelated replies to pre-window or other-recipient parents must stay out of scope
        # even when those out-of-scope replies have link mismatches or corrupt provenance (F4).
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
        other_recipient_parent = service.publish_message(
            sender="operator",
            body=f"other recipient parent {BODY_SENTINEL}",
            recipients=("claude",),
            created_at="2026-11-28T01:00:30Z",
            idempotency_key="reply-other-recipient-parent",
        )
        out_of_scope_mismatch = service.publish_message(
            sender="codex",
            body=f"out of scope mismatch {BODY_SENTINEL}",
            recipients=("claude-cto",),
            created_at="2026-11-28T01:04:00Z",
            idempotency_key="reply-out-of-scope-mismatch",
        )
        out_of_scope_corrupt = service.publish_message(
            sender="codex",
            body=f"out of scope corrupt {BODY_SENTINEL}",
            recipients=("claude-cto",),
            in_reply_to=other_recipient_parent.message_id,
            created_at="2026-11-28T01:05:00Z",
            idempotency_key="reply-out-of-scope-corrupt",
        )

    conn = sqlite3.connect(str(plane / "comms.sqlite3"))
    try:
        conn.execute("DROP TRIGGER IF EXISTS authority_comms_message_immutable_update")
        conn.execute("DROP TRIGGER IF EXISTS authority_message_metadata_immutable_update")
        conn.execute(
            "UPDATE comms_messages SET in_reply_to = ? WHERE message_id = ?",
            (other_recipient_parent.message_id, out_of_scope_mismatch.message_id),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET provenance_json = ? WHERE message_id = ?",
            ("{corrupt", out_of_scope_corrupt.message_id),
        )
        conn.commit()
    finally:
        conn.close()

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
    """Defect class: orphan or in-scope self-reply crashes or drops when delivery fails normalization."""
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
            sender="claude",
            body=f"orphan {BODY_SENTINEL}",
            recipients=("claude-cto",),
            conversation_id=parent.conversation_id,
            created_at="2026-11-28T01:02:00Z",
            idempotency_key="missing-parent-orphan",
        )
        # In-scope self-replies (selected conversation and queried recipient) must survive F4 filtering (F8).
        conv_self_reply = service.publish_message(
            sender="claude",
            body=f"conv self {BODY_SENTINEL}",
            recipients=("claude-cto",),
            conversation_id=parent.conversation_id,
            created_at="2026-11-28T01:02:10Z",
            idempotency_key="self-reply-conv",
        )
        sender_self_reply = service.publish_message(
            sender="codex",
            body=f"sender self {BODY_SENTINEL}",
            recipients=("claude-cto",),
            created_at="2026-11-28T01:02:20Z",
            idempotency_key="self-reply-sender",
        )

    # Point orphan.in_reply_to at a nonexistent parent, point self-replies at themselves,
    # and corrupt delivery attempt_count to prove conversation retention (F5, F8).
    conn = sqlite3.connect(str(plane / "comms.sqlite3"))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER IF EXISTS authority_comms_message_immutable_update")
        conn.execute(
            "UPDATE comms_messages SET in_reply_to = ? WHERE message_id = ?",
            (missing_parent_id, orphan.message_id),
        )
        conn.execute(
            "UPDATE comms_messages SET in_reply_to = message_id WHERE message_id IN (?, ?)",
            (conv_self_reply.message_id, sender_self_reply.message_id),
        )
        conn.execute(
            "UPDATE authority_deliveries SET attempt_count = 'bad' WHERE delivery_id = ?",
            (parent.delivery_ids[0],),
        )
        conn.commit()
    finally:
        conn.close()

    expected_orphan = _expected_message_dict(plane, orphan.message_id)
    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["deliveries"][0]["ok"] is False
    assert payload["deliveries"][0]["error"] == "delivery_row_corrupt"
    assert payload["reply_count"] == 3
    assert payload["error_count"] == 4

    replies_by_mid = {row["message_id"]: row for row in payload["replies"]}
    row = replies_by_mid[orphan.message_id]
    assert row["ok"] is False
    assert row["error"] == "reply_parent_not_found"
    assert row["reply_link_status"] == "missing_parent"
    assert row["message_id"] == orphan.message_id
    assert row["in_reply_to"] == missing_parent_id
    assert row["parent_conversation_id"] is None
    assert row["message"] == expected_orphan

    for self_mid in (conv_self_reply.message_id, sender_self_reply.message_id):
        self_row = replies_by_mid[self_mid]
        assert self_row["ok"] is False
        assert self_row["error"] == "reply_self_reference"
        assert self_row["reply_link_status"] == "self_reply"


def test_reply_in_other_conversation_emits_typed_reply_thread_mismatch_error(
    plane: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Defect class: cross-conversation reply passes as valid or loses mismatch diagnosis on corrupt parent metadata."""
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

    # Corrupt parent provenance as well: conversation mismatch must still be reported as
    # reply_thread_mismatch rather than masked by unreadable_parent (F6).
    conn = sqlite3.connect(str(plane / "comms.sqlite3"))
    try:
        conn.execute("DROP TRIGGER IF EXISTS authority_comms_message_immutable_update")
        conn.execute("DROP TRIGGER IF EXISTS authority_message_metadata_immutable_update")
        conn.execute(
            "UPDATE comms_messages SET in_reply_to = ? WHERE message_id = ?",
            (parent.message_id, other_conv_seed.message_id),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET provenance_json = ? WHERE message_id = ?",
            ("{bad-parent-json", parent.message_id),
        )
        conn.commit()
    finally:
        conn.close()

    expected_reply = _expected_message_dict(plane, other_conv_seed.message_id)
    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 1
    assert payload["deliveries"][0]["ok"] is False
    assert payload["deliveries"][0]["error"] == "provenance_corrupt"
    assert payload["reply_count"] == 1
    assert payload["error_count"] == 2

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
    plane: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class: corrupt metadata, storage types, overflow timestamps, invalid UTF-8, or root errors break isolation or JSON output."""
    with AuthorityService(root=plane) as service:
        good_msg = service.publish_message(
            sender="operator",
            body=f"good {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:00:00Z",
            idempotency_key="corrupt-sibling-good",
        )
        bad_json_msg = service.publish_message(
            sender="operator",
            body=f"bad json {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:00Z",
            idempotency_key="corrupt-sibling-bad-json",
        )
        bad_nan_msg = service.publish_message(
            sender="operator",
            body=f"bad nan {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:10Z",
            idempotency_key="corrupt-sibling-bad-nan",
        )
        bad_ctx_nan_msg = service.publish_message(
            sender="operator",
            body=f"bad ctx nan {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:15Z",
            idempotency_key="corrupt-sibling-bad-ctx-nan",
        )
        bad_ctx_inf_msg = service.publish_message(
            sender="operator",
            body=f"bad ctx inf {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:16Z",
            idempotency_key="corrupt-sibling-bad-ctx-inf",
        )
        bad_ctx_dup_key_msg = service.publish_message(
            sender="operator",
            body=f"bad ctx dup key {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:17Z",
            idempotency_key="corrupt-sibling-bad-ctx-dup-key",
        )
        bad_prov_dup_key_msg = service.publish_message(
            sender="operator",
            body=f"bad prov dup key {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:18Z",
            idempotency_key="corrupt-sibling-bad-prov-dup-key",
        )
        bad_blob_sender_msg = service.publish_message(
            sender="operator",
            body=f"bad blob sender {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:18.100000Z",
            idempotency_key="corrupt-sibling-bad-blob-sender",
        )
        bad_blob_meta_msg = service.publish_message(
            sender="operator",
            body=f"bad blob meta {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:18.200000Z",
            idempotency_key="corrupt-sibling-bad-blob-meta",
        )
        bad_blob_foreign_delivery_msg = service.publish_message(
            sender="operator",
            body=f"bad blob foreign delivery {BODY_SENTINEL}",
            recipients=("codex", "claude"),
            created_at="2026-11-28T01:01:18.300000Z",
            idempotency_key="corrupt-sibling-bad-blob-foreign-delivery",
        )
        bad_blob_reply_parent_msg = service.publish_message(
            sender="codex",
            body=f"bad blob reply parent {BODY_SENTINEL}",
            recipients=("claude-cto",),
            created_at="2026-11-28T01:01:18.400000Z",
            idempotency_key="corrupt-sibling-bad-blob-reply-parent",
        )
        bad_utf8_msg = service.publish_message(
            sender="operator",
            body=f"bad utf8 {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:20Z",
            idempotency_key="corrupt-sibling-bad-utf8",
        )
        bad_overflow_msg = service.publish_message(
            sender="operator",
            body=f"bad overflow {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:30Z",
            idempotency_key="corrupt-sibling-bad-overflow",
        )
        missing_meta_parent = service.publish_message(
            sender="operator",
            body=f"missing meta {BODY_SENTINEL}",
            recipients=("codex",),
            created_at="2026-11-28T01:01:40Z",
            idempotency_key="corrupt-sibling-missing-meta",
        )
        reply_to_missing_meta = service.publish_message(
            sender="codex",
            body=f"reply to missing meta {BODY_SENTINEL}",
            recipients=("claude-cto",),
            in_reply_to=missing_meta_parent.message_id,
            created_at="2026-11-28T01:01:50Z",
            idempotency_key="corrupt-sibling-reply-missing-meta",
        )
        good_reply = service.publish_message(
            sender="codex",
            body=f"good reply {BODY_SENTINEL}",
            recipients=("claude-cto",),
            in_reply_to=good_msg.message_id,
            created_at="2026-11-28T01:02:00Z",
            idempotency_key="corrupt-sibling-good-reply",
        )

    conn = sqlite3.connect(str(plane / "comms.sqlite3"))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER IF EXISTS authority_comms_message_immutable_update")
        conn.execute("DROP TRIGGER IF EXISTS authority_message_metadata_immutable_update")
        conn.execute("DROP TRIGGER IF EXISTS authority_message_metadata_immutable_delete")
        conn.execute(
            "UPDATE authority_message_metadata SET provenance_json = ? WHERE message_id = ?",
            ("{not-valid-json", bad_json_msg.message_id),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET provenance_json = ? WHERE message_id = ?",
            ('{"score": NaN}', bad_nan_msg.message_id),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET context_revisions_json = ? WHERE message_id = ?",
            ('{"rev": NaN}', bad_ctx_nan_msg.message_id),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET context_revisions_json = ? WHERE message_id = ?",
            ('{"rev": {"nested": -Infinity}, "overflow": 1e999}', bad_ctx_inf_msg.message_id),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET context_revisions_json = ? WHERE message_id = ?",
            ('{"rev": 123, "rev": "ok"}', bad_ctx_dup_key_msg.message_id),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET provenance_json = ? WHERE message_id = ?",
            ('{"outer": {"x": 1e999, "x": 1}}', bad_prov_dup_key_msg.message_id),
        )
        conn.execute(
            "UPDATE comms_messages SET sender = X'73796e746865746963' WHERE message_id = ?",
            (bad_blob_sender_msg.message_id,),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET correlation_id = X'626164' WHERE message_id = ?",
            (bad_blob_meta_msg.message_id,),
        )
        conn.execute(
            "UPDATE authority_deliveries SET delivery_id = X'666f726569676e' WHERE message_id = ? AND recipient = 'claude'",
            (bad_blob_foreign_delivery_msg.message_id,),
        )
        conn.execute(
            "UPDATE comms_messages SET in_reply_to = X'626164' WHERE message_id = ?",
            (bad_blob_reply_parent_msg.message_id,),
        )
        conn.execute(
            "UPDATE authority_message_metadata SET provenance_json = CAST(X'80' AS TEXT) WHERE message_id = ?",
            (bad_utf8_msg.message_id,),
        )
        conn.execute(
            "UPDATE authority_deliveries SET updated_at = ? WHERE delivery_id = ?",
            ("9999-12-31T23:59:59-01:00", bad_overflow_msg.delivery_ids[0]),
        )
        conn.execute(
            "DELETE FROM authority_message_metadata WHERE message_id = ?",
            (missing_meta_parent.message_id,),
        )
        conn.commit()
    finally:
        conn.close()

    expected_good_delivery = _expected_delivery_dict(plane, good_msg.delivery_ids[0])
    expected_good_message = _expected_message_dict(plane, good_msg.message_id)
    expected_bad_delivery = _expected_delivery_dict(plane, bad_json_msg.delivery_ids[0])
    expected_good_reply = _expected_message_dict(plane, good_reply.message_id)

    payload = _run_exchange(capsys, plane, recipient="codex")
    assert payload["delivery_count"] == 13
    assert payload["reply_count"] == 3
    assert payload["error_count"] == 14

    by_mid = {row["message_id"]: row for row in payload["deliveries"]}
    assert by_mid[good_msg.message_id]["ok"] is True
    assert by_mid[good_msg.message_id]["delivery"] == expected_good_delivery
    assert by_mid[good_msg.message_id]["message"] == expected_good_message

    assert by_mid[bad_json_msg.message_id]["ok"] is False
    assert by_mid[bad_json_msg.message_id]["error"] == "provenance_corrupt"
    assert by_mid[bad_json_msg.message_id]["delivery"] == expected_bad_delivery
    assert by_mid[bad_json_msg.message_id]["message"] is None

    assert by_mid[bad_nan_msg.message_id]["ok"] is False
    assert by_mid[bad_nan_msg.message_id]["error"] == "provenance_corrupt"
    assert by_mid[bad_nan_msg.message_id]["message"] is None

    assert by_mid[bad_ctx_nan_msg.message_id]["ok"] is False
    assert by_mid[bad_ctx_nan_msg.message_id]["error"] == "context_revisions_corrupt"
    assert by_mid[bad_ctx_nan_msg.message_id]["message"] is None

    assert by_mid[bad_ctx_inf_msg.message_id]["ok"] is False
    assert by_mid[bad_ctx_inf_msg.message_id]["error"] == "context_revisions_corrupt"
    assert by_mid[bad_ctx_inf_msg.message_id]["message"] is None

    assert by_mid[bad_ctx_dup_key_msg.message_id]["ok"] is False
    assert by_mid[bad_ctx_dup_key_msg.message_id]["error"] == "context_revisions_corrupt"
    assert by_mid[bad_ctx_dup_key_msg.message_id]["message"] is None

    assert by_mid[bad_prov_dup_key_msg.message_id]["ok"] is False
    assert by_mid[bad_prov_dup_key_msg.message_id]["error"] == "provenance_corrupt"
    assert by_mid[bad_prov_dup_key_msg.message_id]["message"] is None

    for blob_msg in (bad_blob_sender_msg, bad_blob_meta_msg, bad_blob_foreign_delivery_msg):
        assert by_mid[blob_msg.message_id]["ok"] is False
        assert by_mid[blob_msg.message_id]["error"] == "message_row_corrupt"
        assert by_mid[blob_msg.message_id]["message"] is None

    assert by_mid[bad_utf8_msg.message_id]["ok"] is False
    assert by_mid[bad_utf8_msg.message_id]["error"] == "provenance_corrupt"
    assert by_mid[bad_utf8_msg.message_id]["message"] is None

    assert by_mid[bad_overflow_msg.message_id]["ok"] is False
    assert by_mid[bad_overflow_msg.message_id]["error"] == "invalid_updated_at"
    assert by_mid[bad_overflow_msg.message_id]["delivery"] is None

    assert by_mid[missing_meta_parent.message_id]["ok"] is False
    assert by_mid[missing_meta_parent.message_id]["error"] == "message_not_found"

    replies_by_mid = {row["message_id"]: row for row in payload["replies"]}
    assert replies_by_mid[good_reply.message_id]["ok"] is True
    assert replies_by_mid[good_reply.message_id]["message"] == expected_good_reply
    # Existing parent missing metadata reports unreadable_parent (message_not_found), not missing_parent (F6).
    assert replies_by_mid[reply_to_missing_meta.message_id]["ok"] is False
    assert replies_by_mid[reply_to_missing_meta.message_id]["reply_link_status"] == "unreadable_parent"
    assert replies_by_mid[reply_to_missing_meta.message_id]["error"] == "message_not_found"
    assert replies_by_mid[reply_to_missing_meta.message_id]["parent_conversation_id"] == missing_meta_parent.conversation_id
    # BLOB in_reply_to fails closed as corrupt_reply rather than misreporting missing_parent with coerced bytes.
    assert replies_by_mid[bad_blob_reply_parent_msg.message_id]["ok"] is False
    assert replies_by_mid[bad_blob_reply_parent_msg.message_id]["reply_link_status"] == "corrupt_reply"
    assert replies_by_mid[bad_blob_reply_parent_msg.message_id]["error"] == "message_row_corrupt"
    assert replies_by_mid[bad_blob_reply_parent_msg.message_id]["message"] is None

    # Boundary refusals still fail closed with typed JSON errors and exit 3 (F1, F9, R1-F1).
    bad_since = _run_exchange(capsys, plane, recipient="codex", since="not-a-date", expected_rc=EXIT_ERROR)
    assert bad_since["error"] == "invalid_since"
    overflow_since = _run_exchange(
        capsys, plane, recipient="codex", since="9999-12-31T23:59:59-01:00", expected_rc=EXIT_ERROR,
    )
    assert overflow_since["error"] == "invalid_since"
    surrogate_recipient = _run_exchange(
        capsys, plane, recipient="codex\udc80", expected_rc=EXIT_ERROR,
    )
    assert surrogate_recipient["error"] == "invalid_recipient"
    missing_store = _run_exchange(
        capsys, plane.parent / "absent-plane", recipient="codex", expected_rc=EXIT_ERROR,
    )
    assert missing_store["error"] == "exchange_store_unavailable"

    # Root-resolution refusals emit path-free JSON with exit 3 across --root and default/env forms.
    checkout_root = plane.parent / "checkout-root"
    (checkout_root / ".git").mkdir(parents=True)
    assert _run_exchange(capsys, checkout_root, recipient="codex", expected_rc=EXIT_ERROR)["error"] == "plane_root_is_git_checkout"
    assert _run_exchange(capsys, Path("~nonexistent_exchange_user_zzz"), recipient="codex", expected_rc=EXIT_ERROR)["error"] == "plane_root_unresolvable"
    assert _run_exchange(capsys, "", recipient="codex", expected_rc=EXIT_ERROR)["error"] == "plane_root_unresolvable"

    retired_root = plane.parent / "retired-plane"
    retired_root.mkdir()
    (retired_root / "READ_ME_CANONICAL_ON_JOB_HOST.txt").write_text("retired\n", encoding="utf-8")
    with monkeypatch.context() as guard:
        guard.delenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW", raising=False)
        guard.setattr("lu_machinery.fleet_comms.paths.configured_host_state_root", lambda: None)
        assert _run_exchange(capsys, retired_root, recipient="codex", expected_rc=EXIT_ERROR)["error"] == "retired_local_plane"
        guard.setenv("FLEET_COMMS_ROOT", str(retired_root))
        assert _run_exchange(capsys, None, recipient="codex", expected_rc=EXIT_ERROR)["error"] == "retired_local_plane"
        guard.setenv("FLEET_COMMS_ROOT", str(checkout_root))
        assert _run_exchange(capsys, None, recipient="codex", expected_rc=EXIT_ERROR)["error"] == "plane_root_is_git_checkout"
        guard.delenv("FLEET_COMMS_ROOT", raising=False)
        guard.delenv("LU_STATE_ROOT", raising=False)
        guard.setattr("lu_machinery.common.repo_root.configured_host_state_root", lambda *_a, **_kw: None)
        assert _run_exchange(capsys, None, recipient="codex", expected_rc=EXIT_ERROR)["error"] == "exchange_store_unavailable"
