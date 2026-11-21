"""Diagnostic usage observations must not alter the capacity consumer's budget."""

import copy
import io
import json
import subprocess
from datetime import UTC, datetime, timedelta
from itertools import count
from types import SimpleNamespace

import pytest

from lu_machinery.api import state_router, subscription_usage
from lu_machinery.fleet import capacity_pick, credit_lane, usage
from lu_machinery.fleet.reset_reserve import unavailable_reserve
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots")


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "monitor"])
@pytest.mark.parametrize("agy_signal", [False, True], ids=["inherit-gemini", "own-hot-row"])
@pytest.mark.parametrize("diagnostic", ["hot", "zero", "full", "stale", "error", "cold"])
def test_hot_agy_remains_avoid_through_budget_and_capacity_cli(monkeypatch, capsys, fresh, agy_signal, diagnostic):
    # Keep legitimate observation ages independent of the private credit digits.
    # Advance time so ttl=0 still exercises the stale observation path. Scope
    # the clock to this module rather than changing the shared time module.
    clock = count(step=0.125)
    monkeypatch.setattr(subscription_usage, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    quota = {
        "status": "hot",
        "remaining_pct": 3.0,
        "freshness": "fresh",
        "age_s": 0.0,
        "eligible": True,
        "health": {"healthy": True},
    }
    budget = {
        "agents": {"gemini": quota, "agy": copy.deepcopy(quota) if agy_signal else {"status": "unknown"}},
        "api_accounts": {},
        "recommendation": {"primary_agent_for_code": "agy", "warnings": []},
    }
    original = copy.deepcopy(budget)
    fraction = {"zero": 0, "full": 1}.get(diagnostic, 0.03)
    payload = {
        "command": {
            "data": {
                "groups": [
                    {"name": "Gemini PRIVATE_LABEL", "buckets": [{"window": "5h", "remaining_fraction": fraction}]}
                ]
            }
        },
        "account": "PRIVATE_ACCOUNT account@example.com",
        "credits": 987654,
    }
    if diagnostic == "error":
        payload["error"] = {"kind": "rate_limit", "code": 429, "message": "PRIVATE_ERROR"}
    subscription_usage._reset_agy_quota_observation_for_tests()
    if diagnostic == "stale":
        monkeypatch.setattr(subscription_usage, "_codexbar_cache_ttl_s", lambda: 0)
    monkeypatch.setattr(subscription_usage, "_agy_cli_bin", lambda: "/fixture/agy")
    monkeypatch.setattr(
        subscription_usage.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr="PRIVATE_STDERR"
        ),
    )
    monkeypatch.setattr(subscription_usage, "get_provider_usage_data", lambda *args, **kwargs: {})
    refreshes = []

    def observe():
        if diagnostic == "cold":
            return
        subscription_usage._probe_antigravity_native(timeout_s=1)

    def refresh(providers):
        refreshes.append("subscription")
        observe()

    monkeypatch.setattr(subscription_usage, "refresh_provider_usage_data", refresh)
    monkeypatch.setattr(subscription_usage, "refresh_api_account_data", lambda providers: refreshes.append("prepaid"))
    monkeypatch.setattr(state_router, "compute_routing_budget", lambda **kwargs: budget)
    monkeypatch.setattr(usage.urllib.request, "urlopen", lambda *args, **kwargs: io.StringIO(json.dumps(budget)))
    monkeypatch.setattr(capacity_pick, "fetch_active_in_flight", lambda: {})
    monkeypatch.setattr(capacity_pick, "admission_status", lambda: {"admitted": None, "line": "fixture"})
    monkeypatch.setattr(capacity_pick, "load_write_success_stats", lambda: {})
    monkeypatch.setattr(capacity_pick, "load_reset_reserve", lambda *args, **kwargs: {"available": False})
    if not fresh:
        observe()

    try:
        # A diagnostic reading exists even though it must never replace routing facts.
        display_status = "healthy" if diagnostic in {"hot", "zero", "full"} else "unknown"
        assert subscription_usage.project_agy_for_status(subscription_usage.get_agy_quota_observation())["status"] == (
            "unknown" if fresh else display_status
        )
        read = usage.read_budget(fresh=fresh)
        assert (
            subscription_usage.project_agy_for_status(subscription_usage.get_agy_quota_observation())["status"]
            == display_status
        )
        report = capacity_pick.build_report(read, active_in_flight={})
        agy = next(row for row in report["rows"] if row["lane"] == "agy")
        assert agy["avoid"] is True, agy
        assert (agy["status"], agy["remaining_pct"], agy["capacity"]["state"]) == ("hot", 3.0, "avoid")
        assert read["agents"] == original["agents"]
        assert budget == original
        argv = ["--json", "--strict"] + (["--fresh"] if fresh else [])
        assert capacity_pick.main(argv) == 2
        output = json.loads(capsys.readouterr().out)
        pick = next(row for row in output["pick_order"] if row["lane"] == "agy")
        assert pick["pick"] == "AVOID"
        assert output["recommendation"]["primary_agent_for_code"] is None
        assert "agy" not in output["cooler_lanes"]
        assert not any(row["lane"] == "agy" and row["pick"] != "AVOID" for row in output["pick_order"])
        assert "muse" not in read["agents"]
        if not agy_signal:
            assert "quota:gemini" in agy["notes"]

        # Render both public views through the real refresh/read/display boundary,
        # then prove that neither the source budget nor later capacity picks changed.
        for command in ("json", "show", "qa"):
            expected_exit = 1 if command == "qa" and not agy_signal else 0
            assert usage.main([command] + (["--fresh"] if fresh else [])) == expected_exit
            public = capsys.readouterr().out
            for sentinel in (
                "PRIVATE_LABEL",
                "PRIVATE_ACCOUNT",
                "account@example.com",
                "987654",
                "PRIVATE_ERROR",
                "PRIVATE_STDERR",
            ):
                assert sentinel not in public
            if command == "qa" and not agy_signal:
                # The inherited routing row is an empty shell. Its diagnostic
                # replacement cannot conceal the existing QA failure either.
                assert public == "QA FAIL:\n- agy: unavailable with no fail= and no pools\n"
                assert budget == original
                continue
            if command == "json":
                rows = json.loads(public)["agents"]
                assert {"agy", "gemini"} <= rows.keys() and "muse" not in rows
                assert rows["gemini"] == quota
                assert rows["agy"]["status"] == display_status
                if diagnostic in {"hot", "zero", "full"}:
                    group = rows["agy"]["quota_groups"][0]
                    assert group["label"] == "Gemini Models"
                    assert group["windows"][0]["remaining_pct"] == fraction * 100
                elif diagnostic == "cold":
                    assert (rows["agy"]["error_kind"], rows["agy"]["error_code"]) == ("unavailable", None)
                elif diagnostic == "error":
                    assert (rows["agy"]["error_kind"], rows["agy"]["error_code"]) == ("rate_limit", 429)
                else:
                    assert rows["agy"]["freshness"] == "stale_last_good"
                    assert rows["agy"]["quota_groups"][0]["windows"][0]["remaining_pct"] is None
            else:
                if command == "qa":
                    assert public.splitlines()[0] == ("QA PASS: 2 subscription lane(s) have usable allotment signal(s)")
                assert "mix-max is blind" not in public
                assert f"agy | {display_status} | unknown |" in public
                assert "muse" not in public
            assert budget == original
        later = capacity_pick.build_report(usage.read_budget(fresh=fresh), active_in_flight={})
        assert next(row for row in later["pick_order"] if row["lane"] == "agy")["pick"] == "AVOID"
        assert sorted(refreshes) == (["prepaid"] * 6 + ["subscription"] * 6 if fresh else [])
    finally:
        subscription_usage._reset_agy_quota_observation_for_tests()


