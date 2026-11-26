"""Tests for runtime monitor API endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import lu_machinery.api.config as api_config
import lu_machinery.api.fleet_router as fleet_router
import lu_machinery.api.runtime_router as runtime_router
from lu_machinery.agent_runtime.acpx_discuss import AcpxDiscussionController
from lu_machinery.api.main import app
from lu_machinery.control_plane.storage import Authority
from lu_machinery.fleet_comms.artifacts import ArtifactStore

client = TestClient(app, raise_server_exceptions=False)
_LISTENER_ORIGIN = f"http://{api_config.API_HOST}:{api_config.API_PORT}"
def _ctx_with_batch_state_dir(batch_state_dir: Path):
    """Return a MonitorContext pinned to a disposable ``batch_state`` root.

    ``runtime_router`` resolves its usage-log directory from
    ``ctx.roots.batch_state_dir / "api_usage"`` (no module-level ``Path``
    global to monkeypatch anymore — see #7324 step 6), so tests redirect it
    by swapping ``app.state.ctx`` for the duration of the test instead.
    """
    base = app.state.ctx
    return base.with_roots(batch_state_dir=Path(batch_state_dir))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_usage_file(path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _write_acp_db(root, conversations: list[tuple], events: list[tuple]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "comms.sqlite3")
    try:
        connection.executescript(
            """
            CREATE TABLE acp_conversations (
                conversation_id TEXT PRIMARY KEY,
                task_digest TEXT,
                correlation_digest TEXT,
                idempotency_digest TEXT,
                rounds_requested INTEGER,
                participants_json TEXT,
                created_at TEXT,
                deadline_at TEXT,
                token_budget INTEGER,
                content_budget_bytes INTEGER
            );
            CREATE TABLE acp_conversation_events (
                event_id TEXT,
                conversation_id TEXT,
                sequence INTEGER,
                event_type TEXT,
                state TEXT,
                sender TEXT,
                recipient TEXT,
                round INTEGER,
                outcome TEXT,
                duration_ms INTEGER,
                token_count INTEGER,
                leg_key_digest TEXT,
                message_id TEXT,
                metadata_json TEXT,
                created_at TEXT,
                UNIQUE(conversation_id, sequence)
            );
            CREATE TABLE comms_messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                in_reply_to TEXT,
                kind TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT,
                body_inline TEXT,
                body_artifact_id TEXT,
                content_sha256 TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO acp_conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            conversations,
        )
        connection.executemany(
            "INSERT INTO acp_conversation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            events,
        )
        connection.commit()
    finally:
        connection.close()


def test_usage_aggregates_by_agent(tmp_path, monkeypatch):
    usage_dir = tmp_path / "api_usage"
    today = datetime.now(UTC)
    monkeypatch.setattr(app.state, "ctx", _ctx_with_batch_state_dir(usage_dir.parent))

    _write_usage_file(
        usage_dir / f"usage_codex-dispatch_{today:%Y-%m-%d}.jsonl",
        [
            {
                "ts": _iso(today - timedelta(minutes=4)),
                "agent": "codex",
                "entrypoint": "dispatch",
                "model": "gpt-5.5",
                "duration_s": 10.5,
                "outcome": "ok",
            },
            {
                "ts": _iso(today - timedelta(minutes=3)),
                "agent": "codex",
                "entrypoint": "dispatch",
                "model": "gpt-5.5",
                "duration_s": 4.0,
                "outcome": "error",
            },
        ],
    )
    _write_usage_file(
        usage_dir / f"usage_gemini-bridge_{today:%Y-%m-%d}.jsonl",
        [
            {
                "ts": _iso(today - timedelta(minutes=2)),
                "agent": "gemini",
                "entrypoint": "bridge",
                "model": "gemini-3.1-pro-preview",
                "duration_s": 8.0,
                "outcome": "rate_limited",
            }
        ],
    )

    data = runtime_router.summarize_runtime_usage(days=7, ctx=app.state.ctx)
    assert data["records_total"] == 3
    assert data["by_agent"]["codex"]["total"] == 2
    assert data["by_agent"]["codex"]["ok"] == 1
    assert data["by_agent"]["codex"]["error"] == 1
    assert data["by_agent"]["codex"]["total_duration_s"] == 14.5
    assert data["by_entrypoint"]["bridge"]["rate_limited"] == 1


def test_acp_conversation_api_is_ordered_allowlisted_and_read_only(tmp_path, monkeypatch):
    root = tmp_path / "plane"
    now = datetime.now(UTC)
    conversation = (
        "conv-001",
        "task-digest-must-not-leak",
        "correlation-digest-must-not-leak",
        "idempotency-digest-must-not-leak",
        2,
        '["root", "codex", "grok", "poisoned"]',
        _iso(now - timedelta(minutes=5)),
        _iso(now + timedelta(minutes=5)),
        999,
        12345,
    )
    events = [
        (
            "event-1", "conv-001", 1, "created", "CREATED", "root", None, None,
            "queued", 0, 0, "leg-secret", "message-secret", '{"body":"must-not-leak"}',
            _iso(now - timedelta(minutes=5)),
        ),
        (
            "event-2", "conv-001", 2, "participant_message", "INITIAL_FANOUT", "root", "codex", 1,
            "running", 100, 12, "leg-secret", "message-secret", '{"prompt":"must-not-leak"}',
            _iso(now - timedelta(minutes=4)),
        ),
        (
            "event-3", "conv-001", 3, "participant_message", "INITIAL_COMPLETE", "root", "grok", 1,
            "succeeded", 200, 15, "leg-secret", "message-secret", '{"response":"must-not-leak"}',
            _iso(now - timedelta(minutes=3)),
        ),
        (
            "event-4", "conv-001", 4, "synthesis_complete", "COMPLETE", "root", None, 2,
            "succeeded", 300, 8, "leg-secret", "message-secret", '{"credential":"must-not-leak"}',
            _iso(now - timedelta(minutes=2)),
        ),
    ]
    _write_acp_db(root, [conversation], events)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    db_path = root / "comms.sqlite3"
    before = db_path.stat().st_mtime_ns

    collection = client.get("/api/runtime/acp/conversations")
    detail = client.get("/api/runtime/acp/conversations/conv-001")

    assert collection.status_code == 200
    assert collection.json()["availability"] == "available"
    assert collection.json()["conversations"] == [
        {
            "conversation_id": "conv-001",
            "current_state": "COMPLETE",
            "classification": "complete",
            "participants": ["codex", "grok"],
            "rounds_requested": 2,
            "rounds_completed": 0,
            "created_at": _iso(now - timedelta(minutes=5)),
            "deadline_at": _iso(now + timedelta(minutes=5)),
            "expired": False,
            "stale_or_unhealthy": False,
            "updated_at": _iso(now - timedelta(minutes=2)),
            "total_duration_ms": 600,
            "total_tokens": 35,
            "synthesis_state": "complete",
            "duplicate_suppressed": False,
            "termination_reason": None,
        }
    ]
    assert detail.status_code == 200
    timeline = detail.json()
    assert [event["sequence"] for event in timeline["events"]] == [1, 2, 3, 4]
    assert timeline["events"][1]["sender"] == "root"
    assert timeline["events"][1]["recipient"] == "codex"
    assert db_path.stat().st_mtime_ns == before
    assert not (root / "comms.sqlite3-journal").exists()
    assert not (root / "comms.sqlite3-wal").exists()
    for private_value in (
        "task_digest", "correlation_digest", "idempotency_digest",
        "token_budget", "content_budget_bytes", "metadata_json", "message_id",
        "leg_key_digest", "must-not-leak", "leg-secret", "message-secret",
    ):
        assert private_value not in detail.text


