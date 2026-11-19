"""#7494: /api/orient threads the app's own context — no production bleed."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lu_machinery.api.main as api_main
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.api.product_contribution import OrientSection, ProductContribution
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.guardrails import worktree_containment


def _product(collector, *, ttl=0.0):
    return ProductContribution(orient_sections=lambda: {
        "pipeline": OrientSection(collector, {"summary": {}}, True, ttl, "test-product", after="idle_prs"),
    })


def _join_refresh(ctx):
    key = f"orient_pipeline{api_main._ctx_cache_scope(ctx)}"
    thread = api_main._detached_orient_threads.get(key)
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_fixture_app_orient_git_is_not_production(tmp_path: Path) -> None:
    """A create_app() instance must report ITS root's git state (here: a
    non-repo tmp dir → error/degraded), never the production checkout's."""
    app = api_main.create_app(fixture_context(tmp_path))
    data = TestClient(app).get("/api/orient?sections=git&fresh=true").json()
    git = data.get("git") or {}
    production_head = None
    try:
        import subprocess

        production_head = subprocess.run(
            ["git", "rev-parse", "--short=9", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        pass
    if production_head:
        assert git.get("head") != production_head


def test_no_router_owns_a_private_resolve_context_copy() -> None:
    """#7496: byte-identical per-router _resolve_context copies are gone."""
    import importlib
    import pkgutil

    import lu_machinery.api as api_pkg

    offenders: list[str] = []
    for mod in pkgutil.iter_modules(api_pkg.__path__, api_pkg.__name__ + "."):
        if mod.name.endswith(".monitor_context"):
            continue
        try:
            module = importlib.import_module(mod.name)
        except Exception:
            continue
        if hasattr(module, "_resolve_context"):
            offenders.append(mod.name)
    assert offenders == []


def test_blocked_async_product_refresh_returns_without_waiting_and_is_single_flight(monkeypatch, tmp_path):
    """A blocked async product scan must not hold an orient request or spawn duplicate scans."""
    entered, release = threading.Event(), threading.Event()
    calls = []
    ctx = fixture_context(tmp_path)
    monkeypatch.setattr(api_main, "DETACHED_ORIENT_INLINE_WAIT_S", 0.0)

    async def collect(context):
        calls.append(context)
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        return {"summary": {"total": 123}}

    client = TestClient(api_main.create_app(ctx, product=_product(collect)))
    try:
        cold = client.get("/api/orient?sections=pipeline").json()
        assert entered.wait(5)
        assert cold["pipeline"] == {"summary": {}, "error": "refreshing"}
        assert cold["meta"]["pipeline"]["refreshing"] is True
        again = client.get("/api/orient?sections=pipeline").json()
        assert again["pipeline"]["error"] == "refreshing"
        assert calls == [ctx]
    finally:
        release.set()
        _join_refresh(ctx)


def test_product_zero_ttl_refreshes_without_a_second_cache_and_isolates_roots(tmp_path):
    """A product's downstream cache must not gain an extra TTL or cross-context values."""
    from lu_machinery.api.state_helpers import cache_get_stored

    calls = {}

    async def collect(context):
        root = context.roots.project_root.name
        calls[root] = calls.get(root, 0) + 1
        return {"summary": {"root": root, "revision": calls[root]}}

    first, second = fixture_context(tmp_path / "first"), fixture_context(tmp_path / "second")
    a = TestClient(api_main.create_app(first, product=_product(collect)))
    b = TestClient(api_main.create_app(second, product=_product(collect)))
    try:
        assert a.get("/api/orient?sections=pipeline").json()["pipeline"]["summary"] == {
            "root": "first", "revision": 1,
        }
        data = a.get("/api/orient?sections=pipeline").json()
        assert data["pipeline"]["summary"] == {"root": "first", "revision": 2}
        assert data["meta"]["pipeline"]["stale_after_s"] == 0
        assert data["meta"]["pipeline"]["source"] == "test-product"
        assert data["meta"]["pipeline"]["stale"] is False
        assert cache_get_stored(f"orient_pipeline{api_main._ctx_cache_scope(first)}") is None
        assert b.get("/api/orient?sections=pipeline").json()["pipeline"]["summary"] == {
            "root": "second", "revision": 1,
        }
    finally:
        _join_refresh(first)
        _join_refresh(second)


def test_expired_product_snapshot_is_stale_during_refresh_however_old(monkeypatch, tmp_path):
    """Expiry must refresh real data, never label an old value as fresh, and never drop it for an empty fallback."""
    from lu_machinery.api import state_helpers as cache

    now = [0.0]
    monkeypatch.setattr(cache, "_ttl_clock", lambda: now[0])
    monkeypatch.setattr(api_main, "_orient_clock", lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    ctx = fixture_context(tmp_path)
    revision = [1]

    async def collect(context):
        if revision[0] > 1:
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
        return {"summary": {"revision": revision[0]}}

    client = TestClient(api_main.create_app(ctx, product=_product(collect, ttl=10.0)))
    try:
        original = client.get("/api/orient?sections=pipeline").json()
        assert original["pipeline"]["summary"] == {"revision": 1}
        assert client.get("/api/orient?sections=pipeline").json()["meta"]["pipeline"]["cache"] == "hit"
        now[0] = 11
        revision[0] = 2
        monkeypatch.setattr(api_main, "DETACHED_ORIENT_INLINE_WAIT_S", 0.0)
        stale = client.get("/api/orient?sections=pipeline").json()
        assert entered.wait(5)
        assert stale["pipeline"]["summary"] == {"revision": 1}
        assert stale["meta"]["pipeline"]["stale"] is True
        assert stale["meta"]["pipeline"]["generated_at"] == original["meta"]["pipeline"]["generated_at"]
        release.set()
        _join_refresh(ctx)
        refreshed = client.get("/api/orient?sections=pipeline").json()
        assert refreshed["pipeline"]["summary"] == {"revision": 2}

        # Minutes later, past every TTL: a sparse reader still gets the last
        # value with its own generated_at, labelled stale, not an empty fallback.
        now[0] = 1000
        release.clear()
        entered.clear()
        expired = client.get("/api/orient?sections=pipeline").json()
        assert entered.wait(5)
        assert expired["pipeline"] == {"summary": {"revision": 2}}
        assert expired["meta"]["pipeline"]["stale"] is True
        assert expired["meta"]["pipeline"]["refreshing"] is True
        assert expired["meta"]["pipeline"]["generated_at"] == refreshed["meta"]["pipeline"]["generated_at"]
    finally:
        release.set()
        _join_refresh(ctx)


def test_sparse_reader_gets_an_inline_section_at_once_and_fresh_still_collects_it(monkeypatch, tmp_path):
    """Defect class: a reader minutes apart paid an inline collection (or a join) on every read.

    An inline section with a last value is served at once, labelled stale, while
    one worker refreshes it; ``fresh=true`` still collects it on the request path.
    """
    from lu_machinery.api import state_helpers as cache

    now = [0.0]
    monkeypatch.setattr(cache, "_ttl_clock", lambda: now[0])
    monkeypatch.setattr(api_main, "_orient_clock", lambda: now[0])
    # A request that joined the refresh would outlive the blocked collector.
    monkeypatch.setattr(api_main, "DETACHED_ORIENT_INLINE_WAIT_S", 30.0)
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def collect(context):
        calls.append(threading.current_thread().name)
        if len(calls) == 2:
            entered.set()
            assert release.wait(10)
        return {"revision": len(calls)}

    monkeypatch.setattr(api_main, "_collect_session_hints_orient_data", collect)
    ctx = fixture_context(tmp_path)
    key = f"orient_session_hints{api_main._ctx_cache_scope(ctx)}"
    client = TestClient(api_main.create_app(ctx))
    try:
        first = client.get("/api/orient?sections=session_hints").json()
        assert first["session_hints"] == {"revision": 1}
        assert "stale" not in first["meta"]["session_hints"]

        now[0] = 1000
        sparse = client.get("/api/orient?sections=session_hints").json()
        assert entered.wait(5)
        assert api_main._detached_orient_threads[key].is_alive()
        assert sparse["session_hints"] == {"revision": 1}
        assert sparse["meta"]["session_hints"]["stale"] is True
        assert sparse["meta"]["session_hints"]["refreshing"] is True
        assert sparse["meta"]["session_hints"]["generated_at"] == first["meta"]["session_hints"]["generated_at"]
        release.set()
        api_main._detached_orient_threads[key].join(timeout=5)
        assert client.get("/api/orient?sections=session_hints").json()["session_hints"] == {"revision": 2}

        now[0] = 2000
        fresh = client.get("/api/orient?sections=session_hints&fresh=true").json()
        assert fresh["session_hints"] == {"revision": 3}
        assert "stale" not in fresh["meta"]["session_hints"]
        assert calls[2].startswith("orient-sync")
    finally:
        release.set()
        thread = api_main._detached_orient_threads.get(key)
        if thread is not None:
            thread.join(timeout=5)


def test_fresh_read_supersedes_only_its_own_apps_refreshes_and_never_labels_them_current(monkeypatch, tmp_path):
    """Defect class: a fresh read let an older refresh read as current, or discarded another app's refresh.

    A zero-TTL section's waiting read gets the value its own refresh published,
    labelled current, when a fresh read in another app context lands meanwhile.
    When a fresh read in the same app supersedes that refresh, nothing it
    collected is stored and the older value it returns is labelled stale.
    """
    monkeypatch.setattr(api_main, "DETACHED_ORIENT_INLINE_WAIT_S", 10.0)
    entered, release = threading.Event(), threading.Event()
    revision = [0]

    async def collect(context):
        revision[0] += 1
        if revision[0] > 1:
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
        return {"summary": {"revision": revision[0]}}

    ctx = fixture_context(tmp_path / "app")
    client = TestClient(api_main.create_app(ctx, product=_product(collect)))
    other = TestClient(api_main.create_app(fixture_context(tmp_path / "other")))

    def read_while(fresh_client):
        entered.clear()
        release.clear()
        result = {}
        waiting = threading.Thread(target=lambda: result.update(client.get("/api/orient?sections=pipeline").json()))
        waiting.start()
        assert entered.wait(5)
        fresh_client.get("/api/orient?sections=session_hints&fresh=true")
        release.set()
        waiting.join(10)
        assert not waiting.is_alive()
        _join_refresh(ctx)
        return result

    try:
        assert client.get("/api/orient?sections=pipeline").json()["pipeline"]["summary"] == {"revision": 1}

        across = read_while(other)
        assert across["pipeline"]["summary"] == {"revision": 2}
        assert across["meta"]["pipeline"]["stale"] is False

        superseded = read_while(client)
        assert superseded["pipeline"]["summary"] == {"revision": 2}
        assert superseded["meta"]["pipeline"]["stale"] is True
        assert superseded["meta"]["pipeline"]["generated_at"] == across["meta"]["pipeline"]["generated_at"]
    finally:
        release.set()
        _join_refresh(ctx)


def test_orient_lifespan_warms_shared_sections_and_cancels_timer(monkeypatch, tmp_path):
    """A warmer must use this app's collectors and stop when the app shuts down."""
    from unittest.mock import Mock

    entered, cancelled = threading.Event(), threading.Event()
    ctx = fixture_context(tmp_path)
    seen = []

    async def collect(context):
        seen.append(context)
        return {"summary": {"revision": 4}}

    product = _product(collect)
    original_collect = api_main._collect_orient_sections

    async def observe(context, layout, selected, **_options):
        # Use the actual collector path while avoiding unrelated machinery probes.
        result = await original_collect(context, layout, ["pipeline"])
        assert "pipeline" in selected
        entered.set()
        return result

    async def wait_for_cancellation(context, contribution):
        try:
            await original_refresh(context, contribution)
        finally:
            cancelled.set()

    original_refresh = api_main.refresh_orient_periodically
    monkeypatch.setattr(api_main, "_collect_orient_sections", observe)
    monkeypatch.setattr(api_main, "refresh_orient_periodically", wait_for_cancellation)
    for name in ("preload_all", "install_signal_logging", "seed_manifest_inventory",
                 "warm_projection_cache", "schedule_state_scan_warmup", "start_periodic_refresh", "stop_periodic_refresh"):
        monkeypatch.setattr(api_main, name, Mock())
    monkeypatch.setattr(api_main.isa, "schedule_refresh", Mock())
    try:
        with TestClient(api_main.create_app(ctx, product=product)) as client:
            assert entered.wait(5)
            assert seen == [ctx]
            data = client.get("/api/orient?sections=pipeline").json()
            assert data["pipeline"]["summary"] == {"revision": 4}
        assert cancelled.is_set()
    finally:
        _join_refresh(ctx)


def test_orient_warmer_skips_sections_without_recent_reads(monkeypatch, tmp_path):
    """Defect class: one orient kept every section it named warm forever, so an unread API ran scans nonstop.

    The warmer refreshes the product scans once at startup and, after that, only
    sections an /api/orient request read within the demand window.
    """
    from lu_machinery.api import state_helpers

    now = [0.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: now[0])
    monkeypatch.setattr(api_main, "_orient_demand", {})
    ctx = fixture_context(tmp_path)
    product = _product(lambda context: {"summary": {}})
    warmer: list[list[str]] = []

    async def collect(context, layout, selected, **_options):
        if asyncio.current_task().get_name() == "warmer":
            warmer.append(list(selected))
        return [({}, {"generated_at": "2026-01-01T00:00:00Z"}) for _ in selected]

    monkeypatch.setattr(api_main, "_collect_orient_sections", collect)
    monkeypatch.setattr(api_main, "ORIENT_REFRESH_INTERVAL_S", 0.01)
    client = TestClient(api_main.create_app(ctx, product=product))

    async def until(predicate):
        deadline = asyncio.get_running_loop().time() + 5.0
        while not predicate():
            assert asyncio.get_running_loop().time() < deadline, f"warmer selections: {warmer}"
            await asyncio.sleep(0.01)

    assert client.get("/api/orient?sections=runtime").status_code == 200

    async def scenario():
        task = asyncio.create_task(api_main.refresh_orient_periodically(ctx, product), name="warmer")
        try:
            await until(lambda: len(warmer) >= 3)
            assert sorted(warmer[0]) == ["pipeline", "runtime"]  # startup product warm + the read section
            assert warmer[1] == ["runtime"]  # product scans are not re-warmed unread

            now[0] += state_helpers.DEMAND_WINDOW_S  # nobody orients for a whole window
            await until(lambda: warmer[-1] == [])
            unread = len(warmer)
            await until(lambda: len(warmer) >= unread + 3)
            assert all(selected == [] for selected in warmer[unread:]), "an unread section was refreshed"

            response = await asyncio.to_thread(client.get, "/api/orient?sections=runtime")
            assert response.status_code == 200
            await until(lambda: warmer[-1] == ["runtime"])  # a new read resumes warming
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=sanitized_git_env(), timeout=30,
    ).stdout.strip()


def _git_checkout(tmp_path: Path, layout: str) -> tuple[Path, Path]:
    """A throwaway primary (with an owner/name origin) and the live root the orient reads.

    ``bare_primary`` drifts the primary's ``core.bare`` to true; ``linked_worktree``
    does the same and reads a linked worktree of it.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    _git(primary, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
         "commit", "-q", "--allow-empty", "-m", "init")
    _git(primary, "remote", "add", "origin", "https://github.com/example/checkout.git")
    live = primary
    if layout == "linked_worktree":
        live = tmp_path / "linked"
        _git(primary, "worktree", "add", "-q", "-b", "linked", str(live))
    if layout != "work_tree":
        _git(primary, "config", "core.bare", "true")
    return primary, live


@pytest.mark.parametrize("layout", ["work_tree", "bare_primary", "linked_worktree"])
def test_orient_git_reads_authority_after_the_status_read_heals(monkeypatch, tmp_path, layout):
    """Defect class: the authority read runs before or beside the status read's heal of a bare-drifted primary.

    The heal writes Git configuration (``core.bare`` and, for linked worktrees,
    ``extensions.worktreeConfig``); an authority read that overtakes it scans a
    checkout the same request then changes, or fails on a bare one.
    """
    primary, live = _git_checkout(tmp_path, layout)
    events: list[str] = []
    real_status = worktree_containment.primary_checkout_dirty_status
    real_authority = api_main.build_repository_authority

    def status(root):
        events.append("status-start")
        try:
            return real_status(root)
        finally:
            events.append("status-end")

    def authority(**kwargs):
        events.append("authority-start")
        return real_authority(**kwargs)

    monkeypatch.setattr(worktree_containment, "primary_checkout_dirty_status", status)
    monkeypatch.setattr(api_main, "build_repository_authority", authority)

    data = api_main._collect_git_orient_data(fixture_context(live))

    assert events == ["status-start", "status-end", "authority-start"]
    assert _git(primary, "config", "--get", "core.bare") == "false"
    assert data["branch"] == ("linked" if layout == "linked_worktree" else "main")
    assert data["authority"]["repository"] == "example/checkout"
    assert data["authority"]["primary_checkout"]["dirty_count"] == 0