# --- #1120: pace-surplus-per-hour ranking, over-pace non-AVOID, single Grok+Cursor meter ---

_PACE_NOW = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)


def _fresh_sub_lane(
    *,
    remaining_pct: float,
    hours_to_reset: float | None,
    status: str = "cool",
    burn_pct_per_hour: float | None = None,
    unused_pct: float | None = None,
    will_last: bool | None = None,
    pace_delta: float | None = None,
    expected_pct: float | None = None,
    stale: bool = False,
    status_source: str = "weekly_pace",
) -> dict:
    resets_at = (
        (_PACE_NOW + timedelta(hours=hours_to_reset)).isoformat().replace("+00:00", "Z")
        if hours_to_reset is not None
        else None
    )
    record: dict = {
        "status": status,
        "status_source": status_source,
        "remaining_pct": remaining_pct,
        "burn_pct_7d": round(100.0 - remaining_pct, 2),
        "resets_at": resets_at,
        "freshness": "stale_last_good" if stale else "fresh",
        "age_s": 1200.0 if stale else 30.0,
        "stale": stale,
        "fetched_at": (_PACE_NOW - timedelta(seconds=1200 if stale else 30)).isoformat().replace("+00:00", "Z"),
        "eligible": True,
        "health": {"healthy": True},
        "codexbar": {
            "weekly_used_pct": round(100.0 - remaining_pct, 2),
            "weekly_remaining_pct": remaining_pct,
            "weekly_resets_at": resets_at,
            "will_last_to_reset": will_last,
            "weekly_pace_delta_pct": pace_delta,
            "weekly_expected_pct": expected_pct,
            "freshness": "stale_last_good" if stale else "fresh",
            "age_s": 1200.0 if stale else 30.0,
            "stale": stale,
        },
    }
    if burn_pct_per_hour is not None:
        record["burn_pct_per_hour"] = burn_pct_per_hour
    if unused_pct is not None:
        record["unused_pct"] = unused_pct
    return record


