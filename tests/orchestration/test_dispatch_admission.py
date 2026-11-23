"""Host admission for write-capable dispatches (#8645 part A).

Every record lives under tmp_path; memory and load come from a monkeypatched
probe, never from allocating memory or generating load.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.common import json_depth
from lu_machinery.dispatcher import host_admission, pool_selector
from lu_machinery.orchestration import dispatch_admission as adm
from lu_machinery.orchestration import worktree_prep
from tests.helpers.product_tree import configured_roots as configured_roots

_GIB = 1024**3
_LIMITS = adm.Thresholds(max_live_write_workers=2, min_mem_available_gib=3.5, max_load_per_cpu=1.5)


pytestmark = pytest.mark.usefixtures("configured_roots")


def _healthy_probe() -> adm.HostProbe:
    return adm.HostProbe(mem_available_bytes=12 * _GIB, load1=2.0, cpu_count=8, proc_available=True)


def _record(tasks_dir: Path, task_id: str, *, status: str = "running", mode: str = "workspace-write", **fields) -> Path:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "task_id": task_id,
        "status": status,
        "mode": mode,
        "pid": fields.pop("pid", 1000 + len(list(tasks_dir.glob("*.json")))),
        "run_nonce": f"nonce-{task_id}",
        "started_at": fields.pop("started_at", datetime.now(UTC).isoformat()),
        **fields,
    }
    path = tasks_dir / f"{task_id}.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def probe(monkeypatch):
    current = {"probe": _healthy_probe()}
    monkeypatch.setattr(adm, "probe_host", lambda *_a, **_k: current["probe"])
    return current


def test_cap_reached_is_refused(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "a", pid=11)
    _record(tasks, "b", pid=12, mode="danger")

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    assert not decision.admitted
    assert decision.live_task_ids == ("a", "b")
    [failure] = decision.failures
    assert "live write workers 2/2 reached the cap (DISPATCH_MAX_LIVE_WRITE_WORKERS=2)" in failure
    assert "--force-admission" in decision.refusal_line()


def test_fixed_meter_ceiling_defers_only_the_meter_at_its_limit(tmp_path, probe):
    """Defect class: a full meter admits another write or blocks a different account."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    _record(tasks, "codex-b", agent="codex", codex_account="b-sub")
    limits = adm.load_thresholds({
        "DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER": "1",
        "DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN": "5",
        "DISPATCH_MIN_METER_LAUNCH_INTERVAL_S": "0",
    })
    blocked = adm.evaluate("workspace-write", tasks, thresholds=limits,
                           pid_alive=lambda _pid: True, meter="codex:b", now=now)
    admitted = adm.evaluate("workspace-write", tasks, thresholds=limits,
                            pid_alive=lambda _pid: True, meter="codex:a", now=now)

    assert not blocked.admitted
    assert blocked.meter is not None and blocked.meter.defer == adm.MeterDefer.CAP
    assert blocked.meter.cap == 1
    assert blocked.meter.telemetry == "unavailable:no_budget"
    assert "meter codex:b: live write workers 1/1 (effective cap 1, telemetry=unavailable:no_budget)" in blocked.refusal_line()
    assert "subscription_meter_defer=cap" in blocked.refusal_line()
    assert admitted.admitted
    assert "meter codex:a: live write workers 0/1 (effective cap 1, telemetry=unavailable:no_budget)" in admitted.summary()


