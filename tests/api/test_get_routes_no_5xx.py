"""Every parameter-free GET on a fresh checkout must answer without a 5xx.

Defect class: a plain GET against a checkout that has ``origin/main`` and no
``FETCH_HEAD``, or ``/artifacts/`` when neither the wheel nor the product ships
an artifacts page, returned HTTP 500 (#722, releases 0.1.27 and 0.1.28). The
clean-machine drill's API probe and ``verify_run`` are what request these
routes on a release. This test requests that same set through the app's test
client, on a fixture, with no network and no live service.

The drill's set is every OpenAPI path that has a GET and no ``{``.
``verify_run`` adds the page URLs and the concrete route-contract paths. A
contract path that is not itself a GET (a POST the smoke still requests) stays
in the set, because that is what ``verify_run`` calls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import config, epics_router
from lu_machinery.api.epics_router import seed_manifest_inventory
from lu_machinery.api.fleet_workers_collect import reset_workers_payload_cache
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
    "MESSAGE_DB",
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
    }
    previous_env = {key: os.environ.get(key) for key in offline}
    os.environ.update(offline)
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
            yield client, repo
    finally:
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
    fresh_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect class: plain GET 500 on a never-fetched checkout or a missing artifacts page (#722)."""
    if route in _SKIPS:
        pytest.skip(_SKIPS[route])
    client, repo = fresh_client
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
    response = client.get(route)
    assert response.status_code < 500, f"{route} -> HTTP {response.status_code}"
