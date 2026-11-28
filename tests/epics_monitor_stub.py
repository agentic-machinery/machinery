"""Loopback Monitor epics stub for subprocess launcher acceptance tests."""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Any
from urllib.request import urlopen

import pytest

from lu_machinery.session_supervisor.remote import envelope_digest

if TYPE_CHECKING:
    from fastapi import FastAPI

    from lu_machinery.session_streams.store import SessionStreamStore


def public_lease(lease: Any) -> Any:
    """``lease`` (an exact envelope plus state fields) as Monitor's status read publishes it.

    Fakes that answer ``RemoteEpicClient.stream`` keep the full envelope, so a test can still model "another
    lease" by changing ``session_id``, and answer with this projection (epics_router._lease_payload): no
    ``session_id``, ``lease_id``, ``holder.instance_id`` or ``holder.process_id``, plus ``lease_digest``.
    """
    if not isinstance(lease, dict):
        return lease
    public = {key: value for key, value in lease.items() if key not in ("session_id", "lease_id")}
    holder = lease.get("holder")
    if isinstance(holder, dict):
        public["holder"] = {key: value for key, value in holder.items() if key not in ("instance_id", "process_id")}
    public["lease_digest"] = envelope_digest(lease)
    return public


@asynccontextmanager
async def _stub_lifespan(_app: FastAPI):
    """No schedulers / inventory seed — acceptance stubs only need the router."""
    yield


def epics_app_for_store(
    store: SessionStreamStore,
    root: Path,
    *,
    live_repo_root: Path | None = None,
) -> FastAPI:
    """Build a create_app() Monitor instance whose epics routes read ``store``.

    Goes through ``create_app`` so OPSEC path-sanitizer middleware (and the
    rest of the production stack) actually runs — the prior bare-FastAPI
    bypass was #7494 item 4.8a.
    """
    # The Monitor app is imported only when a stub is built, so ``public_lease`` stays importable alone.
    from lu_machinery.api.main import create_app
    from lu_machinery.api.monitor_context import fixture_context
    from lu_machinery.api.product_contribution import ProductContribution

    ctx = fixture_context(root)
    if live_repo_root is not None:
        ctx = ctx.with_roots(live_repo_root=Path(live_repo_root))
    ctx = replace(ctx, stores=replace(ctx.stores, epics_store=store))
    return create_app(ctx, lifespan=_stub_lifespan, product=ProductContribution())


@contextmanager
def epics_monitor_stub(store: SessionStreamStore) -> Iterator[str]:
    """Serve the production epics router on an owned loopback port.

    The router is the same contract exercised in ``tests/api/test_epics_router.py``;
    this wrapper adds a real socket so launcher subprocesses cannot bypass the
    remote lifecycle client with an in-process test adapter.
    """
    import uvicorn

    app = epics_app_for_store(store, store.database.path.parent)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="off",
            log_level="error",
        )
    )
    thread = Thread(target=server.run, name="epics-monitor-stub", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    health_url = f"{base_url}/api/epics/v1/health"
    try:
        for _ in range(100):
            try:
                with urlopen(health_url, timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("Monitor epics stub did not become ready")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def isolated_monitor_state(monkeypatch):
    """Restore Monitor providers and cached roots after an explicitly opted-in test."""
    from lu_machinery.api import monitor_context, product_contribution

    with monkeypatch.context() as patch:
        for name in ("_atlas", "_fleet_batches", "_product"):
            patch.setattr(product_contribution, name, getattr(product_contribution, name))
        try:
            yield
        finally:
            config = sys.modules.get("lu_machinery.api.config")
            if config is not None:
                for name in (
                    "PROJECT_ROOT", "PRODUCT_ROOT", "LIVE_REPO_ROOT", "CURRICULUM_ROOT",
                    "DASHBOARDS_DIR", "BATCH_STATE_DIR",
                ):
                    vars(config).pop(name, None)
            monitor_context._cached_production_context.cache_clear()