def test_meter_cadence_measures_from_the_last_launch_even_if_it_finished(tmp_path, probe):
    """Defect class: a meter relaunches inside its minimum interval."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    _record(tasks, "finished", status="done", agent="claude", started_at=now.isoformat())
    # A refusal does not extend the interval.
    _record(tasks, "refused", status="failed", agent="claude", never_started_reason="admission_capacity",
            started_at=(now + timedelta(seconds=4)).isoformat())
    limits = replace(_LIMITS, min_meter_launch_interval_s=5)
    blocked = adm.evaluate("workspace-write", tasks, thresholds=limits, meter="claude:a",
                           now=now + timedelta(seconds=4))
    admitted = adm.evaluate("workspace-write", tasks, thresholds=limits, meter="claude:a",
                            now=now + timedelta(seconds=5))

    assert not blocked.admitted
    assert blocked.meter is not None and blocked.meter.defer == adm.MeterDefer.CADENCE
    assert "launch interval 5s, subscription_meter_defer=cadence (retry in 1.00s)" in blocked.refusal_line()
    assert admitted.admitted


def test_meter_exhaustion_defers_and_unknown_usage_keeps_fixed_cap(tmp_path, probe, monkeypatch):
    """Defect class: throttled or depleted meters admit writes, stale readings trigger quota denials, or unknown usage gets full cap."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    limits = replace(adm.load_thresholds({}), max_live_write_workers=10, min_meter_launch_interval_s=0)
    assert limits.max_live_write_workers_per_meter == 8
    assert limits.max_live_write_workers_per_meter_unknown == 2
    for index in range(8):
        _record(tasks, f"worker-{index}", agent="codex")

    def decide(row, *, stale=False, generated_at=None, meter="codex:a", extra=None):
        budget = {
            "diagnostics": {"stale": stale, "data_age_s": 1200 if stale else 0},
            "generated_at": now.isoformat() if generated_at is None else generated_at,
            "codex_accounts": {"codex:a": row},
            **(extra or {}),
        }
        return adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                            meter=meter, meter_budget=budget, now=now)

    recent_rl = (now - timedelta(seconds=60)).isoformat()
    expired_rl = (now - timedelta(seconds=899)).isoformat()
    boundary_rl = (now - timedelta(seconds=300)).isoformat()
    for row, reason in (
        ({"status": "rate_limited", "runtime": {"last_rate_limited_at": recent_rl}}, "rate_limited"),
        ({"status": "cool", "runtime": {"rate_limited": 1, "last_rate_limited_at": recent_rl}}, "rate_limited"),
        ({"status": "near_cap", "freshness": "fresh", "age_s": 0, "remaining_pct": 1.0}, "near_cap"),
        ({"status": "cool", "freshness": "fresh", "age_s": 0, "remaining_pct": 0}, "exhausted"),
    ):
        decision = decide(row)
        assert not decision.admitted
        assert f"subscription_meter_defer={reason}" in decision.refusal_line()

    # Expired or unattributed Monitor runtime vetoes do not trigger rate_limited deferral.
    empty_tasks = tmp_path / "empty-tasks"
    for rt_row in (
        {"status": "cool", "freshness": "fresh", "age_s": 0, "remaining_pct": 50,
         "runtime": {"rate_limited": 1, "headroom_blocked": True, "last_rate_limited_at": expired_rl}},
        {"status": "cool", "freshness": "fresh", "age_s": 0, "remaining_pct": 50,
         "runtime": {"rate_limited": 1, "last_rate_limited_at": boundary_rl}},
        {"status": "cool", "freshness": "fresh", "age_s": 0, "remaining_pct": 50,
         "runtime": {"rate_limited": 1}},
    ):
        result = adm.evaluate(
            "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
            meter="codex:a",
            meter_budget={"generated_at": now.isoformat(), "codex_accounts": {"codex:a": rt_row}},
            now=now,
        )
        assert result.admitted
        assert result.meter is not None and result.meter.telemetry == "fresh" and result.meter.cap == 8

    # Quota denials apply only to verified fresh readings; stale/unverified rows use the reduced cap.
    for unverified in (
        {"status": "near_cap", "remaining_pct": 0},
        {"status": "exhausted", "freshness": "fresh", "age_s": 0, "stale": True, "remaining_pct": 0},
        {"status": "exhausted", "freshness": "fresh", "age_s": 900, "remaining_pct": 0},
    ):
        result = adm.evaluate(
            "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
            meter="codex:a",
            meter_budget={"generated_at": now.isoformat(), "codex_accounts": {"codex:a": unverified}},
            now=now,
        )
        assert result.admitted
        assert result.meter is not None and result.meter.cap == 2 and result.meter.telemetry != "fresh"

    # Cached row age includes elapsed snapshot time; invalid/expired snapshot timestamps degrade to unknown cap.
    aged_ok = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="codex:a",
        meter_budget={
            "generated_at": (now - timedelta(seconds=500)).isoformat(),
            "codex_accounts": {"codex:a": {"status": "cool", "freshness": "fresh", "age_s": 399, "remaining_pct": 50}},
        },
        now=now,
    )
    assert aged_ok.admitted and aged_ok.meter is not None and aged_ok.meter.telemetry == "fresh" and aged_ok.meter.cap == 8
    aged_stale = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="codex:a",
        meter_budget={
            "generated_at": (now - timedelta(seconds=500)).isoformat(),
            "codex_accounts": {"codex:a": {"status": "cool", "freshness": "fresh", "age_s": 400, "remaining_pct": 0}},
        },
        now=now,
    )
    assert aged_stale.admitted and aged_stale.meter is not None
    assert aged_stale.meter.telemetry == "stale:probe_age_exceeded" and aged_stale.meter.cap == 2
    for bad_ts, expected_tel in (
        ("", "unavailable:snapshot_timestamp_missing"),
        ("not-a-timestamp", "unavailable:snapshot_timestamp_invalid"),
        ((now + timedelta(seconds=5)).isoformat(), "unavailable:snapshot_timestamp_invalid"),
        ((now - timedelta(seconds=900)).isoformat(), "stale:snapshot_expired"),
    ):
        bad_result = adm.evaluate(
            "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
            meter="codex:a",
            meter_budget={
                "generated_at": bad_ts,
                "codex_accounts": {"codex:a": {"status": "cool", "freshness": "fresh", "age_s": 0, "remaining_pct": 50}},
            },
            now=now,
        )
        assert bad_result.admitted and bad_result.meter is not None
        assert bad_result.meter.telemetry == expected_tel and bad_result.meter.cap == 2

    # AGY Claude/GPT does not inherit Gemini's fresh label when its own window is absent.
    gemini_without_cg = {
        "generated_at": now.isoformat(),
        "agents": {"gemini": {"status": "cool", "freshness": "fresh", "age_s": 10, "remaining_pct": 70}},
    }
    agy_missing_cg = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="agy:claude_gpt", meter_budget=gemini_without_cg, now=now,
    )
    assert agy_missing_cg.admitted and agy_missing_cg.meter is not None
    assert agy_missing_cg.meter.telemetry == "unavailable:allowance_missing" and agy_missing_cg.meter.cap == 2
    gemini_with_cg = {
        "generated_at": now.isoformat(),
        "agents": {
            "gemini": {
                "status": "cool", "freshness": "fresh", "age_s": 10, "remaining_pct": 70,
                "claude_gpt_windows": {"five_hour": {"remaining_pct": 100}, "weekly": {"remaining_pct": 0}},
            },
        },
    }
    agy_exhausted_cg = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="agy:claude_gpt", meter_budget=gemini_with_cg, now=now,
    )
    assert not agy_exhausted_cg.admitted and agy_exhausted_cg.meter is not None
    assert agy_exhausted_cg.meter.telemetry == "fresh" and agy_exhausted_cg.meter.defer == adm.MeterDefer.EXHAUSTED

    # AGY Monitor runtime vetoes require attributable single-group model membership within 300s (F5).
    for agy_models, expected_cg, expected_gem in (
        (["claude-opus-5-5"], adm.MeterDefer.RATE_LIMITED, None),
        (["gemini-4-neon-high"], None, adm.MeterDefer.RATE_LIMITED),
        (["claude-opus-5-5", "gemini-4-neon-high"], None, None),
        ([], None, None),
        ([None], None, None),
    ):
        agy_rl_budget = {
            "generated_at": now.isoformat(),
            "agents": {
                "gemini": {
                    "status": "cool", "freshness": "fresh", "age_s": 10, "remaining_pct": 70,
                    "claude_gpt_windows": {"five_hour": {"remaining_pct": 80}, "weekly": {"remaining_pct": 80}},
                    "runtime": {
                        "rate_limited": 1, "headroom_blocked": True,
                        "last_rate_limited_at": recent_rl, "models_rate_limited": agy_models,
                    },
                },
            },
        }
        cg_res = adm.evaluate("workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
                              meter="agy:claude_gpt", meter_budget=agy_rl_budget, now=now)
        gem_res = adm.evaluate("workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
                               meter="agy:gemini", meter_budget=agy_rl_budget, now=now)
        assert cg_res.meter is not None and cg_res.meter.defer == expected_cg
        assert gem_res.meter is not None and gem_res.meter.defer == expected_gem

    # Grok+Cursor requires verified bindings and BOTH components fresh with usable allowance (F3/F4).
    verified_gc = {
        "generated_at": now.isoformat(),
        "agents": {
            "grok": {"status": "cool", "freshness": "fresh", "age_s": 10, "remaining_pct": 50},
            "cursor": {"status": "cool", "freshness": "fresh", "age_s": 15, "remaining_pct": 60},
        },
    }
    gc_fresh = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=verified_gc, now=now,
    )
    assert gc_fresh.admitted and gc_fresh.meter is not None and gc_fresh.meter.telemetry == "fresh" and gc_fresh.meter.cap == 8
    unverified_gc = {
        "generated_at": now.isoformat(),
        "agents": {
            "grok": {"status": "cool", "freshness": "fresh", "age_s": 10, "remaining_pct": 50},
            "cursor": {"status": "need_login"},
        },
    }
    gc_unverified = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=unverified_gc, now=now,
    )
    assert gc_unverified.admitted and gc_unverified.meter is not None
    assert gc_unverified.meter.telemetry == "unavailable:binding_unverified" and gc_unverified.meter.cap == 2

    # F3: fresh Grok + unavailable Cursor (even with status=near_cap) must not report fresh or trigger quota refusal.
    gc_mixed_unavail = {
        "generated_at": now.isoformat(),
        "agents": {
            "grok": {"status": "cool", "freshness": "fresh", "age_s": 10, "remaining_pct": 60},
            "cursor": {"status": "near_cap", "freshness": "unavailable"},
        },
    }
    gc_f3 = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=gc_mixed_unavail, now=now,
    )
    assert gc_f3.admitted and gc_f3.meter is not None
    assert gc_f3.meter.telemetry == "unavailable:probe_unavailable" and gc_f3.meter.cap == 2

    # F4: malformed non-scalar login_state or deeply nested metadata degrades safely without raising,
    # and preserves independent local rate-limit vetoes.
    nested: dict[str, object] = {}
    cursor_node = nested
    for _ in range(400):
        nxt: dict[str, object] = {}
        cursor_node["deep"] = nxt
        cursor_node = nxt
    gc_deep = {
        "generated_at": now.isoformat(),
        "agents": {
            "grok": {"status": "cool", "freshness": "fresh", "age_s": 10, "remaining_pct": 50,
                     "codexbar": {"freshness": "fresh", "age_s": 10, "extra": nested}},
            "cursor": {"status": "cool", "freshness": "fresh", "age_s": 15, "remaining_pct": 60},
        },
    }
    gc_deep_res = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=gc_deep, now=now,
    )
    assert gc_deep_res.admitted and gc_deep_res.meter is not None and gc_deep_res.meter.telemetry == "fresh"
    gc_malformed = {
        "generated_at": now.isoformat(),
        "agents": {
            "grok": {"status": "cool", "login_state": ["NEED_LOGIN"], "freshness": "fresh", "age_s": 10, "remaining_pct": 50},
            "cursor": {"status": "cool", "freshness": "fresh", "age_s": 15, "remaining_pct": 60},
        },
    }
    gc_bad_res = adm.evaluate(
        "workspace-write", empty_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=gc_malformed, now=now,
    )
    assert gc_bad_res.admitted and gc_bad_res.meter is not None
    assert gc_bad_res.meter.telemetry == "unavailable:invalid_budget" and gc_bad_res.meter.cap == 2
    rl_tasks = tmp_path / "gc-rl-tasks"
    _record(rl_tasks, "grok-rl", status="rate_limited", agent="grok", finished_at=now.isoformat())
    gc_bad_rl = adm.evaluate(
        "workspace-write", rl_tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=gc_malformed, now=now,
    )
    assert not gc_bad_rl.admitted and gc_bad_rl.meter is not None
    assert gc_bad_rl.meter.telemetry == "unavailable:invalid_budget" and gc_bad_rl.meter.defer == adm.MeterDefer.RATE_LIMITED

    # Fresh reading with 8 live hits cap 8; aggregate stale flag does not lower a fresh meter's cap;
    # missing row drops to unknown cap 2.
    fresh = {"status": "cool", "freshness": "fresh", "age_s": 0, "remaining_pct": 20}
    for result, expected_cap in ((decide({}), 2), (decide(fresh, stale=True), 8), (decide(fresh), 8)):
        assert not result.admitted
        assert result.meter is not None and result.meter.cap == expected_cap
        assert result.meter.defer == adm.MeterDefer.CAP

    _record(tasks, "claude-b-limited", status="rate_limited", agent="claude", claude_account="b",
            finished_at=now.isoformat())
    for account, expected in (("b", adm.MeterDefer.RATE_LIMITED), ("a", None)):
        result = adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                              meter=f"claude:{account}", now=now)
        assert result.meter is not None and result.meter.defer == expected
        assert result.admitted == (expected is None)

    def overdeep_budget():
        return json_depth.loads("[" * 600 + "]" * 600, max_depth=512)

    monkeypatch.setattr(pool_selector, "read_budget", overdeep_budget)
    monkeypatch.setattr(host_admission, "tasks_dir", lambda: tmp_path / "overdeep-tasks")
    monkeypatch.setattr(adm, "load_thresholds", lambda: limits)
    spec = SimpleNamespace(mode="workspace-write", agent="codex", model="gpt-6.2-sol",
                           claude_account=None, codex_account="b-sub")
    result = host_admission.check(spec.mode, dry_run=True, disk_guard=False, spec=spec)
    assert result.admitted
    assert "meter codex:b: live write workers 0/2 (effective cap 2, telemetry=unavailable:invalid_budget)" in result.summary()

    # Monitor HTTP errors and timeouts map to typed unavailable reasons.
    def _raise(exc):
        def _fn():
            raise exc
        return _fn

    assert host_admission._monitor_json(_raise(urllib.error.HTTPError("http://x", 403, "Forbidden", {}, io.BytesIO()))) == (None, "http_403")
    assert host_admission._monitor_json(_raise(urllib.error.HTTPError("http://x", 503, "Warming", {}, io.BytesIO()))) == (None, "http_503")
    assert host_admission._monitor_json(_raise(urllib.error.URLError(TimeoutError("timed out")))) == (None, "timeout")
    assert host_admission._monitor_json(_raise(ConnectionRefusedError("refused"))) == (None, "unreachable")