def test_pace_order_24pct_35h_outranks_92pct_6_5d_at_equal_burn():
    """Defect class (#1120 AC-01): 24% left / 35 h must outrank 92% left / 6.5 d at equal burn and fit."""
    agents = {
        "claude": _fresh_sub_lane(remaining_pct=24.0, hours_to_reset=35.0, status="warm", burn_pct_per_hour=0.4),
        "codex": _fresh_sub_lane(remaining_pct=92.0, hours_to_reset=156.0, status="cool", burn_pct_per_hour=0.4),
    }
    budget = {"agents": agents, "in_flight": {"claude": 0, "codex": 0}, "diagnostics": {"stale": False}}
    report = capacity_pick.build_report(budget, reset_reserve=unavailable_reserve(), now=_PACE_NOW)
    ranked = [row["lane"] for row in report["pick_order"] if row["pick"] != "AVOID"]
    assert report["pace_order_mode"] == "surplus_per_hour"
    assert ranked[:2] == ["claude", "codex"]

    # Monitor recommendation agrees on the same inputs.
    rec = state_router._recommend_agent(
        agents, [], current_time=_PACE_NOW, is_stale=False, records_loaded=2, authoritative_data_available=True
    )
    assert rec["primary_agent_for_code"] == "claude"
    assert "surplus per hour" in rec["rationale"]


def test_pace_order_40pct_projected_surplus_outranks_on_target():
    """Defect class (#1120 AC-01): a route projected to leave 40% at reset must outrank one projected on target."""
    # claude: 70% remaining, 84h left (30% used in 84h) -> projected 40% left at reset (+0.476%/h).
    # codex:  80% remaining, 134.4h left (20% used in 33.6h) -> projected 0% left (on target, 0.0%/h),
    # even though codex has higher raw remaining_pct (80% > 70%).
    agents = {
        "claude": _fresh_sub_lane(remaining_pct=70.0, hours_to_reset=84.0, status="cool", will_last=True),
        "codex": _fresh_sub_lane(remaining_pct=80.0, hours_to_reset=134.4, status="cool", will_last=True),
    }
    budget = {"agents": agents, "in_flight": {"claude": 0, "codex": 0}, "diagnostics": {"stale": False}}
    report = capacity_pick.build_report(budget, reset_reserve=unavailable_reserve(), now=_PACE_NOW)
    ranked = [row["lane"] for row in report["pick_order"] if row["pick"] != "AVOID"]
    claude_row = next(row for row in report["rows"] if row["lane"] == "claude")
    codex_row = next(row for row in report["rows"] if row["lane"] == "codex")
    assert claude_row["pace_forecast"]["projected_surplus_pct"] == pytest.approx(40.0, abs=0.1)
    assert codex_row["pace_forecast"]["projected_surplus_pct"] == pytest.approx(0.0, abs=0.1)
    assert ranked[:2] == ["claude", "codex"]


def test_over_pace_lane_lasting_to_reset_is_not_avoid_while_near_cap_stays_avoid():
    """Defect class (#1120 AC-02): an over-pace lane that lasts to reset is not AVOID; a near-cap lane still is."""
    agents = {
        "claude": _fresh_sub_lane(remaining_pct=50.0, hours_to_reset=84.0, status="cool", burn_pct_per_hour=0.2),
        # Over-pace (+15% ahead of elapsed fraction) but recent burn lasts to reset (+18.2% surplus).
        "agy": _fresh_sub_lane(
            remaining_pct=35.0,
            hours_to_reset=84.0,
            status="hot",
            burn_pct_per_hour=0.2,
            will_last=True,
            pace_delta=15.0,
            expected_pct=50.0,
        ),
        # Near-cap / exhausted lane must stay AVOID even if marked lasting.
        "codex": _fresh_sub_lane(
            remaining_pct=0.5,
            hours_to_reset=10.0,
            status="near_cap",
            burn_pct_per_hour=0.0,
            will_last=True,
        ),
    }
    budget = {"agents": agents, "in_flight": {"claude": 0, "agy": 0, "codex": 0}, "diagnostics": {"stale": False}}
    report = capacity_pick.build_report(budget, reset_reserve=unavailable_reserve(), now=_PACE_NOW)
    rows = {row["lane"]: row for row in report["rows"]}
    picks = {row["lane"]: row["pick"] for row in report["pick_order"]}

    assert rows["agy"]["avoid"] is False
    assert rows["agy"]["capacity"]["state"] == credit_lane.CAPACITY_VERIFIED
    assert isinstance(picks["agy"], int)
    assert picks["claude"] < picks["agy"]  # over-pace sorts lower than cool
    assert rows["codex"]["avoid"] is True
    assert picks["codex"] == "AVOID"


