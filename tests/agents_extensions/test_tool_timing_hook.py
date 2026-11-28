"""Contract tests for the fire-and-forget tool-timing hook."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lu_machinery.api.telemetry_router import ToolTimingIngest
from lu_machinery.common.repo_root import machinery_path

_ROOT = Path(__file__).resolve().parents[2]
_HOOK = machinery_path("agents_extensions/shared/hooks/tool-timing.sh")


@pytest.mark.parametrize(
    "event_result",
    [
        {"hook_event_name": "PostToolUseFailure"},
        {
            "hook_event_name": "PostToolUse",
            "tool_response": {"exit_code": 1},
        },
    ],
)
def test_hook_posts_json_that_validates_against_tool_timing_model(
    event_result: dict[str, object],
) -> None:
    posted: list[dict[str, object]] = []
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers["Content-Length"])
            posted.append(json.loads(self.rfile.read(content_length)))
            self.send_response(200)
            self.end_headers()
            received.set()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        event: dict[str, object] = {
            "tool_name": 'Bash "quoted" \\ path\nnext',
            "duration_ms": 12.5,
            "tool_use_id": 'tool "id"',
            "session_id": "session\\id",
            **event_result,
        }
        environment = os.environ.copy()
        environment["TOOL_TIMING_API_URL"] = f"http://127.0.0.1:{server.server_port}/tool-timings"
        result = subprocess.run(
            ["/bin/bash", str(_HOOK)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert received.wait(timeout=3), "hook did not post telemetry"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()

    assert len(posted) == 1
    payload = posted[0]
    model = ToolTimingIngest.model_validate(payload)
    assert model.tool_name == event["tool_name"]
    assert model.duration_ms == 13
    assert model.tool_use_id == event["tool_use_id"]
    assert model.session_id == event["session_id"]
    assert model.failed is True


def _telemetry_app(tmp_path: Path):
    from fastapi import FastAPI  # lazy-ok: only the route tests build an app

    from lu_machinery.api import telemetry_router  # lazy-ok: route under test
    from lu_machinery.api.monitor_context import fixture_context  # lazy-ok: fixture

    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(telemetry_router.router)
    return app, telemetry_router._tool_timings_path(app.state.ctx)


def _timing(**changes: object) -> dict[str, object]:
    return {"ts": "2026-11-28T00:00:00Z", "tool_name": "Bash", "duration_ms": 5, **changes}


def test_tool_timing_write_is_local_only_and_bounded(tmp_path: Path) -> None:
    """Defect class: an unauthenticated or unbounded telemetry write reaching the database."""
    from fastapi.testclient import TestClient  # lazy-ok: route test

    app, db = _telemetry_app(tmp_path)
    local = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    url = "/api/telemetry/tool-timings"

    for remote in (
        TestClient(app),
        TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000), headers={"Origin": "https://evil.example"}),
    ):
        # Refused before the body is read: a body that is not even JSON still gets the guard's 403.
        refused = remote.post(url, content=b"not json", headers={"Content-Type": "application/json"})
        assert refused.status_code == 403
    assert not db.exists()

    assert local.post(url, content=b"x" * 5000, headers={"Content-Type": "application/json"}).status_code == 413
    nested = b"[" * 2000 + b"]" * 2000
    assert local.post(url, content=nested, headers={"Content-Type": "application/json"}).status_code == 422
    for bad in (
        _timing(tool_name="T" * 257),
        _timing(session_id="s" * 257),
        _timing(tool_use_id="u" * 257),
        _timing(duration_ms=2**63),
        _timing(extra="field"),
    ):
        response = local.post(url, json=bad)
        assert response.status_code == 422, bad
    assert not db.exists()
    assert local.post(url, json=_timing(ts=datetime.now(UTC).isoformat())).json() == {"ok": True}
    assert db.is_file()


def test_tool_timing_store_prunes_by_age_and_row_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: telemetry rows that are never pruned grow the database without bound."""
    import sqlite3  # lazy-ok: inspect the fixture database

    from fastapi.testclient import TestClient  # lazy-ok: route test

    from lu_machinery.api import telemetry_router  # lazy-ok: route under test

    monkeypatch.setattr(telemetry_router, "MAX_STORED_ROWS", 3)
    app, db = _telemetry_app(tmp_path)
    local = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    now = datetime.now(UTC)
    stamps = [now - timedelta(days=8), *(now - timedelta(minutes=minutes) for minutes in (4, 3, 2, 1))]
    for stamp in stamps:
        assert local.post("/api/telemetry/tool-timings", json=_timing(ts=stamp.isoformat())).status_code == 200
    with sqlite3.connect(db) as connection:
        kept = [row[0] for row in connection.execute("SELECT ts FROM tool_timings ORDER BY id")]
    assert kept == [telemetry_router._isoformat_z(stamp) for stamp in stamps[-3:]]


def test_tool_timing_read_never_creates_the_database(tmp_path: Path) -> None:
    """Defect class: a GET that creates directories and a database as a side effect."""
    from fastapi.testclient import TestClient  # lazy-ok: route test

    app, db = _telemetry_app(tmp_path)
    client = TestClient(app)
    assert client.get("/api/telemetry/tool-timings").json() == []
    assert client.get("/api/telemetry/tool-timings", params={"tool": "T" * 257}).status_code == 422
    assert client.get("/api/telemetry/tool-timings", params={"window": "1y"}).status_code == 422
    assert not db.exists()
    assert not db.parent.exists()