def test_stale_lane_does_not_disable_another_meters_quota_deferral(tmp_path, probe):
    """Defect class: one lane's stale row or global diagnostics.stale disables another meter's quota deferral."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    limits = replace(adm.load_thresholds({}), max_live_write_workers=10, min_meter_launch_interval_s=0)
    budget = {
        "diagnostics": {"stale": True, "data_age_s": 1400},
        "generated_at": (now - timedelta(seconds=1200)).isoformat(),
        "claude_accounts": {
            "claude:a": {
                "status": "ok", "stale": False, "age_s": 45.0,
                "updated": (now - timedelta(seconds=45)).isoformat(),
                "weekly": {"left_pct": 0.0}, "session5h": {"left_pct": 50.0},
            },
        },
        "agents": {
            "grok": {"status": "exhausted", "freshness": "stale_last_good", "stale": True, "age_s": 1400, "remaining_pct": 0.0},
            "cursor": {"status": "cool", "freshness": "fresh", "age_s": 20, "remaining_pct": 50.0},
        },
    }
    # Claude account rows are refreshed on serve and defer even when cached snapshot and another lane are stale.
    claude_a = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="claude:a", meter_budget=budget, now=now,
    )
    assert not claude_a.admitted
    assert claude_a.meter is not None
    assert claude_a.meter.telemetry == "fresh"
    assert claude_a.meter.cap == 8
    assert claude_a.meter.defer == adm.MeterDefer.EXHAUSTED

    # With a fresh snapshot timestamp, fresh codex:a still defers despite grok being stale and diagnostics.stale=True.
    budget_fresh_snap = {
        **budget,
        "generated_at": (now - timedelta(seconds=30)).isoformat(),
        "codex_accounts": {
            "codex:a": {"status": "near_cap", "freshness": "fresh", "stale": False, "age_s": 30, "remaining_pct": 1.0},
        },
    }
    codex_a = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="codex:a", meter_budget=budget_fresh_snap, now=now,
    )
    assert not codex_a.admitted
    assert codex_a.meter is not None
    assert codex_a.meter.telemetry == "fresh"
    assert codex_a.meter.defer == adm.MeterDefer.NEAR_CAP

    # The stale grok+cursor meter itself does not apply quota denials and falls back to the unknown cap.
    gc = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=budget_fresh_snap, now=now,
    )
    assert gc.admitted
    assert gc.meter is not None
    assert gc.meter.telemetry == "stale:probe_stale"
    assert gc.meter.cap == 2

    # F2: Monitor's serve-time aggregate staleness withdrawal (_aged_routing_budget + _public_routing_budget)
    # must not turn two fresh, healthy Grok/Cursor rows into binding_unverified, while genuinely unhealthy
    # or non-withdrawn ineligible rows stay unverified.
    from lu_machinery.api import state_router

    raw_withdrawn = {
        "generated_at": (now - timedelta(seconds=10)).isoformat(),
        "diagnostics": {"stale": True, "data_age_s": 1200.0, "stale_threshold_s": 900},
        "recommendation": {"primary_agent_for_code": "codex", "warnings": []},
        "ranked_by_headroom": [{"lane": "cursor", "status": "cool"}],
        "agents": {
            "grok": {"status": "cool", "eligible": True, "freshness": "fresh", "age_s": 10.0, "remaining_pct": 0.0,
                     "health": {"healthy": True, "eligible": True}},
            "cursor": {"status": "cool", "eligible": True, "freshness": "fresh", "age_s": 10.0, "remaining_pct": 0.0,
                       "health": {"healthy": True, "eligible": True}},
        },
    }
    projected = state_router._public_routing_budget(
        state_router._aged_routing_budget(raw_withdrawn, 5.0, claude_fields={})
    )
    gc_withdrawn = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=projected, now=now,
    )
    assert not gc_withdrawn.admitted and gc_withdrawn.meter is not None
    assert gc_withdrawn.meter.telemetry == "fresh" and gc_withdrawn.meter.defer == adm.MeterDefer.EXHAUSTED

    # Genuine ineligibility (unhealthy under withdrawal, or explicit eligible=False without withdrawal) stays unverified.
    unhealthy_withdrawn = json.loads(json.dumps(raw_withdrawn))
    unhealthy_withdrawn["agents"]["cursor"]["health"] = {"healthy": False, "eligible": False}
    projected_unhealthy = state_router._public_routing_budget(
        state_router._aged_routing_budget(unhealthy_withdrawn, 5.0, claude_fields={})
    )
    gc_unhealthy = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=projected_unhealthy, now=now,
    )
    assert gc_unhealthy.admitted and gc_unhealthy.meter is not None
    assert gc_unhealthy.meter.telemetry == "unavailable:binding_unverified" and gc_unhealthy.meter.cap == 2

    non_withdrawn_inelig = {
        "generated_at": now.isoformat(),
        "diagnostics": {"stale": False, "data_age_s": 10.0, "stale_threshold_s": 900},
        "agents": {
            "grok": {"status": "cool", "eligible": True, "freshness": "fresh", "age_s": 10.0, "remaining_pct": 50.0,
                     "health": {"healthy": True}},
            "cursor": {"status": "cool", "eligible": False, "freshness": "fresh", "age_s": 10.0, "remaining_pct": 50.0,
                       "health": {"healthy": True}},
        },
    }
    gc_inelig = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
        meter="grok+cursor", meter_budget=non_withdrawn_inelig, now=now,
    )
    assert gc_inelig.admitted and gc_inelig.meter is not None
    assert gc_inelig.meter.telemetry == "unavailable:binding_unverified" and gc_inelig.meter.cap == 2


def test_unknown_meter_admits_at_most_the_unknown_cap(tmp_path, probe):
    """Defect class: an unknown or stale meter admits up to the full 8-worker ceiling instead of the reduced unknown cap."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    limits = replace(adm.load_thresholds({}), max_live_write_workers=10, min_meter_launch_interval_s=0)

    first = adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                         meter="codex:b", meter_budget=None, meter_budget_error="http_503", now=now)
    assert first.admitted and first.meter is not None and first.meter.cap == 2
    assert first.meter.telemetry == "unavailable:http_503"

    _record(tasks, "codex-b-1", agent="codex", codex_account="b-sub", started_at=now.isoformat())
    second = adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                          meter="codex:b", meter_budget=None, meter_budget_error="http_503", now=now)
    assert second.admitted and second.meter is not None and second.meter.live == 1 and second.meter.cap == 2

    _record(tasks, "codex-b-2", agent="codex", codex_account="b-sub", started_at=now.isoformat())
    third = adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                         meter="codex:b", meter_budget=None, meter_budget_error="http_503", now=now)
    assert not third.admitted and third.meter is not None
    assert third.meter.live == 2 and third.meter.cap == 2 and third.meter.defer == adm.MeterDefer.CAP

    # A verified fresh reading on the same meter restores the full cap of 8.
    fresh_budget = {
        "generated_at": now.isoformat(),
        "codex_accounts": {"codex:b": {"status": "cool", "freshness": "fresh", "stale": False, "age_s": 5, "remaining_pct": 60}},
    }
    fresh_third = adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                               meter="codex:b", meter_budget=fresh_budget, now=now)
    assert fresh_third.admitted and fresh_third.meter is not None
    assert fresh_third.meter.telemetry == "fresh" and fresh_third.meter.cap == 8

    # Configurable unknown cap is honored and clamped to the normal cap.
    raised = replace(
        adm.load_thresholds({"DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN": "3"}),
        max_live_write_workers=10, min_meter_launch_interval_s=0,
    )
    admitted_at_3 = adm.evaluate("workspace-write", tasks, thresholds=raised, pid_alive=lambda _pid: True,
                                 meter="codex:b", meter_budget=None, now=now)
    assert admitted_at_3.admitted and admitted_at_3.meter is not None and admitted_at_3.meter.cap == 3

    clamped = replace(
        adm.load_thresholds({
            "DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER": "2",
            "DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN": "5",
        }),
        max_live_write_workers=10, min_meter_launch_interval_s=0,
    )
    refused_clamped = adm.evaluate("workspace-write", tasks, thresholds=clamped, pid_alive=lambda _pid: True,
                                   meter="codex:b", meter_budget=None, now=now)
    assert not refused_clamped.admitted and refused_clamped.meter is not None and refused_clamped.meter.cap == 2


