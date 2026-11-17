"""Defect class: a GET that returns 5xx is treated as a passing api verify."""

from __future__ import annotations

import json
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lu_machinery.api.route_contracts import RouteContract
from lu_machinery.ops import verify_run
from tests.helpers.wheel import install_release, release_wheel

_REPO = Path(__file__).resolve().parents[2]
_LOOPBACK = "lu_machinery/packaging/systemd/learn-ukrainian-loopback.target"
_INSTALLED_PROBE = r"""
import json
from pathlib import Path

import lu_machinery.ops.verify_run as verify_run
from lu_machinery.common.repo_root import machinery_path, machinery_root

path = machinery_path("packaging/systemd/learn-ukrainian-loopback.target")
text = verify_run._loopback_text()
module = Path(verify_run.__file__).resolve()
legacy = module.parents[2] / "packaging" / "systemd" / "learn-ukrainian-loopback.target"
root = machinery_root().resolve()
print(json.dumps({
    "path": str(path.resolve()),
    "inside_package": path.resolve().is_relative_to(root),
    "is_file": path.is_file(),
    "legacy_exists": legacy.is_file(),
    "units": verify_run.pinned_units(text),
    "text": text,
}))
"""


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


def test_installed_wheel_ships_loopback_target_inside_the_package(tmp_path: Path) -> None:
    """Defect class: an installed wheel omits the loopback target, so host verify reports it unreadable."""
    wheel = release_wheel(_REPO, tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert _LOOPBACK in names
        assert "packaging/systemd/learn-ukrainian-loopback.target" not in names
        shipped = archive.read(_LOOPBACK)
    # The pytest job removes checkout lu_machinery before the test runs and
    # points LU_TEST_WHEEL at the wheel built from that tree. Compare the
    # installed text with the wheel bytes. When the checkout file is still
    # present, it must be those same bytes.
    checkout = _REPO / _LOOPBACK
    if checkout.is_file():
        assert shipped == checkout.read_bytes()
    python = install_release(wheel, tmp_path / "consumer")
    ran = subprocess.run(
        [str(python), "-I", "-c", _INSTALLED_PROBE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    located = json.loads(ran.stdout)
    assert isinstance(located, dict)
    units = located["units"]
    assert isinstance(units, list) and all(isinstance(unit, str) for unit in units)
    assert located["is_file"] is True
    assert located["inside_package"] is True
    assert located["legacy_exists"] is False
    assert located["text"] == shipped.decode()
    assert located["units"] == [
        "learn-ukrainian-api.service",
        "learn-ukrainian-sources.service",
        "learn-ukrainian-work.service",
        "learn-ukrainian-astro.service",
    ]
    assert located["path"].endswith("/lu_machinery/packaging/systemd/learn-ukrainian-loopback.target")
