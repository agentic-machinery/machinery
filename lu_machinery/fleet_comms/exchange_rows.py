"""Read-only per-recipient delivery and reply rows built on Fleet Comms normalizers.

Exposes typed JSON rows for deliveries addressed to one recipient since a UTC
timestamp and the messages replying to those deliveries. Every row is normalized
through ``AuthorityService._delivery_from_row`` and ``AuthorityService.get_message``
against a read-only ``ArtifactStore`` snapshot: no writes, no migrations, no
claims, no lease changes, and no message body content.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from lu_machinery.control_plane.storage import (
    ControlPlaneError,
    ControlPlaneUnsupportedComponentError,
    StoreId,
    assert_component_supported,
)
from lu_machinery.fleet_comms.artifacts import ArtifactStore, ArtifactStoreError
from lu_machinery.fleet_comms.authority import (
    AuthorityMessage,
    AuthorityService,
    AuthorityServiceError,
    _iso,
    _nonempty,
    _parse_iso,
)
from lu_machinery.fleet_comms.opsec_store import COMMS_RESPONSE_SCHEMA_VERSION
from lu_machinery.fleet_comms.paths import (
    PlaneRootAnchorError,
    default_plane_root,
    local_plane_is_retired,
    validate_plane_root,
)
from lu_machinery.lib.readonly_sqlite import SQLiteConnection

_VALID_DELIVERY_STATES = frozenset(
    {"queued", "running", "acknowledged", "failed", "expired", "dead_lettered"}
)
_SQL_IN_CHUNK_SIZE = 200
_SORT_LAST = (1, "")


class ExchangeRowsError(RuntimeError):
    """Typed boundary refusal when the exchange read itself cannot run."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _as_nonempty_str(value: Any, *, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorityServiceError(error_code)
    return value