def test_admit_line_names_the_telemetry_state(tmp_path, probe, capsys):
    """Defect class: admission output or task record omits telemetry state or effective cap."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    limits = replace(adm.load_thresholds({}), max_live_write_workers=10, min_meter_launch_interval_s=0)

    # Fresh
    fresh = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True, meter="codex:a",
        meter_budget={"generated_at": now.isoformat(),
                      "codex_accounts": {"codex:a": {"status": "cool", "freshness": "fresh", "age_s": 5, "remaining_pct": 80}}},
        now=now,
    )
    assert "effective cap 8, telemetry=fresh" in fresh.summary()
    rec = fresh.to_record()
    assert rec["meter_telemetry"] == "fresh" and rec["meter_effective_cap"] == 8 and rec["meter"] == "codex:a"

    # Stale
    stale = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True, meter="codex:a",
        meter_budget={"generated_at": (now - timedelta(seconds=950)).isoformat(),
                      "codex_accounts": {"codex:a": {"status": "cool", "freshness": "fresh", "age_s": 5, "remaining_pct": 80}}},
        now=now,
    )
    assert "effective cap 2, telemetry=stale:snapshot_expired" in stale.summary()
    assert stale.to_record()["meter_telemetry"] == "stale:snapshot_expired"
    assert stale.to_record()["meter_effective_cap"] == 2

    # Unavailable
    unavailable = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True, meter="codex:a",
        meter_budget=None, meter_budget_error="http_403", now=now,
    )
    assert "effective cap 2, telemetry=unavailable:http_403" in unavailable.summary()
    assert unavailable.to_record()["meter_telemetry"] == "unavailable:http_403"

    # Not checked (unmetered write keeps host-only summary for capacity_pick while dispatch report names it)
    unmetered = adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True, now=now)
    assert "meter telemetry=not checked" not in unmetered.summary()
    host_admission.report(unmetered)
    assert "meter telemetry=not checked, effective cap=none" in capsys.readouterr().err
    assert unmetered.to_record()["meter_telemetry"] == "not checked"
    assert unmetered.to_record()["meter_effective_cap"] is None

    probe["probe"] = adm.HostProbe(mem_available_bytes=1 * _GIB, load1=40.0, cpu_count=8, proc_available=True)
    early = adm.precheck("workspace-write", thresholds=limits)
    assert early is not None
    assert "meter telemetry=not checked, effective cap=none" in early.refusal_line()
    assert early.to_record()["meter_telemetry"] == "not checked"
    assert early.to_record()["meter_effective_cap"] is None

    # A meter refusal with --force-admission is not marked forced in the task record.
    exhausted = adm.evaluate(
        "workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True, meter="codex:a",
        meter_budget={"generated_at": now.isoformat(),
                      "codex_accounts": {"codex:a": {"status": "cool", "freshness": "fresh", "age_s": 5, "remaining_pct": 0}}},
        now=now,
    )
    assert not exhausted.admitted and exhausted.meter_refused
    forced_meter_rec = exhausted.to_record(force_reason="attempted override")
    assert forced_meter_rec["forced"] is False
    assert forced_meter_rec["meter_telemetry"] == "fresh"
    assert forced_meter_rec["meter_defer"] == "exhausted"


def test_grok_and_cursor_share_one_meter(tmp_path, probe):
    """Defect class: Grok and Cursor each spend a slot on a different meter."""
    tasks = tmp_path / "tasks"
    _record(tasks, "grok", agent="grok")
    _record(tasks, "cursor", agent="cursor")
    limits = replace(_LIMITS, max_live_write_workers=5, max_live_write_workers_per_meter=2)

    for agent in ("cursor", "grok"):
        decision = adm.evaluate("workspace-write", tasks, thresholds=limits,
                                pid_alive=lambda _pid: True, meter=adm.subscription_meter(agent))
        assert not decision.admitted
        assert "meter grok+cursor: live write workers 2/2" in decision.refusal_line()
        assert "subscription_meter_defer=cap" in decision.refusal_line()


def test_read_only_and_terminal_records_do_not_hold_write_slots(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "reader", pid=11, mode="read-only")
    _record(tasks, "finished", pid=12, status="done")
    _record(tasks, "writer", pid=13)

    decision = adm.evaluate("danger", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    assert decision.admitted
    assert decision.live_task_ids == ("writer",)


def test_dead_pid_does_not_count_and_is_handed_to_the_sweeper(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "alive", pid=11)
    _record(tasks, "dead-writer", pid=12)
    _record(tasks, "dead-reader", pid=13, mode="read-only", status="spawning")
    swept: list[str] = []

    decision = adm.evaluate(
        "workspace-write",
        tasks,
        pid_alive=lambda pid: pid == 11,
        on_dead=lambda _path, state: swept.append(state["task_id"]),
        thresholds=_LIMITS,
    )

    assert decision.admitted
    assert decision.live_task_ids == ("alive",)
    assert sorted(swept) == ["dead-reader", "dead-writer"]
    assert decision.swept
    assert "2 record(s) marked crashed" in decision.summary()


def test_report_only_evaluation_names_dead_records_without_sweeping(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "dead", pid=12)

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: False, thresholds=_LIMITS)

    assert not decision.swept
    assert decision.dead_task_ids == ("dead",)
    assert "1 record(s) dead pid, not counted: dead" in decision.summary()


def test_pidless_spawning_record_holds_a_slot_only_during_the_grace_window(tmp_path, probe):
    tasks = tmp_path / "tasks"
    stale = (datetime.now(UTC) - timedelta(seconds=adm.PIDLESS_SPAWNING_GRACE_S + 60)).isoformat()
    _record(tasks, "publishing", status="spawning", pid=None)
    _record(tasks, "orphaned", status="spawning", pid=None, started_at=stale)

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: False, thresholds=_LIMITS)

    assert decision.live_task_ids == ("publishing",)
    assert decision.dead_task_ids == ()


def test_pidless_spawning_record_from_the_future_does_not_hold_a_slot(tmp_path, probe, caplog):
    tasks = tmp_path / "tasks"
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    skewed = (datetime.now(UTC) + timedelta(seconds=adm.PIDLESS_CLOCK_SKEW_S / 2)).isoformat()
    _record(tasks, "future", status="spawning", pid=None, started_at=future)
    _record(tasks, "skewed", status="spawning", pid=None, started_at=skewed)

    with caplog.at_level("WARNING", logger=adm.__name__):
        decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: False, thresholds=_LIMITS)

    assert decision.live_task_ids == ("skewed",)
    assert "pidless spawning record future has started_at" in caplog.text
    assert "skewed" not in caplog.text


def test_low_mem_available_is_refused(tmp_path, probe):
    probe["probe"] = adm.HostProbe(mem_available_bytes=int(2.5 * _GIB), load1=1.0, cpu_count=8, proc_available=True)

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert not decision.admitted
    [failure] = decision.failures
    assert failure == ("MemAvailable 2.5 GiB is below the floor of 3.5 GiB (DISPATCH_MIN_MEM_AVAILABLE_GIB=3.5)")


def test_high_load_per_cpu_is_refused(tmp_path, probe):
    probe["probe"] = adm.HostProbe(mem_available_bytes=12 * _GIB, load1=14.0, cpu_count=8, proc_available=True)

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert not decision.admitted
    [failure] = decision.failures
    assert failure.startswith("load 1.75 per CPU (1-minute load 14.00 on 8 CPUs) is above the limit of 1.50")
    assert "DISPATCH_MAX_LOAD_PER_CPU=1.5" in failure


def test_single_cpu_load_failure_uses_singular(tmp_path, probe):
    probe["probe"] = adm.HostProbe(mem_available_bytes=12 * _GIB, load1=3.0, cpu_count=1, proc_available=True)

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    [failure] = decision.failures
    assert "(1-minute load 3.00 on 1 CPU) is above" in failure


def test_every_failed_check_is_named_in_one_refusal_line(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "a", pid=11)
    _record(tasks, "b", pid=12)
    probe["probe"] = adm.HostProbe(mem_available_bytes=1 * _GIB, load1=40.0, cpu_count=8, proc_available=True)

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    line = decision.refusal_line()
    assert "\n" not in line
    assert len(decision.failures) == 3
    for name in (adm.ENV_MAX_LIVE_WRITE_WORKERS, adm.ENV_MIN_MEM_AVAILABLE_GIB, adm.ENV_MAX_LOAD_PER_CPU):
        assert name in line


def test_read_only_mode_is_exempt_without_scanning(tmp_path, monkeypatch):
    monkeypatch.setattr(adm, "scan_task_records", lambda *_a, **_k: pytest.fail("read-only must not scan"))
    monkeypatch.setattr(adm, "probe_host", lambda *_a, **_k: pytest.fail("read-only must not probe"))

    decision = adm.evaluate("read-only", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.exempt and decision.admitted


def test_without_proc_memory_and_cpu_are_unknown_and_only_the_cap_applies(tmp_path, monkeypatch):
    missing_proc = tmp_path / "no-proc"
    no_proc_probe = adm.read_host(missing_proc)
    assert no_proc_probe == adm.HostProbe(
        mem_available_bytes=None, load1=None, cpu_count=os.cpu_count(), proc_available=False
    )
    monkeypatch.setattr(adm, "probe_host", lambda *_a, **_k: no_proc_probe)
    tasks = tmp_path / "tasks"
    _record(tasks, "a", pid=11)

    admitted = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)
    assert admitted.admitted
    summary = admitted.summary()
    assert "MemAvailable unknown" in summary
    assert "load unknown" in summary
    assert "only the worker cap is enforced" in summary

    _record(tasks, "b", pid=12)
    refused = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)
    assert not refused.admitted
    assert [f.split(" reached")[0] for f in refused.failures] == ["live write workers 2/2"]


def test_probe_host_reads_meminfo_and_loadavg(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal:       12582912 kB\nMemAvailable:    2621440 kB\n", encoding="ascii")
    (proc / "loadavg").write_text("3.25 2.00 1.00 2/900 12345\n", encoding="ascii")
    (proc / "stat").write_text("cpu  100 2 30 400 5 6 7 8 9 10\ncpu0 10 0 3 40 0 0 0 0 0 0\n", encoding="ascii")

    probe = adm.read_host(proc)

    assert probe.proc_available
    assert probe.mem_available_bytes == 2621440 * 1024
    assert probe.mem_available_gib == pytest.approx(2.5)
    assert probe.load1 == 3.25
    assert probe.cpu_steal_ticks == 8
    assert probe.cpu_total_ticks == 558
    snapshot = adm.AdmissionDecision(
        mode="workspace-write", exempt=False, admitted=True, thresholds=_LIMITS, probe=probe
    ).to_record()
    assert (snapshot["cpu_steal_ticks"], snapshot["cpu_total_ticks"]) == (8, 558)


@pytest.mark.parametrize("stat_text", ["", "cpu  1 2 3\n", "cpu  1 2 3 4 5 6 7 invalid\n", "cpu  1 2 3 4 5 6 7 -1\n"])
def test_probe_host_marks_invalid_cpu_steal_unknown(tmp_path, stat_text):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(stat_text, encoding="ascii")

    probe = adm.read_host(proc)

    assert probe.cpu_steal_ticks is None
    assert probe.cpu_total_ticks is None


def test_thresholds_default_to_config_and_honour_env_overrides():
    from lu_machinery import config

    defaults = adm.load_thresholds({})
    assert defaults == adm.Thresholds(
        max_live_write_workers=config.DISPATCH_MAX_LIVE_WRITE_WORKERS,
        min_mem_available_gib=config.DISPATCH_MIN_MEM_AVAILABLE_GIB,
        max_load_per_cpu=config.DISPATCH_MAX_LOAD_PER_CPU,
    )
    assert (
        defaults.max_live_write_workers,
        defaults.min_mem_available_gib,
        defaults.max_load_per_cpu,
        defaults.worker_mem_reserve_gib,
    ) == (2, 8.5, 1.0, 2.5)

    overridden = adm.load_thresholds(
        {
            adm.ENV_MAX_LIVE_WRITE_WORKERS: "0",
            adm.ENV_MIN_MEM_AVAILABLE_GIB: "6",
            adm.ENV_MAX_LOAD_PER_CPU: "0.75",
        }
    )
    assert overridden == adm.Thresholds(max_live_write_workers=0, min_mem_available_gib=6.0, max_load_per_cpu=0.75)


def test_defaults_allow_cap_slots_and_refuse_below_the_floor(tmp_path, probe):
    tasks = tmp_path / "tasks"
    defaults = adm.load_thresholds({})
    _record(tasks, "writer-00", pid=1000)

    before_cap = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=defaults)
    assert before_cap.admitted
    assert "live write workers 1/2" in before_cap.summary()

    _record(tasks, "writer-01", pid=1001)
    at_cap = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=defaults)
    assert not at_cap.admitted
    assert "live write workers 2/2 reached the cap" in at_cap.failures[0]

    probe["probe"] = adm.HostProbe(mem_available_bytes=int(8.4 * _GIB), load1=0.0, cpu_count=8, proc_available=True)
    below_floor = adm.evaluate("workspace-write", tmp_path / "empty", thresholds=defaults)
    assert not below_floor.admitted
    assert "below the floor of 8.5 GiB" in below_floor.failures[0]


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        (adm.ENV_MAX_LIVE_WRITE_WORKERS, "five"),
        (adm.ENV_MAX_LIVE_WRITE_WORKERS, "2.5"),
        (adm.ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN, "0"),
        (adm.ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN, "-1"),
        (adm.ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN, "2.5"),
        (adm.ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN, "two"),
        (adm.ENV_MIN_MEM_AVAILABLE_GIB, "-1"),
        (adm.ENV_MAX_LOAD_PER_CPU, "nan"),
    ],
)
def test_invalid_threshold_env_is_rejected(name, raw):
    with pytest.raises(ValueError, match=name):
        adm.load_thresholds({name: raw})


def test_admission_lock_is_exclusive_across_holders(tmp_path):
    tasks = tmp_path / "tasks"
    with adm.admission_lock(tasks), pytest.raises(adm.AdmissionLockTimeout), adm.admission_lock(tasks, timeout_s=0.1):
        pass
    # Released on exit: the next holder gets it at once.
    with adm.admission_lock(tasks, timeout_s=0.1):
        pass
    assert (tasks / adm.LOCK_FILE_NAME).is_file()


# --- #8717: pid-less records owned by a live dispatcher --------------------------------------


def _own_identity() -> dict:
    return worktree_prep.process_identity(os.getpid())


def _pidless_owned_record(tasks_dir: Path, task_id: str, *, block: str, owner: dict, age_s: float) -> Path:
    """A pid-less ``spawning`` write record whose ``block`` (worktree_prep or admission_hold) names ``owner``."""
    return _record(
        tasks_dir,
        task_id,
        status="spawning",
        pid=None,
        started_at=(datetime.now(UTC) - timedelta(seconds=age_s)).isoformat(),
        **{block: {"run_nonce": f"nonce-{task_id}", "owner_pid": owner["pid"], "owner_start": owner["start"]}},
    )


@pytest.mark.parametrize("block", ["worktree_prep", adm.ADMISSION_HOLD_KEY])
def test_pidless_record_of_a_live_dispatcher_holds_a_slot_past_the_grace_window(tmp_path, probe, block):
    """A slow ``git worktree add`` (up to 900 s) keeps its admitted slot while the dispatcher lives."""
    tasks = tmp_path / "tasks"
    _pidless_owned_record(tasks, "slow-prep", block=block, owner=_own_identity(), age_s=300)
    assert adm.PIDLESS_SPAWNING_GRACE_S < 300

    decision = adm.evaluate("workspace-write", tasks, thresholds=_LIMITS)

    assert decision.live_task_ids == ("slow-prep",)
    assert decision.dead_task_ids == ()


@pytest.mark.parametrize("block", ["worktree_prep", adm.ADMISSION_HOLD_KEY])
def test_pidless_record_of_a_dead_dispatcher_is_not_counted_and_is_handed_to_the_sweeper(tmp_path, probe, block):
    from tests.worktree_prep_helpers import exited_process_identity

    tasks = tmp_path / "tasks"
    pid, start = exited_process_identity()
    path = _pidless_owned_record(tasks, "orphan", block=block, owner={"pid": pid, "start": start}, age_s=10)
    swept: list[Path] = []

    decision = adm.evaluate("workspace-write", tasks, on_dead=lambda p, _state: swept.append(p), thresholds=_LIMITS)

    assert decision.live_task_ids == ()
    assert decision.dead_task_ids == ("orphan",)
    assert swept == [path]


def test_pidless_record_of_a_gone_dispatcher_without_start_time_is_not_counted_or_swept(tmp_path, probe):
    """Without a recorded start time a missing pid frees the slot, but is no proof for marking it crashed."""
    from tests.worktree_prep_helpers import exited_process_identity

    tasks = tmp_path / "tasks"
    pid, _start = exited_process_identity()
    _pidless_owned_record(tasks, "no-start", block="worktree_prep", owner={"pid": pid, "start": None}, age_s=10)

    decision = adm.evaluate("workspace-write", tasks, on_dead=lambda *_a: pytest.fail("no proof"), thresholds=_LIMITS)

    assert decision.live_task_ids == ()
    assert decision.dead_task_ids == ()


@pytest.fixture(autouse=True)
def _omit_live_slice(monkeypatch):
    """Admission fixtures do not read the host's lu-dispatch.slice."""
    monkeypatch.setattr(adm, "slice_usage_clause", lambda: None)


