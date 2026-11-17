"""Route-registration contract for the fleet observer routes."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from lu_machinery.api import fleet_router as fleet_router_module
from lu_machinery.api import state_helpers
from lu_machinery.api.fleet_router import router as fleet_router
from lu_machinery.api.monitor_context import production_context
from lu_machinery.api.route_contracts import contract_for_route
from lu_machinery.fleet_comms import cold_start_board
from lu_machinery.fleet_comms.cold_start_board import ProbeResult


def test_fleet_routes_are_registered_get_only_and_contracted() -> None:
    observer_app = FastAPI()
    observer_app.state.ctx = production_context()
    observer_app.include_router(fleet_router, prefix="/api/fleet")
    openapi = observer_app.openapi()["paths"]
    paths = {path for path in openapi if path.startswith("/api/fleet")}

    expected = {
        "/api/fleet/health",
        "/api/fleet/overview",
        "/api/fleet/operations",
        "/api/fleet/agents",
        "/api/fleet/endpoints",
        "/api/fleet/requests",
        "/api/fleet/authority/jobs",
        "/api/fleet/messages",
        "/api/fleet/messages/{message_id}",
        "/api/fleet/discussions",
        "/api/fleet/discussions/{conversation_id}",
        "/api/fleet/reviews",
        "/api/fleet/reviews/{review_id}",
        "/api/fleet/dead-letters",
        "/api/fleet/migrations",
        "/api/fleet/acp/conversations",
        "/api/fleet/acp/conversations/{conversation_id}",
        "/api/fleet/activity",
    }
    assert expected <= paths
    assert all(set(openapi[path]) == {"get"} for path in paths)
    assert contract_for_route("/api/fleet/messages", "http") is not None
    operations_contract = contract_for_route("/api/fleet/operations", "http")
    assert operations_contract is not None
    assert operations_contract.pattern == "/api/fleet/operations"



@pytest.fixture
def board_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, list[float]]]:
    """A context scoped to ``tmp_path`` and a cache clock the test advances by hand."""
    now = [1_000.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: now[0])
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path))
    yield ctx, now
    state_helpers.cache_invalidate(str(tmp_path.resolve()))


def test_board_bottleneck_slice_is_recomputed_once_its_ttl_expires(
    board_cache: tuple[Any, list[float]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: the cached bottleneck slice is served past its TTL, or a failed refresh drops it."""
    ctx, now = board_cache
    calls: list[int] = []

    def collect(tasks_dir: Path, plane_db: Path) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 3:
            raise OSError("ledger unreadable")
        return {"by_stream_epic": {}, "generation": len(calls)}

    monkeypatch.setattr(cold_start_board, "collect_board_bottleneck_metrics", collect)

    def slice_() -> dict[str, Any] | None:
        return fleet_router_module._board_bottleneck_metrics(ctx, tmp_path / "tasks", tmp_path / "plane.sqlite3")

    assert slice_() == {"by_stream_epic": {}, "generation": 1, "cache_age_s": 0.0}
    now[0] += fleet_router_module.BOARD_BOTTLENECK_TTL_S - 1
    assert slice_() == {"by_stream_epic": {}, "generation": 1, "cache_age_s": 59.0}
    assert len(calls) == 1

    now[0] += 2
    assert slice_() == {"by_stream_epic": {}, "generation": 2, "cache_age_s": 0.0}
    assert len(calls) == 2

    # A failed refresh keeps serving the last good slice with its real age.
    now[0] += fleet_router_module.BOARD_BOTTLENECK_TTL_S + 5
    assert slice_() == {"by_stream_epic": {}, "generation": 2, "cache_age_s": 65.0}
    assert len(calls) == 3


def test_board_bottleneck_slice_never_holds_a_request_for_a_slow_scan(
    board_cache: tuple[Any, list[float]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: a cold or expired ledger scan blocks the board past the request timeout.

    A cold process reports the slice as warming; an expired slice is served stale
    while one background scan refreshes it, and the next read gets the new value.
    """
    ctx, now = board_cache
    gate = threading.Event()
    calls: list[int] = []

    def collect(tasks_dir: Path, plane_db: Path) -> dict[str, Any]:
        calls.append(1)
        assert gate.wait(10)
        return {"by_stream_epic": {}, "generation": len(calls)}

    monkeypatch.setattr(cold_start_board, "collect_board_bottleneck_metrics", collect)

    def slice_(wait_s: float) -> dict[str, Any] | None:
        monkeypatch.setattr(fleet_router_module, "BOARD_BOTTLENECK_WAIT_S", wait_s)
        return fleet_router_module._board_bottleneck_metrics(ctx, tmp_path / "tasks", tmp_path / "plane.sqlite3")

    assert slice_(0.05) is None
    assert slice_(0.05) is None
    gate.set()
    assert slice_(10) == {"by_stream_epic": {}, "generation": 1, "cache_age_s": 0.0}
    assert len(calls) == 1

    gate.clear()
    now[0] += fleet_router_module.BOARD_BOTTLENECK_TTL_S + 1
    assert slice_(0.05) == {"by_stream_epic": {}, "generation": 1, "cache_age_s": 61.0}
    gate.set()
    assert slice_(10) == {"by_stream_epic": {}, "generation": 2, "cache_age_s": 0.0}
    assert len(calls) == 2


def test_board_pr_list_is_reprobed_once_its_ttl_expires(
    board_cache: tuple[Any, list[float]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: the cached gh pr list result outlives its TTL or is mutated through a shared copy."""
    ctx, now = board_cache
    calls: list[int] = []

    def probe() -> ProbeResult:
        calls.append(1)
        return ProbeResult(status="ok", elapsed_ms=1.0, data={"probe": len(calls)})

    monkeypatch.setattr(cold_start_board, "_probe_gh_pr_list", probe)

    first = fleet_router_module._board_pr_list(ctx)
    first.elapsed_ms = 999.0
    now[0] += fleet_router_module.BOARD_PR_LIST_TTL_S - 1
    second = fleet_router_module._board_pr_list(ctx)
    assert (second.data, second.elapsed_ms, len(calls)) == ({"probe": 1}, 1.0, 1)

    now[0] += 2
    assert fleet_router_module._board_pr_list(ctx).data == {"probe": 2}
    assert len(calls) == 2
