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


def test_expired_product_snapshot_is_stale_during_refresh_and_eventually_unavailable(monkeypatch, tmp_path):
    """Expiry must refresh real data and never label an old fallback as fresh or retain it forever."""
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
        assert client.get("/api/orient?sections=pipeline").json()["pipeline"]["summary"] == {"revision": 2}

        now[0] = 132
        release.clear()
        expired = client.get("/api/orient?sections=pipeline").json()
        assert expired["pipeline"] == {"summary": {}, "error": "refreshing"}
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
    for name in ("preload_all", "install_signal_logging", "ensure_broker_db_ready", "seed_manifest_inventory",
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


def _git_checkout(tmp_path: Path, *, bare: bool) -> Path:
    """A throwaway checkout with an owner/name origin; ``bare`` drifts its ``core.bare`` to true."""
    repo = tmp_path / "checkout"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=sanitized_git_env())

    git("init", "-q", "-b", "main")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "init")
    git("remote", "add", "origin", "https://github.com/example/checkout.git")
    if bare:
        git("config", "core.bare", "true")
    return repo


def _record_slow_git_reads(monkeypatch, events: list[str], *, status_waits_for: threading.Event | None = None):
    real_status = worktree_containment.primary_checkout_dirty_status
    real_authority = api_main.build_repository_authority

    def status(root):
        events.append("status-start")
        try:
            if status_waits_for is not None and status_waits_for.wait(timeout=10):
                events.append("status-saw-authority")
            return real_status(root)
        finally:
            events.append("status-end")

    def authority(**kwargs):
        events.append("authority-start")
        if status_waits_for is not None:
            status_waits_for.set()
        return real_authority(**kwargs)

    monkeypatch.setattr(worktree_containment, "primary_checkout_dirty_status", status)
    monkeypatch.setattr(api_main, "build_repository_authority", authority)


def _core_bare(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "core.bare"],
        capture_output=True, text=True, check=False, env=sanitized_git_env(),
    ).stdout.strip()


def test_orient_git_runs_the_slow_reads_together_on_a_checkout_with_a_work_tree(monkeypatch, tmp_path):
    """Defect class: the two ``git status`` scans run one after the other on a healthy checkout.

    The status read holds until the authority read has started; serial reads
    would leave it waiting out its 10 s bound instead.
    """
    repo = _git_checkout(tmp_path, bare=False)
    events: list[str] = []
    _record_slow_git_reads(monkeypatch, events, status_waits_for=threading.Event())

    data = api_main._collect_git_orient_data(fixture_context(repo))

    assert "status-saw-authority" in events
    assert data["branch"] == "main"
    assert data["authority"]["repository"] == "example/checkout"
    assert data["primary_checkout"]["dirty_count"] == data["authority"]["primary_checkout"]["dirty_count"] == 0


def test_orient_git_heals_a_bare_primary_before_the_authority_read(monkeypatch, tmp_path):
    """Defect class: the authority read scans a bare-drifted primary before the status read heals it.

    ``git status`` needs a work tree, so an authority read that overtakes the
    heal fails the whole git section where the serial reads succeeded.
    """
    repo = _git_checkout(tmp_path, bare=True)
    events: list[str] = []
    _record_slow_git_reads(monkeypatch, events)

    data = api_main._collect_git_orient_data(fixture_context(repo))

    assert events.index("status-end") < events.index("authority-start"), events
    assert _core_bare(repo) == "false"
    assert data["authority"]["repository"] == "example/checkout"
    assert data["authority"]["primary_checkout"]["dirty_count"] == 0


@pytest.mark.parametrize("probe", ["git_missing", "git_fails"])
def test_orient_git_keeps_serial_order_when_the_work_tree_probe_fails(monkeypatch, tmp_path, probe):
    """Defect class: a failed bare-detection probe is read as "has a work tree" and lets the authority read overtake the heal.

    The checkout is really bare. The probe either cannot start (no ``git`` on its
    PATH) or exits non-zero while printing ``false``; neither proves a work tree.
    """
    repo = _git_checkout(tmp_path, bare=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    if probe == "git_fails":
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\necho false\nexit 1\n", encoding="utf-8")
        fake_git.chmod(0o755)
    probe_env = {**sanitized_git_env(), "PATH": str(fake_bin)}
    monkeypatch.setattr(api_main, "sanitized_git_env", lambda: dict(probe_env))
    events: list[str] = []
    _record_slow_git_reads(monkeypatch, events)

    data = api_main._collect_git_orient_data(fixture_context(repo))

    assert events.index("status-end") < events.index("authority-start"), events
    assert _core_bare(repo) == "false"
    assert data["authority"]["primary_checkout"]["dirty_count"] == 0