def test_summary_adds_active_slice_use_without_changing_the_decision(tmp_path, probe, monkeypatch):
    tasks = tmp_path / "tasks"
    _record(tasks, "writer", pid=11)
    monkeypatch.setattr(adm, "slice_usage_clause", lambda: "lu-dispatch.slice 0.4/11.0 GiB")

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    assert decision.admitted
    assert decision.failures == ()
    assert "lu-dispatch.slice 0.4/11.0 GiB" in decision.summary()


def test_summary_omits_the_slice_when_it_is_not_reported(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "writer", pid=11)

    summary = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS).summary()

    assert "lu-dispatch.slice" not in summary


# --- Shared lu.slice pool headroom (#9975) --------------------------------------------------


def _fake_pool(directory: Path, *, current: int, file_cache: int, high: int = 21 * _GIB) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "memory.current").write_text(f"{current}\n", encoding="ascii")
    (directory / "memory.high").write_text(f"{high}\n", encoding="ascii")
    (directory / "memory.max").write_text(f"{23 * _GIB}\n", encoding="ascii")
    (directory / "memory.stat").write_text(f"active_file {file_cache}\ninactive_file 0\n", encoding="ascii")
    return directory


def test_full_shared_pool_refuses_a_write_worker(tmp_path, probe, monkeypatch):
    pool = _fake_pool(tmp_path / "lu.slice", current=21 * _GIB, file_cache=_GIB // 2)
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))
    tasks = tmp_path / "tasks"

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    assert not decision.admitted
    [failure] = decision.failures
    assert failure == (
        "lu.slice non-cache use 20.5 GiB plus a 2.5 GiB worker reserve exceeds MemoryHigh 21.0 GiB "
        "(DISPATCH_WORKER_MEM_RESERVE_GIB=2.5)"
    )
    record = decision.to_record()
    assert record["pool_nonreclaimable_gib"] == 20.5
    assert record["pool_file_cache_gib"] == 0.5
    assert record["pool_limit_gib"] == 21.0
    assert record["worker_mem_reserve_gib"] == 2.5
    assert record["pool_check_skipped"] is None


