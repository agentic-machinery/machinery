"""#7486: /api/comms/v1 metric-family routes follow the plane/storage switch."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lu_machinery.api import main as api_main
from lu_machinery.api.monitor_context import fixture_context

_ROUTES = ("/api/comms/v1/metrics",)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(api_main.create_app(fixture_context(tmp_path)))


def test_authority_mode_reads_plane_not_legacy_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")
    client = _client(tmp_path)
    for route in _ROUTES:
        payload = client.get(route).json()
        assert payload["source"] == "authority", route
        # Fixture has no plane db — fail-open db_missing, never legacy data.
        assert payload.get("db_missing") is True, route
        assert payload["store"]["kind"] == "comms-plane", route


def test_exporter_refresh_never_blocks_scrape_or_exports_expired_counts(monkeypatch, tmp_path):
    """A slow/failed refresh must not pin a scrape or label old counts as healthy."""
    import threading
    from datetime import UTC, datetime

    from prometheus_client import CollectorRegistry, generate_latest

    from lu_machinery.api import comms_router as comms
    from lu_machinery.api import state_helpers as cache
    from lu_machinery.api.comms_prometheus import CommsCollector

    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")
    now = [0.0]
    monkeypatch.setattr(cache, "_ttl_clock", lambda: now[0])
    ctx = fixture_context(tmp_path)
    key = comms._metrics_cache_key(ctx)
    cache.cache_invalidate(key)
    entered, release = threading.Event(), threading.Event()
    count = [7]
    calls = []

    def compute(context, include_agent_success):
        calls.append(context)
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(5)
        return datetime.now(UTC).isoformat(), {
            "source": "authority", "store": {"reachable": True},
            "failure_windows": {
                window: {metric: {"total": count[0], "by_recipient": {"codex": count[0]}}
                         for metric in comms._FAILURE_METRICS}
                for window in ("24h", "7d")
            },
        }

    monkeypatch.setattr(comms, "_collect_panel_metrics", compute)
    registry = CollectorRegistry()
    registry.register(CommsCollector(ctx))

    def scrape():
        return generate_latest(registry).decode()

    def finish():
        release.set()
        # Join the shared flight; this must never initiate another collection.
        return cache.cache_get_or_compute(key, comms.COMMS_METRICS_TTL_S,
                                          lambda: pytest.fail("duplicate collection"))

    try:
        cold = scrape()
        assert entered.wait(5)
        assert "lu_fleet_comms_collection_success 0.0" in cold
        assert "lu_fleet_comms_failed_jobs_recent{" not in cold
        assert "lu_fleet_comms_collection_success 0.0" in scrape()
        assert len(calls) == 1
        finish()
        assert 'lu_fleet_comms_failed_jobs_recent{window="24h"} 7.0' in scrape()

        now[0] = 31
        entered.clear()
        release.clear()
        stale = scrape()
        assert entered.wait(5)
        assert 'lu_fleet_comms_failed_jobs_recent{window="24h"} 7.0' in stale
        assert "lu_fleet_comms_snapshot_age_seconds 31.0" in stale
        count[0] = 9
        finish()
        assert 'lu_fleet_comms_failed_jobs_recent{window="24h"} 9.0' in scrape()

        now[0] = 152
        release.clear()
        expired = scrape()
        assert "lu_fleet_comms_collection_success 0.0" in expired
        assert "lu_fleet_comms_failed_jobs_recent{" not in expired
    finally:
        release.set()
        finish()
        cache.cache_invalidate(key)


def test_exporter_snapshot_isolates_contexts_and_plane_switch(monkeypatch, tmp_path):
    """A cached authority reading must not bleed into another root or plane."""
    from lu_machinery.api import comms_router as comms
    from lu_machinery.api import state_helpers as cache

    first = fixture_context(tmp_path / "first")
    second = fixture_context(tmp_path / "second")
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "authority")
    key = comms._metrics_cache_key(first)
    try:
        cache.cache_set(key, ("2026-11-17T00:00:00Z", {"source": "authority"}))
        assert comms.cached_panel_metrics(first)[0] == {"source": "authority"}
        assert comms.cached_panel_metrics(second) is None
        monkeypatch.setenv("FLEET_COMMS_ROOT", str(tmp_path / "new-plane"))
        assert comms.cached_panel_metrics(first) is None
        monkeypatch.delenv("FLEET_COMMS_ROOT")
        cache.cache_invalidate(key)
        assert comms.cached_panel_metrics(first) is None
    finally:
        cache.cache_invalidate(key)