def test_acp_runtime_api_hides_persisted_message_content_and_references(
    tmp_path, monkeypatch
):
    root = tmp_path / "plane"
    controller = AcpxDiscussionController(root=root)
    try:
        conversation_id, replay = controller._reserve(
            task_digest="task-digest",
            correlation_digest="correlation-digest",
            idempotency_digest="idempotency-digest",
            rounds=1,
            deadline_at="2099-01-01T00:00:00Z",
        )
        assert replay is None
        message_id = controller._message(
            conversation_id,
            sender="codex",
            recipient="root",
            body="private ACP body must not leak",
            reply_to=None,
        )
        artifact_id = controller.conn.execute(
            "SELECT body_artifact_id FROM comms_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()[0]
    finally:
        controller.close()

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    response = client.get(f"/api/runtime/acp/conversations/{conversation_id}")

    assert response.status_code == 200
    for private_value in (
        "private ACP body must not leak",
        message_id,
        artifact_id,
    ):
        assert private_value not in response.text


def test_acp_transcript_is_loopback_only_ordered_allowlisted_and_no_store(tmp_path, monkeypatch):
    root = tmp_path / "plane"
    now = datetime.now(UTC)
    conversation = (
        "conv-transcript", "secret", "secret", "secret", 1, "[]", _iso(now), None, 1, 1,
    )
    events = [
        (
            "event-later", "conv-transcript", 2, "CALL_TERMINAL", "INITIAL_FANOUT", "codex", "root", 1,
            "ok", 1, 1, "secret", "message-later", "{}", _iso(now + timedelta(seconds=2)),
        ),
        (
            "event-early", "conv-transcript", 1, "CALL_RESERVED", "INITIAL_FANOUT", "root", "codex", 1,
            "queued", 1, 1, "secret", "message-early", "{}", _iso(now + timedelta(seconds=1)),
        ),
        (
            "event-synthesis", "conv-transcript", 3, "SYNTHESIS_TERMINAL", "COMPLETE", "codex", "root", None,
            "ok", 1, 1, "secret", "message-synthesis", "{}", _iso(now + timedelta(seconds=3)),
        ),
    ]
    _write_acp_db(root, [conversation], events)
    connection = sqlite3.connect(root / "comms.sqlite3")
    try:
        connection.execute(
            """CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL UNIQUE,
                bytes INTEGER NOT NULL,
                mime_type TEXT,
                logical_filename TEXT,
                producer TEXT NOT NULL,
                retention_class TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        artifacts_data = [
            ("artifact-secret-later", "second body"),
            ("artifact-secret-early", "first body"),
            ("artifact-secret-synth", "final synthesis"),
        ]
        digests: dict[str, str] = {}
        for art_id, body_text in artifacts_data:
            raw = body_text.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            digests[art_id] = digest
            blob_path = root / "blobs" / "sha256" / digest[:2] / digest
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(raw)
            connection.execute(
                """INSERT INTO artifacts(
                    artifact_id, sha256, bytes, mime_type, logical_filename, producer, retention_class, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (art_id, digest, len(raw), "text/plain; charset=utf-8", "msg.txt", "acpx-discuss:codex", "acpx-discussion", _iso(now)),
            )
        connection.executemany(
            """INSERT INTO comms_messages(
                message_id, conversation_id, kind, sender, recipient, body_inline,
                body_artifact_id, content_sha256, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "message-later", "conv-transcript", "reply", "codex", "root", "second body",
                    "artifact-secret-later", digests["artifact-secret-later"], '{"credential":"secret"}', _iso(now + timedelta(seconds=2)),
                ),
                (
                    "message-early", "conv-transcript", "request", "root", "codex", "first body",
                    "artifact-secret-early", digests["artifact-secret-early"], '{"command":"secret"}', _iso(now + timedelta(seconds=1)),
                ),
                (
                    "message-synthesis", "conv-transcript", "synthesis", "codex", "root", "final synthesis",
                    "artifact-secret-synth", digests["artifact-secret-synth"], '{}', _iso(now + timedelta(seconds=3)),
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    loopback_client = TestClient(
        app,
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
        base_url=_LISTENER_ORIGIN,
    )
    response = loopback_client.get("/api/runtime/acp/conversations/conv-transcript/transcript")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "conversation_id": "conv-transcript",
        "state": "ok",
        "label": "ok",
        "reason": None,
        "truncated": False,
        "messages": [
            {
                "ordinal": 1,
                "state": "ok",
                "label": "ok",
                "reason": None,
                "kind": "request",
                "sender": "root",
                "recipient": "codex",
                "created_at": _iso(now + timedelta(seconds=1)),
                "body": "first body",
                "round": 1,
            },
            {
                "ordinal": 2,
                "state": "ok",
                "label": "ok",
                "reason": None,
                "kind": "reply",
                "sender": "codex",
                "recipient": "root",
                "created_at": _iso(now + timedelta(seconds=2)),
                "body": "second body",
                "round": 1,
            },
            {
                "ordinal": 3,
                "state": "ok",
                "label": "ok",
                "reason": None,
                "kind": "synthesis",
                "sender": "codex",
                "recipient": "root",
                "created_at": _iso(now + timedelta(seconds=3)),
                "body": "final synthesis",
            },
        ],
    }
    for private_value in (
        "message-later",
        "message-early",
        "artifact-secret-later",
        "artifact-secret-early",
        "artifact-secret-synth",
        *digests.values(),
        "metadata_json",
        "secret",
    ):
        assert private_value not in response.text

    remote = TestClient(
        app,
        raise_server_exceptions=False,
        client=("203.0.113.10", 50000),
        base_url=_LISTENER_ORIGIN,
    )
    denied = remote.get("/api/runtime/acp/conversations/conv-transcript/transcript")
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    assert denied.json() == {"detail": "Forbidden", "reason": "non_loopback_peer"}

    for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        loopback = TestClient(
            app,
            raise_server_exceptions=False,
            client=(host, 50000),
            base_url=_LISTENER_ORIGIN,
        )
        assert loopback.get("/api/runtime/acp/conversations/conv-transcript/transcript").status_code == 200

    rebound = TestClient(
        app,
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
        base_url="http://monitor.example",
    )
    rebound_response = rebound.get("/api/runtime/acp/conversations/conv-transcript/transcript")
    assert rebound_response.status_code == 403
    assert rebound_response.json() == {"detail": "Forbidden", "reason": "invalid_listener"}


def _seed_transcript_db(root: Path) -> None:
    now = datetime.now(UTC)
    conversation = (
        "conv-guarded", "secret", "secret", "secret", 1, "[]", _iso(now), None, 1, 1,
    )
    events = [
        (
            "event-1", "conv-guarded", 1, "CALL_TERMINAL", "COMPLETE", "codex", "root", 1,
            "ok", 1, 1, "secret", "message-1", "{}", _iso(now),
        ),
    ]
    _write_acp_db(root, [conversation], events)
    connection = sqlite3.connect(root / "comms.sqlite3")
    try:
        connection.execute(
            """INSERT INTO comms_messages(
                message_id, conversation_id, kind, sender, recipient, body_inline, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("message-1", "conv-guarded", "reply", "codex", "root", "guarded transcript body", "{}", _iso(now)),
        )
        connection.commit()
    finally:
        connection.close()


def _raw_transcript_request(
    headers: list[tuple[bytes, bytes]],
    *,
    scheme: str = "http",
    peer: tuple[str, int] | None = ("127.0.0.1", 50000),
    server: tuple[str, int] | None = (api_config.API_HOST, api_config.API_PORT),
) -> Request:
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "scheme": scheme,
        "path": "/api/runtime/acp/conversations/conv-guarded/transcript",
        "query_string": b"",
        "headers": headers,
    }
    if server is not None:
        scope["server"] = server
    if peer is not None:
        scope["client"] = peer
    return Request(scope)


def test_acp_transcript_refuses_foreign_origin_and_omits_credentialed_cors(tmp_path, monkeypatch):
    """Defect class: cross-origin/cross-site browser reads or credentialed wildcard CORS expose ACP transcripts."""
    root = tmp_path / "plane"
    _seed_transcript_db(root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    local = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000), base_url=_LISTENER_ORIGIN)
    url = "/api/runtime/acp/conversations/conv-guarded/transcript"

    for bad_origin in (
        "https://evil.example",
        f"http://{api_config.API_HOST}:{api_config.API_PORT + 1}",
        f"http://localhost:{api_config.API_PORT}",
        f"https://{api_config.API_HOST}:{api_config.API_PORT}",
        f"http://{api_config.API_HOST}",
        f"{_LISTENER_ORIGIN}/",
        "null",
        "",
    ):
        resp = local.get(url, headers={"Origin": bad_origin})
        assert resp.status_code == 403, bad_origin
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == {"detail": "Forbidden", "reason": "invalid_origin"}
        assert "access-control-allow-credentials" not in resp.headers
        assert resp.headers.get("access-control-allow-origin") != bad_origin or bad_origin == ""
        assert "guarded transcript body" not in resp.text

    dup_origin = _raw_transcript_request(
        [
            (b"host", f"{api_config.API_HOST}:{api_config.API_PORT}".encode()),
            (b"origin", _LISTENER_ORIGIN.encode()),
            (b"origin", _LISTENER_ORIGIN.encode()),
        ]
    )
    assert runtime_router._acp_transcript_access_refusal(dup_origin) == "invalid_origin"

    for bad_fetch_site in ("cross-site", "same-site", ""):
        resp = local.get(url, headers={"Sec-Fetch-Site": bad_fetch_site})
        assert resp.status_code == 403, bad_fetch_site
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == {"detail": "Forbidden", "reason": "cross_site_request"}
        assert "access-control-allow-origin" not in resp.headers
        assert "access-control-allow-credentials" not in resp.headers
        assert "guarded transcript body" not in resp.text

    dup_fetch_site = _raw_transcript_request(
        [
            (b"host", f"{api_config.API_HOST}:{api_config.API_PORT}".encode()),
            (b"sec-fetch-site", b"same-origin"),
            (b"sec-fetch-site", b"same-origin"),
        ]
    )
    assert runtime_router._acp_transcript_access_refusal(dup_fetch_site) == "cross_site_request"

    # Other Monitor routes keep non-credentialed CORS (no Access-Control-Allow-Credentials).
    for other_path in ("/api/runtime/acp/conversations", "/api/runtime/acp/conversations/transcript"):
        other = local.get(other_path, headers={"Origin": "https://viewer.example"})
        assert other.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in other.headers


def _run_raw_asgi_get(path: str, *, origin: str) -> tuple[int, dict[str, str], str]:
    """Send an unnormalized HTTP GET directly through the ASGI app."""
    status_code = 0
    headers_out: dict[str, str] = {}
    body_chunks: list[bytes] = []

    async def _invoke() -> None:
        nonlocal status_code
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", f"{api_config.API_HOST}:{api_config.API_PORT}".encode()),
                (b"origin", origin.encode("latin-1")),
            ],
            "client": ("127.0.0.1", 50000),
            "server": (api_config.API_HOST, api_config.API_PORT),
        }

        async def _receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(message: dict[str, object]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])  # type: ignore[arg-type]
                for k, v in message.get("headers", []):  # type: ignore[union-attr]
                    headers_out[k.decode("latin-1").lower()] = v.decode("latin-1")
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if isinstance(chunk, bytes):
                    body_chunks.append(chunk)

        await app(scope, _receive, _send)

    asyncio.run(_invoke())
    return status_code, headers_out, b"".join(body_chunks).decode("utf-8", errors="replace")