def _parse_required_iso(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AuthorityServiceError(f"invalid_{field}")
    return _parse_iso(value, field=field)


def _parse_optional_iso(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    return _parse_required_iso(value, field=field)


def _require_nonneg_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthorityServiceError("delivery_row_corrupt")
    return value


def _message_to_dict(message: AuthorityMessage) -> dict[str, Any]:
    _as_nonempty_str(message.message_id, error_code="message_row_corrupt")
    _as_nonempty_str(message.conversation_id, error_code="message_row_corrupt")
    _as_nonempty_str(message.thread_id, error_code="message_row_corrupt")
    _as_nonempty_str(message.sender, error_code="message_row_corrupt")
    _as_nonempty_str(message.kind, error_code="message_row_corrupt")
    _as_nonempty_str(message.body_artifact_id, error_code="message_body_artifact_missing")
    _parse_required_iso(message.created_at, field="created_at")
    payload = asdict(message)
    payload["delivery_ids"] = list(message.delivery_ids)
    return payload


def _read_message_cached(
    service: AuthorityService,
    cache: dict[str, tuple[dict[str, Any] | None, str | None]],
    message_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    cached = cache.get(message_id)
    if cached is not None:
        return cached
    try:
        message = service.get_message(message_id)
        result: tuple[dict[str, Any] | None, str | None] = (_message_to_dict(message), None)
    except AuthorityServiceError as exc:
        result = (None, str(exc))
    except RecursionError:
        result = (None, "message_metadata_corrupt")
    except (TypeError, ValueError, KeyError):
        result = (None, "message_row_corrupt")
    cache[message_id] = result
    return result


def _normalize_delivery_row(
    service: AuthorityService,
    row: sqlite3.Row,
) -> tuple[dict[str, Any], datetime]:
    _as_nonempty_str(row["delivery_id"], error_code="delivery_id_required")
    _as_nonempty_str(row["message_id"], error_code="message_id_required")
    _as_nonempty_str(row["recipient"], error_code="recipient_required")
    state = _as_nonempty_str(row["state"], error_code="invalid_delivery_state")
    if state not in _VALID_DELIVERY_STATES:
        raise AuthorityServiceError("invalid_delivery_state")
    _require_nonneg_int(row["fence_token"])
    _require_nonneg_int(row["attempt_count"])
    try:
        delivery = service._delivery_from_row(row)
    except (TypeError, ValueError, KeyError) as exc:
        raise AuthorityServiceError("delivery_row_corrupt") from exc
    created_dt = _parse_required_iso(delivery.created_at, field="created_at")
    _parse_required_iso(delivery.updated_at, field="updated_at")
    _parse_optional_iso(delivery.deadline_at, field="deadline_at")
    _parse_optional_iso(delivery.lease_expires_at, field="lease_expires_at")
    _parse_optional_iso(delivery.completed_at, field="completed_at")
    return asdict(delivery), created_dt


def _delivery_lifecycle(
    conn: SQLiteConnection,
    delivery: dict[str, Any],
) -> tuple[dict[str, str | None], str | None]:
    delivery_id = str(delivery["delivery_id"])
    state = str(delivery["state"])
    completed_at = delivery["completed_at"]
    lifecycle: dict[str, str | None] = {
        "claimed_at": None,
        "acked_at": completed_at if state == "acknowledged" else None,
        "expired_at": completed_at if state == "expired" else None,
        "dead_lettered_at": completed_at if state == "dead_lettered" else None,
        "dead_letter_reason": None,
    }
    try:
        attempt = conn.execute(
            """SELECT started_at FROM authority_delivery_attempts
               WHERE delivery_id = ?
               ORDER BY fence_token DESC, started_at DESC
               LIMIT 1""",
            (delivery_id,),
        ).fetchone()
        if attempt is not None:
            started_at = attempt["started_at"]
            _parse_required_iso(started_at, field="claimed_at")
            lifecycle["claimed_at"] = str(started_at)

        dlq = conn.execute(
            """SELECT reason_code, created_at FROM authority_dead_letters
               WHERE delivery_id = ?""",
            (delivery_id,),
        ).fetchone()
        if dlq is not None:
            reason_code = _as_nonempty_str(dlq["reason_code"], error_code="dead_letter_row_corrupt")
            _parse_required_iso(dlq["created_at"], field="dead_lettered_at")
            lifecycle["dead_letter_reason"] = reason_code
            lifecycle["dead_lettered_at"] = str(dlq["created_at"])
    except AuthorityServiceError as exc:
        return lifecycle, str(exc)
    return lifecycle, None


def _try_parse_created_at(raw_value: Any) -> datetime | None:
    try:
        return _parse_required_iso(raw_value, field="created_at")
    except AuthorityServiceError:
        return None


def _sort_key_for_iso(raw_value: Any) -> tuple[int, str]:
    parsed = _try_parse_created_at(raw_value)
    if parsed is None:
        return _SORT_LAST
    return (0, parsed.isoformat())


def _chunked(values: list[str], size: int = _SQL_IN_CHUNK_SIZE) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _candidate_reply_rows(
    conn: SQLiteConnection,
    *,
    recipient: str,
    delivery_message_ids: list[str],
    conversation_ids: list[str],
) -> list[sqlite3.Row]:
    """Collect candidate reply metadata rows without ever selecting message body columns."""
    seen: set[str] = set()
    rows: list[sqlite3.Row] = []

    def _collect(fetched: list[sqlite3.Row]) -> None:
        for row in fetched:
            mid = row["message_id"]
            key = str(mid) if mid is not None else ""
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

    for chunk in _chunked(sorted(delivery_message_ids)):
        placeholders = ", ".join("?" for _ in chunk)
        _collect(
            conn.execute(
                f"""SELECT message_id, conversation_id, in_reply_to, sender, created_at
                    FROM comms_messages
                    WHERE in_reply_to IN ({placeholders})
                    ORDER BY created_at ASC, message_id ASC""",
                chunk,
            ).fetchall()
        )

    for chunk in _chunked(sorted(conversation_ids)):
        placeholders = ", ".join("?" for _ in chunk)
        _collect(
            conn.execute(
                f"""SELECT message_id, conversation_id, in_reply_to, sender, created_at
                    FROM comms_messages
                    WHERE in_reply_to IS NOT NULL AND conversation_id IN ({placeholders})
                    ORDER BY created_at ASC, message_id ASC""",
                chunk,
            ).fetchall()
        )

    _collect(
        conn.execute(
            """SELECT message_id, conversation_id, in_reply_to, sender, created_at
               FROM comms_messages
               WHERE in_reply_to IS NOT NULL AND sender = ?
               ORDER BY created_at ASC, message_id ASC""",
            (recipient,),
        ).fetchall()
    )
    return rows


def _raw_str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def read_exchange_rows(
    recipient: str,
    since: str,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Read typed delivery and reply rows for ``recipient`` since UTC ``since``.

    Opens the Fleet Comms SQLite store strictly read-only without running
    migrations, claiming deliveries, or reading message body content.
    """
    try:
        recipient_name = _nonempty(recipient, field="recipient")
        since_dt = _parse_required_iso(since, field="since")
    except AuthorityServiceError as exc:
        raise ExchangeRowsError(str(exc)) from exc
    since_iso = (
        since_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if since_dt.microsecond
        else _iso(since_dt)
    )

    try:
        assert_component_supported(StoreId.FLEET_COMMS, "authority_service")
    except ControlPlaneUnsupportedComponentError as exc:
        raise ExchangeRowsError("authority_unsupported_component") from exc
    except ControlPlaneError as exc:
        raise ExchangeRowsError("exchange_store_unavailable") from exc

    try:
        plane_root = validate_plane_root(root) if root is not None else default_plane_root()
    except PlaneRootAnchorError as exc:
        raise ExchangeRowsError("exchange_store_unavailable") from exc
    if local_plane_is_retired(root=plane_root):
        raise ExchangeRowsError("retired_local_plane")

    try:
        with ArtifactStore.open_readonly(root=plane_root) as store:
            conn: SQLiteConnection = store.connection
            if isinstance(conn, sqlite3.Connection):
                if hasattr(conn, "enable_load_extension"):
                    conn.enable_load_extension(False)
                conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            conn.execute("PRAGMA query_only = ON")
            service = AuthorityService(store=store)
            conn.execute("BEGIN")
            try:
                return _collect_exchange_payload(
                    conn=conn,
                    service=service,
                    recipient_name=recipient_name,
                    since_dt=since_dt,
                    since_iso=since_iso,
                )
            finally:
                if conn.in_transaction:
                    conn.rollback()
    except AuthorityServiceError as exc:
        raise ExchangeRowsError(str(exc)) from exc
    except ControlPlaneUnsupportedComponentError as exc:
        raise ExchangeRowsError("authority_unsupported_component") from exc
    except (OSError, sqlite3.Error, ControlPlaneError, ArtifactStoreError, PlaneRootAnchorError) as exc:
        raise ExchangeRowsError("exchange_store_unavailable") from exc


def _collect_exchange_payload(
    *,
    conn: SQLiteConnection,
    service: AuthorityService,
    recipient_name: str,
    since_dt: datetime,
    since_iso: str,
) -> dict[str, Any]:
    message_cache: dict[str, tuple[dict[str, Any] | None, str | None]] = {}
    delivery_rows_raw = conn.execute(
        """SELECT * FROM authority_deliveries
           WHERE recipient = ?
           ORDER BY created_at ASC, delivery_id ASC""",
        (recipient_name,),
    ).fetchall()

    deliveries: list[tuple[tuple[int, str], str, dict[str, Any]]] = []
    delivery_ids_by_message: dict[str, list[str]] = {}
    selected_conversations: set[str] = set()

    for raw in delivery_rows_raw:
        raw_delivery_id = _raw_str_or_none(raw["delivery_id"])
        raw_message_id = _raw_str_or_none(raw["message_id"])
        try:
            delivery_dict, created_dt = _normalize_delivery_row(service, raw)
        except AuthorityServiceError as exc:
            parsed_created = _try_parse_created_at(raw["created_at"])
            if parsed_created is not None and parsed_created < since_dt:
                continue
            row_payload: dict[str, Any] = {
                "ok": False,
                "error": str(exc),
                "delivery_id": raw_delivery_id,
                "message_id": raw_message_id,
                "claimed_at": None,
                "acked_at": None,
                "expired_at": None,
                "dead_lettered_at": None,
                "dead_letter_reason": None,
                "delivery": None,
                "message": None,
            }
            deliveries.append((_sort_key_for_iso(raw["created_at"]), raw_delivery_id or "", row_payload))
            if raw_message_id and raw_delivery_id:
                delivery_ids_by_message.setdefault(raw_message_id, []).append(raw_delivery_id)
            continue

        if created_dt < since_dt:
            continue

        delivery_id = str(delivery_dict["delivery_id"])
        message_id = str(delivery_dict["message_id"])
        delivery_ids_by_message.setdefault(message_id, []).append(delivery_id)

        lifecycle, lifecycle_error = _delivery_lifecycle(conn, delivery_dict)
        message_dict, message_error = _read_message_cached(service, message_cache, message_id)
        if message_dict is not None:
            selected_conversations.add(str(message_dict["conversation_id"]))
        else:
            conv_row = conn.execute(
                "SELECT conversation_id FROM comms_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if conv_row is not None and isinstance(conv_row["conversation_id"], str) and conv_row["conversation_id"].strip():
                selected_conversations.add(str(conv_row["conversation_id"]))

        row_error = message_error or lifecycle_error
        row_payload = {
            "ok": row_error is None,
            "error": row_error,
            "delivery_id": delivery_id,
            "message_id": message_id,
            **lifecycle,
            "delivery": delivery_dict,
            "message": message_dict,
        }
        deliveries.append(((0, created_dt.isoformat()), delivery_id, row_payload))

    deliveries.sort(key=lambda item: (item[0], item[1]))
    delivery_list = [item[2] for item in deliveries]

    reply_candidates = _candidate_reply_rows(
        conn,
        recipient=recipient_name,
        delivery_message_ids=list(delivery_ids_by_message),
        conversation_ids=list(selected_conversations),
    )

    replies: list[tuple[tuple[int, str], str, dict[str, Any]]] = []
    for raw_reply in reply_candidates:
        raw_mid = _raw_str_or_none(raw_reply["message_id"]) or ""
        raw_parent = _raw_str_or_none(raw_reply["in_reply_to"])
        raw_conv = _raw_str_or_none(raw_reply["conversation_id"])
        raw_sender = _raw_str_or_none(raw_reply["sender"])

        is_direct_reply = raw_parent is not None and raw_parent in delivery_ids_by_message
        if not is_direct_reply:
            parsed_reply_created = _try_parse_created_at(raw_reply["created_at"])
            if parsed_reply_created is not None and parsed_reply_created < since_dt:
                continue

        parent_delivery_ids = sorted(delivery_ids_by_message.get(raw_parent or "", []))
        reply_dict, reply_error = _read_message_cached(service, message_cache, raw_mid)
        if reply_error is not None:
            reply_payload: dict[str, Any] = {
                "ok": False,
                "error": reply_error,
                "reply_link_status": "corrupt_reply",
                "message_id": raw_mid or None,
                "in_reply_to": raw_parent,
                "conversation_id": raw_conv,
                "parent_conversation_id": None,
                "parent_delivery_ids": parent_delivery_ids,
                "message": None,
            }
            replies.append((_sort_key_for_iso(raw_reply["created_at"]), raw_mid, reply_payload))
            continue

        assert reply_dict is not None
        parent_id = reply_dict["in_reply_to"]
        reply_conv = str(reply_dict["conversation_id"])
        parent_conv: str | None = None
        link_status: str
        link_error: str | None

        if not isinstance(parent_id, str) or not parent_id.strip():
            link_status = "missing_parent"
            link_error = "reply_parent_not_found"
        elif parent_id == reply_dict["message_id"]:
            link_status = "self_reply"
            link_error = "reply_self_reference"
            parent_conv = reply_conv
        else:
            parent_dict, parent_error = _read_message_cached(service, message_cache, parent_id)
            if parent_dict is not None:
                parent_conv = str(parent_dict["conversation_id"])
                if reply_conv == parent_conv:
                    if not is_direct_reply:
                        # Valid reply to a message outside the selected deliveries window.
                        continue
                    link_status = "valid"
                    link_error = None
                else:
                    link_status = "other_conversation"
                    link_error = "reply_thread_mismatch"
            elif parent_error == "message_not_found":
                link_status = "missing_parent"
                link_error = "reply_parent_not_found"
            else:
                # Parent exists with metadata, but Fleet refused to read it.
                link_status = "unreadable_parent"
                link_error = parent_error
                parent_row = conn.execute(
                    "SELECT conversation_id FROM comms_messages WHERE message_id = ?",
                    (parent_id,),
                ).fetchone()
                if parent_row is not None and isinstance(parent_row["conversation_id"], str):
                    parent_conv = parent_row["conversation_id"]

        # Non-direct candidate with an error belongs only if sent by recipient or in a selected conversation.
        if (
            not is_direct_reply
            and raw_sender != recipient_name
            and reply_conv not in selected_conversations
        ):
            continue

        reply_payload = {
            "ok": link_error is None,
            "error": link_error,
            "reply_link_status": link_status,
            "message_id": reply_dict["message_id"],
            "in_reply_to": parent_id,
            "conversation_id": reply_conv,
            "parent_conversation_id": parent_conv,
            "parent_delivery_ids": parent_delivery_ids,
            "message": reply_dict,
        }
        replies.append((_sort_key_for_iso(reply_dict["created_at"]), str(reply_dict["message_id"]), reply_payload))

    replies.sort(key=lambda item: (item[0], item[1]))
    reply_list = [item[2] for item in replies]

    error_count = sum(1 for row in delivery_list if not row["ok"]) + sum(
        1 for row in reply_list if not row["ok"]
    )
    return {
        "response_schema_version": COMMS_RESPONSE_SCHEMA_VERSION,
        "content_included": False,
        "read_only": True,
        "source": "authority",
        "recipient": recipient_name,
        "since": since_iso,
        "deliveries": delivery_list,
        "replies": reply_list,
        "delivery_count": len(delivery_list),
        "reply_count": len(reply_list),
        "error_count": error_count,
    }


def exchange_error_payload(code: str) -> dict[str, Any]:
    """Return a path-free, body-free typed error payload for CLI boundary refusals."""
    return {
        "response_schema_version": COMMS_RESPONSE_SCHEMA_VERSION,
        "content_included": False,
        "read_only": True,
        "source": "authority",
        "error": code,
    }
