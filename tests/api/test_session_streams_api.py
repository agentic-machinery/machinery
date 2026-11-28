"""PR-K Monitor surfaces for session streams (read-only)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import main as api_main
from lu_machinery.api.monitor_context import (
    MonitorContext,
    fixture_context,
    production_context,
)
from lu_machinery.api.session_streams_router import router
from lu_machinery.session_streams.db import SessionStreamDatabase


def _client(ctx: MonitorContext | None = None) -> TestClient:
    app = FastAPI()
    app.state.ctx = ctx or production_context()
    app.include_router(router, prefix="/api/session-streams")
    return TestClient(app)


def test_session_streams_health_and_plane_continuity() -> None:
    client = _client()
    h = client.get("/api/session-streams/v1/health")
    assert h.status_code == 200
    body = h.json()
    assert body["ok"] is True
    assert "repo_root" not in body
    assert "db_path" not in body
    assert set(body["repo"]) == {"role", "sha"}
    assert body["repo"]["role"] in {"live", "release"}
    assert set(body["store"]) == {"reachable", "schema_versions"}

    assert client.get("/api/session-streams/v1/dual-write-status").status_code == 404

    c = client.get("/api/session-streams/v1/plane-continuity")
    assert c.status_code == 200
    assert c.json()["cutover"] == "operator-gated"
    assert "dual_write" not in c.json()


def test_session_stream_status_and_digest_for_harness() -> None:
    client = _client()
    s = client.get("/api/session-streams/v1/status/epic:4707")
    assert s.status_code in {200, 404}
    if s.status_code == 200:
        assert s.json()["stream_id"] == "epic:4707"  # allow-hardcoded-epic: session stream status api probe

    dig = client.get("/api/session-streams/v1/digest/epic:4707", params={"limit": 5})
    assert dig.status_code in {200, 404}
    if dig.status_code == 200:
        assert dig.json()["limit"] == 5
        assert "pinned" in dig.json()
        assert "recent" in dig.json()


def test_session_stream_status_rejects_bad_id() -> None:
    client = _client()
    r = client.get("/api/session-streams/v1/status/not-an-epic")
    assert r.status_code == 400


def test_session_streams_repo_root_uses_live_primary_under_release(
    tmp_path: Path, monkeypatch
) -> None:
    """PR-K residual: release code root must not own .agent/session-streams."""
    live = tmp_path / "live"
    live.mkdir()
    db_dir = live / ".agent" / "session-streams" / "v1"
    db_dir.mkdir(parents=True)
    db = db_dir / "session-streams.sqlite3"
    connection = SessionStreamDatabase(db).connect()
    connection.close()

    # Synthetic release path shape: .runtime/api/releases/<40-hex>
    release = tmp_path / ".runtime" / "api" / "releases" / ("a" * 40)
    release.mkdir(parents=True)

    from lu_machinery.api import config
    monkeypatch.setattr(config, "PROJECT_ROOT", release)
    monkeypatch.setattr(config, "LIVE_REPO_ROOT", live)
    monkeypatch.setattr(
        "lu_machinery.session_streams.db.canonical_state_root",
        lambda root=None: live,
    )

    ctx = production_context()
    client = _client(ctx)
    resp = client.get("/api/session-streams/v1/health")
    assert resp.status_code == 200
    health = resp.json()
    assert "repo_root" not in health
    assert "db_path" not in health
    assert health["repo"] == {"role": "live", "sha": None}
    assert health["store"]["reachable"] is True
    assert health["store"]["schema_versions"]


def test_session_streams_missing_db_returns_404(tmp_path: Path) -> None:
    """When the SQLite DB does not exist, status and digest return 404 cleanly."""
    ctx = fixture_context(tmp_path)
    client = _client(ctx)

    s = client.get("/api/session-streams/v1/status/epic:4707")
    assert s.status_code == 404
    assert "session-stream database is unavailable" in s.json()["detail"]

    d = client.get("/api/session-streams/v1/digest/epic:4707")
    assert d.status_code == 404
    assert "session-stream database is unavailable" in d.json()["detail"]

    # Full app factory regression probe (mirrors reviewer live probe)
    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app = api_main.create_app(ctx, lifespan=no_lifespan)
    with TestClient(app) as factory_client:
        s = factory_client.get("/api/session-streams/v1/status/epic:4707")
        assert s.status_code == 404
        assert "session-stream database is unavailable" in s.json()["detail"]

        d = factory_client.get("/api/session-streams/v1/digest/epic:4707")
        assert d.status_code == 404
        assert "session-stream database is unavailable" in d.json()["detail"]


def test_status_and_digest_serve_the_epics_safe_projection(monkeypatch) -> None:
    """Defect class: raw session-stream entries and holder fields served past the epics API's safe projection."""
    from types import SimpleNamespace  # lazy-ok: only this test builds fakes

    from lu_machinery.api import session_streams_router as module  # lazy-ok: monkeypatch target
    from lu_machinery.session_streams.handoff import HandoffStatus  # lazy-ok: fake status

    private = "/Users/alice/private"
    entry = {
        "entry_id": 1,
        "stream": "epic:7178",  # allow-hardcoded-epic: projection fixture
        "session_id": "session",
        "agent": "codex",
        "harness": "codex-cli",
        "idempotency_key": "key",
        "body": "safe",
        "refs": [{"kind": "source", "uri": f"{private}.txt", "target_entry_id": None}],
    }
    digest = SimpleNamespace(pinned=[entry], recent=[{**entry, "entry_id": 2, "agent": private}])
    store = SimpleNamespace(load_digest=lambda stream_id, *, limit: digest)
    status = HandoffStatus(
        stream_id="epic:7178",  # allow-hardcoded-epic: projection fixture
        session_id="session",
        session_state="open",
        lease_state="active",
        lease_expired=False,
        holder_agent=private,
        holder_harness="codex-cli",
        holder_instance_id="instance",
        holder_kind="process",
        holder_process_id=1,
        holder_process_alive=True,
        expires_at="2099-01-01T00:00:00Z",
        heartbeat_at="2099-01-01T00:00:00Z",
        claimable_force_close=False,
        reason=f"remote lease held by {private}/codex-cli",
    )
    monkeypatch.setattr(module, "_store", lambda ctx: store)
    monkeypatch.setattr(module, "entry_as_dict", lambda value: dict(value))
    monkeypatch.setattr(module, "diagnose_handoff", lambda store, stream_id: status)
    client = _client()

    digest_body = client.get("/api/session-streams/v1/digest/epic:7178").json()
    assert private not in json.dumps(digest_body)
    assert digest_body["pinned"][0]["refs"] == []
    assert digest_body["recent"][0]["agent"] == "[redacted]"
    status_body = client.get("/api/session-streams/v1/status/epic:7178").json()
    assert private not in json.dumps(status_body)
    assert (status_body["holder_agent"], status_body["reason"]) == ("[redacted]", "[redacted]")
    assert status_body["holder_harness"] == "codex-cli"

    # The canonical stream-ID rule, not a prefix check, admits the path.
    for bad in ("epic:abc", "epic:7178x", "epic:"):
        assert client.get(f"/api/session-streams/v1/status/{bad}").status_code == 400, bad
        assert client.get(f"/api/session-streams/v1/digest/{bad}").status_code == 400, bad
