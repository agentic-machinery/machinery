"""Defect class: a GET that returns 5xx is treated as a passing api verify."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lu_machinery.api.route_contracts import RouteContract
from lu_machinery.ops import verify_run


def _contract(pattern: str, match: str) -> RouteContract:
    return RouteContract(
        pattern,
        match,  # type: ignore[arg-type]
        "http",
        "purpose",
        "source",
        "freshness",
        (),
        "overlap",
        "risk",
        "keep",
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/broken":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"no")
            return
        if self.path == "/ok":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"yes")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


def test_a_5xx_route_fails_and_a_fast_200_does_not(monkeypatch, tmp_path) -> None:
    """A 5xx must fail the command, and the dashboard pipeline GET must be selected."""
    targets = verify_run.select_api_targets(
        ["/machinery"],
        [
            _contract("/api/dashboard", "prefix"),
            _contract("/api/fleet/facade/board", "exact"),
        ],
        [
            "/api/dashboard/pipeline",
            "/api/fleet/facade/board",
            "/api/dashboard/track/{track_id}",
        ],
    )
    assert "/api/dashboard/pipeline" in targets
    assert "/api/fleet/facade/board" in targets
    assert "/machinery" in targets
    assert all("{" not in target for target in targets)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    monkeypatch.setattr(
        verify_run,
        "discover_api_paths",
        lambda _base, budget=1.0: (["/ok", "/broken"], False),
    )
    receipt_path = tmp_path / "receipt.json"
    try:
        code = verify_run.main(
            ["--area", "api", "--base-url", base, "--receipt", str(receipt_path)]
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["ok"] is False
    checks = {item["target"]: item for item in receipt["areas"][0]["checks"]}
    assert checks["/broken"]["ok"] is False
    assert checks["/broken"]["status"] == 500
    assert checks["/ok"]["ok"] is True