def test_acp_transcript_refuses_foreign_origin_across_path_spellings(tmp_path, monkeypatch):
    """Defect class: alternate path spellings (trailing slash, dot-segment, encoding, newline) bypass origin refusal."""
    root = tmp_path / "plane"
    _seed_transcript_db(root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    local = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000), base_url=_LISTENER_ORIGIN)
    url = "/api/runtime/acp/conversations/conv-guarded/transcript"
    foreign_headers = {"Origin": "https://evil.example"}

    # Trailing-slash spellings never serve a transcript (404 on Monitor's router; 307->403 where redirect_slashes fires).
    for trailing_path in (f"{url}/", "/api/runtime/acp/conversations/conv-guarded/x/../transcript/"):
        for follow in (False, True):
            resp = local.get(trailing_path, headers=foreign_headers, follow_redirects=follow)
            assert resp.status_code == 404, (trailing_path, follow)
            assert "guarded transcript body" not in resp.text
            assert "access-control-allow-credentials" not in resp.headers

    # Slash-redirected %0a/ exercises Starlette redirect_slashes: both the 307 and followed 403 return no transcript.
    redirect_resp = local.get(f"{url}%0a/", headers=foreign_headers, follow_redirects=False)
    assert redirect_resp.status_code == 307
    assert "guarded transcript body" not in redirect_resp.text
    assert "access-control-allow-credentials" not in redirect_resp.headers

    # Dot-segments, percent-encoding, and decoded terminal newline reaching the route all refuse with no transcript.
    for path_spelling in (
        "/api/runtime/acp/conversations/conv-guarded/./transcript",
        "/api/runtime/acp/conversations/other/../conv-guarded/transcript",
        "/api/runtime/acp/conversations/%63onv-guarded/%74ranscript",
        f"{url}%0a",
        f"{url}%0a/",
    ):
        resp = local.get(path_spelling, headers=foreign_headers, follow_redirects=True)
        assert resp.status_code == 403, path_spelling
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == {"detail": "Forbidden", "reason": "invalid_origin"}
        assert "guarded transcript body" not in resp.text
        assert "access-control-allow-credentials" not in resp.headers

    # Unnormalized dot-segments, trailing slash, and decoded newline sent directly at the ASGI layer yield no transcript.
    for raw_path, expected_status in (
        (f"{url}/", 404),
        ("/api/runtime/acp/conversations/conv-guarded/./transcript", 404),
        ("/api/runtime/acp/conversations/other/../conv-guarded/transcript", 403),
        (f"{url}\n", 403),
    ):
        status_code, resp_headers, body_text = _run_raw_asgi_get(raw_path, origin="https://evil.example")
        assert status_code == expected_status, (raw_path, status_code)
        assert "guarded transcript body" not in body_text
        assert "access-control-allow-credentials" not in resp_headers

    # Preflight OPTIONS never emits credentialed CORS or transcript content.
    for preflight_path in (url, f"{url}/"):
        preflight = local.options(
            preflight_path,
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert "guarded transcript body" not in preflight.text
        assert "access-control-allow-credentials" not in preflight.headers


def test_acp_transcript_refuses_mismatched_missing_or_duplicate_host(tmp_path, monkeypatch):
    """Defect class: DNS rebinding, wrong listener authority/scheme, missing/duplicate Host, or bad listener bypasses guard."""
    root = tmp_path / "plane"
    _seed_transcript_db(root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    url = "/api/runtime/acp/conversations/conv-guarded/transcript"
    local = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000), base_url=_LISTENER_ORIGIN)

    for bad_host in (
        "monitor.example",
        f"localhost:{api_config.API_PORT}",
        f"{api_config.API_HOST}:{api_config.API_PORT + 1}",
        api_config.API_HOST,
    ):
        resp = local.get(url, headers={"Host": bad_host})
        assert resp.status_code == 403, bad_host
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == {"detail": "Forbidden", "reason": "invalid_host"}
        assert "guarded transcript body" not in resp.text

    https_client = TestClient(
        app,
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
        base_url=f"https://{api_config.API_HOST}:{api_config.API_PORT}",
    )
    https_resp = https_client.get(url)
    assert https_resp.status_code == 403
    assert https_resp.headers["cache-control"] == "no-store"
    assert https_resp.json() == {"detail": "Forbidden", "reason": "invalid_scheme"}

    expected_host = f"{api_config.API_HOST}:{api_config.API_PORT}".encode()
    assert runtime_router._acp_transcript_access_refusal(_raw_transcript_request([])) == "invalid_host"
    assert (
        runtime_router._acp_transcript_access_refusal(
            _raw_transcript_request([(b"host", expected_host), (b"host", expected_host)])
        )
        == "invalid_host"
    )
    assert (
        runtime_router._acp_transcript_access_refusal(
            _raw_transcript_request([(b"host", expected_host)], peer=None)
        )
        == "non_loopback_peer"
    )
    for bad_server in (None, ("0.0.0.0", api_config.API_PORT), ("monitor.example", 80), ("127.0.0.1", 0)):
        assert (
            runtime_router._acp_transcript_access_refusal(
                _raw_transcript_request([(b"host", expected_host)], server=bad_server)
            )
            == "invalid_listener"
        )

    # HTTP port 80 requires the canonical WHATWG Host without explicit ':80' (IPv4 and IPv6).
    for server_host, canonical_host, peer_host in (
        ("127.0.0.1", b"127.0.0.1", "127.0.0.1"),
        ("::1", b"[::1]", "::1"),
    ):
        canonical_origin = b"http://" + canonical_host
        assert (
            runtime_router._acp_transcript_access_refusal(
                _raw_transcript_request(
                    [(b"host", canonical_host), (b"origin", canonical_origin)],
                    peer=(peer_host, 50000),
                    server=(server_host, 80),
                )
            )
            is None
        )
        assert (
            runtime_router._acp_transcript_access_refusal(
                _raw_transcript_request(
                    [(b"host", canonical_host + b":80")],
                    peer=(peer_host, 50000),
                    server=(server_host, 80),
                )
            )
            == "invalid_host"
        )
        assert (
            runtime_router._acp_transcript_access_refusal(
                _raw_transcript_request(
                    [(b"host", canonical_host), (b"origin", canonical_origin + b":80")],
                    peer=(peer_host, 50000),
                    server=(server_host, 80),
                )
            )
            == "invalid_origin"
        )


def test_acp_transcript_refuses_forwarding_headers(tmp_path, monkeypatch):
    """Defect class: proxied requests carrying Forwarded or X-Forwarded-* headers read ACP transcripts."""
    root = tmp_path / "plane"
    _seed_transcript_db(root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    local = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000), base_url=_LISTENER_ORIGIN)
    url = "/api/runtime/acp/conversations/conv-guarded/transcript"

    for header_name, header_value in (
        ("Forwarded", "for=127.0.0.1"),
        ("Forwarded", ""),
        ("X-Forwarded-For", "127.0.0.1"),
        ("X-Forwarded-Host", f"{api_config.API_HOST}:{api_config.API_PORT}"),
        ("X-Forwarded-Proto", "http"),
        ("X-Forwarded-Port", str(api_config.API_PORT)),
        ("X-FoRwArDeD-For", "198.51.100.1"),
    ):
        resp = local.get(url, headers={header_name: header_value})
        assert resp.status_code == 403, (header_name, header_value)
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == {"detail": "Forbidden", "reason": "forwarding_header_present"}
        assert "access-control-allow-origin" not in resp.headers
        assert "access-control-allow-credentials" not in resp.headers
        assert "guarded transcript body" not in resp.text


def test_acp_transcript_allows_same_origin_and_no_origin_local_reads(tmp_path, monkeypatch):
    """Defect class: listener/Origin/CORS hardening breaks authorized local reads across ports or IPv6 listeners."""
    root = tmp_path / "plane"
    _seed_transcript_db(root)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    local = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000), base_url=_LISTENER_ORIGIN)
    url = "/api/runtime/acp/conversations/conv-guarded/transcript"

    for headers in (
        {"Origin": _LISTENER_ORIGIN, "Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
        {},
    ):
        resp = local.get(url, headers=headers)
        assert resp.status_code == 200, headers
        assert resp.headers["cache-control"] == "no-store"
        assert "access-control-allow-credentials" not in resp.headers
        if "Origin" not in headers:
            assert "access-control-allow-origin" not in resp.headers
        assert resp.json()["messages"][0]["body"] == "guarded transcript body"

    # Alternate bound loopback port (e.g. launchd_supervisor start --port 9876) authorizes its own exact origin.
    alt_origin = "http://127.0.0.1:9876"
    alt_client = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000), base_url=alt_origin)
    for headers in ({}, {"Origin": alt_origin, "Sec-Fetch-Site": "same-origin"}):
        alt_ok = alt_client.get(url, headers=headers)
        assert alt_ok.status_code == 200, headers
        assert alt_ok.json()["messages"][0]["body"] == "guarded transcript body"
    assert alt_client.get(url, headers={"Origin": _LISTENER_ORIGIN}).json() == {
        "detail": "Forbidden",
        "reason": "invalid_origin",
    }
    assert alt_client.get(url, headers={"Host": f"{api_config.API_HOST}:{api_config.API_PORT}"}).json() == {
        "detail": "Forbidden",
        "reason": "invalid_host",
    }

    # IPv4-mapped IPv6 listener uses WHATWG browser serialization ([::ffff:7f00:1]:port) for Host and Origin.
    mapped_browser_host = f"[::ffff:7f00:1]:{api_config.API_PORT}".encode()
    mapped_browser_origin = f"http://[::ffff:7f00:1]:{api_config.API_PORT}".encode()
    assert (
        runtime_router._acp_transcript_access_refusal(
            _raw_transcript_request(
                [(b"host", mapped_browser_host), (b"origin", mapped_browser_origin)],
                peer=("::ffff:127.0.0.1", 50000),
                server=("::ffff:127.0.0.1", api_config.API_PORT),
            )
        )
        is None
    )
    assert (
        runtime_router._acp_transcript_access_refusal(
            _raw_transcript_request(
                [(b"host", f"[::ffff:127.0.0.1]:{api_config.API_PORT}".encode())],
                peer=("::ffff:127.0.0.1", 50000),
                server=("::ffff:127.0.0.1", api_config.API_PORT),
            )
        )
        == "invalid_host"
    )


