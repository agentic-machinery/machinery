"""Regression coverage for routing-budget event-loop scans and compute age."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx2 as httpx
from fastapi import FastAPI

from lu_machinery.api import state_helpers, state_router
from lu_machinery.api.lane_health import LaneHealthScan
from lu_machinery.api.monitor_context import fixture_context


def _app(monkeypatch, tmp_path, compute):
    monkeypatch.setattr(state_router, "compute_routing_budget", compute)
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(state_router.router, prefix="/api/state")
    return app


@asynccontextmanager
async def _saturated_executor():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    entered = asyncio.Event()
    release = threading.Event()

    def blocker():
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    blocked = loop.run_in_executor(None, blocker)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        yield blocked
    finally:
        release.set()
        await blocked


def test_warm_lane_error_never_scans_tasks_on_event_loop(monkeypatch, tmp_path):
    """Warm scans cannot wait on the pool or overwrite a newer snapshot's errors."""
    scan_threads = []
    scan_workers = []
    refreshed = {"value": False}
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: 1000.0)

    def agents():
        if refreshed["value"]:
            return {
                "codex": {"health": {"healthy": True}},
                "claude": {"health": {"healthy": False, "last_error": "new failure"}},
            }
        return {
            "codex": {"health": {"healthy": False, "last_error": "previous failure"}},
            "claude": {"health": {"healthy": True}},
        }

    def compute(**kwargs):
        kwargs["task_generation"].append(state_router._tasks_content_token(kwargs["tasks_dir"]))
        return {
            "agents": agents(),
            "diagnostics": {"data_age_s": 0, "stale": False, "stale_threshold_s": 900},
        }

    app = _app(monkeypatch, tmp_path, compute)

    async def scenario():
        loop_thread = threading.get_ident()
        loop = asyncio.get_running_loop()
        scan_started = asyncio.Event()
        scan_done = asyncio.Event()
        release_scan = threading.Event()

        def scan(_tasks_dir):
            scan_threads.append(threading.get_ident())
            scan_workers.append(threading.current_thread())
            records = {lane: info["health"] for lane, info in agents().items()}
            loop.call_soon_threadsafe(scan_started.set)
            try:
                release_scan.wait()
                return LaneHealthScan(observed=True, records=records)
            finally:
                loop.call_soon_threadsafe(scan_done.set)

        monkeypatch.setattr(state_router, "scan_lane_health", scan)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            cold = await client.get("/api/state/routing-budget")
            assert cold.status_code == 200
            try:
                async with _saturated_executor() as blocked:
                    for _ in range(2):
                        warm = await asyncio.wait_for(client.get("/api/state/routing-budget"), timeout=5)
                        assert warm.status_code == 200
                        assert warm.json()["agents"]["codex"]["health"]["reason_code"] == "provider_error"
                        assert not blocked.done()
                    await asyncio.wait_for(scan_started.wait(), timeout=5)
                    assert not scan_done.is_set()
                    assert len(scan_threads) == 1, "concurrent warm reads must coalesce the scan"
            finally:
                release_scan.set()
                await asyncio.wait_for(scan_done.wait(), timeout=5)
                await asyncio.to_thread(scan_workers[-1].join, 5)
                assert not scan_workers[-1].is_alive()

            # Read the completed scan on the same snapshot before replacing it.
            scan_started.clear()
            completed = await client.get("/api/state/routing-budget")
            assert completed.json()["agents"]["codex"]["health"] == {
                "healthy": False, "reason_code": "provider_error",
            }
            assert completed.json()["agents"]["claude"]["health"] == {"healthy": True}
            await asyncio.wait_for(scan_started.wait(), timeout=5)
            await asyncio.to_thread(scan_workers[-1].join, 5)
            assert not scan_workers[-1].is_alive()

            # Publish a newer routing snapshot with reversed lane health. The
            # previous completed error scan remains in the shared TTL cache.
            # The clock stays fixed, so timestamps cannot stand in for identity.
            refreshed["value"] = True
            ctx = app.state.ctx
            newer_snapshot = await asyncio.to_thread(state_router._routing_budget_compute(ctx, "dispatch", False))
            key = state_router._ctx_cache_key(ctx, "routing-budget", "dispatch", 0)
            state_helpers.cache_set(key, newer_snapshot)
            scan_started.clear()
            scan_done.clear()
            release_scan.clear()
            try:
                async with _saturated_executor() as blocked:
                    newer = await asyncio.wait_for(client.get("/api/state/routing-budget"), timeout=5)
                    assert newer.status_code == 200
                    assert newer.json()["agents"]["codex"]["health"] == {"healthy": True}
                    assert newer.json()["agents"]["claude"]["health"] == {
                        "healthy": False, "reason_code": "provider_error",
                    }
                    assert not blocked.done()
                    await asyncio.wait_for(scan_started.wait(), timeout=5)
            finally:
                release_scan.set()
                await asyncio.wait_for(scan_done.wait(), timeout=5)
                await asyncio.to_thread(scan_workers[-1].join, 5)
                assert not scan_workers[-1].is_alive()
            scan_started.clear()
            completed = await client.get("/api/state/routing-budget")
            assert completed.json()["agents"]["codex"]["health"] == {"healthy": True}
            assert completed.json()["agents"]["claude"]["health"] == {
                "healthy": False, "reason_code": "provider_error",
            }
            await asyncio.wait_for(scan_started.wait(), timeout=5)
            await asyncio.to_thread(scan_workers[-1].join, 5)
            assert not scan_workers[-1].is_alive()
        assert scan_threads, "live lane errors were never refreshed"
        assert loop_thread not in scan_threads, "warm lane-error scan blocked the event loop"

    asyncio.run(scenario())


