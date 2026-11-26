"""FLEET-READ slice 1 tests: ACP Discussions panel, shared guard, C1 redaction/allowlist, and E3 caps."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lu_machinery.api.config as api_config
import lu_machinery.api.runtime_router as runtime_router
from lu_machinery.agent_runtime.acpx_discuss import AcpxDiscussionController
from lu_machinery.api.main import app, create_app
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.api.product_contribution import EMPTY
from lu_machinery.api.route_contracts import contract_for_page, contract_for_route
from lu_machinery.common.repo_root import machinery_path

_LISTENER_ORIGIN = f"http://{api_config.API_HOST}:{api_config.API_PORT}"


def _loopback_client(target_app=app) -> TestClient:
    return TestClient(
        target_app,
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
        base_url=_LISTENER_ORIGIN,
    )


def _seed_conversation(
    root: Path,
    *,
    participants: tuple[str, ...] = ("codex", "claude"),
    turns: list[tuple[str, str, str, str, int | None]],
    suffix: str = "s1",
) -> tuple[str, list[tuple[str, str, str]]]:
    """Seed one ACP conversation through the real controller with CAS blobs."""
    controller = AcpxDiscussionController(root=root)
    try:
        conversation_id, _ = controller._reserve(
            task_digest=f"task-{suffix}",
            correlation_digest=f"corr-{suffix}",
            idempotency_digest=f"idem-{suffix}",
            rounds=1,
            deadline_at="2099-01-01T00:00:00Z",
            participants=participants,
        )
        records: list[tuple[str, str, str]] = []
        for idx, (kind, sender, recipient, body, round_no) in enumerate(turns, start=2):
            message_id = controller._message(
                conversation_id,
                sender=sender,
                recipient=recipient,
                body=body,
                reply_to=None,
                kind=kind,
            )
            controller._append(
                conversation_id,
                event_type="SYNTHESIS_TERMINAL" if kind == "synthesis" else "CALL_TERMINAL",
                state="COMPLETE" if idx == len(turns) + 1 else "INITIAL_FANOUT",
                sender=sender,
                recipient=recipient,
                round_no=round_no,
                outcome="succeeded",
                message_id=message_id,
            )
            row = controller.conn.execute(
                "SELECT body_artifact_id, content_sha256 FROM comms_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            records.append((message_id, str(row[0]), str(row[1])))
        return conversation_id, records
    finally:
        controller.close()


def _insert_custom_turn(
    root: Path,
    conversation_id: str,
    *,
    sequence: int,
    message_id: str,
    kind: str,
    sender: str,
    recipient: str,
    body: str,
    producer: str | None,
    round_no: int | None = 1,
    event_type: str = "CALL_TERMINAL",
    retention_class: str = "acpx-discussion",
) -> None:
    """Insert a turn with explicit producer/kind into an existing Fleet Comms SQLite store."""
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    conn = sqlite3.connect(root / "comms.sqlite3")
    try:
        artifact_id: str | None = None
        digest: str | None = None
        if producer is not None:
            raw = body.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            blob_path = root / "blobs" / "sha256" / digest[:2] / digest
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(raw)
            artifact_id = f"art-{message_id}"
            conn.execute(
                """INSERT INTO artifacts(
                    artifact_id, sha256, bytes, mime_type, logical_filename, producer, retention_class, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, digest, len(raw), "text/plain; charset=utf-8", f"{message_id}.txt", producer, retention_class, now_iso),
            )
        conn.execute(
            """INSERT INTO comms_messages(
                message_id, conversation_id, kind, sender, recipient, body_inline,
                body_artifact_id, content_sha256, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, conversation_id, kind, sender, recipient, body, artifact_id, digest, "{}", now_iso),
        )
        if artifact_id is not None:
            conn.execute(
                "INSERT INTO message_artifacts(message_id, artifact_id, relation) VALUES (?, ?, 'body')",
                (message_id, artifact_id),
            )
        conn.execute(
            """INSERT INTO acp_conversation_events(
                event_id, conversation_id, sequence, event_type, state, sender, recipient,
                round, outcome, message_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"evt-{message_id}", conversation_id, sequence, event_type, "COMPLETE",
                sender, recipient, round_no, "succeeded", message_id, "{}", now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_cross_origin_read_refused(tmp_path: Path, monkeypatch) -> None:
    """Defect class: cross-origin, cross-site, proxied, or non-loopback requests read ACP transcripts or bypass the shared guard."""
    root = tmp_path / "plane"
    secret_body = "CROSS_ORIGIN_SENSITIVE_BODY"
    conv_id, _ = _seed_conversation(
        root,
        turns=[("reply", "codex", "root", secret_body, 1)],
        suffix="cross-origin",
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    # A2: single shared guard function wired directly into the transcript route (no copy).
    assert runtime_router._acp_transcript_access_refusal is runtime_router.local_body_read_access_refusal
    route_src = inspect.getsource(runtime_router.runtime_acp_conversation_transcript)
    assert "local_body_read_refusal_response" in route_src

    local = _loopback_client()
    url = f"/api/runtime/acp/conversations/{conv_id}/transcript"

    cases = [
        ({"Origin": "https://attacker.example"}, "invalid_origin"),
        ({"Origin": "http://localhost:8765"}, "invalid_origin"),
        ({"Sec-Fetch-Site": "cross-site"}, "cross_site_request"),
        ({"Sec-Fetch-Site": "same-site"}, "cross_site_request"),
        ({"Host": "evil.example"}, "invalid_host"),
        ({"Forwarded": "for=127.0.0.1"}, "forwarding_header_present"),
        ({"X-Forwarded-For": "127.0.0.1"}, "forwarding_header_present"),
    ]
    for headers, expected_reason in cases:
        resp = local.get(url, headers=headers)
        assert resp.status_code == 403, headers
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == {"detail": "Forbidden", "reason": expected_reason}
        assert secret_body not in resp.text

    remote = TestClient(app, raise_server_exceptions=False, client=("198.51.100.7", 54321), base_url=_LISTENER_ORIGIN)
    remote_resp = remote.get(url)
    assert remote_resp.status_code == 403
    assert remote_resp.headers["cache-control"] == "no-store"
    assert remote_resp.json() == {"detail": "Forbidden", "reason": "non_loopback_peer"}
    assert secret_body not in remote_resp.text


def test_served_artifact_or_file_page_cannot_read_transcript(tmp_path: Path, monkeypatch) -> None:
    """Defect class: a page served from /artifacts or /files executes same-origin and reads ACP transcripts."""
    docs_reports = tmp_path / "docs" / "reports"
    docs_reports.mkdir(parents=True)
    probe_html = "<!doctype html><script>fetch('/api/runtime/acp/conversations/x/transcript')</script>"
    (docs_reports / "probe.html").write_text(probe_html, encoding="utf-8")

    plane_root = tmp_path / "plane"
    secret_body = "SANDBOXED_PAGE_MUST_NOT_READ_THIS"
    conv_id, _ = _seed_conversation(
        plane_root,
        turns=[("reply", "codex", "root", secret_body, 1)],
        suffix="sandbox",
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane_root))

    isolated_app = create_app(fixture_context(tmp_path), product=EMPTY)
    local = _loopback_client(isolated_app)

    for prefix in ("artifacts", "files"):
        served = local.get(f"/{prefix}/reports/probe.html")
        assert served.status_code == 200, prefix
        csp = served.headers["content-security-policy"]
        assert csp == "sandbox"
        assert "allow-same-origin" not in csp
        assert served.headers["x-content-type-options"] == "nosniff"

    # A sandboxed document without allow-same-origin runs in an opaque origin and sends Origin: null.
    transcript_url = f"/api/runtime/acp/conversations/{conv_id}/transcript"
    for headers in (
        {"Origin": "null"},
        {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    ):
        denied = local.get(transcript_url, headers=headers)
        assert denied.status_code == 403, headers
        assert denied.headers["cache-control"] == "no-store"
        assert denied.json() == {"detail": "Forbidden", "reason": "invalid_origin"}
        assert secret_body not in denied.text


def test_secrets_paths_nested_json_and_labels_redacted_in_json(tmp_path: Path, monkeypatch) -> None:
    """Defect class: secrets, absolute home paths, nested JSON secrets/paths, over-deep JSON, non-finite JSON, or poisoned labels leak or 500."""
    root = tmp_path / "plane"
    planted_ghp = "ghp_" + "1234567890" + "abcdefghijklmnopqrstuvwx"
    planted_ant = "sk-" + "ant-" + "abcdefghijklmnopqrstuvwxyz1234"
    planted_url_pw = "super_secret_url_pw_99"
    planted_home = "/home/alice/private-key-dir/token.txt"
    planted_users = "/Users/bob/Documents/secret.env"
    nested_pw = "nested_json_password_777"
    deep_secret = "DEEP_CONTAINER_SECRET_MUST_NOT_LEAK"
    nested_depth_secret = "DEEP_NESTED_JSON_SECRET_MUST_NOT_LEAK"

    nested_payload = json.dumps(
        {
            "outer": {
                "api_key": "outer_api_key_secret_123",
                "nested_json": json.dumps({"password": nested_pw, "dir": planted_users}),
                "path": planted_home,
            }
        }
    )
    plain_body = (
        f"[note] Check token {planted_ghp} and {planted_ant}\n"
        f"URL https://operator:{planted_url_pw}@example.com/path\n"
        f"Local file {planted_home}"
    )
    # F4: >64-level JSON container and >4-level nested JSON string fail closed as withheld.
    over_deep_container = '{"a":' * 66 + json.dumps({"token": deep_secret}) + "}" * 66
    over_deep_nested = json.dumps({"token": nested_depth_secret})
    for _ in range(5):
        over_deep_nested = json.dumps({"wrap": over_deep_nested})
    # F2: syntactically valid JSON containing a float outside Python's finite range (1e309 -> inf).
    non_finite_json = '{"overflow": 1e309, "note": "must not 500"}'
    # F4: syntactically valid JSON containing an integer exceeding Python's 4300-digit conversion limit
    # alongside a secret must fail closed as body_redaction_failed (never fall back to plain-text filters).
    over_digit_secret = "OVER_DIGIT_INT_SECRET_MUST_NOT_LEAK"
    over_digit_int_json = '{"huge": ' + ("9" * 4400) + ', "nested": "{\\"password\\": \\"' + over_digit_secret + '\\"}"}'

    conv_id, _ = _seed_conversation(
        root,
        turns=[
            ("request", "root", "codex", plain_body, 1),
            ("reply", "codex", "root", nested_payload, 1),
            ("reply", "claude", "root", over_deep_container, 2),
            ("reply", "codex", "root", over_deep_nested, 2),
            ("reply", "claude", "root", non_finite_json, 2),
            ("reply", "codex", "root", over_digit_int_json, 2),
        ],
        suffix="redaction",
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    # Both top-level and per-turn labels pass through redact_text then sanitize_text server-side.
    poisoned_ok_label = f"ok {planted_ant} {planted_home}"
    poisoned_corrupt_label = f"missing or corrupt {planted_ant} {planted_home}"
    monkeypatch.setitem(runtime_router._ACP_TRANSCRIPT_STATE_LABELS, "ok", poisoned_ok_label)
    monkeypatch.setitem(runtime_router._ACP_TRANSCRIPT_STATE_LABELS, "missing_or_corrupt", poisoned_corrupt_label)

    resp = _loopback_client().get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()

    for forbidden in (
        planted_ghp,
        planted_ant,
        planted_url_pw,
        planted_home,
        planted_users,
        "outer_api_key_secret_123",
        nested_pw,
        deep_secret,
        nested_depth_secret,
        "must not 500",
        over_digit_secret,
    ):
        assert forbidden not in resp.text, forbidden

    assert data["messages"][0]["state"] == "ok"
    assert "[REDACTED_SECRET]" in data["messages"][0]["body"]
    assert "[redacted-path]" in data["messages"][0]["body"]
    assert data["messages"][1]["state"] == "ok"
    assert "[REDACTED_SECRET]" in data["messages"][1]["body"]
    assert "[redacted-path]" in data["messages"][1]["body"]
    assert data["messages"][2]["state"] == "withheld"
    assert data["messages"][2]["reason"] == "redaction_depth_exceeded"
    assert data["messages"][2]["body"] is None
    assert data["messages"][3]["state"] == "withheld"
    assert data["messages"][3]["reason"] == "redaction_depth_exceeded"
    assert data["messages"][3]["body"] is None
    assert data["messages"][4]["state"] == "missing_or_corrupt"
    assert data["messages"][4]["reason"] == "body_redaction_failed"
    assert data["messages"][4]["body"] is None
    assert data["messages"][5]["state"] == "missing_or_corrupt"
    assert data["messages"][5]["reason"] == "body_redaction_failed"
    assert data["messages"][5]["body"] is None
    assert "[REDACTED_SECRET]" in data["label"]
    assert "[redacted-path]" in data["label"]
    assert "[REDACTED_SECRET]" in data["messages"][0]["label"]
    assert "[redacted-path]" in data["messages"][0]["label"]

    # C2: route contract and panel explicitly state redaction covers secrets and paths only (no host/IP regex in router).
    route_contract = contract_for_route("/api/runtime/acp/conversations/{conversation_id}/transcript", "http")
    assert route_contract is not None
    assert route_contract.match == "exact"
    assert route_contract.mutates is False
    assert "secrets and filesystem paths only" in route_contract.purpose.lower()
    assert "other encodings" in route_contract.purpose.lower()
    assert "out of scope" in route_contract.purpose.lower()
    page_contract = contract_for_page("fleet.html")
    assert page_contract is not None
    assert "secrets-and-paths" in page_contract.purpose.lower()
    fleet_html = (machinery_path("api/dashboards") / "fleet.html").read_text(encoding="utf-8")
    assert "secrets and paths only" in fleet_html.lower()


def test_off_allowlist_producers_and_kinds_withheld_with_typed_reason(tmp_path: Path, monkeypatch) -> None:
    """Defect class: off-allowlist producers (spelling/case/inline-only) or kind variants leak instead of being withheld."""
    root = tmp_path / "plane"
    conv_id, _ = _seed_conversation(
        root,
        participants=("codex", "claude"),
        turns=[("request", "root", "codex", "allowed acpx-discuss body", 1)],
        suffix="allowlist",
    )

    # Allowed authority ACP message producer ("authority-message").
    _insert_custom_turn(
        root, conv_id, sequence=10, message_id="msg-authority-ok",
        kind="reply", sender="claude", recipient="root",
        body="allowed authority ACP body", producer="authority-message",
    )

    # Off-allowlist producer classes (spelling, case, whitespace, unknown seat, non-participant seat,
    # extra suffix, authority-import, authority-delivery-result, adapter, empty string, and inline-only).
    bad_producers = [
        ("msg-prod-upper", "ACPX-DISCUSS:codex", "LEAK_UPPER_PREFIX"),
        ("msg-prod-seat-case", "acpx-discuss:CODEX", "LEAK_UPPER_SEAT"),
        ("msg-prod-space", "acpx-discuss:codex ", "LEAK_TRAILING_SPACE"),
        ("msg-prod-unknown", "acpx-discuss:evil", "LEAK_UNKNOWN_SEAT"),
        ("msg-prod-other-seat", "acpx-discuss:agy", "LEAK_NON_PARTICIPANT_SEAT"),
        ("msg-prod-bare", "acpx-discuss:", "LEAK_EMPTY_SEAT"),
        ("msg-prod-extra", "acpx-discuss:codex:extra", "LEAK_EXTRA_SUFFIX"),
        ("msg-prod-import", "authority-import", "LEAK_AUTHORITY_IMPORT"),
        ("msg-prod-auth-case", "Authority-Message", "LEAK_AUTH_CASE"),
        ("msg-prod-delivery", "authority-delivery-result", "LEAK_DELIVERY_RESULT"),
        ("msg-prod-adapter", "adapter:codex", "LEAK_ADAPTER"),
        ("msg-prod-empty", "", "LEAK_EMPTY_PRODUCER"),
        ("msg-prod-inline-only", None, "LEAK_INLINE_WITHOUT_PRODUCER"),
    ]
    seq = 20
    for msg_id, producer, secret in bad_producers:
        _insert_custom_turn(
            root, conv_id, sequence=seq, message_id=msg_id,
            kind="reply", sender="codex", recipient="root",
            body=secret, producer=producer,
        )
        seq += 1

    # Off-allowlist kind variants (case, whitespace, non-ACP kinds).
    bad_kinds = [
        ("msg-kind-upper", "REPLY", "LEAK_KIND_UPPER"),
        ("msg-kind-title", "Request", "LEAK_KIND_TITLE"),
        ("msg-kind-space", " reply ", "LEAK_KIND_SPACE"),
        ("msg-kind-note", "note", "LEAK_KIND_NOTE"),
        ("msg-kind-retro", "retro", "LEAK_KIND_RETRO"),
        ("msg-kind-handoff", "handoff", "LEAK_KIND_HANDOFF"),
    ]
    for msg_id, kind, secret in bad_kinds:
        _insert_custom_turn(
            root, conv_id, sequence=seq, message_id=msg_id,
            kind=kind, sender="codex", recipient="root",
            body=secret, producer="acpx-discuss:codex",
        )
        seq += 1

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    resp = _loopback_client().get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()

    assert data["state"] == "withheld"
    assert data["label"] == "withheld"
    assert data["reason"] == "producer_not_allowlisted"
    expected_total = 2 + len(bad_producers) + len(bad_kinds)
    assert len(data["messages"]) == expected_total

    assert data["messages"][0]["state"] == "ok"
    assert data["messages"][0]["body"] == "allowed acpx-discuss body"
    assert data["messages"][1]["state"] == "ok"
    assert data["messages"][1]["body"] == "allowed authority ACP body"

    for offset, (_, _, secret) in enumerate(bad_producers, start=2):
        turn = data["messages"][offset]
        assert turn["state"] == "withheld", (offset, turn)
        assert turn["label"] == "withheld"
        assert turn["reason"] == "producer_not_allowlisted"
        assert turn["body"] is None
        assert secret not in resp.text

    kind_start = 2 + len(bad_producers)
    for offset, (_, bad_kind, secret) in enumerate(bad_kinds, start=kind_start):
        turn = data["messages"][offset]
        assert turn["state"] == "withheld", (offset, bad_kind, turn)
        assert turn["label"] == "withheld"
        assert turn["reason"] == "kind_not_allowlisted"
        assert turn["kind"] is None
        assert turn["body"] is None
        assert secret not in resp.text


def test_no_turn_lost_or_reordered_across_mixed_states(tmp_path: Path, monkeypatch) -> None:
    """Defect class: turns are silently dropped or reordered when ok, expired, withheld, corrupt, and oversized turns mix."""
    root = tmp_path / "plane"
    oversized = "X" * (runtime_router._ACP_TRANSCRIPT_MAX_BODY_BYTES + 64)
    conv_id, _ = _seed_conversation(
        root,
        participants=("codex", "claude", "agy"),
        turns=[("request", "root", "claude", "turn-1-ok", 1)],
        suffix="ordering",
    )
    # Insert out-of-order by SQLite insertion order, with explicit monotonic event sequences 20..70.
    _insert_custom_turn(
        root, conv_id, sequence=70, message_id="msg-seq-70",
        kind="synthesis", sender="codex", recipient="root",
        body="turn-6-synthesis-ok", producer="acpx-discuss:codex",
        round_no=None, event_type="SYNTHESIS_TERMINAL",
    )
    _insert_custom_turn(
        root, conv_id, sequence=30, message_id="msg-seq-30",
        kind="reply", sender="agy", recipient="root",
        body="turn-3-withheld", producer="adapter:agy", round_no=1,
    )
    _insert_custom_turn(
        root, conv_id, sequence=20, message_id="msg-seq-20",
        kind="reply", sender="claude", recipient="root",
        body="turn-2-expired", producer="acpx-discuss:claude",
        round_no=1, retention_class="expired:acpx-discussion",
    )
    _insert_custom_turn(
        root, conv_id, sequence=50, message_id="msg-seq-50",
        kind="reply", sender="codex", recipient="root",
        body=oversized, producer="acpx-discuss:codex", round_no=2,
    )
    _insert_custom_turn(
        root, conv_id, sequence=40, message_id="msg-seq-40",
        kind="reply", sender="claude", recipient="root",
        body="turn-4-corrupt-blob", producer="acpx-discuss:claude", round_no=2,
    )
    # Remove turn-4 blob without marking expired -> missing_or_corrupt.
    corrupt_sha = hashlib.sha256(b"turn-4-corrupt-blob").hexdigest()
    (root / "blobs" / "sha256" / corrupt_sha[:2] / corrupt_sha).unlink()

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    resp = _loopback_client().get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
    assert resp.status_code == 200
    messages = resp.json()["messages"]

    assert [m["ordinal"] for m in messages] == [1, 2, 3, 4, 5, 6]
    assert [m["state"] for m in messages] == [
        "ok",
        "body_expired",
        "withheld",
        "missing_or_corrupt",
        "oversized_or_partial",
        "ok",
    ]
    assert [m["sender"] for m in messages] == ["root", "claude", "agy", "claude", "codex", "codex"]
    assert messages[0]["body"] == "turn-1-ok"
    assert messages[5]["body"] == "turn-6-synthesis-ok"
    assert "round" not in messages[5]


def test_get_transcript_never_writes_store_bytes_unchanged(tmp_path: Path, monkeypatch) -> None:
    """Defect class: GET on the transcript route mutates SQLite, WAL/SHM sidecars, or CAS blob files."""
    def _snapshot(directory: Path) -> dict[str, bytes]:
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }

    # 1. Rollback-journal (DELETE) mode: every file and byte under plane_root is bit-for-bit unchanged (no sidecars).
    root = tmp_path / "plane-delete"
    conv_id, _ = _seed_conversation(
        root,
        turns=[
            ("request", "root", "codex", "immutable request", 1),
            ("reply", "codex", "root", "immutable reply", 1),
        ],
        suffix="readonly",
    )
    _insert_custom_turn(
        root, conv_id, sequence=20, message_id="msg-ro-expired",
        kind="reply", sender="claude", recipient="root",
        body="expired body", producer="acpx-discuss:claude",
        retention_class="expired:acpx-discussion",
    )
    conn = sqlite3.connect(root / "comms.sqlite3")
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    before = _snapshot(root)
    local = _loopback_client()
    assert local.get(f"/api/runtime/acp/conversations/{conv_id}/transcript").status_code == 200
    assert local.get("/api/runtime/acp/conversations/nonexistent-conv/transcript").status_code == 404
    assert local.get("/api/fleet/acp/conversations").status_code == 200
    assert _snapshot(root) == before

    # 2. WAL mode: comms.sqlite3 and all CAS blobs remain byte-for-byte unchanged and zero WAL frames are written.
    wal_root = tmp_path / "plane-wal"
    wal_conv_id, _ = _seed_conversation(
        wal_root,
        turns=[("reply", "codex", "root", "wal immutable reply", 1)],
        suffix="readonly-wal",
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(wal_root))
    wal_before = _snapshot(wal_root)
    assert local.get(f"/api/runtime/acp/conversations/{wal_conv_id}/transcript").status_code == 200
    wal_after = _snapshot(wal_root)
    for rel_path, expected_bytes in wal_before.items():
        assert wal_after[rel_path] == expected_bytes, rel_path
    assert wal_after.get("comms.sqlite3-wal", b"") == b""


def test_each_e3_cap_trips_with_typed_state(tmp_path: Path, monkeypatch) -> None:
    """Defect class: any E3 cap (raw bytes, serialized response, SQL row count, busy timeout, concurrency) fails open or untyped."""
    local = _loopback_client()

    # 1. Raw body byte cap AND post-redaction body cap -> oversized_or_partial (body_too_large).
    root_raw = tmp_path / "plane-raw"
    conv_raw, _ = _seed_conversation(
        root_raw,
        turns=[
            ("request", "root", "codex", "small ok", 1),
            ("reply", "codex", "root", "R" * (runtime_router._ACP_TRANSCRIPT_MAX_BODY_BYTES + 1), 1),
        ],
        suffix="raw-cap",
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_raw))
    raw_resp = local.get(f"/api/runtime/acp/conversations/{conv_raw}/transcript")
    assert raw_resp.status_code == 200
    assert raw_resp.json()["state"] == "oversized_or_partial"
    assert raw_resp.json()["messages"][1]["reason"] == "body_too_large"
    assert raw_resp.json()["messages"][1]["body"] is None

    # 1b. Post-redaction expansion past body cap (redaction runs BEFORE cap).
    root_expand = tmp_path / "plane-expand"
    monkeypatch.setattr(runtime_router, "_ACP_TRANSCRIPT_MAX_BODY_BYTES", 24)
    conv_expand, _ = _seed_conversation(
        root_expand,
        turns=[("reply", "codex", "root", "token=abc123", 1)],  # 12 bytes raw -> 23 bytes redacted; + "/tmp/x" -> >24 bytes
        suffix="expand-cap",
    )
    _insert_custom_turn(
        root_expand, conv_expand, sequence=10, message_id="msg-expand-over",
        kind="reply", sender="codex", recipient="root",
        body="a /tmp/x /tmp/y", producer="acpx-discuss:codex",
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_expand))
    expand_resp = local.get(f"/api/runtime/acp/conversations/{conv_expand}/transcript")
    assert expand_resp.status_code == 200
    assert expand_resp.json()["messages"][0]["state"] == "ok"
    assert expand_resp.json()["messages"][1]["state"] == "oversized_or_partial"
    assert expand_resp.json()["messages"][1]["reason"] == "body_too_large"
    assert expand_resp.json()["messages"][1]["body"] is None
    monkeypatch.undo()

    # 2. Whole serialized response cap -> oversized_or_partial (response_budget_exceeded).
    root_resp = tmp_path / "plane-response-cap"
    chunk = "M" * (runtime_router._ACP_TRANSCRIPT_MAX_BODY_BYTES - 16)
    conv_resp, _ = _seed_conversation(
        root_resp,
        turns=[
            ("request", "root", "codex", chunk, 1),
            ("reply", "codex", "root", chunk, 1),
        ],
        suffix="resp-cap",
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_resp))
    resp_cap = local.get(f"/api/runtime/acp/conversations/{conv_resp}/transcript")
    assert resp_cap.status_code == 200
    assert len(resp_cap.content) <= runtime_router._ACP_TRANSCRIPT_MAX_RESPONSE_BYTES
    assert resp_cap.json()["state"] == "oversized_or_partial"
    assert resp_cap.json()["reason"] == "response_budget_exceeded"
    assert [m["state"] for m in resp_cap.json()["messages"]] == ["ok", "oversized_or_partial"]

    # 3. Row count checked in SQL before rows are materialized (>32 turns -> truncated=True, 32 rows).
    root_rows = tmp_path / "plane-rows"
    many_turns = [("reply", "codex", "root", f"row-{i}", 1) for i in range(runtime_router._ACP_TRANSCRIPT_MAX_MESSAGES + 3)]
    conv_rows, _ = _seed_conversation(root_rows, turns=many_turns, suffix="row-cap")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_rows))
    executed_sql: list[str] = []
    real_open_store = runtime_router._open_acp_transcript_store

    class _RecordingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql: str, params=()):
            executed_sql.append(sql)
            return self._inner.execute(sql, params)

        def rollback(self) -> None:
            self._inner.rollback()

        def close(self) -> None:
            self._inner.close()

    def _recording_open(ctx):
        plane_root, conn = real_open_store(ctx)
        return plane_root, _RecordingConn(conn)

    monkeypatch.setattr(runtime_router, "_open_acp_transcript_store", _recording_open)
    rows_resp = local.get(f"/api/runtime/acp/conversations/{conv_rows}/transcript")
    monkeypatch.setattr(runtime_router, "_open_acp_transcript_store", real_open_store)
    assert rows_resp.status_code == 200
    rows_data = rows_resp.json()
    assert rows_data["state"] == "oversized_or_partial"
    assert rows_data["reason"] == "message_limit_exceeded"
    assert rows_data["truncated"] is True
    assert len(rows_data["messages"]) == runtime_router._ACP_TRANSCRIPT_MAX_MESSAGES
    # Confirm SQL checked message count before joining comms_messages inside one BEGIN DEFERRED snapshot.
    begin_queries = [q for q in executed_sql if q.strip() == "BEGIN DEFERRED"]
    count_queries = [q for q in executed_sql if "COUNT(*) AS message_count" in q]
    join_queries = [q for q in executed_sql if "LEFT JOIN comms_messages" in q]
    assert begin_queries and count_queries and join_queries
    assert executed_sql.index(begin_queries[0]) < executed_sql.index(count_queries[0]) < executed_sql.index(join_queries[0])
    assert "LIMIT ?" in join_queries[0].split("LEFT JOIN comms_messages")[0]

    # 3b. F3/F6 snapshot consistency: insert a 33rd turn at sequence=0 (inside the capped 32-row window,
    # before existing message sequences 2..33 while leaving CONVERSATION_CREATED at sequence=1 intact)
    # right after COUNT(*) sees 32. Without one read transaction, the capped join would observe
    # sequence=0 and displace snap-31 while truncated stayed False.
    root_snap = tmp_path / "plane-snap"
    exact_32 = [("reply", "codex", "root", f"snap-{i}", 1) for i in range(runtime_router._ACP_TRANSCRIPT_MAX_MESSAGES)]
    conv_snap, _ = _seed_conversation(root_snap, turns=exact_32, suffix="snap-cap")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_snap))
    injected = {"done": False}

    class _InterveningCommitConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql: str, params=()):
            cursor = self._inner.execute(sql, params)
            if "COUNT(*) AS message_count" in sql and not injected["done"]:
                injected["done"] = True
                _insert_custom_turn(
                    root_snap, conv_snap, sequence=0, message_id="msg-snap-seq0",
                    kind="reply", sender="codex", recipient="root",
                    body="snap-inserted-seq-0", producer="acpx-discuss:codex",
                )
            return cursor

        def rollback(self) -> None:
            self._inner.rollback()

        def close(self) -> None:
            self._inner.close()

    monkeypatch.setattr(
        runtime_router,
        "_open_acp_transcript_store",
        lambda ctx: (lambda pair: (pair[0], _InterveningCommitConn(pair[1])))(real_open_store(ctx)),
    )
    snap_resp = local.get(f"/api/runtime/acp/conversations/{conv_snap}/transcript")
    monkeypatch.setattr(runtime_router, "_open_acp_transcript_store", real_open_store)
    assert injected["done"] is True
    assert snap_resp.status_code == 200
    snap_data = snap_resp.json()
    assert snap_data["truncated"] is False
    assert len(snap_data["messages"]) == runtime_router._ACP_TRANSCRIPT_MAX_MESSAGES
    assert snap_data["messages"][0]["body"] == "snap-0"
    assert snap_data["messages"][-1]["body"] == f"snap-{runtime_router._ACP_TRANSCRIPT_MAX_MESSAGES - 1}"
    assert "snap-inserted-seq-0" not in snap_resp.text
    after_snap = local.get(f"/api/runtime/acp/conversations/{conv_snap}/transcript")
    assert after_snap.status_code == 200
    after_data = after_snap.json()
    assert after_data["truncated"] is True
    assert after_data["reason"] == "message_limit_exceeded"
    assert after_data["messages"][0]["body"] == "snap-inserted-seq-0"

    # 4. SQLite busy timeout (configured via open_readonly timeout alone -> 500 ms default, 503 store_busy on lock).
    root_busy = tmp_path / "plane-busy"
    conv_busy, _ = _seed_conversation(
        root_busy,
        turns=[("reply", "codex", "root", "locked body", 1)],
        suffix="busy",
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_busy))
    _, default_conn = real_open_store(app.state.ctx)
    try:
        assert default_conn.execute("PRAGMA busy_timeout").fetchone()[0] == 500
    finally:
        default_conn.close()
    monkeypatch.setattr(runtime_router, "_ACP_TRANSCRIPT_SQLITE_TIMEOUT_S", 0.05)
    locker = sqlite3.connect(root_busy / "comms.sqlite3", isolation_level=None)
    try:
        locker.execute("PRAGMA journal_mode=DELETE")
        locker.execute("BEGIN EXCLUSIVE")
        busy_resp = local.get(f"/api/runtime/acp/conversations/{conv_busy}/transcript")
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert busy_resp.status_code == 503
    assert busy_resp.headers["cache-control"] == "no-store"
    assert busy_resp.json()["state"] == "backend_unavailable"
    assert busy_resp.json()["reason"] == "store_busy"
    assert "locked body" not in busy_resp.text

    # 5. Concurrency semaphore -> HTTP 503 backend_unavailable (concurrency_limit_exceeded) with Retry-After, then releases.
    monkeypatch.setattr(runtime_router, "_ACP_TRANSCRIPT_MAX_CONCURRENCY", 1)
    entered = threading.Event()
    release = threading.Event()
    real_reader = runtime_router.get_acp_conversation_transcript

    def _blocking_reader(conversation_id: str, *, ctx):
        entered.set()
        release.wait(timeout=5.0)
        return real_reader(conversation_id, ctx=ctx)

    monkeypatch.setattr(runtime_router, "get_acp_conversation_transcript", _blocking_reader)
    url_busy = f"/api/runtime/acp/conversations/{conv_busy}/transcript"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lambda: _loopback_client().get(url_busy))
        assert entered.wait(timeout=5.0)
        second_resp = _loopback_client().get(url_busy)
        release.set()
        first_resp = first.result(timeout=5.0)

    assert first_resp.status_code == 200
    assert second_resp.status_code == 503
    assert second_resp.headers["cache-control"] == "no-store"
    assert second_resp.headers["retry-after"] == "1"
    assert second_resp.json()["state"] == "backend_unavailable"
    assert second_resp.json()["reason"] == "concurrency_limit_exceeded"
    assert "locked body" not in second_resp.text
    # Slot released cleanly after in-flight request completes.
    monkeypatch.setattr(runtime_router, "get_acp_conversation_transcript", real_reader)
    after_resp = local.get(url_busy)
    assert after_resp.status_code == 200


