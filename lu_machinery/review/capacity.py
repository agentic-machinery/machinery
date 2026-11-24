"""Snapshot-only allowance reading for review seats (#10016), never writer routing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

from lu_machinery.fleet import credit_lane


@dataclass(frozen=True)
class ReviewCapacity:
    remaining_pct: float | None
    freshness: str
    fallback_reason: str | None
    near_cap: bool
    pressure: float
    hard_reason: str | None


def _codex_exhaustion_reason(
    record: Mapping[str, object], remaining: float | None, max_age_s: float,
) -> str | None:
    """Read a Codex subscription's allowance from a fresh budget snapshot."""
    scheduler = record.get("scheduler")
    if isinstance(scheduler, Mapping) and scheduler.get("quota_stale", False) is False:
        quota = scheduler.get("quota_remaining_pct")
        if type(quota) in (int, float) and quota == 0:
            return "Codex subscription allowance exhausted (0% scheduler quota remaining)"

    native = record.get("codexbar")
    has_probe = "freshness" in record or (isinstance(native, Mapping) and "freshness" in native)
    if has_probe and credit_lane.probe_freshness(record, max_age_s)[0] != credit_lane.FRESH:
        return None
    if record.get("status_source") == "ledger_burn":
        # The ledger's dollar cap is a different meter from the subscription.
        remaining = credit_lane.plan_remaining_pct({**record, "remaining_pct": None})
    if remaining is not None and remaining <= 0:
        return "Codex subscription allowance exhausted (0% remaining)"

    credit = record.get("credit")
    if isinstance(credit, Mapping) and credit.get("state") == credit_lane.CREDITS_EXHAUSTED:
        evidence = credit.get("evidence")
        fetched_at = evidence.get("credit_fetched_at") if isinstance(evidence, Mapping) else None
        if fetched_at is None or credit_lane._fresh_at(fetched_at, datetime.now(UTC), max_age_s) is not None:
            return "Codex subscription credits exhausted"
    return None


def review_capacity(
    record: Mapping[str, object],
    diagnostics: Mapping[str, object] | None = None,
    *,
    codex_subscription: bool = False,
) -> ReviewCapacity:
    """Read capacity, with a hard allowance check for an account-bound Codex seat."""
    remaining = credit_lane.plan_remaining_pct(record)
    try:
        policy = credit_lane.load_policy()
    except ValueError:
        # Same retained reserve as the owner's unreadable-policy fallback.
        threshold = credit_lane._CREDIT_NEAR_CAP_REMAINING_PCT
        max_age_s = credit_lane.UNREADABLE_POLICY_MAX_AGE_S
    else:
        threshold = policy.near_cap_remaining_pct
        max_age_s = credit_lane.probe_max_age_s(policy)
    freshness, reason = credit_lane.observation_freshness(record, diagnostics, max_age_s)
    pace = record.get("codexbar") or record.get("pace") or record
    delta = pace.get("weekly_pace_delta_pct") if isinstance(pace, Mapping) else None
    pressure = (
        max(0.0, delta) if isinstance(delta, (int, float)) and not isinstance(delta, bool) and isfinite(delta) else 0.0
    )
    if str(record.get("status") or "").strip().lower() == "hot":
        pressure = max(pressure, 1.0)
    runtime = record.get("runtime")
    scheduler = record.get("scheduler")
    hard_reason = None
    if record.get("eligible") is False:
        hard_reason = "ineligible route"
    elif record.get("login_state") == "NEED_LOGIN" or record.get("probe_state") == "NEED_LOGIN":
        hard_reason = "NEED_LOGIN"
    elif isinstance(runtime, Mapping) and runtime.get("headroom_blocked"):
        hard_reason = "runtime headroom blocked"
    elif isinstance(scheduler, Mapping) and scheduler.get("capacity_exhausted"):
        hard_reason = "credential bucket has no unreserved concurrency slot"
    elif codex_subscription and freshness == credit_lane.FRESH:
        hard_reason = _codex_exhaustion_reason(record, remaining, max_age_s)
    return ReviewCapacity(
        remaining_pct=remaining,
        freshness=freshness if remaining is not None else credit_lane.UNKNOWN,
        fallback_reason=(reason or "allowance window missing")
        if remaining is None or freshness != credit_lane.FRESH
        else None,
        near_cap=remaining is not None and remaining <= threshold and freshness == credit_lane.FRESH,
        pressure=pressure,
        hard_reason=hard_reason,
    )


def review_capacity_action(
    lane: str, record: Mapping[str, object], diagnostics: Mapping[str, object] | None, model: str | None
) -> tuple[bool, str]:
    """Ignore usage for reviews except the existing Claude account protection."""
    capacity = review_capacity(record, diagnostics)
    health, _ = credit_lane.health_fact(record)
    if health == credit_lane.UNHEALTHY or str(record.get("status") or "").strip().lower() == "unhealthy":
        return True, "unhealthy lane"
    runtime = record.get("runtime")
    scheduler = record.get("scheduler")
    if (
        record.get("circuit_open")
        or (isinstance(runtime, Mapping) and runtime.get("circuit_open"))
        or (isinstance(scheduler, Mapping) and scheduler.get("circuit_open"))
    ):
        return True, "route circuit is open"
    if capacity.hard_reason:
        return True, capacity.hard_reason
    if lane == "claude" and capacity.near_cap:
        relief = credit_lane.published_credit_relief(
            lane, record.get("credit"), model, record=dict(record), snapshot_stale=False
        )
        if relief is None or relief["state"] != credit_lane.CREDIT_BALANCE_PRESENT or not relief["model_allowed"]:
            return True, f"near_cap ({capacity.remaining_pct:g}% remaining)"
    return False, ""