def test_acp_transcript_hides_unavailable_malformed_and_body_free_routes(tmp_path, monkeypatch):
    root = tmp_path / "plane"
    now = datetime.now(UTC)
    conversation = (
        "conv-malformed", "secret", "secret", "secret", 1, "[]", _iso(now), None, 1, 1,
    )
    _write_acp_db(root, [conversation], [])
    connection = sqlite3.connect(root / "comms.sqlite3")
    try:
        connection.executemany(
            """INSERT INTO comms_messages(
                message_id, conversation_id, kind, sender, recipient, body_inline, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("message-private", "conv-malformed", "reply", "codex", "root", "body only in transcript", '{"path":"secret"}', _iso(now)),
                ("message-unlinked", "conv-malformed", "reply", "codex", "root", "must not appear", '{}', _iso(now)),
            ],
        )
        connection.execute(
            """INSERT INTO acp_conversation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("event-private", "conv-malformed", 1, "CALL_TERMINAL", "COMPLETE", "codex", "root", 1, "ok", 1, 1, "secret", "message-private", "{}", _iso(now)),
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    loopback_client = TestClient(
        app,
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
        base_url=_LISTENER_ORIGIN,
    )
    body_free = client.get("/api/runtime/acp/conversations/conv-malformed")
    transcript = loopback_client.get("/api/runtime/acp/conversations/conv-malformed/transcript")
    malformed = loopback_client.get("/api/runtime/acp/conversations/not%20an%20id/transcript")
    missing = loopback_client.get("/api/runtime/acp/conversations/not-found/transcript")

    assert body_free.status_code == 200
    assert "body only in transcript" not in body_free.text
    assert transcript.status_code == 200
    assert transcript.json()["messages"][0]["body"] == "body only in transcript"
    assert "must not appear" not in transcript.text
    assert malformed.status_code == 404
    assert missing.status_code == 404
    assert malformed.headers["cache-control"] == "no-store"
    assert missing.headers["cache-control"] == "no-store"

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(tmp_path / "missing"))
    unavailable = loopback_client.get("/api/runtime/acp/conversations/conv-malformed/transcript")
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"
    assert unavailable.json()["state"] == "backend_unavailable"

    malformed_root = tmp_path / "malformed"
    malformed_root.mkdir()
    malformed_db = sqlite3.connect(malformed_root / "comms.sqlite3")
    try:
        malformed_db.execute("CREATE TABLE unrelated (value TEXT)")
        malformed_db.commit()
    finally:
        malformed_db.close()
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(malformed_root))
    malformed_storage = loopback_client.get("/api/runtime/acp/conversations/conv-malformed/transcript")
    assert malformed_storage.status_code == 503
    assert malformed_storage.headers["cache-control"] == "no-store"
    assert malformed_storage.json()["state"] == "backend_unavailable"


def _loopback_transcript_client() -> TestClient:
    return TestClient(
        app,
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
        base_url=_LISTENER_ORIGIN,
    )