def test_grok_and_cursor_return_one_meter_row_with_summed_in_flight():
    """Defect class (#1120 AC-03): verified Grok and Cursor return one meter row with summed in-flight."""
    agents = {
        "grok": _fresh_sub_lane(remaining_pct=60.0, hours_to_reset=60.0, status="cool", burn_pct_per_hour=0.4),
        "cursor": _fresh_sub_lane(remaining_pct=62.0, hours_to_reset=60.0, status="cool", burn_pct_per_hour=0.4),
        "codex": _fresh_sub_lane(remaining_pct=80.0, hours_to_reset=120.0, status="cool", burn_pct_per_hour=0.4),
    }
    budget = {
        "agents": agents,
        "in_flight": {"grok": 2, "cursor": 1, "codex": 0},
        "diagnostics": {"stale": False},
    }
    rows = capacity_pick.build_lane_rows(budget, reset_reserve=unavailable_reserve(), now=_PACE_NOW)
    gc_rows = [row for row in rows if row["lane"] in {"grok", "cursor"}]
    assert len(gc_rows) == 1
    assert gc_rows[0]["lane"] == "cursor"
    assert gc_rows[0]["in_flight"] == 3
    assert gc_rows[0]["remaining_pct"] == 60.0  # tighter reading wins
    assert "meter:grok+cursor" in gc_rows[0]["notes"]


def test_deepseek_is_absent_from_subscription_pace_order():
    """Defect class (#1120 AC-03): API-credit route DeepSeek must be absent from the subscription pace order."""
    agents = {
        "claude": _fresh_sub_lane(remaining_pct=50.0, hours_to_reset=60.0, status="cool", burn_pct_per_hour=0.3),
        "codex": _fresh_sub_lane(remaining_pct=80.0, hours_to_reset=140.0, status="cool", burn_pct_per_hour=0.3),
    }
    budget = {
        "agents": agents,
        "api_accounts": {
            "deepseek": {
                "probe_state": "ok",
                "freshness": "fresh",
                "age_s": 10.0,
                "currency": "USD",
                "total_balance": 50.0,
                "is_available": True,
                "health": {"healthy": True},
            }
        },
        "in_flight": {"claude": 0, "codex": 0, "deepseek": 0},
        "diagnostics": {"stale": False},
    }
    assert "deepseek" not in capacity_pick.CODE_LANES
    report = capacity_pick.build_report(budget, reset_reserve=unavailable_reserve(), now=_PACE_NOW)
    assert all(row["lane"] != "deepseek" for row in report["rows"])
    assert all(row["lane"] != "deepseek" for row in report["pick_order"])

    # Even if an API-credit row is passed directly to build_pick_order, it gets no subscription pace slot.
    direct = capacity_pick.build_pick_order(
        [*report["rows"], {"lane": "deepseek", "status": "cool", "remaining_pct": 100.0, "in_flight": 0}],
        now=_PACE_NOW,
    )
    assert all(row["lane"] != "deepseek" for row in direct)


@pytest.mark.parametrize(
    ("mode", "expected_reason"),
    [
        ("stale_snapshot", "stale"),
        ("stale_probe", "stale"),
        ("missing_reset", "missing"),
        ("reset_crossing", "reset-crossing"),
    ],
)
def test_stale_missing_or_reset_crossing_forecast_keeps_headroom_order_and_names_reason(mode, expected_reason):
    """Control (#1120 AC-04): stale, missing, or reset-crossing forecast keeps today's order and states the reason."""
    claude = _fresh_sub_lane(
        remaining_pct=24.0,
        hours_to_reset=-1.0 if mode == "reset_crossing" else (None if mode == "missing_reset" else 35.0),
        status="warm",
        burn_pct_per_hour=0.4,
        stale=(mode == "stale_probe"),
    )
    codex = _fresh_sub_lane(
        remaining_pct=92.0,
        hours_to_reset=156.0,
        status="cool",
        burn_pct_per_hour=0.4,
    )
    budget = {
        "agents": {"claude": claude, "codex": codex},
        "in_flight": {"claude": 0, "codex": 0},
        "diagnostics": {"stale": mode == "stale_snapshot"},
    }
    report = capacity_pick.build_report(budget, reset_reserve=unavailable_reserve(), now=_PACE_NOW)
    ranked = [row["lane"] for row in report["pick_order"] if row["pick"] != "AVOID"]
    assert report["pace_order_mode"] == "headroom_fallback"
    assert ranked[:2] == ["codex", "claude"]  # today's order: cool 92% outranks warm 24%
    assert expected_reason in report["pace_order_reason"].lower()
    assert expected_reason in str(report["recommendation"]["rationale"]).lower()
