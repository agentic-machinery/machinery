"""Every parameter-free GET on a fresh checkout must answer without a 5xx.

Defect class: a plain GET against a checkout that has ``origin/main`` and no
``FETCH_HEAD``, or ``/artifacts/`` when neither the wheel nor the product ships
an artifacts page, returned HTTP 500 (#722, releases 0.1.27 and 0.1.28). The
clean-machine drill's API probe and ``verify_run`` are what request these
routes on a release. This test requests that same set through the app's test
client, on a fixture, with no network and no live service. TCP connects,
including loopback, are refused for the fixture's lifetime, so a handler
cannot reach Alertmanager, the live Monitor, or the sources port.

The drill's set is every OpenAPI path that has a GET and no ``{``.
``verify_run`` adds the page URLs and the concrete route-contract paths. A
contract path that is not itself a GET (a POST the smoke still requests) stays
in the set, because that is what ``verify_run`` calls.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import config, docs_router, epics_router
from lu_machinery.api.epics_router import seed_manifest_inventory
from lu_machinery.api.fleet_workers_collect import reset_workers_payload_cache
from lu_machinery.api.fleet_workers_router import workers_snapshot
from lu_machinery.api.main import create_app
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.api.occupancy import PRODUCTION_LINUX_HOST_ID
from lu_machinery.api.product_contribution import ProductContribution
from lu_machinery.api.project_state_router import reset_local_document_cache
from lu_machinery.api.route_contracts import all_page_contracts, all_route_contracts
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.ops.verify_run import select_api_targets
from lu_machinery.orchestration import issue_stream_audit

# The two routes the drill caught. Discovery fails closed if either is gone.
_CAUGHT = ("/api/fleet/projects/v1", "/artifacts/")

# A route that cannot answer without a live external service is named here,
# with the reason, after claude-cto approves that name. None today: each route
# answers from the fixture.
_SKIPS: dict[str, str] = {}

_CONFIG_ROOTS = (
    "PROJECT_ROOT",
    "PRODUCT_ROOT",
    "LIVE_REPO_ROOT",
    "CURRICULUM_ROOT",
    "DASHBOARDS_DIR",
    "BATCH_STATE_DIR",
)


@asynccontextmanager
async def _no_lifespan(_app: FastAPI) -> Iterator[None]:
    """Skip the production lifespan. It warms GitHub and other live services."""
    yield


def _never_fetched(repo: Path) -> None:
    """A checkout with ``origin/main`` and no ``FETCH_HEAD``, the fresh-clone shape."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for command in (
        ["init", "-q", "-b", "main"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(
            ["git", "-C", str(repo), *command],
            check=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
    if (repo / ".git" / "FETCH_HEAD").is_file():
        raise RuntimeError("fresh checkout wrote FETCH_HEAD")


def _hold_audit_cooldown(repo: Path) -> None:
    """Park the audit scheduler inside its cooldown so the request does not spawn a worker.

    The suite redirects the refresh sidecar to a fresh temp directory for each
    test. A cooldown is honored only after ``last_outcome: failed``. Write only
    there, or under this fixture. Never touch another checkout's sidecar.
    """
    state, _lock = issue_stream_audit.refresh_sidecar_paths()
    if not (state.is_relative_to(repo) or state.parent.name.startswith("issue-audit-refresh-")):
        return
    state.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": None,
                "phase": "idle",
                "requested_at": None,
                "started_at": None,
                "last_outcome": "failed",
                "last_outcome_at": now,
                "failure_code": "spawn_failed",
                "cooldown_until": now + 86400,
            }
        ),
        encoding="utf-8",
    )


def _offline_bin(directory: Path) -> None:
    """``gh`` and ``lsof`` that fail at once, so a route cannot reach a live service."""
    directory.mkdir()
    for name in ("gh", "lsof"):
        script = directory / name
        script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        script.chmod(0o755)


# The handler dials this port and no other. The log is per request: CI splits
# the cases across workers, and a module-wide set would hide a second dial of
# a port an earlier case already recorded.
_ROUTE_PORTS = {
    "/api/ops/v1/alerts": 9093,
    "/api/orient": 8766,
}


