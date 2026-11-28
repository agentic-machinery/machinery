"""The shared, unauthenticated /metrics scrape shows public-scope settlement data only (TRACETEL-1128 Slice 2).

Binding condition (Sol R3): the settlement-derived series on the actual unauthenticated scrape are identical with
and without one private settlement, also near the shared free-space reserve with the same public append.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import model_stats_router, model_stats_rows, subscriptions
from lu_machinery.api.main import create_app
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.api.product_contribution import ProductContribution
from lu_machinery.dispatcher import store
from lu_machinery.dispatcher.types import StartRefusal
from lu_machinery.orchestration.fleet_repos import FleetRepo
from lu_machinery.telemetry import emit as emit_mod
from lu_machinery.telemetry import reader

NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
REPOS = {
    # The row writer's default key for a record without a repository is "public": keep it registered and public,
    # so a substituted default would be visible to the public readers.
    "public": FleetRepo(key="public", github="example-org/site", local_name="site", role="public-monorepo",
                        default=True),
    "pub": FleetRepo(key="pub", github="example-org/pub", local_name="pub", role="public-labs"),
    "priv": FleetRepo(key="priv", github="example-org/priv", local_name="priv", role="private-labs"),
}
MIB = 1024 * 1024
BLOCK = 4096
LIMITS = emit_mod.EventLogLimits(reserve_fraction=0.0, reserve_floor_bytes=1 * MIB, scope_bytes=1 * MIB,
                                 segment_bytes=512 * 1024, slack_bytes=64 * 1024)
# Just enough for one public append (one record block; new segment, counters, three directories) and a little.
NEAR_RESERVE = LIMITS.reserve_floor_bytes + BLOCK + 5 * BLOCK + 2048
SETTLEMENT_FAMILIES = ("lu_telemetry_public_", "lu_model_stats_", "lu_tokens_", "lu_token_data_age")


class FakeDisk:
    """Free space that shrinks by the blocks of every directory and file of the telemetry tree, like a filesystem."""

    def __init__(self, events_root: Path, start: int) -> None:
        self.events_root, self.start = events_root, start

    def __call__(self, _path: Path) -> emit_mod.DiskSpace:
        telemetry = self.events_root.parent  # every directory takes one block, every file its rounded-up size
        entries = [telemetry, *telemetry.rglob("*")] if telemetry.is_dir() else []
        used = sum(max(1, -(-p.stat().st_size // BLOCK)) * BLOCK if p.is_file() else BLOCK for p in entries)
        return emit_mod.DiskSpace(available=self.start - used, total=100 * MIB)


@asynccontextmanager
async def _no_lifespan(_app: FastAPI) -> Iterator[None]:
    yield


@pytest.fixture(autouse=True)
def _fixture_scope(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(emit_mod, "load_fleet_repos", lambda: REPOS)
    monkeypatch.setattr(model_stats_router, "fleet_catalog", lambda: REPOS)
    monkeypatch.setattr(model_stats_rows, "load_fleet_repos", lambda: REPOS)
    model_stats_rows.refresh_readings()  # no registry reading cached by an earlier test
    monkeypatch.setattr(emit_mod, "_utcnow", lambda: NOW)
    monkeypatch.setattr(emit_mod, "DEFAULT_LIMITS", LIMITS)
    monkeypatch.setattr(store, "now", lambda: NOW.isoformat())
    monkeypatch.setattr(subscriptions, "subscriptions_snapshot", lambda: {})
    monkeypatch.setattr(reader, "_ROWS", reader._RowsCursor())

    def snapshot_now(ctx, *, public_only: bool = False):
        # The real aggregation over the real rows, computed in the scrape instead of the off-loop cache.
        snapshot = model_stats_router.collect(ctx.roots.batch_state_dir, ctx.roots.live_repo_root,
                                              now=NOW + timedelta(hours=1), landing=model_stats_router.Landing(),
                                              public_only=public_only)
        return snapshot, 1.0

    monkeypatch.setattr(model_stats_router, "cached_snapshot", snapshot_now)
    yield
    model_stats_rows.refresh_readings()  # nor one of this fixture registry for a later test


def _scrape(root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """One unauthenticated GET /metrics on a Monitor app bound to ``root``."""
    monkeypatch.setattr(reader, "_ROWS", reader._RowsCursor())  # no reader state carried between the cases
    app = create_app(fixture_context(root), lifespan=_no_lifespan, product=ProductContribution())
    with TestClient(app) as client:
        app.state.metrics_snapshot.refresh()
        response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def _settlement_series(body: str) -> list[str]:
    def family(line: str) -> str:
        return line.split(" ", 3)[2] if line.startswith(("# HELP ", "# TYPE ")) else line

    return [line for line in body.splitlines() if family(line).startswith(SETTLEMENT_FAMILIES)]


def _bind_store(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    tasks = root / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    monkeypatch.setenv("LU_STATE_ROOT", str(root))
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    return tasks.parent


def _settle(repo: str | None) -> None:
    store.record_admission_refusal(None, "claude", {"repo": repo, "model": "claude-opus-5-5", "effort": "high",
                                                    "mode": "workspace-write"}, StartRefusal.CAPACITY)


def _settle_conflicting() -> None:
    """A settled record whose repository declarations disagree (``repo`` public, ``options.repo`` private)."""
    record = {"schema_version": 2, "task_id": "task-conflicting", "run_nonce": "c" * 32, "status": "done",
              "agent": "claude", "model": "claude-opus-5-5", "effort": "high", "mode": "workspace-write",
              "repo": "pub", "options": {"repo": "priv"}, "pid": None, "started_at": NOW.isoformat(),
              "finished_at": NOW.isoformat(), "duration_s": 1.0}
    store._write(store.record_path("task-conflicting"), record, exclusive=True)


# Each is a private settlement for the shared scrape: a registered private repository, no repository at all (the
# row writer stores repo "public" for it), and contradicting repository declarations.
PRIVATE_SETTLEMENTS = {
    "registered_private": lambda: _settle("priv"),
    "unclassified": lambda: _settle(None),
    "conflicting": _settle_conflicting,
}


@pytest.mark.parametrize("kind", sorted(PRIVATE_SETTLEMENTS))
def test_private_settlement_leaves_unauthenticated_scrape_identical_near_reserve(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Defect class: one private or unclassified settlement changes a settlement-derived series on the shared scrape
    (mixed-scope model-stat aggregates, a row writer default read as public, or a private append spending the free
    space the same public append then lacks)."""
    bodies, full = {}, {}
    for case in ("without_private", "with_private"):
        root = tmp_path / case
        batch_state = _bind_store(monkeypatch, root)
        monkeypatch.setattr(emit_mod, "disk_space", FakeDisk(emit_mod.events_root(batch_state), NEAR_RESERVE))
        if case == "with_private":
            PRIVATE_SETTLEMENTS[kind]()
        _settle("pub")
        bodies[case] = _scrape(root, monkeypatch)
        full[case] = model_stats_router.collect(batch_state, root, now=NOW + timedelta(hours=1),
                                                landing=model_stats_router.Landing())["metrics"]
        rows = [json.loads(row) for row in (batch_state / "model-stats" / "rows.jsonl").read_text().splitlines()]
        assert [row["visibility"] for row in rows] == (["private", "public"] if case == "with_private" else ["public"])

    # The fixture is sensitive: the mixed-scope aggregation does see the private settlement.
    assert full["without_private"] != full["with_private"]
    series = _settlement_series(bodies["with_private"])
    assert any(line.startswith("lu_model_stats_week_tasks{") for line in series)
    assert any(line.startswith("lu_telemetry_public_events_last_ts ") for line in series)
    assert any(line.startswith("lu_telemetry_public_independent_settlement_last_ts ") for line in series)
    assert 'lu_telemetry_public_dropped_events_total{reason="reserve"} 0.0' in series
    assert _settlement_series(bodies["without_private"]) == series
    assert bodies["without_private"] == bodies["with_private"]