def _create_controller_conversation(
    root: Path,
    *,
    participants: tuple[str, ...] = ("codex", "claude"),
    deadline_at: str = "2099-01-01T00:00:00Z",
    turns: list[tuple[str, str, str, str, int | None]],
) -> tuple[str, list[tuple[str, str, str]]]:
    """Create a real Fleet Comms ACP conversation with CAS blobs and return ``(conv_id, [(msg_id, art_id, sha256)])``."""
    controller = AcpxDiscussionController(root=root)
    try:
        conversation_id, _ = controller._reserve(
            task_digest=f"task-{len(turns)}-{participants[0]}",
            correlation_digest=f"corr-{len(turns)}-{participants[0]}",
            idempotency_digest=f"idem-{len(turns)}-{'-'.join(participants)}-{deadline_at}",
            rounds=1,
            deadline_at=deadline_at,
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


def test_acp_transcript_state_ok_reads_verified_cas_body_and_multi_participants(tmp_path, monkeypatch):
    """Defect class: ACP transcript reader returns truncated inline prefix instead of verified CAS body or drops 3-participant turns."""
    root = tmp_path / "plane"
    full_body = "A" * 620 + " tail-only-in-cas-blob"
    conv_id, records = _create_controller_conversation(
        root,
        participants=("codex", "claude", "agy"),
        turns=[
            ("request", "root", "claude", "prompt for three seats", 1),
            ("reply", "agy", "root", full_body, 1),
            ("synthesis", "codex", "root", "three-seat synthesis", None),
        ],
    )
    # Simulate an authority-style 500-char inline prefix while CAS holds the full verified body.
    conn = sqlite3.connect(root / "comms.sqlite3")
    try:
        conn.execute(
            "UPDATE comms_messages SET body_inline = ? WHERE message_id = ?",
            (full_body[:500], records[1][0]),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    resp = _loopback_transcript_client().get(f"/api/runtime/acp/conversations/{conv_id}/transcript")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()
    assert data["conversation_id"] == conv_id
    assert data["state"] == "ok"
    assert data["label"] == "ok"
    assert data["reason"] is None
    assert data["truncated"] is False
    assert [m["state"] for m in data["messages"]] == ["ok", "ok", "ok"]
    assert [m["sender"] for m in data["messages"]] == ["root", "agy", "codex"]
    assert data["messages"][1]["body"] == full_body
    for msg_id, art_id, sha in records:
        for secret in (msg_id, art_id, sha, str(root)):
            assert secret not in resp.text


def test_acp_transcript_state_body_expired_withholds_inline_and_handles_race(tmp_path, monkeypatch):
    """Defect class: expired CAS bodies leak via body_inline or Phase-1 markers, or mark/unlink race reports corrupt."""
    root = tmp_path / "plane"
    expired_secret = "EXPIRED_INLINE_CREDENTIAL_SENTINEL"
    phase1_secret = "PHASE1_MARKED_BLOB_SENTINEL"
    race_secret = "RACE_UNLINK_CREDENTIAL_SENTINEL"
    conv_id, records = _create_controller_conversation(
        root,
        deadline_at="2020-01-01T00:00:00Z",
        turns=[
            ("request", "root", "codex", "still live turn", 1),
            ("reply", "codex", "root", expired_secret, 1),
            ("reply", "claude", "root", phase1_secret, 1),
            ("synthesis", "codex", "root", race_secret, None),
        ],
    )

    # Backdate turn 2 so real retention expires and unlinks its blob while body_inline stays in SQLite.
    old_ts = "2020-01-01T00:00:00Z"
    store = ArtifactStore(root=root)
    try:
        store.connection.execute(
            "UPDATE comms_messages SET created_at = ? WHERE message_id = ?",
            (old_ts, records[1][0]),
        )
        store.connection.execute(
            "UPDATE artifacts SET created_at = ? WHERE artifact_id = ?",
            (old_ts, records[1][1]),
        )
        store.connection.commit()
        blob2 = store.blob_path_for(records[1][2])
        os.utime(blob2, (1_577_836_800, 1_577_836_800))
        report = store.expire_message_blobs(older_than_days=30, grace_seconds=60, apply=True)
        assert report.removed == 1
        assert not blob2.exists()
        # Simulate retention Phase 1 (marked expired while blob file is still present on disk).
        store.connection.execute(
            "UPDATE artifacts SET retention_class = 'expired:acpx-discussion' WHERE artifact_id = ?",
            (records[2][1],),
        )
        store.connection.commit()
        assert store.blob_path_for(records[2][2]).is_file()
    finally:
        store.close()

    # Simulate the retention mark/unlink race on turn 4: reader reads live metadata, then
    # retention commits Phase 1 (expired marker) and Phase 2 (unlinks blob) before blob open.
    race_art_id, race_sha = records[3][1], records[3][2]
    real_open = runtime_router._open_acp_blob_no_follow

    def _racy_open(plane_root: Path, sha256: str) -> int:
        if sha256 == race_sha:
            writer = sqlite3.connect(root / "comms.sqlite3")
            try:
                writer.execute(
                    "UPDATE artifacts SET retention_class = 'expired:acpx-discussion' WHERE artifact_id = ?",
                    (race_art_id,),
                )
                writer.commit()
            finally:
                writer.close()
            (plane_root / "blobs" / "sha256" / sha256[:2] / sha256).unlink(missing_ok=True)
        return real_open(plane_root, sha256)

    monkeypatch.setattr(runtime_router, "_open_acp_blob_no_follow", _racy_open)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    resp = _loopback_transcript_client().get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()
    assert data["state"] == "body_expired"
    assert data["label"] == "body expired"
    assert data["reason"] == "body_expired"
    assert data["messages"][0]["state"] == "ok"
    assert data["messages"][0]["body"] == "still live turn"
    for idx in (1, 2, 3):
        turn = data["messages"][idx]
        assert turn["state"] == "body_expired", idx
        assert turn["label"] == "body expired", idx
        assert turn["reason"] == "body_expired", idx
        assert turn["body"] is None, idx
    for leaked in (expired_secret, phase1_secret, race_secret):
        assert leaked not in resp.text

    # Cross-turn retention race (F1): turn 1 is expired and unlinked while opening turn 2 -> turn 1 is cleared.
    cross_root = tmp_path / "plane-cross-turn"
    turn1_secret = "CROSS_TURN_EXPIRED_TURN1_SECRET"
    conv_cross, cross_records = _create_controller_conversation(
        cross_root,
        deadline_at="2020-01-01T00:00:00Z",
        turns=[
            ("request", "root", "codex", turn1_secret, 1),
            ("reply", "codex", "root", "turn 2 stays live", 1),
        ],
    )
    cross_store = ArtifactStore(root=cross_root)
    try:
        cross_store.connection.execute(
            "UPDATE comms_messages SET created_at = ? WHERE message_id = ?",
            (old_ts, cross_records[0][0]),
        )
        cross_store.connection.execute(
            "UPDATE artifacts SET created_at = ? WHERE artifact_id = ?",
            (old_ts, cross_records[0][1]),
        )
        cross_store.connection.commit()
        os.utime(cross_store.blob_path_for(cross_records[0][2]), (1_577_836_800, 1_577_836_800))
    finally:
        cross_store.close()

    turn2_sha = cross_records[1][2]

    def _expire_turn1_on_turn2_open(plane_root: Path, sha256: str) -> int:
        if sha256 == turn2_sha:
            s = ArtifactStore(root=cross_root)
            try:
                rep = s.expire_message_blobs(older_than_days=30, grace_seconds=60, apply=True)
                assert rep.removed == 1
            finally:
                s.close()
        return real_open(plane_root, sha256)

    monkeypatch.setattr(runtime_router, "_open_acp_blob_no_follow", _expire_turn1_on_turn2_open)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(cross_root))
    cross_resp = _loopback_transcript_client().get(f"/api/runtime/acp/conversations/{conv_cross}/transcript")
    assert cross_resp.status_code == 200
    assert cross_resp.headers["cache-control"] == "no-store"
    cross_data = cross_resp.json()
    assert cross_data["state"] == "body_expired"
    assert cross_data["messages"][0]["state"] == "body_expired"
    assert cross_data["messages"][0]["label"] == "body expired"
    assert cross_data["messages"][0]["body"] is None
    assert cross_data["messages"][1]["state"] == "ok"
    assert cross_data["messages"][1]["body"] == "turn 2 stays live"
    assert turn1_secret not in cross_resp.text

    # Single-statement final retention check (F1): CAS turn + legacy inline turn sharing one digest
    # both expire together in one snapshot when retention commits while opening a later turn.
    shared_root = tmp_path / "plane-shared-digest"
    shared_secret = "SHARED_CAS_AND_INLINE_EXPIRED_SECRET"
    conv_shared, shared_records = _create_controller_conversation(
        shared_root,
        deadline_at="2020-01-01T00:00:00Z",
        turns=[
            ("request", "root", "codex", shared_secret, 1),
            ("reply", "codex", "root", "trigger turn 3", 1),
        ],
    )
    shared_store = ArtifactStore(root=shared_root)
    try:
        shared_store.connection.execute(
            "UPDATE comms_messages SET created_at = ? WHERE message_id = ?",
            (old_ts, shared_records[0][0]),
        )
        shared_store.connection.execute(
            "UPDATE artifacts SET created_at = ? WHERE artifact_id = ?",
            (old_ts, shared_records[0][1]),
        )
        # Add a legacy inline-only turn sharing the same content_sha256 (body_artifact_id IS NULL).
        shared_store.connection.execute(
            """INSERT INTO comms_messages(
                message_id, conversation_id, kind, sender, recipient, body_inline,
                body_artifact_id, content_sha256, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "msg-inline-shared", conv_shared, "reply", "claude", "root", shared_secret,
                None, shared_records[0][2], "{}", old_ts,
            ),
        )
        shared_store.connection.execute(
            """INSERT INTO acp_conversation_events(
                event_id, conversation_id, sequence, event_type, state, sender, recipient,
                round, outcome, message_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "evt-inline-shared", conv_shared, 50, "CALL_TERMINAL", "COMPLETE", "claude", "root",
                1, "succeeded", "msg-inline-shared", "{}", old_ts,
            ),
        )
        shared_store.connection.commit()
        os.utime(shared_store.blob_path_for(shared_records[0][2]), (1_577_836_800, 1_577_836_800))
    finally:
        shared_store.close()

    trigger_sha = shared_records[1][2]

    def _expire_shared_on_trigger(plane_root: Path, sha256: str) -> int:
        if sha256 == trigger_sha:
            s = ArtifactStore(root=shared_root)
            try:
                rep = s.expire_message_blobs(older_than_days=30, grace_seconds=60, apply=True)
                assert rep.removed == 1
            finally:
                s.close()
        return real_open(plane_root, sha256)

    monkeypatch.setattr(runtime_router, "_open_acp_blob_no_follow", _expire_shared_on_trigger)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(shared_root))
    shared_resp = _loopback_transcript_client().get(f"/api/runtime/acp/conversations/{conv_shared}/transcript")
    assert shared_resp.status_code == 200
    shared_data = shared_resp.json()
    assert shared_data["state"] == "body_expired"
    assert [m["state"] for m in shared_data["messages"]] == ["body_expired", "ok", "body_expired"]
    assert shared_data["messages"][0]["body"] is None
    assert shared_data["messages"][2]["body"] is None
    assert shared_secret not in shared_resp.text