class _OfflineSockets:
    """Refuse TCP connects for the fixture's lifetime. Unix sockets still work.

    The route handlers stay the real ones. A refused connect is the offline
    result they already return (unreachable, probe skipped, port closed).
    """

    def __init__(self) -> None:
        self.dialed: list[int] = []
        self.completed: list[object] = []
        self._connect = socket.socket.connect
        self._connect_ex = socket.socket.connect_ex
        self._create = socket.create_connection

    def install(self) -> None:
        guard = self

        def connect(sock: socket.socket, address: object) -> object:
            return guard._refuse_connect(sock, address)

        def connect_ex(sock: socket.socket, address: object) -> int:
            return guard._refuse_connect_ex(sock, address)

        def create(address: object, *_args: object, **_kwargs: object) -> socket.socket:
            return guard._refuse_create(address)

        # Plain functions, not bound methods: a bound method would drop the socket.
        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
        socket.create_connection = create  # type: ignore[method-assign]

    def restore(self) -> None:
        socket.socket.connect = self._connect  # type: ignore[method-assign]
        socket.socket.connect_ex = self._connect_ex  # type: ignore[method-assign]
        socket.create_connection = self._create  # type: ignore[method-assign]

    def _note(self, address: object) -> None:
        if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[1], int):
            self.dialed.append(address[1])

    def _unix(self, sock: socket.socket, address: object) -> bool:
        if isinstance(address, (str, bytes)) or address is None:
            return True
        return getattr(sock, "family", None) == getattr(socket, "AF_UNIX", None)

    def _refuse_connect(self, sock: socket.socket, address: object) -> object:
        if self._unix(sock, address):
            return self._connect(sock, address)
        self._note(address)
        raise OSError(errno.ECONNREFUSED, "offline fixture refused the connection")

    def _refuse_connect_ex(self, sock: socket.socket, address: object) -> int:
        if self._unix(sock, address):
            return self._connect_ex(sock, address)
        self._note(address)
        return errno.ECONNREFUSED

    def _refuse_create(self, address: object, *_args: object, **_kwargs: object) -> socket.socket:
        self._note(address)
        raise OSError(errno.ECONNREFUSED, "offline fixture refused the connection")


def _parameter_free_gets(app: FastAPI) -> list[str]:
    document = app.openapi()
    paths = document.get("paths") if isinstance(document, dict) else None
    if not isinstance(paths, dict) or not paths:
        raise RuntimeError("openapi has no paths")
    gets = [
        path
        for path, item in paths.items()
        if isinstance(path, str) and isinstance(item, dict) and "get" in item and "{" not in path
    ]
    product = app.state.product
    pages = [page.url for page in all_page_contracts(product)]
    contracts = list(all_route_contracts(product))
    chosen = set(gets) | set(select_api_targets(pages, contracts, gets))
    missing = [path for path in _CAUGHT if path not in chosen]
    if missing:
        raise RuntimeError(f"route discovery missed {missing}")
    return sorted(chosen)


def _discovered_routes() -> list[str]:
    with tempfile.TemporaryDirectory(prefix="routesmoke-list-") as raw:
        app = create_app(
            fixture_context(Path(raw)),
            lifespan=_no_lifespan,
            product=ProductContribution(),
        )
        return _parameter_free_gets(app)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "route" in metafunc.fixturenames:
        metafunc.parametrize("route", _discovered_routes())


