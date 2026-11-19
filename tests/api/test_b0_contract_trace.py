"""B0 handler-contract trace for #10667 D02, D05 and D06.

Each test asserts the same semantics the frozen design requires before any UI
claim: a real field or an explicit unknown, never a fabricated healthy value.
D01, D03, D04, D10, D12 and D13 have dedicated tests beside their handlers.
"""

from __future__ import annotations

from lu_machinery.api import claude_account_usage
from lu_machinery.api.route_contracts import contract_for_route
from lu_machinery.common.repo_root import machinery_path

HOME_PAGE = machinery_path("api/dashboards/index.html")


def test_d02_unknown_account_windows_are_explicit_unknown() -> None:
    """A missing per-account probe never becomes zero capacity or a preference."""
    claude_account_usage._reset_for_tests()
    try:
        fields = claude_account_usage.routing_budget_fields()
    finally:
        claude_account_usage._reset_for_tests()
    rows = fields["claude_accounts"]
    assert set(rows) == {"claude:a", "claude:b"}
    for row in rows.values():
        assert row["status"] == "unknown"
        assert row["age_s"] is None
        assert row["stale"] is False
        assert row["reason"] == "not_probed"
        for window in ("session5h", "weekly"):
            assert row[window] == {
                "left_pct": None,
                "resets_at": None,
                "pace": None,
                "pace_delta_pct": None,
                "leftover_pct": None,
            }
    assert fields["preferred_claude_account"] is None


def test_d02_routing_contract_declares_its_source() -> None:
    contract = contract_for_route("/api/state/routing-budget")
    assert contract is not None
    assert contract.mutates is False
    assert contract.locality == "host_affine"


def test_d06_home_reads_exclude_agent_monitor_status() -> None:
    """No machinery dashboard page or shared script may trigger the reconciling agent-monitor GET."""
    dashboards = HOME_PAGE.parent
    files = sorted((*dashboards.glob("*.html"), *dashboards.glob("*.js")))
    assert HOME_PAGE in files
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert "/api/agent-monitor/status" not in source, path.name
        assert "agent-monitor" not in source, path.name