def test_acp_transcript_state_missing_or_corrupt_covers_blobs_turns_and_participants(tmp_path, monkeypatch):
    """Defect class: corrupt blobs, missing rows, or malformed/unhashable participants drop turns or raise TypeError."""
    root = tmp_path / "plane"
    unlinked_secret = "UNLINKED_NON_EXPIRED_SECRET"
    tampered_secret = "TAMPERED_BLOB_INLINE_SECRET"
    conv_id, records = _create_controller_conversation(
        root,
        turns=[
            ("request", "root", "codex", "healthy turn", 1),
            ("reply", "codex", "root", unlinked_secret, 1),
            ("reply", "claude", "root", tampered_secret, 1),
            ("reply", "codex", "root", "non-utf8 body", 1),
            ("reply", "claude", "root", "missing artifact row", 1),
            ("reply", "codex", "root", "poisoned metadata body", 1),
            ("reply", "claude", "root", "overflow timestamp body", 1),
        ],
    )
    # 1. Missing blob WITHOUT expired marker.
    blob_missing = root / "blobs" / "sha256" / records[1][2][:2] / records[1][2]
    blob_missing.unlink()
    # 2. Tampered blob bytes (same length -> digest mismatch).
    blob_tampered = root / "blobs" / "sha256" / records[2][2][:2] / records[2][2]
    blob_tampered.write_bytes(b"X" * len(tampered_secret.encode("utf-8")))
    # 3. Non-UTF8 blob with matching digest and byte count.
    bad_utf8 = b"\xff\xfe\xfd"
    bad_sha = hashlib.sha256(bad_utf8).hexdigest()
    bad_blob = root / "blobs" / "sha256" / bad_sha[:2] / bad_sha
    bad_blob.parent.mkdir(parents=True, exist_ok=True)
    bad_blob.write_bytes(bad_utf8)

    conn = sqlite3.connect(root / "comms.sqlite3")
    try:
        conn.execute(
            "UPDATE artifacts SET sha256 = ?, bytes = ? WHERE artifact_id = ?",
            (bad_sha, len(bad_utf8), records[3][1]),
        )
        conn.execute(
            "UPDATE comms_messages SET content_sha256 = ? WHERE message_id = ?",
            (bad_sha, records[3][0]),
        )
        # 4. Missing artifact row referenced by turn 5.
        conn.execute(
            "UPDATE comms_messages SET body_artifact_id = 'art-does-not-exist' WHERE message_id = ?",
            (records[4][0],),
        )
        # 5. Malformed metadata (unallowlisted sender) on turn 6.
        conn.execute(
            "UPDATE comms_messages SET sender = 'intruder-seat' WHERE message_id = ?",
            (records[5][0],),
        )
        # 5b. Timestamp that overflows UTC conversion on turn 7 (F4).
        conn.execute(
            "UPDATE comms_messages SET created_at = '0001-01-01T00:00:00+00:01' WHERE message_id = ?",
            (records[6][0],),
        )
        # 6. Dangling event referencing a missing comms_messages row.
        conn.execute(
            """INSERT INTO acp_conversation_events(
                event_id, conversation_id, sequence, event_type, state, sender, recipient,
                round, outcome, message_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "evt-dangling", conv_id, 99, "CALL_TERMINAL", "COMPLETE", "codex", "root",
                1, "succeeded", "msg-missing-row", "{}", "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    local = _loopback_transcript_client()
    resp = local.get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()
    assert data["state"] == "missing_or_corrupt"
    assert data["label"] == "missing or corrupt"
    assert len(data["messages"]) == 8
    assert data["messages"][0]["state"] == "ok"
    assert data["messages"][0]["body"] == "healthy turn"
    expected_reasons = [
        (1, "blob_missing"),
        (2, "blob_digest_mismatch"),
        (3, "body_not_utf8"),
        (4, "artifact_row_missing"),
        (5, "malformed_turn_metadata"),
        (6, "malformed_turn_metadata"),
        (7, "message_row_missing"),
    ]
    for idx, expected_reason in expected_reasons:
        turn = data["messages"][idx]
        assert turn["state"] == "missing_or_corrupt", (idx, turn)
        assert turn["label"] == "missing or corrupt", (idx, turn)
        assert turn["reason"] == expected_reason, (idx, turn)
        assert turn["body"] is None, (idx, turn)
    for forbidden in (unlinked_secret, tampered_secret, "intruder-seat", "poisoned metadata body", "overflow timestamp body"):
        assert forbidden not in resp.text

    # Malformed participant items (unhashable dicts/lists, >4 seats, <2 seats, unknown seats) refused with typed reason.
    for bad_participants in (
        '[{"nested": 1}]',
        '[["codex"]]',
        '[123, "codex"]',
        '["codex", "evil"]',
        '["codex"]',
        '["codex", "claude", "agy", "grok", "cursor"]',
        'not-json',
        '{}',
    ):
        conn = sqlite3.connect(root / "comms.sqlite3")
        try:
            conn.execute(
                "UPDATE acp_conversations SET participants_json = ? WHERE conversation_id = ?",
                (bad_participants, conv_id),
            )
            conn.commit()
        finally:
            conn.close()
        bad_resp = local.get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
        assert bad_resp.status_code == 200, bad_participants
        assert bad_resp.headers["cache-control"] == "no-store"
        assert bad_resp.json() == {
            "conversation_id": conv_id,
            "state": "missing_or_corrupt",
            "label": "missing or corrupt",
            "reason": "malformed_participants",
            "truncated": False,
            "messages": [],
        }
        assert "TypeError" not in bad_resp.text
        assert "unhashable" not in bad_resp.text
        assert "healthy turn" not in bad_resp.text

    # Body-free routes also tolerate unhashable or >4 participant items without raising TypeError.
    for body_free_participants in ('[{"nested": 1}]', '["codex", "claude", "agy", "grok", "cursor"]'):
        conn = sqlite3.connect(root / "comms.sqlite3")
        try:
            conn.execute(
                "UPDATE acp_conversations SET participants_json = ? WHERE conversation_id = ?",
                (body_free_participants, conv_id),
            )
            conn.commit()
        finally:
            conn.close()
        detail_resp = client.get(f"/api/runtime/acp/conversations/{conv_id}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["participants"] == ["codex", "grok"]
        assert client.get("/api/runtime/acp/conversations").status_code == 200


def test_acp_transcript_state_backend_unavailable_returns_503_and_reserves_404(tmp_path, monkeypatch):
    """Defect class: storage, schema, authority, or blob backend errors return 404 instead of 503 backend_unavailable."""
    local = _loopback_transcript_client()

    # 1. Missing store directory / comms.sqlite3 -> 503 even for unknown id.
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(tmp_path / "does-not-exist"))
    missing_store = local.get("/api/runtime/acp/conversations/unknown-conv/transcript")
    assert missing_store.status_code == 503
    assert missing_store.headers["cache-control"] == "no-store"
    assert missing_store.json()["state"] == "backend_unavailable"
    assert missing_store.json()["label"] == "backend unavailable"
    assert missing_store.json()["reason"] == "store_unavailable"
    assert str(tmp_path) not in missing_store.text

    # 2. Corrupt non-SQLite database file -> 503.
    corrupt_root = tmp_path / "corrupt-db"
    corrupt_root.mkdir()
    (corrupt_root / "comms.sqlite3").write_bytes(b"not a sqlite file at all")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(corrupt_root))
    corrupt_resp = local.get("/api/runtime/acp/conversations/conv-1/transcript")
    assert corrupt_resp.status_code == 503
    assert corrupt_resp.headers["cache-control"] == "no-store"
    assert corrupt_resp.json()["state"] == "backend_unavailable"
    assert corrupt_resp.json()["reason"] == "store_unavailable"

    # 3. Missing artifacts table when a message references body_artifact_id -> 503 schema_unavailable.
    no_art_root = tmp_path / "no-artifacts-table"
    now = datetime.now(UTC)
    _write_acp_db(
        no_art_root,
        [("conv-no-art", "t", "c", "i", 1, "[]", _iso(now), None, 1, 1)],
        [("evt-1", "conv-no-art", 1, "CALL_TERMINAL", "COMPLETE", "codex", "root", 1, "ok", 1, 1, "s", "msg-1", "{}", _iso(now))],
    )
    conn = sqlite3.connect(no_art_root / "comms.sqlite3")
    try:
        conn.execute(
            """INSERT INTO comms_messages(
                message_id, conversation_id, kind, sender, recipient, body_inline, body_artifact_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("msg-1", "conv-no-art", "reply", "codex", "root", "must not leak", "art-1", "{}", _iso(now)),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(no_art_root))
    no_art_resp = local.get("/api/runtime/acp/conversations/conv-no-art/transcript")
    assert no_art_resp.status_code == 503
    assert no_art_resp.json()["state"] == "backend_unavailable"
    assert no_art_resp.json()["reason"] == "schema_unavailable"
    assert "must not leak" not in no_art_resp.text

    # 4. Real store: unknown conversation id -> 404 ONLY after successful lookup.
    good_root = tmp_path / "good-plane"
    conv_id, _ = _create_controller_conversation(
        good_root,
        turns=[("request", "root", "codex", "hello", 1)],
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(good_root))
    not_found = local.get("/api/runtime/acp/conversations/unknown-id/transcript")
    assert not_found.status_code == 404
    assert not_found.headers["cache-control"] == "no-store"
    assert not_found.json() == {"detail": "Conversation not found", "reason": "conversation_not_found"}

    # 5. Shared CAS directory replaced with a regular file (ENOTDIR) -> 503 blob_store_unavailable (F2).
    bad_blobs_root = tmp_path / "bad-blobs-dir"
    conv_bad_dir, _ = _create_controller_conversation(
        bad_blobs_root,
        turns=[("request", "root", "codex", "cannot open blobs dir", 1)],
    )
    import shutil

    shutil.rmtree(bad_blobs_root / "blobs")
    (bad_blobs_root / "blobs").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(bad_blobs_root))
    bad_dir_resp = local.get(f"/api/runtime/acp/conversations/{conv_bad_dir}/transcript")
    assert bad_dir_resp.status_code == 503
    assert bad_dir_resp.headers["cache-control"] == "no-store"
    assert bad_dir_resp.json()["state"] == "backend_unavailable"
    assert bad_dir_resp.json()["reason"] == "blob_store_unavailable"
    assert "cannot open blobs dir" not in bad_dir_resp.text

    # 6. Blob backend OSError (not FileNotFoundError) -> 503 blob_store_unavailable.
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(good_root))
    def _fail_blob_open(_plane_root: Path, _sha256: str) -> int:
        raise PermissionError("secret host path /private/blob")

    monkeypatch.setattr(runtime_router, "_open_acp_blob_no_follow", _fail_blob_open)
    io_err = local.get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
    assert io_err.status_code == 503
    assert io_err.headers["cache-control"] == "no-store"
    assert io_err.json()["state"] == "backend_unavailable"
    assert io_err.json()["reason"] == "blob_store_unavailable"
    assert "PermissionError" not in io_err.text
    assert "/private/blob" not in io_err.text

    # 7. Unsupported PG authority -> 503 authority_unsupported_component.
    monkeypatch.setattr(runtime_router, "resolve_authority", lambda _store_id: Authority.PG)
    pg_resp = local.get(f"/api/runtime/acp/conversations/{conv_id}/transcript")
    assert pg_resp.status_code == 503
    assert pg_resp.headers["cache-control"] == "no-store"
    assert pg_resp.json()["state"] == "backend_unavailable"
    assert pg_resp.json()["reason"] == "authority_unsupported_component"