def test_legacy_and_unclassified_records_never_reach_unauthenticated_scrape(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: a legacy v1 record (with cwd) or an unclassified record moves a public freshness series."""
    batch_state = _bind_store(monkeypatch, tmp_path)
    monkeypatch.setattr(emit_mod, "disk_space", lambda _p: emit_mod.DiskSpace(available=1 << 40, total=1 << 41))
    root = emit_mod.events_root(batch_state)
    (root / "public").mkdir(parents=True)
    (root / f"{NOW:%Y-%m-%d}.jsonl").write_text(json.dumps({
        "schema_version": 1, "event_type": "dispatch", "ts": NOW.isoformat(), "cwd": "/private/checkout"}) + "\n")
    event = emit_mod.settlement_event({"task_id": "t", "run_nonce": "n", "status": "done", "repo": "pub"},
                                      repos=REPOS)
    (root / "public" / f"{NOW:%Y-%m-%d}.jsonl").write_text(json.dumps({**event, "repo": None}) + "\n")
    _settle(repo="unregistered")  # an unclassified settlement goes to the private scope

    body = _scrape(tmp_path, monkeypatch)
    assert "lu_telemetry_public_events_last_ts" not in body
    assert "lu_model_stats_week_tasks{" not in body
    assert "/private/checkout" not in body
    assert "lu_telemetry_public_footprint_bytes" in body