@pytest.fixture(scope="module")
def fresh_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, Path]]:
    """App bound to an empty product, shipped dashboards, and a never-fetched checkout."""
    repo = tmp_path_factory.mktemp("never-fetched")
    _never_fetched(repo)
    dashboards = repo / "dashboards"
    shutil.copytree(machinery_path("api/dashboards"), dashboards)
    # The wheel ships no artifacts page. Keep it absent so /artifacts/ still
    # exercises the missing-page path the drill caught.
    artifacts_page = dashboards / "artifacts.html"
    if artifacts_page.is_file():
        artifacts_page.unlink()
    (repo / "batch_state").mkdir()
    _offline_bin(repo / "bin")
    # Held for the whole module. A request starts daemon threads that resolve
    # gh after the test that spawned them has restored its own monkeypatch.
    offline = {
        "SVC_LSOF_BIN": str(repo / "bin" / "lsof"),
        "AGENT_REAL_GH": str(repo / "bin" / "gh"),
        "PATH": str(repo / "bin") + os.pathsep + os.environ.get("PATH", ""),
        # urllib sends localhost through an inherited proxy, so the guard would
        # see the proxy port instead of the Monitor. Bypass the proxy for the
        # hosts the handlers dial.
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
        # The workers snapshot reads the product root while the fixture is
        # still starting, before the test body can point it at this checkout.
        "LU_PRODUCT_ROOT": str(repo),
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(repo),
        "LEARN_UKRAINIAN_BATCH_STATE_DIR": str(repo / "batch_state"),
    }
    previous_env = {key: os.environ.get(key) for key in offline}
    os.environ.update(offline)
    for name in _CONFIG_ROOTS:
        vars(config).pop(name, None)
    sockets = _OfflineSockets()
    sockets.install()
    previous_health: dict[str, object] | None = None
    previous_snapshot: str | None = None
    try:
        app = create_app(fixture_context(repo), lifespan=_no_lifespan, product=ProductContribution())
        with epics_router._REGISTRY_HEALTH_LOCK:
            previous_health = dict(epics_router._REGISTRY_HEALTH)
            previous_snapshot = epics_router._REGISTRY_SNAPSHOT_SHA256
        seed_manifest_inventory(
            repo,
            store=app.state.ctx.stores.epics_store,
            handoff_root=repo,
            ctx=app.state.ctx,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            # The workers map is a lifespan snapshot. Its first collect can
            # outlast the startup wait, and the route then answers 503.
            workers_snapshot(app.state.ctx).refresh()
            yield client, repo, sockets
    finally:
        sockets.restore()
        reset_local_document_cache()
        reset_workers_payload_cache()
        if previous_health is not None:
            with epics_router._REGISTRY_HEALTH_LOCK:
                epics_router._REGISTRY_HEALTH.clear()
                epics_router._REGISTRY_HEALTH.update(previous_health)
                epics_router._REGISTRY_SNAPSHOT_SHA256 = previous_snapshot
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_parameter_free_get_on_a_never_fetched_checkout_is_not_5xx(
    route: str,
    fresh_client: tuple[TestClient, Path, _OfflineSockets],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class: plain GET 500 on a never-fetched checkout or a missing artifacts page (#722)."""
    if route in _SKIPS:
        pytest.skip(_SKIPS[route])
    client, repo, sockets = fresh_client
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(repo))
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(repo))
    monkeypatch.setenv("LEARN_UKRAINIAN_BATCH_STATE_DIR", str(repo / "batch_state"))
    monkeypatch.setenv("LU_MONITOR_HOST_ID", PRODUCTION_LINUX_HOST_ID)
    monkeypatch.delenv("MONITOR_OCCUPANCY_HOST_IDS", raising=False)
    monkeypatch.setenv("SVC_LSOF_BIN", str(repo / "bin" / "lsof"))
    # The GitHub client pins AGENT_REAL_GH, so a PATH stand-in is not enough.
    monkeypatch.setenv("AGENT_REAL_GH", str(repo / "bin" / "gh"))
    monkeypatch.setenv("PATH", str(repo / "bin") + os.pathsep + os.environ.get("PATH", ""))
    for name in _CONFIG_ROOTS:
        vars(config).pop(name, None)
    monkeypatch.chdir(repo)
    reset_local_document_cache()
    _hold_audit_cooldown(repo)
    start = len(sockets.dialed)
    response = client.get(route)
    assert response.status_code < 500, f"{route} -> HTTP {response.status_code} {response.text[:180]}"
    assert not sockets.completed, f"{route} opened a connection: {sockets.completed}"
    expected = _ROUTE_PORTS.get(route)
    if expected is not None:
        dialed = sockets.dialed[start:]
        assert set(dialed) == {expected}, f"{route} dialed {dialed}, expected port {expected}"


def test_artifacts_listing_is_paginated(tmp_path: Path) -> None:
    """Defect class: a listing returns everything in one response, or its pager skips, repeats or breaks links."""
    reports = tmp_path / "docs" / "reports"
    reports.mkdir(parents=True)
    page = docs_router.LISTING_PAGE_SIZE
    names = [f"report-{number:04}.md" for number in range(2 * page + 5)]
    for name in (*names, ".hidden.md"):
        (reports / name).write_text("# report\n", encoding="utf-8")
    ctx = fixture_context(tmp_path).with_roots(effective_roots={"docs/reports": reports})
    app = create_app(ctx, lifespan=_no_lifespan, product=ProductContribution())
    client = TestClient(app)

    for first in ("/artifacts/docs/reports?format=json", "/files/docs/reports"):
        url = first
        pages = [client.get(url).json()]
        urls = [url]
        while pages[-1]["next"] is not None:
            url = urljoin(url, pages[-1]["next"])
            pages.append(client.get(url).json())
            urls.append(url)
        assert [(row["offset"], row["items_count"], row["total"]) for row in pages] == [
            (0, page, len(names)), (page, page, len(names)), (2 * page, 5, len(names)),
        ]
        assert [item["name"] for row in pages for item in row["items"]] == names
        assert pages[0]["previous"] is None
        for cur_url, later, earlier in zip(urls[:0:-1], pages[:0:-1], pages[-2::-1], strict=True):
            assert client.get(urljoin(cur_url, later["previous"])).json()["items"] == earlier["items"]

    # Pages sit on one grid: "previous" then "next" restores the same page at the same size.
    mid_url = "/files/docs/reports?limit=3&offset=6"
    middle = client.get(mid_url).json()
    prev_url = urljoin(mid_url, middle["previous"])
    prev_page = client.get(prev_url).json()
    restored = client.get(urljoin(prev_url, prev_page["next"])).json()
    assert restored == middle
    # Past the end, aligned: "previous" is the last page with rows (205 rows end at offset 204).
    for offset in (207, 999):
        beyond_url = f"/files/docs/reports?limit=3&offset={offset}"
        beyond = client.get(beyond_url).json()
        back = client.get(urljoin(beyond_url, beyond["previous"])).json()
        assert (beyond["items_count"], beyond["next"], back["offset"]) == (0, None, 204), offset
        assert [item["name"] for item in back["items"]] == names[204:], offset
    # An offset off the grid is refused with its reason on both mounts, never served as a page of another size.
    for base in ("/files/docs/reports?", "/artifacts/docs/reports?format=json&"):
        for query in ("limit=3&offset=1", "limit=3&offset=4", "offset=1"):
            refused = client.get(f"{base}{query}")
            assert (refused.status_code, refused.json()["detail"]) == (422, "offset must be a multiple of limit"), (base, query)

    for query in (f"limit={docs_router.LISTING_PAGE_MAX + 1}", "limit=0", "offset=-1"):
        assert client.get(f"/files/docs/reports?{query}").status_code == 422, query

    complete = docs_router.collect_artifacts(ctx=ctx)
    assert (complete["total"], len(complete["artifacts"])) == (len(names), len(names))
    parts = [docs_router.collect_artifacts(limit=page, offset=at, ctx=ctx) for at in range(0, len(names), page)]
    assert [part["total"] for part in parts] == [len(names)] * 3
    assert [row for part in parts for row in part["artifacts"]] == complete["artifacts"]
    unaligned = docs_router.collect_artifacts(limit=3, offset=1, ctx=ctx)
    assert (unaligned["total"], unaligned["artifacts"]) == (len(names), complete["artifacts"][1:4])
    with pytest.raises(ValueError, match="limit must be at least 1"):
        docs_router.collect_artifacts(limit=0, ctx=ctx)

    # Query-only relative links survive production OPSEC sanitization on delimiter/root-like paths
    # and never echo arbitrary extra query parameters.
    for rel_dir in ("Q?A #1 100%", "Résumé/tmp/notes", "100%/home/notes"):
        sub = reports / rel_dir
        sub.mkdir(parents=True)
        for number in range(3):
            (sub / f"{number}.md").write_text("# report\n", encoding="utf-8")
        encoded_rel = "/".join(quote(part, safe="") for part in rel_dir.split("/"))
        for prefix, fmt in (("/files", None), ("/artifacts", "json")):
            fmt_q = f"format={fmt}&" if fmt else ""
            cur = f"{prefix}/docs/reports/{encoded_rel}?{fmt_q}limit=1&offset=1&extra=/tmp/leak"
            resp = client.get(cur)
            assert resp.status_code == 200, (cur, resp.text)
            body = resp.json()
            assert body["previous"] == f"?{fmt_q}limit=1&offset=0", (cur, body["previous"])
            assert body["next"] == f"?{fmt_q}limit=1&offset=2", (cur, body["next"])
            for link, expected_offset, expected_name in (
                (body["previous"], 0, "0.md"),
                (body["next"], 2, "2.md"),
            ):
                follow = client.get(urljoin(cur, link))
                assert follow.status_code == 200, (cur, link, follow.text)
                followed = follow.json()
                assert (followed["offset"], [item["name"] for item in followed["items"]]) == (
                    expected_offset, [expected_name],
                )