def test_acp_transcript_state_oversized_or_partial_preserves_turns_without_404(tmp_path, monkeypatch):
    """Defect class: oversized turns, response byte budget, or >32 turns silently drop turns or return 404."""
    local = _loopback_transcript_client()

    # 1. Single oversized turn (>256 KiB) preserved with state=oversized_or_partial alongside an ok turn.
    root_big = tmp_path / "plane-big"
    oversized_body = "B" * (runtime_router._ACP_TRANSCRIPT_MAX_BODY_BYTES + 128)
    conv_big, _ = _create_controller_conversation(
        root_big,
        turns=[
            ("request", "root", "codex", "small turn", 1),
            ("reply", "codex", "root", oversized_body, 1),
        ],
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_big))
    resp_big = local.get(f"/api/runtime/acp/conversations/{conv_big}/transcript")
    assert resp_big.status_code == 200
    assert resp_big.headers["cache-control"] == "no-store"
    data_big = resp_big.json()
    assert data_big["state"] == "oversized_or_partial"
    assert data_big["label"] == "oversized or partial"
    assert data_big["reason"] == "body_too_large"
    assert data_big["truncated"] is False
    assert data_big["messages"][0]["state"] == "ok"
    assert data_big["messages"][0]["body"] == "small turn"
    assert data_big["messages"][1]["state"] == "oversized_or_partial"
    assert data_big["messages"][1]["label"] == "oversized or partial"
    assert data_big["messages"][1]["reason"] == "body_too_large"
    assert data_big["messages"][1]["body"] is None

    # 2. Total response byte budget (<=512 KiB including JSON envelope and turn metadata, F5).
    root_budget = tmp_path / "plane-budget"
    near_half = "C" * (runtime_router._ACP_TRANSCRIPT_MAX_BODY_BYTES - 2)
    conv_budget, _ = _create_controller_conversation(
        root_budget,
        turns=[
            ("request", "root", "codex", near_half, 1),
            ("reply", "codex", "root", near_half, 1),
        ],
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_budget))
    resp_budget = local.get(f"/api/runtime/acp/conversations/{conv_budget}/transcript")
    assert resp_budget.status_code == 200
    assert len(resp_budget.content) <= runtime_router._ACP_TRANSCRIPT_MAX_RESPONSE_BYTES
    data_budget = resp_budget.json()
    assert data_budget["state"] == "oversized_or_partial"
    assert data_budget["reason"] == "response_budget_exceeded"
    assert data_budget["truncated"] is False
    assert [m["state"] for m in data_budget["messages"]] == ["ok", "oversized_or_partial"]
    assert data_budget["messages"][1]["reason"] == "response_budget_exceeded"
    assert data_budget["messages"][1]["body"] is None

    # 2b. Two live 261,900-byte turns whose exact serialized payload fits <=512 KiB are both admitted (F5).
    root_fit = tmp_path / "plane-fit"
    fit_chunk = "F" * 261_900
    conv_fit, _ = _create_controller_conversation(
        root_fit,
        turns=[
            ("request", "root", "codex", fit_chunk, 1),
            ("reply", "codex", "root", fit_chunk, 1),
        ],
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_fit))
    resp_fit = local.get(f"/api/runtime/acp/conversations/{conv_fit}/transcript")
    assert resp_fit.status_code == 200
    assert len(resp_fit.content) <= runtime_router._ACP_TRANSCRIPT_MAX_RESPONSE_BYTES
    data_fit = resp_fit.json()
    assert data_fit["state"] == "ok"
    assert [m["state"] for m in data_fit["messages"]] == ["ok", "ok"]
    assert data_fit["messages"][1]["body"] == fit_chunk

    # 2c. Bodies expired during read do not consume response budget for surviving live turns (F5).
    root_reclaim = tmp_path / "plane-reclaim"
    old_ts = "2020-01-01T00:00:00Z"
    turn_200k_1 = "1" * (200 * 1024)
    turn_200k_2 = "2" * (200 * 1024)
    turn_200k_3 = "3" * (200 * 1024)
    conv_reclaim, rec_reclaim = _create_controller_conversation(
        root_reclaim,
        deadline_at=old_ts,
        turns=[
            ("request", "root", "codex", turn_200k_1, 1),
            ("reply", "codex", "root", turn_200k_2, 1),
            ("synthesis", "codex", "root", turn_200k_3, None),
        ],
    )
    reclaim_store = ArtifactStore(root=root_reclaim)
    try:
        for msg_id, art_id, sha in rec_reclaim[:2]:
            reclaim_store.connection.execute(
                "UPDATE comms_messages SET created_at = ? WHERE message_id = ?", (old_ts, msg_id)
            )
            reclaim_store.connection.execute(
                "UPDATE artifacts SET created_at = ? WHERE artifact_id = ?", (old_ts, art_id)
            )
            os.utime(reclaim_store.blob_path_for(sha), (1_577_836_800, 1_577_836_800))
        reclaim_store.connection.commit()
    finally:
        reclaim_store.close()

    real_open = runtime_router._open_acp_blob_no_follow
    third_sha = rec_reclaim[2][2]

    def _expire_first_two_on_third(plane_root: Path, sha256: str) -> int:
        if sha256 == third_sha:
            s = ArtifactStore(root=root_reclaim)
            try:
                rep = s.expire_message_blobs(older_than_days=30, grace_seconds=60, apply=True)
                assert rep.removed == 2
            finally:
                s.close()
        return real_open(plane_root, sha256)

    monkeypatch.setattr(runtime_router, "_open_acp_blob_no_follow", _expire_first_two_on_third)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_reclaim))
    resp_reclaim = local.get(f"/api/runtime/acp/conversations/{conv_reclaim}/transcript")
    assert resp_reclaim.status_code == 200
    assert len(resp_reclaim.content) <= runtime_router._ACP_TRANSCRIPT_MAX_RESPONSE_BYTES
    data_reclaim = resp_reclaim.json()
    assert data_reclaim["state"] == "body_expired"
    assert [m["state"] for m in data_reclaim["messages"]] == ["body_expired", "body_expired", "ok"]
    assert data_reclaim["messages"][0]["body"] is None
    assert data_reclaim["messages"][1]["body"] is None
    assert data_reclaim["messages"][2]["body"] == turn_200k_3

    # 2d. Smaller-representation reservation: [262144, 261243, 1, 262144] admits turns 1-3 at exact 512 KiB (PR1493-F5).
    root_exact = tmp_path / "plane-exact-pack"
    exact_t1 = "1" * 262_144
    exact_t2 = "2" * 261_243
    exact_t3 = "3"
    exact_t4 = "4" * 262_144
    conv_exact, _ = _create_controller_conversation(
        root_exact,
        turns=[
            ("reply", "codex", "root", exact_t1, 1),
            ("reply", "codex", "root", exact_t2, 1),
            ("reply", "codex", "root", exact_t3, 1),
            ("reply", "codex", "root", exact_t4, 2),
        ],
    )
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_exact))
    resp_exact = local.get(f"/api/runtime/acp/conversations/{conv_exact}/transcript")
    assert resp_exact.status_code == 200
    assert len(resp_exact.content) == runtime_router._ACP_TRANSCRIPT_MAX_RESPONSE_BYTES
    data_exact = resp_exact.json()
    assert data_exact["state"] == "oversized_or_partial"
    assert data_exact["reason"] == "response_budget_exceeded"
    assert [m["state"] for m in data_exact["messages"]] == ["ok", "ok", "ok", "oversized_or_partial"]
    assert data_exact["messages"][0]["body"] == exact_t1
    assert data_exact["messages"][1]["body"] == exact_t2
    assert data_exact["messages"][2]["body"] == exact_t3
    assert data_exact["messages"][3]["body"] is None
    assert data_exact["messages"][3]["reason"] == "response_budget_exceeded"

    # 3. Message count limit (>32 turns) returns HTTP 200 (not 404) with first 32 turns and truncated=True.
    root_many = tmp_path / "plane-many"
    many_turns = [
        ("reply", "codex", "root", f"turn-{i}", 1)
        for i in range(runtime_router._ACP_TRANSCRIPT_MAX_MESSAGES + 2)
    ]
    conv_many, _ = _create_controller_conversation(root_many, turns=many_turns)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root_many))
    resp_many = local.get(f"/api/runtime/acp/conversations/{conv_many}/transcript")
    assert resp_many.status_code == 200
    data_many = resp_many.json()
    assert data_many["state"] == "oversized_or_partial"
    assert data_many["label"] == "oversized or partial"
    assert data_many["reason"] == "message_limit_exceeded"
    assert data_many["truncated"] is True
    assert len(data_many["messages"]) == runtime_router._ACP_TRANSCRIPT_MAX_MESSAGES
    assert all(m["state"] == "ok" for m in data_many["messages"])


def test_acp_conversations_refuse_poisoned_rows_and_hide_unavailable_storage(tmp_path, monkeypatch):
    root = tmp_path / "plane"
    now = datetime.now(UTC)
    conversation = (
        "conv-good", "secret", "secret", "secret", 1, "not-json", _iso(now), None, 1, 1,
    )
    events = [
        (
            "event-good", "conv-good", 1, "CALL_TERMINAL", "INITIAL_FANOUT", "root", "grok", 1,
            "timeout", 4, 2, "secret", "secret", '{broken', _iso(now),
        ),
        (
            "event-busy", "conv-good", 2, "CALL_TERMINAL", "INITIAL_FANOUT", "root", "codex", 1,
            "busy", 0, 0, "secret", "secret", "{}", _iso(now),
        ),
        (
            "event-duplicate", "conv-good", 3, "duplicate_suppressed", "PARTIAL_COMPLETE", "root", None, 1,
            "duplicate_suppressed", 0, 0, "secret", "secret", '{"body":"must-not-leak"}', _iso(now),
        ),
        (
            "event-poison", "conv-good", 4, "prompt body must not leak", "COMPLETE", "intruder", "grok", 1,
            "succeeded", 4, 2, "secret", "secret", '{"body":"must-not-leak"}', _iso(now),
        ),
    ]
    _write_acp_db(root, [conversation], events)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    response = client.get("/api/runtime/acp/conversations/conv-good")

    assert response.status_code == 200
    data = response.json()
    assert data["current_state"] == "PARTIAL_COMPLETE"
    assert data["termination_reason"] == "duplicate_suppressed"
    assert data["duplicate_suppressed"] is True
    assert len(data["events"]) == 3
    assert data["events"][0]["event_type"] == "CALL_TERMINAL"
    assert data["events"][0]["outcome"] == "failed"
    assert data["events"][1]["recipient"] == "codex"
    assert data["events"][1]["outcome"] == "partial"
    assert "must-not-leak" not in response.text
    assert client.get("/api/runtime/acp/conversations/not-found").status_code == 404

    monkeypatch.setenv("FLEET_COMMS_ROOT", str(tmp_path / "missing"))
    missing = client.get("/api/runtime/acp/conversations")
    assert missing.json() == {"availability": "unavailable", "conversations": []}