def test_file_cache_does_not_count_against_the_pool(tmp_path, probe, monkeypatch):
    pool = _fake_pool(tmp_path / "lu.slice", current=28 * _GIB, file_cache=14 * _GIB)
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert "lu.slice 14.0/21.0 GiB non-cache (+14.0 GiB file cache; worker reserve 2.5 GiB)" in decision.summary()


def test_worker_reserve_env_override_changes_the_pool_decision(tmp_path, probe, monkeypatch):
    pool = _fake_pool(tmp_path / "lu.slice", current=17 * _GIB, file_cache=0)
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))
    tasks = tmp_path / "tasks"

    roomy = adm.evaluate("workspace-write", tasks, thresholds=_LIMITS)
    strict = adm.evaluate(
        "workspace-write", tasks, thresholds=adm.load_thresholds({adm.ENV_WORKER_MEM_RESERVE_GIB: "5.5"})
    )

    assert roomy.admitted
    assert not strict.admitted
    assert any("DISPATCH_WORKER_MEM_RESERVE_GIB=5.5" in failure for failure in strict.failures)


def test_missing_pool_cgroup_skips_the_check_and_logs_why(tmp_path, probe, monkeypatch, caplog):
    monkeypatch.setenv("LU_SLICE_CGROUP", str(tmp_path / "absent" / "lu.slice"))

    with caplog.at_level("WARNING", logger=adm.__name__):
        decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert "lu.slice pool check skipped (lu.slice memory.current unavailable: " in decision.summary()
    assert str(tmp_path) not in decision.summary()
    assert "memory.current unavailable" in decision.to_record()["pool_check_skipped"]
    [warning] = [r for r in caplog.records if "shared pool check skipped" in r.getMessage()]
    assert warning.levelname == "WARNING"


def test_malformed_memory_high_skips_with_reason_in_the_admitted_line(tmp_path, probe, monkeypatch, caplog):
    pool = _fake_pool(tmp_path / "lu.slice", current=25 * _GIB, file_cache=0)
    (pool / "memory.high").write_text("28G\n", encoding="ascii")
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))

    with caplog.at_level("WARNING", logger=adm.__name__):
        decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert "lu.slice pool check skipped (lu.slice memory.high is neither a number nor 'max')" in decision.summary()
    assert "memory.high is neither a number nor 'max'" in caplog.text


def test_healthy_test_host_never_reads_the_real_pool(tmp_path, probe, monkeypatch):
    monkeypatch.delenv("LU_SLICE_CGROUP", raising=False)
    monkeypatch.setattr(adm.pool_headroom, "check_pool", lambda *_a, **_k: pytest.fail("real pool read"))

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert decision.pool is not None
    assert decision.pool.skipped == "test host"
    assert "lu.slice pool check skipped (test host)" in decision.summary()