def test_slow_compute_duration_counts_toward_served_age(monkeypatch, tmp_path):
    """Compute time must not permit a source older than 900 seconds to authorize."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: clock["t"])

    def compute(**kwargs):
        kwargs["task_generation"].append(state_router._tasks_content_token(kwargs["tasks_dir"]))
        clock["t"] += 20
        return {
            "agents": {"codex": {"eligible": True, "status": "cool"}},
            "recommendation": {"primary_agent_for_code": "codex", "warnings": []},
            "diagnostics": {"data_age_s": 895, "stale": False, "stale_threshold_s": 900},
            "ranked_by_headroom": [{"lane": "codex"}],
        }

    app = _app(monkeypatch, tmp_path, compute)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            cold = await client.get("/api/state/routing-budget")
            assert cold.status_code == 200
            clock["t"] += 3
            async with _saturated_executor() as blocked:
                warm = await asyncio.wait_for(client.get("/api/state/routing-budget"), timeout=5)
                assert warm.status_code == 200
                assert not blocked.done()
        for response, expected_age in ((cold, 915.0), (warm, 918.0)):
            body = response.json()
            assert body["diagnostics"] == {
                "data_age_s": expected_age, "stale": True, "stale_threshold_s": 900,
            }
            assert body["recommendation"]["primary_agent_for_code"] is None
            assert body["agents"]["codex"]["eligible"] is False
            assert body["ranked_by_headroom"] == []

    asyncio.run(scenario())


def test_startup_cold_read_answers_typed_503_within_the_request_timeout(monkeypatch, tmp_path):
    """Defect class: right after a restart the cold routing-budget read awaited the startup
    warmup's computation with no bound, so the request middleware cut it off with a 504.
    While the first attempt runs, even with the default executor saturated, the read answers
    the typed warming 503 with Retry-After inside the 2 s client bound, then 200 once it lands.
    Only the context that started the warmup is starting; a running server waits longer."""
    from lu_machinery.api import resilience

    monkeypatch.delenv("API_REQUEST_TIMEOUT_S", raising=False)  # the default 10 s limit
    release = threading.Event()

    def compute(**kwargs):
        assert release.wait(30), "the test did not release the startup computation"
        kwargs["task_generation"].append(state_router._tasks_content_token(kwargs["tasks_dir"]))
        return {"agents": {}, "diagnostics": {"data_age_s": 0, "stale": False, "stale_threshold_s": 900}}

    app = _app(monkeypatch, tmp_path, compute)
    app.middleware("http")(resilience.resilience_middleware)
    ctx = app.state.ctx
    warmup = state_router.schedule_state_scan_warmup(ctx)  # what the lifespan starts at process start
    running_wait = resilience.request_timeout_s() - state_router.ROUTING_BUDGET_RESPONSE_RESERVE_S
    # Another context on the same root that would reuse this thread is not starting.
    assert state_router._routing_budget_cold_wait_s(fixture_context(tmp_path)) == running_wait

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            try:
                async with _saturated_executor():
                    started = asyncio.get_running_loop().time()
                    cold = await client.get("/api/state/routing-budget")
                    elapsed = asyncio.get_running_loop().time() - started
            finally:
                release.set()
            assert cold.status_code == 503, cold.text
            assert cold.json() == {"detail": "routing budget warming"}
            assert cold.headers["Retry-After"] == "2"
            assert cold.headers["X-Snapshot-Age-S"] == "unknown"
            assert elapsed < 2.0
            await asyncio.to_thread(warmup.join, 30)
            assert not warmup.is_alive()
            assert state_router._routing_budget_cold_wait_s(ctx) == running_wait
            assert (await client.get("/api/state/routing-budget")).status_code == 200

    asyncio.run(scenario())