def test_acp_summary_marks_expired_and_partial_conversations_unhealthy(tmp_path, monkeypatch):
    root = tmp_path / "plane"
    now = datetime.now(UTC)
    conversation = (
        "conv-expired", "task", "correlation", "idempotency", 1, '[]',
        _iso(now - timedelta(minutes=10)), _iso(now - timedelta(minutes=1)), 1, 1,
    )
    events = [
        (
            "event-1", "conv-expired", 1, "STATE", "PARTIAL", None, None, None,
            None, None, None, None, None, "{}", _iso(now - timedelta(minutes=2)),
        ),
    ]
    _write_acp_db(root, [conversation], events)
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))

    summary = client.get("/api/runtime/acp/conversations").json()["conversations"][0]

    assert summary["deadline_at"] == _iso(now - timedelta(minutes=1))
    assert summary["expired"] is True
    assert summary["stale_or_unhealthy"] is True


def test_acp_termination_reason_allowlists_budget_and_deadline_events():
    assert runtime_router._acp_termination(
        [{"event_type": "BUDGET_EXHAUSTED"}], "PARTIAL_COMPLETE"
    ) == "budget_exhausted"
    assert runtime_router._acp_termination(
        [{"event_type": "DEADLINE_EXCEEDED"}], "PARTIAL_COMPLETE"
    ) == "deadline_exceeded"
    assert runtime_router._acp_termination(
        [{"event_type": "CALL_TERMINAL", "outcome": "failed"}], "PARTIAL_COMPLETE"
    ) is None
    assert runtime_router._acp_classification("CANCELLED") == "cancelled"
    assert runtime_router._acp_termination([], "CANCELLED") == "cancelled"


def test_recent_limits_results(tmp_path, monkeypatch):
    usage_dir = tmp_path / "api_usage"
    now = datetime.now(UTC)
    monkeypatch.setattr(app.state, "ctx", _ctx_with_batch_state_dir(usage_dir.parent))
    _write_usage_file(
        usage_dir / f"usage_codex-dispatch_{now:%Y-%m-%d}.jsonl",
        [
            {
                "ts": _iso(now - timedelta(minutes=3)),
                "agent": "codex",
                "entrypoint": "dispatch",
                "model": "gpt-5.5",
                "outcome": "ok",
                "duration_s": 9.0,
            },
            {
                "ts": _iso(now - timedelta(minutes=1)),
                "agent": "codex",
                "entrypoint": "dispatch",
                "model": "gpt-5.5",
                "outcome": "timeout",
                "duration_s": 12.0,
            },
            {
                "ts": _iso(now - timedelta(minutes=2)),
                "agent": "gemini",
                "entrypoint": "bridge",
                "initiator": "codex",
                "attribution_source": "explicit",
                "attribution_task_id": "review-6159",
                "model": "gemini-3.1-pro-preview",
                "outcome": "ok",
                "duration_s": 5.0,
            },
        ],
    )

    records = runtime_router.recent_runtime_records(limit=2, ctx=app.state.ctx)["records"]
    assert len(records) == 2
    assert records[0]["outcome"] == "timeout"
    assert records[0]["source"] == "unknown"
    assert records[0]["via"] == "dispatch"
    assert records[0]["failure_code"] == "timeout"
    assert records[1]["agent"] == "gemini"
    assert records[1]["source"] == "codex"
    assert records[1]["source_provenance"] == "explicit"
    assert records[1]["source_task_id"] == "review-6159"
    assert records[1]["failure_code"] is None


def test_recent_preserves_typed_body_free_acp_failure(tmp_path, monkeypatch):
    usage_dir = tmp_path / "api_usage"
    now = datetime.now(UTC)
    monkeypatch.setattr(app.state, "ctx", _ctx_with_batch_state_dir(usage_dir.parent))
    _write_usage_file(
        usage_dir / f"usage_acpx-claude-shadow-acpx-transport_{now:%Y-%m-%d}.jsonl",
        [
            {
                "ts": _iso(now),
                "agent": "acpx-claude-shadow",
                "entrypoint": "acpx-transport",
                "initiator": "codex",
                "attribution_source": "explicit",
                "model": "claude-sonnet-5",
                "outcome": "error",
                "failure_code": "acp_turn_limit",
                "stderr_excerpt": None,
            }
        ],
    )

    record = runtime_router.recent_runtime_records(limit=1, ctx=app.state.ctx)["records"][0]
    assert record["failure_code"] == "acp_turn_limit"
    assert "stderr_excerpt" not in record


def test_transport_health_returns_sanitized_cached_probe(tmp_path, monkeypatch):
    # The receipt lives under the configured shared-state root (#93 replaced TRANSPORT_RECEIPT_PATH).
    receipt_path = tmp_path / "batch_state" / "runtime" / "codex-transport-health.json"
    receipt_path.parent.mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    now = datetime.now(UTC)
    config_path.write_text(
        "[features.multi_agent_v2]\ntool_namespace = \"agents\"\n",
        encoding="utf-8",
    )
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": "codex-transport-health.v2",
                "status": "healthy",
                "checked_at": _iso(now - timedelta(seconds=5)),
                "expires_at": _iso(now + timedelta(minutes=10)),
                "model": "gpt-5.6-terra",
                "effort": "low",
                "task_id": "codex-transport-probe-test",
                "failure_class": None,
                "source": "fresh_native_probe",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_router.codex_transport_health, "state_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(runtime_router.codex_transport_health, "default_config_path", lambda: config_path)

    response = client.get("/api/runtime/transport-health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["fresh"] is True
    assert data["namespace_valid"] is True
    assert data["tool_namespace"] == "agents"
    assert "task_id" not in data
    assert "content" not in data


def test_transport_health_is_unknown_without_probe_receipt(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[features.multi_agent_v2]\ntool_namespace = \"agents\"\n",
        encoding="utf-8",
    )
    # No receipt under the configured shared-state root (#93 replaced TRANSPORT_RECEIPT_PATH).
    monkeypatch.setattr(runtime_router.codex_transport_health, "state_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(runtime_router.codex_transport_health, "default_config_path", lambda: config_path)

    response = client.get("/api/runtime/transport-health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "unknown"
    assert data["fresh"] is False
    assert data["failure_class"] == "no_probe_receipt"


def test_recent_accounting_projections_preserve_legacy_aggregates(tmp_path, monkeypatch):
    usage_dir = tmp_path / "api_usage"
    ctx = _ctx_with_batch_state_dir(usage_dir.parent)
    monkeypatch.setattr(app.state, "ctx", ctx)
    expected = {
        "requested_selector": "requested-model", "observed_model": "observed-model",
        "identity_state": "observed", "observed_model_source": "provider-init",
        "observed_model_provenance": {"requested_model": "attempt-model", "invocation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
    }
    path = usage_dir / f"usage_agy-dispatch_{datetime.now(UTC):%Y-%m-%d}.jsonl"
    original = [
        {"ts": _iso(datetime.now(UTC)), "agent": "agy", "entrypoint": "dispatch",
         "model": "accounting-model", "outcome": "error", "duration_s": 3.0},
        {"ts": _iso(datetime.now(UTC)), "agent": "codex", "entrypoint": "dispatch",
         "model": "legacy-model", "outcome": "ok", "duration_s": 1.0},
    ]
    _write_usage_file(path, original)
    before = runtime_router.summarize_runtime_usage(ctx=ctx)
    last_used = runtime_router._last_used_agent_models(ctx=ctx)
    _write_usage_file(path, [{**original[0], **expected}, original[1]])
    assert runtime_router.summarize_runtime_usage(ctx=ctx) == before
    assert runtime_router._last_used_agent_models(ctx=ctx) == last_used
    rows = {row["agent"]: row for row in runtime_router.recent_runtime_records(ctx=ctx)["records"]}
    assert rows["agy"]["model"] == "accounting-model"
    assert {key: rows["agy"][key] for key in expected} == expected
    assert {key: rows["codex"][key] for key in expected} == {
        "requested_selector": None, "observed_model": None,
        "identity_state": "unknown", "observed_model_source": None, "observed_model_provenance": None,
    }


@pytest.mark.parametrize("conflicting", [False, True])
def test_activity_projects_runtime_model_identity(monkeypatch, conflicting):
    projection = {
        "requested_selector": "requested-model",
        "observed_model": "observed-model",
        "identity_state": "observed",
        "observed_model_source": "provider-init",
        "observed_model_provenance": {
            "requested_model": "attempt-model",
            "invocation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
    }
    raw = {
        "model": "accounting-model",
        **projection,
        "model_identity": {
            "actual_model_known": True,
            "actual_model": "conflicting-model" if conflicting else "observed-model",
            "source": "provider-init",
        },
    }
    monkeypatch.setattr(fleet_router, "recent_runtime_records", lambda **kwargs: {"records": [raw]})

    response = client.get("/api/fleet/activity")

    assert response.status_code == 200
    record, = response.json()["records"]
    assert record["model"] == "accounting-model"
    assert {key: record[key] for key in projection} == (projection if not conflicting else {
        "requested_selector": "requested-model", "observed_model": None,
        "identity_state": "unknown", "observed_model_source": None, "observed_model_provenance": None,
    })