def test_discussions_panel_text_only_on_demand_and_stale_response_guard() -> None:
    """Defect class: Discussions panel polls bodies on load, injects HTML via innerHTML, or lets a late response overwrite a newer selection."""
    dashboards = machinery_path("api/dashboards")
    fleet_html = (dashboards / "fleet.html").read_text(encoding="utf-8")

    # Static check: Discussions code never touches innerHTML/outerHTML/insertAdjacentHTML.
    discussions_start = fleet_html.index("const DISCUSSIONS_LIST_URL")
    discussions_js = fleet_html[discussions_start : fleet_html.index("runner.start();")]
    for forbidden_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert forbidden_sink not in discussions_js

    node_script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const M = require(process.argv[1]);
const dashboardsDir = path.dirname(process.argv[1]);

const html = fs.readFileSync(path.join(dashboardsDir, 'fleet.html'), 'utf8');
const marker = '<script src="/machinery.js"></script>';
const after = html.slice(html.indexOf(marker) + marker.length);
const inline = after.slice(after.indexOf('<script>') + '<script>'.length, after.indexOf('</script>'));
const fleetBody = inline.slice(0, inline.indexOf('\nrunner.start();'));

class FakeNode {
  constructor(tag, id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.className = '';
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.open = false;
    this.hidden = false;
    this.type = '';
    this._text = '';
    this._innerHTML = '';
    this.parentNode = null;
  }
  get textContent() {
    return this._text + this.children.map(c => c.textContent).join('');
  }
  set textContent(v) {
    this._text = String(v);
    this.children = [];
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) {
    if (this.id.startsWith('discussions-')) {
      throw new Error(`innerHTML must never be written on ${this.id}`);
    }
    this._innerHTML = String(v);
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
  }
  replaceChildren(...nodes) {
    this.children = [];
    this._text = '';
    for (const n of nodes) this.appendChild(n);
  }
  get firstChild() { return this.children[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
  closest(selector) {
    if (selector === 'button[data-discussion-id]' && this.tagName === 'BUTTON' && this.dataset.discussionId) return this;
    return this.parentNode ? this.parentNode.closest(selector) : null;
  }
  findAll(pred) {
    const out = [];
    if (pred(this)) out.push(this);
    for (const child of this.children) out.push(...child.findAll(pred));
    return out;
  }
}

const elements = new Map();
const getEl = (id, tag = 'div') => {
  if (!elements.has(id)) elements.set(id, new FakeNode(tag, id));
  return elements.get(id);
};
getEl('discussions-disclosure', 'details');
getEl('discussions-refresh', 'button');
getEl('discussions-status', 'p');
getEl('discussions-list', 'div');
getEl('discussions-transcript', 'div');

const storageWrites = [];
globalThis.localStorage = {
  getItem() { return null; },
  setItem(k, v) { storageWrites.push([k, v]); },
  removeItem() {},
};
globalThis.sessionStorage = {
  getItem() { return null; },
  setItem(k, v) { storageWrites.push([k, v]); },
};
globalThis.location = {origin: 'http://127.0.0.1:8765', pathname: '/fleet', search: '', hash: '', assign() { storageWrites.push(['location', 'assign']); }};
globalThis.document = {
  documentElement: {dataset: {}},
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener() {},
  createElement(tag) { return new FakeNode(tag); },
  getElementById(id) { return getEl(id); },
};

const fetchCalls = [];
let releaseSlowA;
const slowAGate = new Promise(resolve => { releaseSlowA = resolve; });

globalThis.fetch = async (url) => {
  const s = String(url);
  fetchCalls.push(s);
  if (s === '/api/fleet/acp/conversations') {
    return {
      ok: true,
      status: 200,
      headers: {get: () => null},
      text: async () => JSON.stringify({
        availability: 'available',
        conversations: [
          {conversation_id: 'conv-slow-A', participants: ['codex', 'claude'], current_state: 'COMPLETE', rounds_completed: 1, rounds_requested: 1, updated_at: '2026-11-26T05:00:00Z'},
          {conversation_id: 'conv-fast-B', participants: ['codex', 'agy'], current_state: 'PARTIAL_COMPLETE', rounds_completed: 1, rounds_requested: 2, updated_at: '2026-11-26T05:05:00Z'},
        ],
      }),
    };
  }
  if (s === '/api/runtime/acp/conversations/conv-slow-A/transcript') {
    await slowAGate;
    return {
      ok: true,
      status: 200,
      headers: {get: () => 'no-store'},
      text: async () => JSON.stringify({
        conversation_id: 'conv-slow-A',
        state: 'ok',
        label: 'ok',
        reason: null,
        truncated: false,
        messages: [{ordinal: 1, state: 'ok', label: 'ok', reason: null, kind: 'reply', sender: 'codex', recipient: 'root', created_at: '2026-11-26T05:00:00Z', round: 1, body: 'STALE_BODY_FROM_A'}],
      }),
    };
  }
  if (s === '/api/runtime/acp/conversations/conv-fast-B/transcript') {
    return {
      ok: true,
      status: 200,
      headers: {get: () => 'no-store'},
      text: async () => JSON.stringify({
        conversation_id: 'conv-fast-B',
        state: 'withheld',
        label: 'withheld',
        reason: 'producer_not_allowlisted',
        truncated: false,
        messages: [
          {ordinal: 1, state: 'ok', label: 'ok', reason: null, kind: 'request', sender: 'root', recipient: 'agy', created_at: '2026-11-26T05:05:00Z', round: 1, body: '<img src=x onerror=alert(1)>\nline two of B'},
          {ordinal: 2, state: 'body_expired', label: 'body expired', reason: 'body_expired', kind: 'reply', sender: 'agy', recipient: 'root', created_at: '2026-11-26T05:05:01Z', round: 1, body: null},
          {ordinal: 3, state: 'withheld', label: 'withheld', reason: 'producer_not_allowlisted', kind: 'reply', sender: 'codex', recipient: 'root', created_at: '2026-11-26T05:05:02Z', round: 1, body: null},
        ],
      }),
    };
  }
  return {
    ok: true,
    status: 200,
    headers: {get: () => null},
    text: async () => JSON.stringify({
      tasks: [], deliveries: {queued: 0}, jobs: {queued: 0}, store: {reachable: true},
      availability: 'available', driver_roster: 'available', external_roster: 'available', seats: [], total: 0,
    }),
  };
};

(async () => {
  const api = new Function('Machinery', fleetBody + '\nreturn {runner, loadDiscussionsList, openDiscussion};')(M);

  // 1. Initial load and polling make ZERO discussion or transcript requests.
  await api.runner.refreshAll();
  assert.equal(fetchCalls.some(u => u.includes('/acp/conversations')), false, `unexpected ACP fetch on initial load: ${fetchCalls}`);

  // 2. Opening the disclosure fetches the conversation list (still zero transcript requests).
  const disclosure = getEl('discussions-disclosure');
  disclosure.open = true;
  disclosure.dispatch('toggle');
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetchCalls.filter(u => u === '/api/fleet/acp/conversations').length, 1);
  assert.equal(fetchCalls.some(u => u.includes('/transcript')), false);

  const buttons = getEl('discussions-list').findAll(n => n.tagName === 'BUTTON' && n.dataset.discussionId);
  assert.deepEqual(buttons.map(b => b.dataset.discussionId), ['conv-slow-A', 'conv-fast-B']);

  // 3. Stale-response guard: open slow A, then open fast B; B renders, then slow A resolves late and MUST NOT overwrite B.
  const pendingA = api.openDiscussion('conv-slow-A');
  const pendingB = api.openDiscussion('conv-fast-B');
  await pendingB;
  const transcriptBox = getEl('discussions-transcript');
  assert.match(transcriptBox.textContent, /Transcript: conv-fast-B/);
  assert.match(transcriptBox.textContent, /<img src=x onerror=alert\(1\)>\nline two of B/);
  assert.match(transcriptBox.textContent, /Body expired/);
  assert.match(transcriptBox.textContent, /Withheld \(producer not allowlisted\)/);
  assert.equal(transcriptBox.findAll(n => n.tagName === 'IMG' || n.tagName === 'SCRIPT').length, 0);

  releaseSlowA();
  await pendingA;
  assert.match(transcriptBox.textContent, /Transcript: conv-fast-B/);
  assert.doesNotMatch(transcriptBox.textContent, /STALE_BODY_FROM_A/);
  assert.doesNotMatch(transcriptBox.textContent, /Transcript: conv-slow-A/);

  // 4. Zero browser persistence of body text.
  assert.deepEqual(storageWrites, []);
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
    ran = subprocess.run(
        ["node", "-e", node_script, str(dashboards / "machinery.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert ran.returncode == 0, ran.stderr


@pytest.mark.parametrize(
    ("case_id", "body", "expected_state", "expected_reason", "forbidden", "expected_tokens"),
    [
        (
            "top-level-json-string-object",
            json.dumps(json.dumps({"password": "top_str_obj_secret_1", "dir": "/home/alice/top-str.key"})),
            "ok",
            None,
            ("top_str_obj_secret_1", "/home/alice/top-str.key"),
            ("[REDACTED_SECRET]", "[redacted-path]"),
        ),
        (
            "top-level-json-string-array",
            json.dumps(json.dumps([{"api_key": "top_str_arr_secret_2"}, "plain"])),
            "ok",
            None,
            ("top_str_arr_secret_2",),
            ("[REDACTED_SECRET]",),
        ),
        (
            "leaf-double-encoded-and-newline-sibling",
            json.dumps(
                {
                    "leaf": json.dumps(json.dumps({"password": "leaf_double_secret_3"})),
                    "sibling": '{"api_key": 123\n, "inner": "{\\"secret\\": \\"leaf_newline_secret_4\\"}"}',
                }
            ),
            "ok",
            None,
            ("leaf_double_secret_3", "leaf_newline_secret_4"),
            ("[REDACTED_SECRET]",),
        ),
        (
            "object-key-wrapped-json-and-path",
            json.dumps(
                {
                    json.dumps({"password": "key_wrapped_secret_5"}): "under_secret_key_val_6",
                    "/home/alice/key-path.txt": "harmless",
                }
            ),
            "ok",
            None,
            ("key_wrapped_secret_5", "under_secret_key_val_6", "/home/alice/key-path.txt"),
            ("[REDACTED_SECRET]", "[redacted-path]"),
        ),
        (
            "depth-exactly-at-cap-top-level-string",
            # 4 JSON layers: top-level str -> str -> str -> dict (at cap = 4 -> ok, redacted).
            json.dumps(json.dumps(json.dumps(json.dumps({"token": "depth_at_cap_secret_7"})))),
            "ok",
            None,
            ("depth_at_cap_secret_7",),
            ("[REDACTED_SECRET]",),
        ),
        (
            "depth-over-cap-top-level-string",
            # 5 JSON layers: top-level str -> str -> str -> str -> dict (over cap = 4 -> withheld).
            json.dumps(json.dumps(json.dumps(json.dumps(json.dumps({"token": "depth_over_top_secret_8"}))))),
            "withheld",
            "redaction_depth_exceeded",
            ("depth_over_top_secret_8",),
            (),
        ),
        (
            "depth-over-cap-leaf-json-string",
            # 5 JSON layers: dict -> leaf str -> leaf str -> leaf str -> leaf dict (over cap = 4 -> withheld).
            json.dumps({"wrap": json.dumps(json.dumps(json.dumps(json.dumps({"token": "depth_over_leaf_secret_9"}))))}),
            "withheld",
            "redaction_depth_exceeded",
            ("depth_over_leaf_secret_9",),
            (),
        ),
        (
            "top-level-string-over-digit-int",
            json.dumps('{"huge": ' + ("9" * 4400) + ', "password": "over_digit_wrapped_secret_10"}'),
            "missing_or_corrupt",
            "body_redaction_failed",
            ("over_digit_wrapped_secret_10",),
            (),
        ),
        (
            "top-level-string-lone-surrogate",
            r'"{\"bad\": \"\uD800\", \"password\": \"surrogate_wrapped_secret_11\"}"',
            "missing_or_corrupt",
            "body_redaction_failed",
            ("surrogate_wrapped_secret_11",),
            (),
        ),
        (
            "plain-prose-quoted-not-json",
            '"quoted prose" followed by password="plain_quoted_secret_12" and /home/alice/prose.txt',
            "ok",
            None,
            ("plain_quoted_secret_12", "/home/alice/prose.txt"),
            ("[REDACTED_SECRET]", "[redacted-path]"),
        ),
    ],
)
def test_wrapped_json_redaction_class_and_depth_bounds(
    tmp_path: Path,
    monkeypatch,
    case_id: str,
    body: str,
    expected_state: str,
    expected_reason: str | None,
    forbidden: tuple[str, ...],
    expected_tokens: tuple[str, ...],
) -> None:
    """Defect class (F7): JSON string wrappers (top-level, leaf, or object key) skip structured redaction or exceed depth cap."""
    root = tmp_path / f"plane-{case_id}"
    conv_id, _ = _seed_conversation(
        root,
        turns=[("reply", "codex", "root", body, 1)],
        suffix=case_id[:16],
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    resp = _loopback_client().get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    for secret in forbidden:
        assert secret not in resp.text, (case_id, secret)
    turn = resp.json()["messages"][0]
    assert turn["state"] == expected_state, (case_id, turn)
    assert turn["reason"] == expected_reason, (case_id, turn)
    if expected_state == "ok":
        assert isinstance(turn["body"], str)
        for token in expected_tokens:
            assert token in turn["body"], (case_id, token, turn["body"])
    else:
        assert turn["body"] is None, (case_id, turn)
