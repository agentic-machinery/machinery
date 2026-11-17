"""Tests for route locality and machine-readable contract fields (#7365)."""

from __future__ import annotations

from typing import get_args

from lu_machinery.api.route_contracts import ROUTE_CONTRACTS, RouteLocality, all_route_contracts

VALID_LOCALITIES = set(get_args(RouteLocality))


def test_route_contracts_expose_locality() -> None:
    entries = [*ROUTE_CONTRACTS, *all_route_contracts()]
    assert entries
    for entry in entries:
        assert entry.locality in VALID_LOCALITIES