def test_read_only_dispatch_does_not_probe_the_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(adm, "probe_pool", lambda *_a, **_k: pytest.fail("read-only must not probe the pool"))

    assert adm.evaluate("read-only", tmp_path / "tasks").admitted


def test_deployment_env_file_fills_thresholds_the_environment_does_not_set(tmp_path):
    env_file = tmp_path / "admission.env"
    env_file.write_text(
        "# deployment overrides\n"
        f"export {adm.ENV_MAX_LIVE_WRITE_WORKERS}=7\n"
        f"{adm.ENV_MIN_MEM_AVAILABLE_GIB}='5.5'\n"
        "UNRELATED=1\n"
        "not a line\n",
        encoding="utf-8",
    )
    limits = adm.load_thresholds(
        {adm.ENV_ADMISSION_FILE: str(env_file), adm.ENV_MIN_MEM_AVAILABLE_GIB: "9.5"}
    )
    assert limits.max_live_write_workers == 7  # from the file
    assert limits.min_mem_available_gib == 9.5  # the environment wins over the file
    assert limits.max_load_per_cpu == adm.config_defaults().max_load_per_cpu


def test_env_file_is_optional_and_can_be_disabled(tmp_path, monkeypatch):
    assert adm.load_thresholds({adm.ENV_ADMISSION_FILE: str(tmp_path / "missing.env")}) == adm.config_defaults()
    assert adm.admission_env_file({adm.ENV_ADMISSION_FILE: ""}, live=True) is None
    # An explicit mapping without the variable reads no file; the live default is XDG-based.
    assert adm.admission_env_file({}, live=False) is None
    assert adm.admission_env_file({"XDG_CONFIG_HOME": str(tmp_path)}, live=True) == (
        tmp_path / "learn-ukrainian" / adm.ADMISSION_ENV_FILE_NAME
    )


def test_live_thresholds_read_the_default_env_file(tmp_path, monkeypatch):
    target = tmp_path / "learn-ukrainian" / adm.ADMISSION_ENV_FILE_NAME
    target.parent.mkdir()
    target.write_text(f"{adm.ENV_MAX_LOAD_PER_CPU}=0.5\n", encoding="utf-8")
    monkeypatch.delenv(adm.ENV_ADMISSION_FILE, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert adm.load_thresholds().max_load_per_cpu == 0.5


def test_invalid_env_file_value_is_refused(tmp_path):
    env_file = tmp_path / "admission.env"
    env_file.write_text(f"{adm.ENV_MAX_LIVE_WRITE_WORKERS}=many\n", encoding="utf-8")
    with pytest.raises(ValueError, match="is not a whole number"):
        adm.load_thresholds({adm.ENV_ADMISSION_FILE: str(env_file)})


# --- Host test-slot queue (#10662) -----------------------------------------------------------


@pytest.fixture
def slot_pool(tmp_path, monkeypatch):
    """A private one-slot pool; an outer governed run's holder marker must not leak in."""
    from lu_machinery.common import test_slots

    monkeypatch.setenv(test_slots.ENABLE_ENV, "on")
    monkeypatch.setenv(test_slots.DIR_ENV, str(tmp_path / "test-slots"))
    monkeypatch.setenv(test_slots.COUNT_ENV, "1")
    monkeypatch.delenv(test_slots.HOLDER_ENV, raising=False)
    return test_slots


def test_a_long_test_slot_queue_refuses_a_write_worker(tmp_path, probe, monkeypatch):
    monkeypatch.setattr(adm, "probe_test_slots", lambda: adm.SlotQueueProbe(depth=5, pool_size=14))
    limits = adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "4"})

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=limits)

    assert not decision.admitted
    [failure] = decision.failures
    assert failure == "test-slot queue 5 is longer than 4 waiting runs (DISPATCH_MAX_TEST_SLOT_QUEUE=4)"
    record = decision.to_record()
    assert (record["test_slot_queue_depth"], record["max_test_slot_queue"]) == (5, 4)
    assert record["test_slot_queue_skipped"] is None


def test_a_queue_at_the_limit_is_admitted_and_reported(tmp_path, probe, monkeypatch):
    monkeypatch.setattr(adm, "probe_test_slots", lambda: adm.SlotQueueProbe(depth=4, pool_size=14))

    decision = adm.evaluate(
        "workspace-write", tmp_path / "tasks", thresholds=adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "4"})
    )

    assert decision.admitted
    assert "test-slot queue 4 (limit 4, pool 14)" in decision.summary()


def test_the_real_test_slot_queue_is_counted(tmp_path, probe, slot_pool):
    import subprocess
    import sys

    from tests.wait_helpers import wait_for_line

    repo = Path(__file__).resolve().parents[2]
    log = tmp_path / "waiter.log"
    with slot_pool.acquire(1, kind="pytest"):
        env = {k: v for k, v in os.environ.items() if k != slot_pool.HOLDER_ENV}
        env["PYTHONPATH"] = str(repo)
        with log.open("w", encoding="utf-8") as stderr:
            waiter = subprocess.Popen(
                [sys.executable, "-m", "lu_machinery.common.test_slots", "run", "--", sys.executable, "-c", "pass"],
                cwd=repo,
                env=env,
                stderr=stderr,
            )
        try:
            wait_for_line(log, stop_if=lambda: waiter.poll() is not None)
            strict = adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "0"})
            decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=strict)
        finally:
            waiter.terminate()
            waiter.wait(timeout=30)

    assert not decision.admitted
    assert decision.failures == (
        "test-slot queue 1 is longer than 0 waiting runs (DISPATCH_MAX_TEST_SLOT_QUEUE=0)",
    )


@pytest.mark.parametrize(("setting", "clause"), [("off", "pool disabled"), ("maybe", "invalid_configuration")])
def test_a_disabled_or_unreadable_pool_skips_the_queue_check_and_says_why(tmp_path, probe, slot_pool, monkeypatch, setting, clause):
    monkeypatch.setenv(slot_pool.ENABLE_ENV, setting)

    decision = adm.evaluate(
        "workspace-write", tmp_path / "tasks", thresholds=adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "0"})
    )

    assert decision.admitted
    assert f"test-slot queue check skipped ({clause}" in decision.summary()
    assert clause in decision.to_record()["test_slot_queue_skipped"]


def test_test_slot_queue_threshold_defaults_to_config_and_rejects_invalid_values():
    from lu_machinery import config

    assert adm.load_thresholds({}).max_test_slot_queue == config.DISPATCH_MAX_TEST_SLOT_QUEUE == 4
    assert adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "2"}).max_test_slot_queue == 2
    with pytest.raises(ValueError, match=adm.ENV_MAX_TEST_SLOT_QUEUE):
        adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "many"})


def test_read_only_dispatch_does_not_read_the_test_slot_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(adm, "probe_test_slots", lambda: pytest.fail("read-only must not read the slot pool"))

    assert adm.evaluate("read-only", tmp_path / "tasks").admitted


def test_healthy_test_host_never_reads_the_real_test_slot_pool(tmp_path, probe, monkeypatch):
    monkeypatch.delenv(adm.test_slots.DIR_ENV, raising=False)
    monkeypatch.setattr(adm.test_slots, "status", lambda *_a, **_k: pytest.fail("real slot pool read"))

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert decision.slots is not None and decision.slots.skipped == "test host"
    assert "test-slot queue check skipped (test host)" in decision.summary()


_KUBE_DOJO_URL = "https://github.com/kube-dojo/kube-dojo.github.io.git"
_LEARN_UKRAINIAN_URL = "git@github.com:learn-ukrainian/learn-ukrainian.github.io.git"


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    from lu_machinery.orchestration.execution_safe_git import GIT, safe_git_env

    env = {**safe_git_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run([GIT, "-C", str(cwd), *args], env=env, check=True, capture_output=True, timeout=60)


def _checkout(path: Path, *origins: str) -> Path:
    """A real Git checkout at ``path`` with one commit and the given ``origin`` URLs."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    for index, url in enumerate(origins):
        _git(path, "config", *(["--add"] if index else []), "remote.origin.url", url)
    return path


def _mark_curriculum(root: Path) -> None:
    marker = root / "curriculum" / "l2-uk-en" / "curriculum.yaml"
    marker.parent.mkdir(parents=True)
    marker.write_text("levels: {}\n", encoding="utf-8")


def test_consumer_without_admission_policy_uses_fail_closed_machinery_default(tmp_path, probe, monkeypatch):
    """Defect class (F5): a registered non-curriculum consumer without an admission policy file is refused
    (policy_missing), or its default policy admits Ukrainian content or seats."""
    from lu_machinery.orchestration import product_policy

    consumer = _checkout(tmp_path / "renamed-kd", _KUBE_DOJO_URL)
    worktree = tmp_path / "linked-kd"
    _git(consumer, "worktree", "add", "-q", "-b", "wt", str(worktree))
    for root in (consumer, worktree):
        monkeypatch.setenv("LU_PRODUCT_ROOT", str(root))
        policy = adm.load_product_policy()
        assert policy.origin == product_policy.MACHINERY_DEFAULT_ORIGIN
        assert not adm.is_content_seat("curriculum-orchestrator")
        assert not any(adm.ukrainian_content_family_allowed(f) for f in ("anthropic", "google", "openai"))
        assert not adm.is_ukrainian_review_seat("agy", "google")
        assert not adm.requires_ukrainian_review(["curriculum/l2-uk-en/a1/lesson.md", "src/index.ts"])
        assert adm.required_review_receipts(["google"]) == frozenset({"google", "openai"})
        assert adm.precheck("workspace-write", thresholds=_LIMITS) is None
        assert adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS).admitted

    # An explicit path is always read.
    with pytest.raises(product_policy.ProductPolicyError) as explicit_missing:
        adm.load_product_policy(tmp_path / "missing.json")
    assert explicit_missing.value.code == "policy_missing"


def _unidentifiable_roots(tmp_path: Path) -> dict[str, Path]:
    """Checkouts that must keep the Learn Ukrainian requirements: Git cannot tie each to one consumer row."""
    roots = {
        "no-git": tmp_path / "kube-dojo",
        "learn-ukrainian-origin": _checkout(tmp_path / "lu-origin", _LEARN_UKRAINIAN_URL),
        "unregistered-origin": _checkout(tmp_path / "other", "https://github.com/other/repo.git"),
        "lookalike-origin": _checkout(tmp_path / "lookalike", _KUBE_DOJO_URL.replace("k", "K", 1)),
        "two-origins": _checkout(tmp_path / "two", _KUBE_DOJO_URL, _LEARN_UKRAINIAN_URL),
        "no-origin": _checkout(tmp_path / "bare-name"),
        "include": _checkout(tmp_path / "include", _KUBE_DOJO_URL),
        "url-rewrite": _checkout(tmp_path / "rewrite", _KUBE_DOJO_URL),
        "marker": _checkout(tmp_path / "marked", _KUBE_DOJO_URL),
        "malformed-config": _checkout(tmp_path / "malformed", _KUBE_DOJO_URL),
    }
    roots["no-git"].mkdir()
    _git(roots["include"], "config", "include.path", "extra")
    _git(roots["url-rewrite"], "config", "url.https://github.com/learn-ukrainian/.insteadOf", "https://github.com/kube-dojo/")
    _mark_curriculum(roots["marker"])
    with (roots["malformed-config"] / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write("[broken-header\n")
    # Per-worktree config adds a second origin URL that Git itself reports.
    worktree_config = _checkout(tmp_path / "wtconfig", _KUBE_DOJO_URL)
    _git(worktree_config, "config", "extensions.worktreeConfig", "2")
    _git(worktree_config, "config", "--worktree", "--add", "remote.origin.url", _LEARN_UKRAINIAN_URL)
    roots["worktree-config-origin"] = worktree_config
    # A linked worktree whose primary checkout carries the curriculum marker.
    marked_primary = _checkout(tmp_path / "marked-primary", _KUBE_DOJO_URL)
    _mark_curriculum(marked_primary)
    _git(marked_primary, "worktree", "add", "-q", "-b", "wt", str(tmp_path / "marked-linked"))
    roots["marked-primary"] = tmp_path / "marked-linked"
    # A subdirectory of a consumer is not a checkout top; a symlink resolves to its target.
    consumer = _checkout(tmp_path / "consumer", _KUBE_DOJO_URL)
    (consumer / "sub").mkdir()
    roots["subdirectory"] = consumer / "sub"
    (tmp_path / "link-to-lu").symlink_to(roots["learn-ukrainian-origin"])
    roots["symlink-to-learn-ukrainian"] = tmp_path / "link-to-lu"
    # A gitfile Git refuses (no space after "gitdir:") naming the consumer's own Git directory.
    gitfile = tmp_path / "gitfile"
    gitfile.mkdir()
    (gitfile / ".git").write_text(f"gitdir:{consumer / '.git'}\n", encoding="utf-8")
    roots["gitfile-no-space"] = gitfile
    return roots


_UNIDENTIFIABLE = [
    "no-git",
    "learn-ukrainian-origin",
    "unregistered-origin",
    "lookalike-origin",
    "two-origins",
    "no-origin",
    "include",
    "url-rewrite",
    "marker",
    "malformed-config",
    "worktree-config-origin",
    "marked-primary",
    "subdirectory",
    "symlink-to-learn-ukrainian",
    "gitfile-no-space",
]


@pytest.mark.parametrize("case", _UNIDENTIFIABLE)
def test_unidentified_checkout_keeps_the_learn_ukrainian_policy_requirement(tmp_path, monkeypatch, case):
    """Defect class (F1/F3/F5): a checkout Git cannot tie to exactly one consumer row (or a curriculum
    checkout) silently drops the admission policy requirement instead of refusing policy_missing."""
    from lu_machinery.orchestration import product_policy

    root = _unidentifiable_roots(tmp_path)[case]
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(root))
    # Inherited Git redirection must not substitute another repository's metadata.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "consumer" / ".git"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'remote.origin.url'='{_KUBE_DOJO_URL}'")
    with pytest.raises(product_policy.ProductPolicyError) as exc_info:
        adm.evaluate("read-only", tmp_path / "tasks")
    assert exc_info.value.code == "policy_missing"


def test_fleet_repos_validates_declared_product_files(tmp_path, monkeypatch):
    """Defect class (F5 config validation): a malformed product file declaration, a declaration naming a
    location machinery does not read, or a Learn Ukrainian row without both declarations is accepted."""
    import yaml

    from lu_machinery.agent_runtime.sources_read_only import sources_server_path
    from lu_machinery.orchestration import fleet_repos, product_policy

    lu_row = {
        "github": "learn-ukrainian/learn-ukrainian.github.io",
        "local_name": "learn-ukrainian",
        "default": True,
        "sources_server": ".mcp/servers/sources/server.py",
        "admission_policy": "scripts/curriculum/admission_policy.v1.json",
    }

    def load(rows: dict) -> dict:
        cfg = tmp_path / "fleet_repos.yaml"
        cfg.write_text(yaml.safe_dump({"version": 1, "repos": rows}), encoding="utf-8")
        fleet_repos.clear_repository_cache()  # the registry reader caches by path
        return fleet_repos.load_fleet_repos(cfg)

    load({"primary": lu_row})
    for field in ("sources_server", "admission_policy"):
        for bad in (None, "", 123, ["x"]):
            with pytest.raises(fleet_repos.FleetRepoError, match=field):
                load({"primary": {**lu_row, field: bad}})
        for github in ("learn-ukrainian/learn-ukrainian.github.io", "Learn-Ukrainian/LEARN-UKRAINIAN.github.io"):
            with pytest.raises(fleet_repos.FleetRepoError, match=f"{field} is required"):
                load({"primary": {**{k: v for k, v in lu_row.items() if k != field}, "github": github}})

    # A declaration naming any other location refuses where the file is read.
    consumer = _checkout(tmp_path / "consumer", _KUBE_DOJO_URL)
    elsewhere = {"github": "kube-dojo/kube-dojo.github.io", "local_name": "kube-dojo"}
    elsewhere |= {"sources_server": "tools/server.py", "admission_policy": "policy/admission_policy.v1.json"}
    catalog = load({"primary": lu_row, "kube-dojo": elsewhere})
    monkeypatch.setattr(fleet_repos, "_bundled_catalog", lambda: catalog)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(consumer))
    with pytest.raises(ValueError, match="sources_server must be"):
        sources_server_path()
    with pytest.raises(product_policy.ProductPolicyError) as exc_info:
        adm.load_product_policy()
    assert exc_info.value.code == "policy_origin_mismatch"
