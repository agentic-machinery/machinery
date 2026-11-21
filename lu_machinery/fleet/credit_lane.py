"""Credit-period lane state, model allowlist and reset advice (#9518).

A lane whose plan allowance is nearly used can show a prepaid credit balance.
The router cannot see the provider draw it: it sees a balance and our own
runtime outcomes. :func:`lane_credit_state` therefore says only what is known:
``credit_balance_present`` (plan at or below the threshold, fresh positive
balance, and no recent rate limit against it) or a fail-closed state that
keeps the lane in its plan state (near_cap/AVOID as before). That state is the
router's recommendation question only.
:func:`dispatch_refusal` answers a separate admission question: it refuses a
model outside the lane's credit-period allowlist whenever the plan window is at
or below the threshold AND a fresh positive credit balance exists
(:func:`allowlist_applies`: ``credit_balance_present``, ``credit_use_unconfirmed``
and ``credits_unverified`` for unreadable usage records all qualify), and under
``policy_error``, whether or not the router currently recommends the lane. It also
refuses an off-allowlist model when the routing-budget snapshot cannot be read.
:func:`reset_advice` says whether a free full reset is worth using now. The policy lives in
``scripts/config/credit_lanes.yaml``. Everything here reads snapshots only:
nothing consumes credits or resets.

Fail closed: a missing, stale, non-numeric or non-positive balance, a balance
fetch time that is not an explicit fresh UTC timestamp, unreadable runtime
usage records, or one or more ``rate_limited`` outcomes for the lane within
``rate_limit_window_s`` keep the lane in its plan state. A malformed policy
file restricts only the lanes in :data:`DEFAULT_ALLOWED_MODELS`.

:func:`routing_facts` is the one consumer-facing reading of a lane record
(#9740): remaining allowance, snapshot and probe freshness, health, pace,
credit and reset evidence, credit-period model permission and the lane's
capacity class. Every capacity and admission consumer compares these facts
before applying its own legitimate restrictions (role, risk, egress,
retirement, transport, wave configuration). Missing data stays ``unknown``.
"""

from __future__ import annotations

import copy
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from http.client import HTTPException
from pathlib import Path
from typing import Any

import yaml

from lu_machinery.common import json_depth

SCHEMA_VERSION = "credit-lanes.v1"
POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "credit_lanes.yaml"
REFUSAL_CODE = "CREDIT_PERIOD_MODEL_REFUSED"
_MAX_WARMING_RETRY_DELAY_S = 2  # Monitor's routing-budget warming response advertises 2 seconds.

# Hard-wired minimal policy for a policy file that cannot be read: only these
# lanes stay restricted, to these models. A test pins it to the shipped yaml.
DEFAULT_ALLOWED_MODELS: dict[str, tuple[str, ...]] = dict.fromkeys(("codex", "codex:b"), ("gpt-6.2-sol", "gpt-6-luna"))

NOT_CONFIGURED = "not_configured"
POLICY_ERROR = "policy_error"
PLAN_UNKNOWN = "plan_unknown"
PLAN_HEALTHY = "plan_healthy"
CREDIT_BALANCE_PRESENT = "credit_balance_present"
CREDITS_EXHAUSTED = "credits_exhausted"
CREDITS_UNVERIFIED = "credits_unverified"
CREDIT_USE_UNCONFIRMED = "credit_use_unconfirmed"

USE_RESET_NOW = "use_reset_now"
HOLD_RESET = "hold_reset"
NOT_APPLICABLE = "not_applicable"

# Typed observation qualifiers shared by every routing consumer (#9740).
FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"
HEALTHY = "healthy"
UNHEALTHY = "unhealthy"

# Capacity classes of one lane record (lane inventory or a concrete route).
# ``verified``: plan or credit allowance is established usable now.
# ``unknown_stale``: the only evidence against the lane is a pace deficit (or a
# weekly-pace hot label) read from a stale snapshot or probe; it is historical,
# not a confirmed current condition, and never counts as verified capacity.
# ``unknown``: no usable allowance evidence. ``avoid``: a hard or current reason.
CAPACITY_VERIFIED = "verified"
CAPACITY_UNKNOWN_STALE = "unknown_stale"
CAPACITY_UNKNOWN = "unknown"
CAPACITY_AVOID = "avoid"
STALE_ADVISORY_LABEL = "UNKNOWN — stale/advisory"

# Plan statuses below the cap (the producer's allowance rule, see
# ``state_router._status_from_weekly_used``): near_cap at <= 1% remaining,
# warm at <= 50% remaining when pace is unavailable, cool otherwise.
SUBSCRIPTION_NEAR_CAP_USED_PCT = 99.0
_NEAR_CAP_REMAINING_PCT = 100.0 - SUBSCRIPTION_NEAR_CAP_USED_PCT
# Separate Codex credit-policy trigger, also used when the policy is unreadable.
_CREDIT_NEAR_CAP_REMAINING_PCT = 10.0
_WARM_REMAINING_PCT = 50.0
# Probe age limit when the policy file is unreadable: the routing-budget
# ``stale_threshold_s`` (the shipped ``credit_max_age_s`` is the same value).
UNREADABLE_POLICY_MAX_AGE_S = 900.0

DRAW_NOT_VERIFIED = "credit balance present; draw not verified by the router"
RATE_LIMIT_REASON = "recent rate limit while the plan window is exhausted: credit use not confirmed"

# No runtime usage record carries per-task credit consumption (agent_runtime
# usage JSONL has no credit field and its token field is unset for Codex), so
# coverage is reported as unknown rather than estimated from nothing.
COVERAGE_BASIS = "unknown: runtime usage records carry no per-task credit consumption"
ANCHOR_EVIDENCE = (
    "unverified: neither the provider usage record nor repository documentation states "
    "whether a full reset re-anchors the plan window"
)


@dataclass(frozen=True)
class CreditPolicy:
    near_cap_remaining_pct: float
    credit_max_age_s: float
    rate_limit_window_s: float
    reset_hold_hours: float
    allowed_models: dict[str, tuple[str, ...]]
    path: Path

    def lane_models(self, lane: str) -> tuple[str, ...] | None:
        return self.allowed_models.get(lane.strip().lower())


def _policy_number(payload: dict[str, Any], name: str, path: Path, *, positive: bool = False) -> float:
    value = payload.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        kind = "a positive" if positive else "a non-negative"
        raise ValueError(f"{path}: {name} must be {kind} number, got {value!r}")
    return float(value)


def load_policy(path: Path | None = None) -> CreditPolicy:
    """Read and validate the credit-period policy (default :data:`POLICY_PATH`); a missing or malformed file raises ``ValueError``."""
    path = path if path is not None else POLICY_PATH
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: cannot read credit-lane policy: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: expected schema_version {SCHEMA_VERSION}")
    lanes = payload.get("lanes")
    if not isinstance(lanes, dict) or not lanes:
        raise ValueError(f"{path}: lanes must be a non-empty mapping")
    allowed: dict[str, tuple[str, ...]] = {}
    for lane, entry in lanes.items():
        models = entry.get("allowed_models") if isinstance(entry, dict) else None
        if not isinstance(models, list) or not models or not all(isinstance(m, str) and m.strip() for m in models):
            raise ValueError(f"{path}: lanes.{lane}.allowed_models must be a non-empty list of model ids")
        allowed[str(lane).strip().lower()] = tuple(m.strip() for m in models)
    return CreditPolicy(
        near_cap_remaining_pct=_policy_number(payload, "near_cap_remaining_pct", path),
        credit_max_age_s=_policy_number(payload, "credit_max_age_s", path),
        rate_limit_window_s=_policy_number(payload, "rate_limit_window_s", path, positive=True),
        reset_hold_hours=_policy_number(payload, "reset_hold_hours", path),
        allowed_models=allowed,
        path=path,
    )


def policy_error_state(lane: str, error: str) -> dict[str, Any]:
    """Credit field for ``lane`` when the policy cannot be read: the plan state applies.

    Lanes in :data:`DEFAULT_ALLOWED_MODELS` read ``policy_error`` with the default
    allowlist; every other lane is ``not_configured`` and unaffected.
    """
    models = DEFAULT_ALLOWED_MODELS.get(lane.strip().lower())
    if models is None:
        return {"state": NOT_CONFIGURED}
    return {
        "state": POLICY_ERROR,
        "reason": f"credit-lane policy unreadable ({error}); plan state applies",
        "allowed_models": list(models),
        "allowlist_applies": True,
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _field(info: dict[str, Any], key: str) -> Any:
    """``info[key]``, falling back to the native (codexbar) record when absent or null."""
    value = info.get(key)
    if value is None:
        native = info.get("codexbar") if isinstance(info.get("codexbar"), dict) else {}
        value = native.get(key)
    return value


def utc_datetime(value: Any) -> datetime | None:
    """The reset reserve's UTC-only rule (naive and non-UTC offsets rejected), imported on use.

    Imported lazily so the shared test conftest can load this module without
    the provider-probe stack ``reset_reserve`` pulls in.
    """
    from lu_machinery.fleet.reset_reserve import utc_datetime as parse

    return parse(value)


def _reset_time(value: Any) -> datetime | None:
    """Natural plan reset: epoch seconds/milliseconds (UTC by definition) or explicit-UTC ISO text."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if value > 1e12 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return utc_datetime(value)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fresh_at(value: Any, now: datetime, max_age_s: float) -> datetime | None:
    """``value`` as an explicit UTC time no older than ``max_age_s`` and not in the future, else None."""
    parsed = utc_datetime(value)
    if parsed is None or not 0 <= (now - parsed).total_seconds() < max_age_s:
        return None
    return parsed


def _auto_window(info: Mapping[str, Any]) -> Mapping[str, Any]:
    """The Cursor Auto allowance window (``provider_windows.auto``), top-level or native."""
    for source in (info, info.get("codexbar")):
        windows = source.get("provider_windows") if isinstance(source, Mapping) else None
        auto = windows.get("auto") if isinstance(windows, Mapping) else None
        if isinstance(auto, Mapping):
            return auto
    return {}


def plan_remaining_reading(info: Mapping[str, Any] | None) -> tuple[float | None, str | None]:
    """Tightest reported plan window remaining-% and the window it came from.

    Reads the record's ``remaining_pct``, the native weekly/primary/secondary
    windows and the Cursor Auto allowance (``provider_windows.auto``). Booleans,
    non-finite values and burn-only records establish nothing: ``(None, None)``.
    Windows keep their identity; the API/on-demand pool is a separate allowance.
    """
    record = info if isinstance(info, Mapping) else {}
    native = record.get("codexbar") if isinstance(record.get("codexbar"), Mapping) else {}
    candidates = [
        ("remaining_pct", _number(record.get("remaining_pct"))),
        *(
            (f"codexbar.{key}", _number(native.get(key)))
            for key in ("weekly_remaining_pct", "primary_remaining_pct", "secondary_remaining_pct")
        ),
        ("provider_windows.auto.remaining_pct", _number(_auto_window(record).get("remaining_pct"))),
    ]
    known = [(value, source) for source, value in candidates if value is not None]
    if not known:
        return None, None
    value, source = min(known, key=lambda item: item[0])
    return value, source


def plan_remaining_pct(info: Mapping[str, Any] | None) -> float | None:
    """Tightest reported plan window remaining-% (credits apply once any window is used up)."""
    return plan_remaining_reading(info)[0]


def shared_usage_agents(
    agents: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read each catalogued usage pool once, retaining each transport's health.

    Conflicting observations of one meter use the tightest known allowance.
    Its pace and freshness travel with it; another transport cannot turn a
    stale reading fresh. Missing observations stay unknown. Never mutate the
    supplied snapshot or copy route health, login, concurrency or credits.
    """
    from lu_machinery.review.model_catalog import load_model_catalog

    source_catalog = catalog if catalog is not None else load_model_catalog()
    result = dict(agents)
    meter_fields = (
        "codexbar",
        "pace",
        "freshness",
        "age_s",
        "stale",
        "fetched_at",
        "resets_at",
        "burn_pct_7d",
        "status_source",
    )
    for pool in source_catalog.get("usage_pools", {}).values():
        observed = [
            (remaining, seat, record)
            for seat in pool["seats"]
            if isinstance(record := agents.get(seat), Mapping) and (remaining := plan_remaining_pct(record)) is not None
        ]
        if not observed:
            continue
        remaining, meter_seat, meter = min(observed, key=lambda item: item[0])
        for seat in pool["seats"]:
            own = agents.get(seat, {})
            if not isinstance(own, Mapping) or seat == meter_seat:
                continue
            merged = dict(own)
            for field in meter_fields:
                merged.pop(field, None)
                if field in meter:
                    merged[field] = meter[field]
            merged["remaining_pct"] = remaining
            # A route's runtime block is not a usage-pool block.
            own_runtime = own.get("runtime") if isinstance(own.get("runtime"), Mapping) else {}
            meter_runtime = meter.get("runtime") if isinstance(meter.get("runtime"), Mapping) else {}
            if own.get("status") in {
                None,
                "unknown",
                "cool",
                "warm",
                "idle",
                "near_cap",
                "hot",
            } and not own_runtime.get("headroom_blocked"):
                meter_status = meter.get("status")
                if meter_status not in {"cool", "warm", "idle", "near_cap", "hot"} or meter_runtime.get(
                    "headroom_blocked"
                ):
                    meter_status = allowance_status(remaining)
                merged["status"] = meter_status
            if seat not in agents and merged.get("status") not in {"hot", "near_cap"}:
                merged["status"] = "unknown"
            result[seat] = merged
    return result


def plan_window_exhausted(lane: str, info: Mapping[str, Any] | None, policy: CreditPolicy | None = None) -> bool:
    """True when a credit-configured lane's tightest plan window is at or below the near-cap threshold.

    The credit-lane rule: such a lane is near_cap/AVOID unless credit relief
    applies, whichever window (weekly, 5-hour, Auto) is the tight one. Lanes
    outside the policy keep the status-based near_cap (>= 99% weekly used).
    An unreadable policy keeps the built-in credit lanes configured.
    """
    lane_key = lane.strip().lower()
    if policy is None:
        try:
            policy = load_policy()
        except ValueError:
            policy = None
    configured = policy.lane_models(lane_key) is not None if policy is not None else lane_key in DEFAULT_ALLOWED_MODELS
    threshold = policy.near_cap_remaining_pct if policy is not None else _CREDIT_NEAR_CAP_REMAINING_PCT
    remaining = plan_remaining_pct(info)
    return configured and remaining is not None and remaining <= threshold


def allowance_status(remaining: float | None) -> str:
    """Plan status from remaining allowance alone (pace unavailable): the producer's allowance rule."""
    if remaining is None:
        return UNKNOWN
    if remaining <= _NEAR_CAP_REMAINING_PCT:
        return "near_cap"
    return "warm" if remaining <= _WARM_REMAINING_PCT else "cool"


def probe_freshness(info: Mapping[str, Any] | None, max_age_s: float) -> tuple[str, str]:
    """``fresh`` / ``stale`` / ``unknown`` for the lane's own usage probe, with the reason.

    ``fresh`` needs an explicit ``freshness="fresh"``, no ``stale`` flag and a
    numeric ``age_s`` below ``max_age_s`` (the policy's ``credit_max_age_s``). A stale flag, a
    ``stale*`` freshness or an over-age reading is ``stale``; a missing or
    ``unavailable`` freshness or a missing age is ``unknown``, never fresh.
    """
    record = info if isinstance(info, Mapping) else {}
    native = record.get("codexbar") if isinstance(record.get("codexbar"), Mapping) else {}
    freshness = record.get("freshness", native.get("freshness"))
    age = _number(record.get("age_s", native.get("age_s")))
    flagged = native.get("stale") is True or record.get("stale") is True
    if flagged or (isinstance(freshness, str) and freshness.startswith("stale")):
        return STALE, f"credit probe freshness={freshness or 'missing'}"
    if freshness != "fresh":
        return UNKNOWN, f"credit probe freshness={freshness or 'missing'}"
    age_reason = f"credit probe age_s={age if age is not None else 'missing'} (limit {max_age_s:g})"
    if age is None or age < 0:
        return UNKNOWN, age_reason
    if age >= max_age_s:
        return STALE, age_reason
    return FRESH, ""


def probe_max_age_s(policy: CreditPolicy | None = None) -> float:
    """The probe age limit (``credit_max_age_s``); :data:`UNREADABLE_POLICY_MAX_AGE_S` when the policy is unreadable."""
    if policy is not None:
        return policy.credit_max_age_s
    try:
        return load_policy().credit_max_age_s
    except ValueError:
        return UNREADABLE_POLICY_MAX_AGE_S


def snapshot_freshness(snapshot_metadata: Mapping[str, Any] | None) -> tuple[str, str]:
    """``fresh`` / ``stale`` / ``unknown`` from routing-budget ``diagnostics.stale``; missing is unknown."""
    stale = snapshot_metadata.get("stale") if isinstance(snapshot_metadata, Mapping) else None
    if stale is True:
        return STALE, "routing-budget snapshot is stale"
    if stale is False:
        return FRESH, ""
    return UNKNOWN, "routing-budget snapshot staleness missing"


def observation_freshness(
    info: Mapping[str, Any] | None, snapshot_metadata: Mapping[str, Any] | None, max_age_s: float
) -> tuple[str, str]:
    """Freshness of the lane's quota/pace observation: stale if either layer is stale.

    ``fresh`` needs a fresh snapshot and a probe that is not stale (a ledger-only
    lane has no probe; the snapshot's own age covers it). An unknown snapshot
    staleness is ``unknown``.
    """
    snapshot, snapshot_reason = snapshot_freshness(snapshot_metadata)
    probe, probe_reason = probe_freshness(info, max_age_s)
    if snapshot == STALE:
        return STALE, snapshot_reason
    if probe == STALE:
        return STALE, probe_reason
    if snapshot == UNKNOWN:
        return UNKNOWN, snapshot_reason
    return FRESH, ""


def health_fact(info: Mapping[str, Any] | None) -> tuple[str, str]:
    """``healthy`` / ``unhealthy`` / ``unknown`` from the lane's ``health`` record and its basis.

    Only an explicit boolean ``healthy`` establishes anything; a missing record
    or a scan that could not run (``healthy`` null) is unknown, never healthy.
    """
    record = info if isinstance(info, Mapping) else {}
    health = record.get("health")
    if not isinstance(health, Mapping):
        return UNKNOWN, "lane health record missing"
    basis = str(health.get("basis") or "")
    if health.get("healthy") is True:
        return HEALTHY, basis or "health record"
    if health.get("healthy") is False:
        return UNHEALTHY, str(health.get("last_error") or basis or "unhealthy lane")
    return UNKNOWN, basis or "lane health unknown"


def _fresh_probe(info: dict[str, Any], policy: CreditPolicy, *, snapshot_stale: bool) -> tuple[bool, str]:
    if snapshot_stale:
        return False, "routing-budget snapshot is stale"
    state, reason = probe_freshness(info, policy.credit_max_age_s)
    return state == FRESH, reason


def _rate_limit_subject(lane: str) -> tuple[str, str | None]:
    """Runtime agent and, for Codex, the account whose rate limits count.

    ``codex`` and ``codex:a`` are account a (``sub``). ``codex:b`` is account b.
    A limit on one account is not evidence about the other.
    """
    key = lane.strip().lower()
    if key in {"codex", "codex:a"}:
        return "codex", "sub"
    if key == "codex:b":
        return "codex", "b-sub"
    return lane, None


def read_recent_rate_limits(
    lane: str, window_s: float, *, now: datetime | None = None, usage_dir: Path | None = None
) -> dict[str, Any]:
    """``rate_limited`` outcomes for ``lane`` in the runtime usage records within ``window_s``.

    ``usage_dir`` defaults to the shared runtime usage directory. ``unreadable``
    counts relevant records that could not be read (see
    ``summarize_lane_runtime``); a count of 0 with ``unreadable["total"] > 0``
    is not "no rate limits". Codex lanes read only that account's records.
    """
    from lu_machinery.agent_runtime.usage import summarize_lane_runtime

    agent, account = _rate_limit_subject(lane)
    summary = summarize_lane_runtime(
        agent,
        window_s=window_s,
        usage_dir=usage_dir,
        now=now.timestamp() if now is not None else None,
        **({} if account is None else {"codex_account": account}),
    )
    return {
        "count": int(summary["rate_limited"]),
        "last_rate_limited_at": summary["last_rate_limited_at"],
        "unreadable": dict(summary["unreadable"]),
    }


RateLimitReader = Callable[[], dict[str, Any]]


def _shared_rate_limit_reader(
    lane: str, policy: CreditPolicy, now: datetime, usage_dir: Path | None
) -> RateLimitReader:
    """One lazily read rate-limit observation for every decision of one :func:`routing_facts` call.

    Credit state and pace coverage then cannot disagree because they sampled
    the runtime records at different moments. A reader failure is replayed.
    """
    cache: dict[str, Any] = {}

    def read() -> dict[str, Any]:
        if "value" not in cache and "error" not in cache:
            try:
                cache["value"] = read_recent_rate_limits(lane, policy.rate_limit_window_s, now=now, usage_dir=usage_dir)
            except Exception as exc:  # replayed below: unreadable evidence is not "no evidence"
                cache["error"] = exc
        if "error" in cache:
            raise cache["error"]
        return cache["value"]

    return read


def _rate_limit_evidence(
    lane: str,
    record: dict[str, Any],
    policy: CreditPolicy,
    now: datetime,
    usage_dir: Path | None = None,
    *,
    reader: RateLimitReader | None = None,
) -> tuple[int | None, str | None, dict[str, int] | None]:
    """Recent rate-limit count, newest time and unreadable-record counts.

    ``(None, None, None)`` when the reader fails; the third item is the
    unreadable-record tally when part of the lane's records could not be read
    (the count is then not trusted: ``None``), else ``None``.

    The snapshot's own runtime summary counts as well when its window fits
    inside the policy window (it can come from another usage directory).
    ``reader`` replays one shared observation (see :func:`routing_facts`).
    """
    try:
        local = (
            reader()
            if reader is not None
            else read_recent_rate_limits(lane, policy.rate_limit_window_s, now=now, usage_dir=usage_dir)
        )
        count = local["count"]
        last = local["last_rate_limited_at"]
        unreadable = local.get("unreadable")
    except Exception:  # unreadable evidence is not "no evidence": fail closed
        return None, None, None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None, None, None
    if unreadable is not None:
        total = unreadable.get("total") if isinstance(unreadable, dict) else None
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return None, None, None
        if total > 0:
            return None, last, {k: v for k, v in unreadable.items() if k != "total" and v}
    runtime = record.get("runtime") if isinstance(record.get("runtime"), dict) else {}
    window = _number(runtime.get("window_s"))
    snapshot_count = runtime.get("rate_limited")
    if (
        window is not None
        and window <= policy.rate_limit_window_s
        and isinstance(snapshot_count, int)
        and not isinstance(snapshot_count, bool)
        and snapshot_count > count
    ):
        count = snapshot_count
        last = runtime.get("last_rate_limited_at") or last
    if runtime.get("headroom_blocked") is True:
        # The 5-minute headroom block is itself rate-limit evidence.
        count = max(count, 1)
        last = last or runtime.get("last_rate_limited_at")
    return count, last, None


def lane_credit_state(
    lane: str,
    info: dict[str, Any] | None,
    policy: CreditPolicy,
    *,
    now: datetime | None = None,
    snapshot_stale: bool = False,
    usage_dir: Path | None = None,
    for_pace_deficit: bool = False,
    rate_limit_reader: RateLimitReader | None = None,
) -> dict[str, Any]:
    """Credit state of one routing-budget lane record; see the module docstring for fail-closed rules.

    ``usage_dir`` is the runtime usage directory the rate-limit evidence is read
    from (default: the shared one). ``for_pace_deficit`` checks reserves even
    above the plan cap; it does not extend the credit-period admission gate.
    """
    models = policy.lane_models(lane)
    if models is None:
        return {"state": NOT_CONFIGURED}
    current = (now or datetime.now(UTC)).astimezone(UTC)
    record = info if isinstance(info, dict) else {}
    remaining = plan_remaining_pct(record)
    evidence: dict[str, Any] = {
        "rate_limited_count": None,
        "rate_limit_window_s": policy.rate_limit_window_s,
        "last_rate_limited_at": None,
        "credit_balance": None,
        "credit_fetched_at": None,
    }
    result: dict[str, Any] = {
        "plan_remaining_pct": remaining,
        "near_cap_remaining_pct": policy.near_cap_remaining_pct,
        "credit_balance": None,
        "allowed_models": list(models),
        "allowlist_applies": False,
        "evidence": evidence,
    }
    if remaining is None:
        return {**result, "state": PLAN_UNKNOWN, "reason": "plan allowance unknown"}
    if remaining > policy.near_cap_remaining_pct and not for_pace_deficit:
        return {
            **result,
            "state": PLAN_HEALTHY,
            "reason": f"plan remaining {remaining:g}% above {policy.near_cap_remaining_pct:g}%",
        }
    fresh, why = _fresh_probe(record, policy, snapshot_stale=snapshot_stale)
    if not fresh:
        return {**result, "state": CREDITS_UNVERIFIED, "reason": why}
    balance = _number(_field(record, "credit_balance"))
    if balance is None:
        return {**result, "state": CREDITS_UNVERIFIED, "reason": "credit balance missing or non-numeric"}
    evidence["credit_balance"] = balance
    fetched_at = _fresh_at(_field(record, "fetched_at"), current, policy.credit_max_age_s)
    if fetched_at is None:
        return {
            **result,
            "state": CREDITS_UNVERIFIED,
            "reason": "credit balance fetch time missing, not explicit UTC, or older than "
            f"{policy.credit_max_age_s:g}s",
        }
    evidence["credit_fetched_at"] = _iso(fetched_at)
    result["credit_balance"] = balance
    if balance <= 0:
        return {**result, "state": CREDITS_EXHAUSTED, "reason": f"credit balance {balance:g}"}
    count, last, unreadable = _rate_limit_evidence(lane, record, policy, current, usage_dir, reader=rate_limit_reader)
    evidence["rate_limited_count"] = count
    evidence["last_rate_limited_at"] = last
    if unreadable is not None:
        evidence["unreadable_records"] = unreadable
    # A fresh positive balance exists from here on: the admission allowlist applies
    # whatever the router's recommendation state says about the rate-limit evidence.
    result["allowlist_applies"] = remaining <= policy.near_cap_remaining_pct
    if unreadable is not None:
        parts = ", ".join(f"{n} {kind}" for kind, n in unreadable.items())
        return {
            **result,
            "state": CREDITS_UNVERIFIED,
            "reason": f"runtime usage records unreadable ({parts}): rate limits cannot be ruled out",
        }
    if count is None:
        return {**result, "state": CREDITS_UNVERIFIED, "reason": "runtime usage records unreadable"}
    if count > 0:
        return {**result, "state": CREDIT_USE_UNCONFIRMED, "reason": RATE_LIMIT_REASON}
    return {
        **result,
        "state": CREDIT_BALANCE_PRESENT,
        "reason": f"plan remaining {remaining:g}%"
        + ("; " if for_pace_deficit else f" at or below {policy.near_cap_remaining_pct:g}%; ")
        + f"fresh credit balance {balance:g}; no rate limit in the last {policy.rate_limit_window_s:g}s; "
        "draw not verified by the router",
        "coverage": {"dispatches": None, "basis": COVERAGE_BASIS},
    }


def pace_deficit_state(
    lane: str,
    info: dict[str, Any] | None,
    *,
    pace: dict[str, Any] | None = None,
    model: str | None = None,
    policy: CreditPolicy | None = None,
    now: datetime | None = None,
    snapshot_stale: bool = False,
    usage_dir: Path | None = None,
    rate_limit_reader: RateLimitReader | None = None,
) -> dict[str, Any]:
    """Shared uncovered-pace decision for routing, admission and review (#9615).

    A fresh positive balance covers pace only for credit-allowlisted models;
    a fresh full reset that outlasts projected run-out covers any model. Both require readable,
    rate-limit-free runtime evidence. Nothing is consumed. Raw pace remains
    visible, and a runtime hot label or near_cap status is never relaxed here.

    Below the cap, visible allowance lasting to reset or a covering reserve
    is cool, regardless of used-percent. Warm means at least half the allowance
    is used and either visible pace projects a shortfall within the on-pace
    band or pace is unavailable. The existing low-use on-pace tolerance stays
    cool. Only hot labels identified by the producer as weekly pace may be relaxed.
    Missing lane identity or run-out projection leaves reset coverage unverified.
    """
    from lu_machinery.api.subscription_usage import pace_expected_pct, pace_is_deficit, pace_is_visible

    record = info if isinstance(info, dict) else {}
    if pace is None:
        pace = record.get("codexbar") or record.get("pace") or record
    current = (now or datetime.now(UTC)).astimezone(UTC)
    raw = pace_is_deficit(pace, now=current)
    status = record.get("status") or (record.get("interactive") or {}).get("status")
    result: dict[str, Any] = {
        "raw_deficit": raw,
        "uncovered": raw,
        "covered_by": [],
        "status": status,
        "reason": "pace deficit uncovered" if raw is True else "no confirmed pace deficit",
    }
    remaining = plan_remaining_pct(record)
    runtime = record.get("runtime") if isinstance(record.get("runtime"), dict) else {}
    if raw is not True:
        if not isinstance(pace, dict) or status not in {"cool", "warm"} or runtime.get("headroom_blocked"):
            return result
        expected = pace_expected_pct(pace, now=current)
        if not pace_is_visible({"expected_pct": expected}) or remaining is None or remaining <= _NEAR_CAP_REMAINING_PCT:
            return result
        will_last = pace.get("will_last_to_reset", pace.get("willLastToReset"))
        if will_last is True:
            result["status"] = "cool"
            result["reason"] = "allowance lasts to reset at visible pace"
            return result
        if raw is not False or will_last is not False:
            return result
    if (
        raw is True
        and status in {"cool", "warm", "hot"}
        and remaining is not None
        and remaining > _NEAR_CAP_REMAINING_PCT
    ):
        result["status"] = "hot"
    if not lane.strip():
        return {**result, "reason": result["reason"] + ": lane identity missing"}
    if status == "hot" and record.get("status_source") != "weekly_pace":
        return {**result, "reason": result["reason"] + ": hot source is not weekly pace"}
    if policy is None:
        try:
            policy = load_policy()
        except ValueError:
            return {**result, "reason": result["reason"] + ": reserve policy unreadable"}
    credit = lane_credit_state(
        lane,
        record,
        policy,
        now=current,
        snapshot_stale=snapshot_stale,
        usage_dir=usage_dir,
        for_pace_deficit=True,
        rate_limit_reader=rate_limit_reader,
    )
    if credit["state"] == CREDIT_BALANCE_PRESENT and (model is None or model_allowed(policy, lane, model)):
        result["covered_by"].append("credits")
    live = _verified_inventory(record, policy, current, snapshot_stale=snapshot_stale)
    runout = _projected_runout(record, pace, current)
    if live and runout is not None and any(expiry is None or expiry > runout for expiry in live):
        count, _, _ = _rate_limit_evidence(lane, record, policy, current, usage_dir, reader=rate_limit_reader)
        if count == 0:
            result["covered_by"].append("free full reset")
    if result["covered_by"]:
        result["uncovered"] = False
        result["reason"] = (
            ("pace deficit" if raw is True else "projected shortfall")
            + " covered by "
            + " and ".join(result["covered_by"])
        )
        if (
            status in {"cool", "warm", "hot"}
            and not runtime.get("headroom_blocked")
            and remaining is not None
            and remaining > _NEAR_CAP_REMAINING_PCT
        ):
            result["status"] = "cool"
    return result


def allowlist_applies(state: dict[str, Any]) -> bool:
    """True when the credit-period allowlist gates admission for this lane state.

    The admission question, separate from the router recommendation: the plan
    window is at or below the threshold and a fresh positive balance exists
    (any of ``credit_balance_present``, ``credit_use_unconfirmed`` and the
    unreadable-usage ``credits_unverified``), or the policy is unreadable for a
    default lane. A healthy plan, a missing, stale or zero balance, and lanes
    outside the policy are not gated.
    """
    return state.get("allowlist_applies") is True


def _sub_mapping(source: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    val = source.get(key)
    return val if isinstance(val, Mapping) else {}


def _natural_reset(info: Mapping[str, Any]) -> datetime | None:
    native = _sub_mapping(info, "codexbar")
    windows = _sub_mapping(native, "windows")
    secondary = _sub_mapping(windows, "secondary")
    auto = _auto_window(info)
    for value in (
        native.get("weekly_resets_at"),
        secondary.get("resets_at"),
        info.get("resets_at"),
        info.get("reset_at"),
        auto.get("resets_at"),
    ):
        parsed = _reset_time(value)
        if parsed is not None:
            return parsed
    return None


def _block_remaining(block: Mapping[str, Any]) -> float | None:
    """Remaining percentage read from a single window block."""
    rem = _number(block.get("remaining_pct"))
    if rem is not None and 0.0 <= rem <= 100.0:
        return rem
    used = _number(block.get("used_pct"))
    if used is not None and 0.0 <= used <= 100.0:
        return 100.0 - used
    return None


def _block_reset(block: Mapping[str, Any]) -> Any:
    """First non-empty reset timestamp present on a single window block."""
    for key in ("resets_at", "reset_at", "resetsAt"):
        value = block.get(key)
        if value not in (None, ""):
            return value
    return None


def _parse_window_minutes(source: Mapping[str, Any]) -> tuple[int | None, bool]:
    """Positive window length in minutes from one mapping, or ``(None, True)`` if absent.

    Returns ``(None, False)`` when an explicit ``window_minutes`` / ``windowMinutes``
    value is malformed or non-positive (fail closed).
    """
    for key in ("window_minutes", "windowMinutes"):
        if key not in source or source.get(key) is None:
            continue
        value = _number(source.get(key))
        if value is None or value <= 0:
            return None, False
        return int(value), True
    return None, True


def _first_window_minutes(*sources: Mapping[str, Any]) -> tuple[int | None, bool]:
    """First explicit window_minutes reading across ordered mappings (fail closed on malformed)."""
    for source in sources:
        minutes, ok = _parse_window_minutes(source)
        if not ok:
            return None, False
        if minutes is not None:
            return minutes, True
    return None, True


def _matched_pace_window(
    info: Mapping[str, Any],
    fc: Mapping[str, Any] | None = None,
) -> tuple[float | None, Any, int | None, bool, str | None]:
    """Resolve ``(remaining_pct, raw_reset, window_minutes, window_ok, error_reason)`` from one matched window.

    Never pairs one window's remaining-% (such as a 5h ``primary_remaining_pct``)
    with another window's reset or duration (such as ``weekly_resets_at``).
    """
    native = _sub_mapping(info, "codexbar")
    windows = _sub_mapping(native, "windows")
    secondary = _sub_mapping(windows, "secondary")
    primary = _sub_mapping(windows, "primary")
    auto = _auto_window(info)

    weekly_rem = next(
        (
            val
            for val in (
                _number(native.get("weekly_remaining_pct")),
                _number(native.get("secondary_remaining_pct")),
                _number(info.get("remaining_pct")),
            )
            if val is not None and 0.0 <= val <= 100.0
        ),
        None,
    )
    weekly_reset = next(
        (
            val
            for val in (
                native.get("weekly_resets_at"),
                info.get("resets_at"),
                info.get("reset_at"),
            )
            if val not in (None, "")
        ),
        None,
    )

    sec_rem = _block_remaining(secondary)
    sec_reset = _block_reset(secondary)
    sec_mins, sec_ok = _parse_window_minutes(secondary)
    if not sec_ok:
        return None, None, None, False, "missing forecast: invalid window_minutes"
    has_secondary = sec_rem is not None or sec_reset is not None or sec_mins is not None

    auto_rem = _block_remaining(auto)
    auto_reset = _block_reset(auto)
    auto_mins, auto_ok = _parse_window_minutes(auto)
    if not auto_ok:
        return None, None, None, False, "missing forecast: invalid window_minutes"
    has_auto = auto_rem is not None or auto_reset is not None or auto_mins is not None

    pri_rem = _block_remaining(primary)
    pri_reset = _block_reset(primary)
    pri_mins, pri_ok = _parse_window_minutes(primary)
    if not pri_ok:
        return None, None, None, False, "missing forecast: invalid window_minutes"
    has_primary = pri_rem is not None or pri_reset is not None or pri_mins is not None

    # Explicit planning forecast block takes precedence for its own reset/remaining/window.
    if isinstance(fc, Mapping):
        fc_mins, fc_ok = _parse_window_minutes(fc)
        if not fc_ok:
            return None, None, None, False, "missing forecast: invalid window_minutes"
        fc_reset = _block_reset(fc)
        fc_rem = _block_remaining(fc)
        if fc_reset is not None or fc_rem is not None:
            rem = fc_rem if fc_rem is not None else (
                sec_rem if sec_rem is not None else (weekly_rem if weekly_rem is not None else auto_rem)
            )
            reset = fc_reset if fc_reset is not None else (
                sec_reset if sec_reset is not None else (weekly_reset if weekly_reset is not None else auto_reset)
            )
            if fc_mins is not None:
                return rem, reset, fc_mins, True, None
            fallback_mins, fallback_ok = _first_window_minutes(secondary, auto, native, info)
            return rem, reset, fallback_mins, fallback_ok, None

    # 1. Subscription secondary/weekly window (Claude/Codex weekly, Cursor monthly).
    if has_secondary:
        rem = sec_rem if sec_rem is not None else weekly_rem
        reset = sec_reset if sec_reset is not None else weekly_reset
        if sec_mins is not None:
            return rem, reset, sec_mins, True, None
        fallback_mins, fallback_ok = _first_window_minutes(native, info)
        return rem, reset, fallback_mins, fallback_ok, None

    # 2. Cursor Auto monthly window.
    if has_auto:
        rem = auto_rem if auto_rem is not None else (
            _number(info.get("remaining_pct")) if info.get("status_source") == "cursor_auto" else None
        )
        reset = auto_reset
        if auto_mins is not None:
            return rem, reset, auto_mins, True, None
        fallback_mins, fallback_ok = _first_window_minutes(native, info)
        return rem, reset, fallback_mins, fallback_ok, None

    # 3. Single-window primary meter (e.g. 5h-only AGY lane when no secondary/auto exists).
    if has_primary and weekly_reset is None:
        rem = pri_rem if pri_rem is not None else _number(native.get("primary_remaining_pct"))
        reset = pri_reset
        if pri_mins is not None:
            return rem, reset, pri_mins, True, None
        fallback_mins, fallback_ok = _first_window_minutes(native, info)
        return rem, reset, fallback_mins, fallback_ok, None

    # 4. Flat record / flat codexbar (strictly weekly/secondary/top-level; never primary_remaining_pct).
    flat_mins, flat_ok = _first_window_minutes(native, info)
    if not flat_ok:
        return None, None, None, False, "missing forecast: invalid window_minutes"
    if weekly_rem is None and weekly_reset is not None and _number(native.get("primary_remaining_pct")) is not None:
        return None, weekly_reset, flat_mins, True, "missing forecast: window mismatch between primary remaining and reset"
    return weekly_rem, weekly_reset, flat_mins, True, None


def pace_forecast(
    record: Mapping[str, Any] | None,
    *,
    snapshot_metadata: Mapping[str, Any] | None = None,
    policy: CreditPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Projected surplus and surplus-per-hour to reset, reusing the existing pace/planning forecasts.

    Accepts either an explicit planning forecast (``burn_pct_per_hour`` or
    ``unused_pct`` with ``reset_at``/``resets_at``, top-level or under
    ``forecast``) or a standard subscription record evaluated through
    :func:`lu_machinery.api.subscription_usage.compute_usage_pace` on one
    window-matched ``(remaining_pct, resets_at, window_minutes)`` tuple.

    Fails closed (``available=False`` with a typed ``reason``) on stale
    snapshots/probes, missing allowance/reset/window_minutes inputs, cross-window
    mismatches, early hidden windows, or reset-crossing timestamps.
    """
    from lu_machinery.api.subscription_usage import compute_usage_pace, pace_is_visible

    def unavailable(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "source": None,
            "remaining_pct": None,
            "hours_to_reset": None,
            "burn_pct_per_hour": None,
            "projected_surplus_pct": None,
            "projected_surplus_per_hour": None,
            "will_last_to_reset": None,
        }

    if not isinstance(record, Mapping):
        return unavailable("missing forecast: lane record missing")

    current = (now or datetime.now(UTC)).astimezone(UTC)
    max_age_s = probe_max_age_s(policy)
    snap_state, snap_reason = snapshot_freshness(snapshot_metadata)
    if snap_state == STALE:
        return unavailable(f"stale forecast: {snap_reason}")
    probe_state, probe_reason = probe_freshness(record, max_age_s)
    if probe_state == STALE:
        return unavailable(f"stale forecast: {probe_reason}")

    native = _sub_mapping(record, "codexbar")
    capacity_block = _sub_mapping(record, "capacity")
    if capacity_block.get("state") == CAPACITY_UNKNOWN_STALE:
        return unavailable("stale forecast: capacity marked unknown_stale")
    if record.get("freshness") == "unavailable" or native.get("freshness") == "unavailable":
        return unavailable("missing forecast: usage probe unavailable")

    raw_fc = record.get("forecast")
    fc: Mapping[str, Any] | None = raw_fc if isinstance(raw_fc, Mapping) else None
    if fc is not None:
        fc_freshness = fc.get("freshness")
        if fc.get("stale") is True or (isinstance(fc_freshness, str) and fc_freshness.startswith("stale")):
            return unavailable("stale forecast: forecast marked stale")
        if (
            "observed_at" in fc
            and fc.get("observed_at") is not None
            and _fresh_at(fc.get("observed_at"), current, max_age_s) is None
        ):
            return unavailable("stale forecast: forecast observation stale or invalid")

    remaining, raw_reset, window_minutes, window_ok, match_err = _matched_pace_window(record, fc)
    if match_err is not None:
        return unavailable(match_err)
    if not window_ok:
        return unavailable("missing forecast: invalid window_minutes")
    if remaining is None or not 0.0 <= remaining <= 100.0:
        return unavailable("missing forecast: remaining allowance unavailable")
    if raw_reset is None:
        return unavailable("missing forecast: reset time unavailable")
    reset_at = _reset_time(raw_reset)
    if reset_at is None:
        return unavailable("missing forecast: reset time unparseable")
    seconds_to_reset = (reset_at - current).total_seconds()
    if not math.isfinite(seconds_to_reset) or seconds_to_reset <= 0:
        return unavailable("reset-crossing forecast: reset time has passed")
    hours_to_reset = seconds_to_reset / 3600.0
    if window_minutes is not None and seconds_to_reset > window_minutes * 60.0:
        return unavailable("reset-crossing forecast: reset outside active window")

    # Path A: explicit planning.forecast() burn rate or unused projection.
    burn_container = fc if (fc is not None and "burn_pct_per_hour" in fc) else record
    if "burn_pct_per_hour" in burn_container and burn_container.get("burn_pct_per_hour") is not None:
        burn = _number(burn_container.get("burn_pct_per_hour"))
        if burn is None or burn < 0.0:
            return unavailable("reset-crossing forecast: invalid burn rate")
        surplus_pct = remaining - burn * hours_to_reset
        surplus_per_hour = surplus_pct / hours_to_reset
        if not (math.isfinite(surplus_pct) and math.isfinite(surplus_per_hour)):
            return unavailable("missing forecast: non-finite projection")
        return {
            "available": True,
            "reason": "",
            "source": "planning_forecast",
            "remaining_pct": remaining,
            "hours_to_reset": round(hours_to_reset, 4),
            "burn_pct_per_hour": round(burn, 6),
            "projected_surplus_pct": round(surplus_pct, 4),
            "projected_surplus_per_hour": round(surplus_per_hour, 6),
            "will_last_to_reset": surplus_pct >= 0.0,
        }

    unused_container = fc if (fc is not None and "unused_pct" in fc) else record
    if "unused_pct" in unused_container and unused_container.get("unused_pct") is not None:
        unused = _number(unused_container.get("unused_pct"))
        if unused is None or unused < 0.0 or unused > remaining + 1e-6:
            return unavailable("missing forecast: invalid unused projection")
        surplus_per_hour = unused / hours_to_reset
        if not math.isfinite(surplus_per_hour):
            return unavailable("missing forecast: non-finite projection")
        raw_lasts = native.get("will_last_to_reset", record.get("will_last"))
        will_last = True if unused > 0.0 else (raw_lasts if isinstance(raw_lasts, bool) else None)
        return {
            "available": True,
            "reason": "",
            "source": "planning_forecast",
            "remaining_pct": remaining,
            "hours_to_reset": round(hours_to_reset, 4),
            "burn_pct_per_hour": None,
            "projected_surplus_pct": round(unused, 4),
            "projected_surplus_per_hour": round(surplus_per_hour, 6),
            "will_last_to_reset": will_last,
        }

    # Path B: elapsed-window linear forecast requires an explicit window_minutes
    # from the matched window (never silently assume a 7-day weekly duration).
    if window_minutes is None:
        return unavailable("missing forecast: window_minutes unavailable")
    pace = compute_usage_pace(
        100.0 - remaining,
        reset_at.isoformat(),
        window_minutes=window_minutes,
        now=current,
    )
    if pace is None:
        return unavailable("reset-crossing forecast: reset outside active window")
    if not pace_is_visible(pace):
        return unavailable("missing forecast: window too early for pace")
    expected_pct = _number(pace.get("expected_pct"))
    actual_pct = _number(pace.get("actual_pct"))
    if expected_pct is None or actual_pct is None or not 0.0 < expected_pct < 100.0:
        return unavailable("missing forecast: invalid pace window")
    elapsed_hours = hours_to_reset * expected_pct / (100.0 - expected_pct)
    if not math.isfinite(elapsed_hours) or elapsed_hours <= 0.0:
        return unavailable("missing forecast: invalid elapsed window")
    burn = actual_pct / elapsed_hours
    surplus_pct = remaining - burn * hours_to_reset
    surplus_per_hour = surplus_pct / hours_to_reset
    if not (math.isfinite(burn) and math.isfinite(surplus_pct) and math.isfinite(surplus_per_hour)):
        return unavailable("missing forecast: non-finite projection")
    return {
        "available": True,
        "reason": "",
        "source": "usage_pace",
        "remaining_pct": remaining,
        "hours_to_reset": round(hours_to_reset, 4),
        "burn_pct_per_hour": round(burn, 6),
        "projected_surplus_pct": round(surplus_pct, 4),
        "projected_surplus_per_hour": round(surplus_per_hour, 6),
        "will_last_to_reset": bool(pace.get("will_last_to_reset")),
    }


_GROK_CURSOR_SEVERITY = {"idle": 0, "cool": 0, "warm": 1, "hot": 2, "near_cap": 3}


def _has_verified_binding_signal(info: Mapping[str, Any]) -> bool:
    status = str(info.get("status") or "").strip().lower()
    if status and status not in {"unknown", "unavailable", "need_login"}:
        return True
    return plan_remaining_pct(info) is not None


def grok_cursor_bindings_verified(
    grok_info: Mapping[str, Any] | None,
    cursor_info: Mapping[str, Any] | None,
) -> bool:
    """True when both Grok and Cursor have verified bindings on their shared meter.

    Missing records, NEED_LOGIN, explicit ineligibility, transport-specific
    unhealthy / headroom_blocked states, malformed runtime telemetry, or a
    Cursor-only Auto monthly hot cap keep the rows separate instead of hiding
    a per-transport failure.
    """
    if not isinstance(grok_info, Mapping) or not isinstance(cursor_info, Mapping):
        return False
    if not grok_info or not cursor_info:
        return False
    for record in (grok_info, cursor_info):
        if _need_login(record) or record.get("eligible") is False:
            return False
        if health_fact(record)[0] == UNHEALTHY:
            return False
        runtime = record.get("runtime")
        if runtime is not None and not isinstance(runtime, Mapping):
            return False
        if isinstance(runtime, Mapping) and runtime.get("headroom_blocked") is True:
            return False
        if not _has_verified_binding_signal(record):
            return False
    return not (cursor_info.get("status_source") == "cursor_auto" and cursor_info.get("status") == "hot")


def merge_grok_cursor_record(
    grok_info: Mapping[str, Any],
    cursor_info: Mapping[str, Any],
    *,
    policy: CreditPolicy | None = None,
) -> dict[str, Any]:
    """Combine verified Grok and Cursor records into one conservative meter reading.

    Uses the tighter remaining allowance, fails closed on stale freshness and
    higher status severity, and preserves health honesty without mutating inputs.
    """
    grok_rem = plan_remaining_pct(grok_info)
    cursor_rem = plan_remaining_pct(cursor_info)
    if grok_rem is not None and cursor_rem is not None:
        base, other = (grok_info, cursor_info) if grok_rem < cursor_rem else (cursor_info, grok_info)
    elif grok_rem is not None:
        base, other = grok_info, cursor_info
    else:
        base, other = cursor_info, grok_info

    merged = dict(base)
    if isinstance(merged.get("codexbar"), Mapping):
        merged["codexbar"] = copy.deepcopy(dict(merged["codexbar"]))

    base_status = str(merged.get("status") or "unknown")
    other_status = str(other.get("status") or "unknown")
    base_sev = _GROK_CURSOR_SEVERITY.get(base_status, -1)
    other_sev = _GROK_CURSOR_SEVERITY.get(other_status, -1)
    if other_sev > base_sev:
        merged["status"] = other_status
        if "status_source" in other:
            merged["status_source"] = other["status_source"]
        # Do not mix a more severe status with the other reading's pace flags.
        if isinstance(other.get("codexbar"), Mapping):
            merged["codexbar"] = copy.deepcopy(dict(other["codexbar"]))
        else:
            merged.pop("codexbar", None)
    else:
        # Preserve reset, window, and pace evidence observed on the other transport
        # when base lacks it, without mutating inputs or weakening base's tighter remaining.
        base_rem = plan_remaining_pct(base)
        for key in ("resets_at", "reset_at", "window_minutes", "windowMinutes"):
            if merged.get(key) in (None, "") and other.get(key) not in (None, ""):
                merged[key] = other[key]

        def _clamp_window_block(block: dict[str, Any]) -> None:
            if base_rem is None:
                return
            win_rem = _block_remaining(block)
            if win_rem is not None and base_rem < win_rem:
                block["remaining_pct"] = base_rem
                if "used_pct" in block:
                    block["used_pct"] = round(100.0 - base_rem, 4)

        if merged.get("provider_windows") is None and isinstance(other.get("provider_windows"), Mapping):
            cloned_pw = copy.deepcopy(dict(other["provider_windows"]))
            if isinstance(cloned_pw.get("auto"), dict):
                _clamp_window_block(cloned_pw["auto"])
            merged["provider_windows"] = cloned_pw

        other_cb = other.get("codexbar")
        if isinstance(other_cb, Mapping):
            base_cb: dict[str, Any] = (
                merged["codexbar"] if isinstance(merged.get("codexbar"), dict) else {}
            )
            for key in (
                "weekly_resets_at",
                "resets_at",
                "window_minutes",
                "windowMinutes",
                "pace_summary",
                "weekly_expected_pct",
                "weekly_pace_delta_pct",
                "trend",
                "headroom_pct",
            ):
                if base_cb.get(key) in (None, "") and other_cb.get(key) not in (None, ""):
                    base_cb[key] = other_cb[key]
            if not isinstance(base_cb.get("provider_windows"), Mapping) and isinstance(
                other_cb.get("provider_windows"), Mapping
            ):
                cloned_cb_pw = copy.deepcopy(dict(other_cb["provider_windows"]))
                if isinstance(cloned_cb_pw.get("auto"), dict):
                    _clamp_window_block(cloned_cb_pw["auto"])
                base_cb["provider_windows"] = cloned_cb_pw
            if not isinstance(base_cb.get("windows"), Mapping) and isinstance(other_cb.get("windows"), Mapping):
                cloned_windows = copy.deepcopy(dict(other_cb["windows"]))
                for win_name in ("secondary", "primary"):
                    win_block = cloned_windows.get(win_name)
                    if isinstance(win_block, dict):
                        _clamp_window_block(win_block)
                base_cb["windows"] = cloned_windows
            # Conservative will_last_to_reset carry: False always wins; True carries only
            # when base has no reading and base's status is not worse than other's.
            if other_cb.get("will_last_to_reset") is False:
                base_cb["will_last_to_reset"] = False
            elif (
                base_cb.get("will_last_to_reset") is None
                and other_cb.get("will_last_to_reset") is True
                and base_sev <= other_sev
            ):
                base_cb["will_last_to_reset"] = True
            if base_cb:
                merged["codexbar"] = base_cb

    max_age_s = probe_max_age_s(policy)
    if probe_freshness(other, max_age_s)[0] == STALE:
        other_native = _sub_mapping(other, "codexbar")
        for key in ("freshness", "age_s", "stale", "fetched_at"):
            val = other.get(key, other_native.get(key))
            if val is not None:
                merged[key] = val
                if isinstance(merged.get("codexbar"), dict):
                    merged["codexbar"][key] = val

    grok_health, _ = health_fact(grok_info)
    cursor_health, _ = health_fact(cursor_info)
    if grok_health == HEALTHY and cursor_health == HEALTHY:
        merged["health"] = dict(cursor_info.get("health") or grok_info.get("health") or {"healthy": True})
    elif grok_health == UNHEALTHY or cursor_health == UNHEALTHY:
        bad = grok_info if grok_health == UNHEALTHY else cursor_info
        merged["health"] = dict(bad.get("health") or {"healthy": False})
    else:
        merged.pop("health", None)

    return merged


def sum_in_flight(first: Any, second: Any) -> int | None:
    """Sum two observed non-negative integer in-flight counts; None when either is missing or invalid."""
    if (
        isinstance(first, bool)
        or isinstance(second, bool)
        or not isinstance(first, int)
        or not isinstance(second, int)
        or first < 0
        or second < 0
    ):
        return None
    return first + second


def _projected_runout(info: dict[str, Any], pace: dict[str, Any] | None, now: datetime) -> datetime | None:
    """Recompute linear run-out from the current clock, using the existing pace window math."""
    from lu_machinery.api.subscription_usage import _expected_pct_from_reset

    if not isinstance(pace, dict):
        return None
    natural = _natural_reset(info)
    if natural is None:
        for key in ("weekly_resets_at", "resets_at", "resetsAt"):
            natural = _reset_time(pace.get(key))
            if natural is not None:
                break
    if natural is None or natural <= now:
        return None
    try:
        expected = _expected_pct_from_reset({**pace, "weekly_resets_at": natural.isoformat()}, now=now)
    except (ValueError, OverflowError):
        return None
    used = next(
        (
            value
            for key in ("weekly_used_pct", "actual_pct", "used_pct")
            if (value := _number(pace.get(key))) is not None
        ),
        None,
    )
    if expected is None or not 0 < expected < 100 or used is None or not 0 < used <= 100:
        return None
    elapsed_s = (natural - now).total_seconds() * expected / (100 - expected)
    try:
        return now + timedelta(seconds=elapsed_s * (100 - used) / used)
    except OverflowError:
        return None


def _verified_inventory(
    info: dict[str, Any], policy: CreditPolicy, now: datetime, *, snapshot_stale: bool
) -> list[datetime | None] | None:
    """Unexpired free-reset expiries from a fresh inventory; None when unverified."""
    if not _fresh_probe(info, policy, snapshot_stale=snapshot_stale)[0]:
        return None
    inventory = _field(info, "reset_credits")
    if not isinstance(inventory, dict):
        return None
    count = inventory.get("available_count")
    expirations = inventory.get("expires_at")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(expirations, list)
        or _fresh_at(inventory.get("fetched_at"), now, policy.credit_max_age_s) is None
    ):
        return None
    live: list[datetime | None] = []
    for value in expirations:
        expiry = utc_datetime(value)
        if value is not None and expiry is None:
            return None
        if expiry is None or expiry > now:
            live.append(expiry)
    return live[: min(count, len(live))]


def reset_advice(
    lane: str,
    info: dict[str, Any] | None,
    policy: CreditPolicy,
    state: str,
    *,
    now: datetime | None = None,
    snapshot_stale: bool = False,
) -> dict[str, Any]:
    """Whether a free full reset is useful now; advisory only, never consumes one."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    record = info if isinstance(info, dict) else {}
    natural = _natural_reset(record)
    hours = round((natural - current).total_seconds() / 3600.0, 1) if natural is not None else None
    live = _verified_inventory(record, policy, current, snapshot_stale=snapshot_stale)
    advice: dict[str, Any] = {
        "natural_reset_at": _iso(natural) if natural is not None else None,
        "hours_to_natural_reset": hours,
        "free_resets_available": len(live) if live is not None else None,
        "free_reset_expires_at": [_iso(e) if e is not None else None for e in live] if live is not None else None,
        "hold_hours": policy.reset_hold_hours,
        "window_anchor_evidence": ANCHOR_EVIDENCE,
    }

    def decide(verdict: str, reason: str) -> dict[str, Any]:
        return {**advice, "advice": verdict, "reason": reason}

    if live is None:
        return decide(NOT_APPLICABLE, "free-reset inventory missing or stale")
    if not live:
        return decide(NOT_APPLICABLE, "no free full reset available")
    if state in {PLAN_HEALTHY, PLAN_UNKNOWN, NOT_CONFIGURED}:
        return decide(NOT_APPLICABLE, f"plan state {state}: a reset adds nothing now")
    if hours is None:
        return decide(HOLD_RESET, "natural reset time unknown; window anchor behaviour unverified")
    if hours <= policy.reset_hold_hours:
        return decide(
            HOLD_RESET,
            f"natural reset in {hours:g}h is within the {policy.reset_hold_hours:g}h hold window; "
            "window anchor behaviour unverified",
        )
    return decide(
        USE_RESET_NOW,
        f"natural reset in {hours:g}h is beyond the {policy.reset_hold_hours:g}h hold window; "
        "using a reset is an operator decision (window anchor behaviour unverified)",
    )


def lane_credit_report(
    lane: str,
    info: dict[str, Any] | None,
    policy: CreditPolicy,
    *,
    now: datetime | None = None,
    snapshot_stale: bool = False,
    usage_dir: Path | None = None,
    rate_limit_reader: RateLimitReader | None = None,
) -> dict[str, Any]:
    """Credit state plus reset advice for a configured lane; ``{"state": "not_configured"}`` otherwise."""
    state = lane_credit_state(
        lane,
        info,
        policy,
        now=now,
        snapshot_stale=snapshot_stale,
        usage_dir=usage_dir,
        rate_limit_reader=rate_limit_reader,
    )
    if state["state"] == NOT_CONFIGURED:
        return state
    return {
        **state,
        "reset_advice": reset_advice(lane, info, policy, state["state"], now=now, snapshot_stale=snapshot_stale),
    }


def published_credit_relief(
    lane: str,
    published: Any,
    model: str | None,
    *,
    policy: CreditPolicy | None = None,
    now: datetime | None = None,
    record: Mapping[str, Any] | None = None,
    snapshot_stale: bool = False,
) -> dict[str, Any] | None:
    """Re-check the credit state a routing-budget snapshot published for ``lane`` (``agents.<lane>.credit``).

    For consumers that act on a snapshot (the reviewer resolver and the
    coordinator wave gate). None when there is nothing that could relax the plan
    state: the snapshot does not say ``credit_balance_present``, the lane is
    not in the local policy, or the policy is unreadable. Otherwise a receipt
    with the state, reason, evidence, the local allowlist and whether ``model``
    is on it; the state reads ``credits_unverified`` when the published balance
    fetch time is no longer fresh now (an old snapshot file proves nothing).

    ``record`` is the complete published lane record (#9740 F6): relief then
    needs the owner's own decision, :func:`lane_credit_state` over that record
    now, to read ``credit_balance_present`` as well. The snapshot staleness, the
    probe's freshness, age and stale flag, the raw balance and its fetch time,
    the record's runtime evidence (``runtime.rate_limited``,
    ``runtime.headroom_blocked``) and the current shared rate-limit records all
    count, so a published label cannot override contradictory inputs; the
    receipt carries the owner's state and reason otherwise. Without ``record``
    only the leaf is re-checked; every routing consumer passes it.

    The published rate-limit evidence is as old as the snapshot, so the
    current evidence is re-read through :func:`read_recent_rate_limits` (the
    shared runtime usage records): a rate limit since then reads
    ``credit_use_unconfirmed`` and unreadable records ``credits_unverified``,
    as :func:`lane_credit_state` would decide now.
    """
    if not isinstance(published, dict) or published.get("state") != CREDIT_BALANCE_PRESENT:
        return None
    if policy is None:
        try:
            policy = load_policy()
        except ValueError:
            return None
    allowed = policy.lane_models(lane)
    if allowed is None:
        return None
    current = (now or datetime.now(UTC)).astimezone(UTC)
    evidence = published.get("evidence") if isinstance(published.get("evidence"), dict) else {}
    receipt: dict[str, Any] = {
        "lane": lane.strip().lower(),
        "state": CREDIT_BALANCE_PRESENT,
        "reason": published.get("reason"),
        "credit_balance": published.get("credit_balance"),
        "evidence": dict(evidence),
        "allowed_models": list(allowed),
        "model": model,
        "model_allowed": _allowed(allowed, model),
        "draw": DRAW_NOT_VERIFIED,
    }
    if record is None and snapshot_stale:
        receipt["state"] = CREDITS_UNVERIFIED
        receipt["reason"] = "published credit relief not re-verified: routing-budget snapshot is stale"
        return receipt
    if _fresh_at(evidence.get("credit_fetched_at"), current, policy.credit_max_age_s) is None:
        receipt["state"] = CREDITS_UNVERIFIED
        receipt["reason"] = (
            f"published credit balance fetch time missing, not explicit UTC, or older than {policy.credit_max_age_s:g}s"
        )
        return receipt
    if record is not None:
        owner = lane_credit_state(lane, dict(record), policy, now=current, snapshot_stale=snapshot_stale)
        receipt["evidence"].update({**owner["evidence"], "rate_limits_checked_at": _iso(current)})
        receipt["credit_balance"] = owner.get("credit_balance")
        if owner["state"] != CREDIT_BALANCE_PRESENT:
            receipt["state"] = owner["state"]
            receipt["reason"] = f"published credit relief not re-verified: {owner.get('reason')}"
        return receipt
    count, last, unreadable = _rate_limit_evidence(lane, {}, policy, current)
    receipt["evidence"].update(
        {"rate_limited_count": count, "last_rate_limited_at": last, "rate_limits_checked_at": _iso(current)}
    )
    if unreadable is not None:
        receipt["evidence"]["unreadable_records"] = unreadable
    if count is None:
        receipt["state"] = CREDITS_UNVERIFIED
        receipt["reason"] = "runtime usage records unreadable now: rate limits cannot be ruled out"
    elif count > 0:
        receipt["state"] = CREDIT_USE_UNCONFIRMED
        receipt["reason"] = RATE_LIMIT_REASON
    return receipt


def _allowed(allowed: tuple[str, ...], model: str | None) -> bool:
    if not model:
        return False
    from lu_machinery.review.model_catalog import canonical_model_id

    return (canonical_model_id(model) or model.strip().lower()) in allowed


def model_allowed(policy: CreditPolicy, lane: str, model: str | None) -> bool:
    """True when ``model`` (catalog-canonicalised) is on ``lane``'s credit-period allowlist."""
    allowed = policy.lane_models(lane)
    return True if allowed is None else _allowed(allowed, model)


def refusal_text(lane: str, model: str | None, state: dict[str, Any], policy: CreditPolicy) -> str:
    return (
        f"{REFUSAL_CODE}: lane {lane} is {state['state']} ({state.get('reason')}); a fresh credit balance "
        f"exists while the plan window is exhausted, so model {model or '(none)'} "
        f"is outside the credit-period allowlist [{', '.join(policy.lane_models(lane) or ())}] "
        f"({policy.path.name}). Dispatch an allowlisted model or another lane."
    )


@dataclass(frozen=True)
class RoutingFacts:
    """Typed routing facts of one lane record, compared by every consumer before its final action (#9740).

    ``model`` None is lane inventory: credit coverage then reads "for the
    allowlisted models" (``credit_models``) and ``model_permission`` stays
    unresolved (None). A concrete model is a route-specific observation.
    ``raw_deficit``/``uncovered`` are the owner's pace decision unchanged;
    ``observation_freshness`` qualifies it, and ``capacity`` is the lane's
    class for recommendation and capacity consumers (see ``CAPACITY_*``).
    """

    lane: str
    model: str | None
    observed_at: str
    plan_remaining_pct: float | None
    remaining_source: str | None
    snapshot_freshness: str
    probe_freshness: str
    observation_freshness: str
    freshness_reason: str
    health: str
    health_basis: str
    pace_visible: bool | None
    raw_deficit: bool | None
    uncovered: bool | None
    covered_by: tuple[str, ...]
    pace_reason: str
    status: str
    status_source: str | None
    capacity: str
    capacity_reason: str
    credit: dict[str, Any]
    credit_models: tuple[str, ...] | None
    model_permission: bool | None
    refusal_reason: str | None

    @property
    def pace_deficit(self) -> dict[str, Any]:
        """The :func:`pace_deficit_state` shape, with the effective status."""
        return {
            "raw_deficit": self.raw_deficit,
            "uncovered": self.uncovered,
            "covered_by": list(self.covered_by),
            "status": self.status,
            "reason": self.pace_reason,
        }

    @property
    def credit_relief(self) -> bool:
        """A fresh credit balance relaxes the plan state for this observation (lane or allowlisted model)."""
        return self.credit.get("state") == CREDIT_BALANCE_PRESENT and self.model_permission is not False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["covered_by"] = list(self.covered_by)
        payload["credit_models"] = None if self.credit_models is None else list(self.credit_models)
        return payload

    def summary(self) -> dict[str, Any]:
        """:meth:`to_dict` without the credit report (published next to it as ``credit``)."""
        payload = self.to_dict()
        payload.pop("credit")
        payload["credit_state"] = self.credit.get("state")
        return payload


def _need_login(record: Mapping[str, Any]) -> bool:
    return (
        "NEED_LOGIN" in {record.get("login_state"), record.get("probe_state")} or record.get("status") == "need_login"
    )


def _capacity_class(
    record: Mapping[str, Any],
    *,
    status: str,
    remaining: float | None,
    near_cap_pct: float,
    health: str,
    freshness: str,
    freshness_reason: str,
    pace: dict[str, Any],
    window_exhausted: bool,
    credit_relief: bool,
    credit_reason: str,
    lasts_to_reset: bool | None = None,
    probe: str = UNKNOWN,
) -> tuple[str, str]:
    """Capacity class and reason; the ordering is the routing rule (A2, #9740).

    Hard reasons (ineligible, unhealthy, NEED_LOGIN), near cap without credit
    relief and runtime-blocked hot stay ``avoid`` whatever the freshness. Only
    then does a stale observation turn a raw pace deficit or a weekly-pace hot
    label into ``unknown_stale``. A positively fresh weekly-pace over-pace
    (``hot``) subscription lane that still lasts to reset is ``verified`` (sorts
    lower, not AVOID); missing telemetry (unknown snapshot or probe freshness),
    non-weekly-pace hot labels, near cap, and real exhaustion stay ``avoid``.
    """
    if record.get("eligible") is False:
        return CAPACITY_AVOID, "ineligible"
    if health == UNHEALTHY:
        return CAPACITY_AVOID, "unhealthy lane"
    if _need_login(record):
        return CAPACITY_AVOID, "NEED_LOGIN"
    runtime = record.get("runtime") if isinstance(record.get("runtime"), Mapping) else {}
    at_near_cap = (
        status == "near_cap"
        or window_exhausted
        or (remaining is not None and remaining <= _NEAR_CAP_REMAINING_PCT)
    )
    if at_near_cap:
        if credit_relief:
            return CAPACITY_VERIFIED, f"near cap; {credit_reason}"
        where = f"{remaining:g}% remaining" if remaining is not None else "status near_cap"
        return CAPACITY_AVOID, f"near cap ({where}); {credit_reason}"
    if status == "hot" and runtime.get("headroom_blocked"):
        return CAPACITY_AVOID, "runtime headroom blocked"
    if record.get("freshness") == "unavailable":
        return CAPACITY_UNKNOWN, "usage probe unavailable"
    # A hot label from any source other than weekly pace (ledger burn, Cursor
    # Auto, a source-less record) is its own current reason and stays avoid.
    pace_only = status != "hot" or record.get("status_source") == "weekly_pace"
    pace_signal = pace["raw_deficit"] is True or status == "hot"
    if freshness == STALE and pace_only and pace_signal and not pace["covered_by"]:
        return (
            CAPACITY_UNKNOWN_STALE,
            f"{STALE_ADVISORY_LABEL}: pace deficit read from stale evidence ({freshness_reason})",
        )
    if (
        status == "hot"
        and record.get("status_source") == "weekly_pace"
        and freshness == FRESH
        and probe == FRESH
        and lasts_to_reset is True
        and pace["uncovered"] is not True
    ):
        return CAPACITY_VERIFIED, "over-pace; lasts to reset"
    if status == "hot" or pace["uncovered"] is True:
        return CAPACITY_AVOID, pace["reason"] if pace["uncovered"] is True else "status hot"
    if status in {"cool", "warm", "idle"}:
        return CAPACITY_VERIFIED, f"plan status {status}"
    return CAPACITY_UNKNOWN, f"plan status {status}"


def routing_facts(
    lane: str,
    record: Mapping[str, Any] | None,
    *,
    model: str | None,
    snapshot_metadata: Mapping[str, Any] | None = None,
    policy: CreditPolicy | None = None,
    now: datetime | None = None,
    usage_dir: Path | None = None,
) -> RoutingFacts:
    """The one consumer-facing reading of a complete routing-budget lane record (#9740).

    Composes the owner's calculations over one clock and one shared
    rate-limit observation: remaining allowance (:func:`plan_remaining_reading`),
    snapshot/probe freshness, health, pace (:func:`pace_deficit_state`), credit
    and reset evidence (:func:`lane_credit_report`) and credit-period model
    permission. Published ``credit``/``pace_deficit`` conclusions in the record
    are ignored: only their inputs count. ``snapshot_metadata`` is the
    routing-budget ``diagnostics`` (missing staleness is unknown, not fresh).
    ``model`` is required: None means lane inventory. ``policy`` None loads the
    shipped policy; an unreadable one reads ``policy_error`` (plan state applies).

    A weekly-pace hot label without a runtime block that the owner's own pace
    reading does not confirm takes the remaining-allowance status, never a
    default ``cool``: pace hidden below the visibility floor (A3), or a visible
    reading with no deficit on a fresh observation (#9040, A8: the early-window
    false positive). Fresh there means a fresh snapshot *and* a positively
    fresh probe: a missing probe age or an unknown probe freshness keeps the
    label hot. A hot label from any other source stays hot.
    """
    from lu_machinery.api.subscription_usage import ON_PACE_BAND_PCT, pace_expected_pct, pace_is_visible

    lane_key = lane.strip().lower()
    data: dict[str, Any] = dict(record) if isinstance(record, Mapping) else {}
    current = (now or datetime.now(UTC)).astimezone(UTC)
    policy_error = ""
    if policy is None:
        try:
            policy = load_policy()
        except ValueError as exc:
            policy_error = str(exc)
    near_cap_pct = policy.near_cap_remaining_pct if policy is not None else _CREDIT_NEAR_CAP_REMAINING_PCT
    max_age_s = policy.credit_max_age_s if policy is not None else UNREADABLE_POLICY_MAX_AGE_S
    snapshot, _ = snapshot_freshness(snapshot_metadata)
    probe, _ = probe_freshness(data, max_age_s)
    freshness, freshness_reason = observation_freshness(data, snapshot_metadata, max_age_s)
    snapshot_stale = snapshot == STALE
    health, health_basis = health_fact(data)
    remaining, remaining_source = plan_remaining_reading(data)

    reader = _shared_rate_limit_reader(lane_key, policy, current, usage_dir) if policy is not None else None
    if policy is not None:
        credit = lane_credit_report(
            lane_key,
            data,
            policy,
            now=current,
            snapshot_stale=snapshot_stale,
            usage_dir=usage_dir,
            rate_limit_reader=reader,
        )
    else:
        credit = policy_error_state(lane_key, policy_error)
    pace_record = data.get("codexbar") or data.get("pace") or data
    pace = pace_deficit_state(
        lane_key,
        data,
        model=model,
        policy=policy,
        now=current,
        snapshot_stale=snapshot_stale,
        usage_dir=usage_dir,
        rate_limit_reader=reader,
    )
    forecast = pace_forecast(data, snapshot_metadata=snapshot_metadata, policy=policy, now=current)
    raw_lasts = (
        pace_record.get("will_last_to_reset", pace_record.get("willLastToReset"))
        if isinstance(pace_record, Mapping)
        else None
    )
    if forecast["available"] and forecast["source"] == "planning_forecast" and isinstance(
        forecast.get("will_last_to_reset"), bool
    ):
        lasts_to_reset: bool | None = forecast["will_last_to_reset"]
    elif isinstance(raw_lasts, bool):
        lasts_to_reset = raw_lasts
    elif isinstance(data.get("will_last"), bool):
        lasts_to_reset = data["will_last"]
    elif forecast["available"] and isinstance(forecast.get("will_last_to_reset"), bool):
        lasts_to_reset = forecast["will_last_to_reset"]
    else:
        lasts_to_reset = None

    raw_delta = (
        next(
            (
                val
                for key in ("delta_pct", "weekly_pace_delta_pct", "deltaPercent")
                if (val := _number(pace_record.get(key))) is not None
            ),
            None,
        )
        if isinstance(pace_record, Mapping)
        else None
    )
    over_pace_lasts = lasts_to_reset is True and raw_delta is not None and raw_delta > ON_PACE_BAND_PCT

    expected = pace_expected_pct(pace_record, now=current) if isinstance(pace_record, dict) else None
    visible = None if expected is None else pace_is_visible({"expected_pct": expected})
    status = str(pace["status"] or UNKNOWN)
    runtime = data.get("runtime") if isinstance(data.get("runtime"), Mapping) else {}
    if status == "hot" and data.get("status_source") == "weekly_pace" and not runtime.get("headroom_blocked"):
        cleared_by = None
        if pace["raw_deficit"] is None and visible is False:
            cleared_by = "pace hidden below the visibility floor"
        elif pace["raw_deficit"] is False and freshness == FRESH and probe == FRESH and not over_pace_lasts:
            # The label is the probe's own pace reading: a fresh snapshot alone (which covers a
            # probe-less ledger lane) does not verify it, so the probe must be positively fresh.
            cleared_by = "no pace deficit on a fresh observation"
        if cleared_by is not None:
            status = allowance_status(remaining)
            pace = {**pace, "status": status, "reason": f"{cleared_by}; status {status} from remaining allowance"}

    allowed = policy.lane_models(lane_key) if policy is not None else DEFAULT_ALLOWED_MODELS.get(lane_key)
    permission: bool | None = None
    refusal: str | None = None
    if model is not None:
        permission = not allowlist_applies(credit) or allowed is None or _allowed(tuple(allowed), model)
        if not permission:
            refusal = (
                refusal_text(lane_key, model, credit, policy)
                if policy is not None
                else f"{REFUSAL_CODE}: lane {lane_key} is {POLICY_ERROR}; model {model} is outside the built-in "
                f"credit-period allowlist [{', '.join(allowed or ())}]"
            )
    credit_present = credit.get("state") == CREDIT_BALANCE_PRESENT
    credit_models = (
        tuple(allowed) if allowed is not None and (credit_present or "credits" in pace["covered_by"]) else None
    )
    capacity, capacity_reason = _capacity_class(
        data,
        status=status,
        remaining=remaining,
        near_cap_pct=near_cap_pct,
        health=health,
        freshness=freshness,
        freshness_reason=freshness_reason,
        pace=pace,
        window_exhausted=plan_window_exhausted(lane_key, data, policy),
        credit_relief=credit_present and permission is not False,
        credit_reason=f"credit {credit.get('state')}: {credit.get('reason') or ''}".rstrip(": "),
        lasts_to_reset=lasts_to_reset,
        probe=probe,
    )
    return RoutingFacts(
        lane=lane_key,
        model=model,
        observed_at=_iso(current),
        plan_remaining_pct=remaining,
        remaining_source=remaining_source,
        snapshot_freshness=snapshot,
        probe_freshness=probe,
        observation_freshness=freshness,
        freshness_reason=freshness_reason,
        health=health,
        health_basis=health_basis,
        pace_visible=visible,
        raw_deficit=pace["raw_deficit"],
        uncovered=pace["uncovered"],
        covered_by=tuple(pace["covered_by"]),
        pace_reason=str(pace["reason"]),
        status=status,
        status_source=data.get("status_source"),
        capacity=capacity,
        capacity_reason=capacity_reason,
        credit=credit,
        credit_models=credit_models,
        model_permission=permission,
        refusal_reason=refusal,
    )


def read_routing_budget(*, timeout: float = 8.0) -> dict[str, Any] | None:
    """Read Monitor's routing budget, retrying its typed warming response once within the read budget."""
    base = os.environ.get("DELEGATE_MONITOR_API", "http://127.0.0.1:8765").rstrip("/")
    deadline = time.monotonic() + timeout
    try:
        for attempt in range(2):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                return None
            try:
                with urllib.request.urlopen(f"{base}/api/state/routing-budget?fresh_codexbar=true", timeout=timeout) as resp:
                    payload = json_depth.loads(resp.read().decode("utf-8"), max_depth=512)
                return payload if isinstance(payload, dict) else None
            except urllib.error.HTTPError as exc:
                with exc:
                    if attempt or exc.code != 503 or exc.headers is None:
                        return None
                    retry_after = exc.headers.get("Retry-After", "").strip()
                    if not retry_after.isascii() or not retry_after.isdecimal():
                        return None
                    delay = int(retry_after)
                    if delay > _MAX_WARMING_RETRY_DELAY_S:
                        return None
                    if json_depth.loads(exc.read(1024), max_depth=8) != {"detail": "routing budget warming"}:
                        return None
                if delay >= deadline - time.monotonic():
                    return None
                time.sleep(delay)
    except (OSError, HTTPException, ValueError, RecursionError):
        return None
    return None


def _policy_error_refusal(lane: str, model: str | None, error: str) -> str | None:
    """Admission under an unreadable policy: only off-default-allowlist models on default lanes are refused."""
    print(
        f"⚠ credit-lane policy unreadable ({error}); only "
        f"{', '.join(sorted(DEFAULT_ALLOWED_MODELS))} dispatches are restricted, to the built-in allowlist.",
        file=sys.stderr,
    )
    allowed = DEFAULT_ALLOWED_MODELS.get(lane)
    if allowed is None or _allowed(allowed, model):
        return None
    return (
        f"{REFUSAL_CODE}: lane {lane} is {POLICY_ERROR} (credit-lane policy unreadable: {error}); "
        f"model {model or '(none)'} is outside the built-in credit-period allowlist [{', '.join(allowed)}]. "
        "Dispatch an allowlisted model or another lane, and repair the policy file."
    )


def dispatch_refusal(
    lane: str, model: str | None, *, codex_account: str | None = None, policy: CreditPolicy | None = None
) -> str | None:
    """Check an off-allowlist model against the credit policy and the routing-budget snapshot.

    ``model`` is the model the dispatch launches (the caller resolves the
    lane default first). The snapshot is read only for an off-allowlist model
    on a configured lane. An unreadable snapshot prevents admission. An
    unreadable policy restricts only :data:`DEFAULT_ALLOWED_MODELS` lanes.
    The Codex subscription uses its own meter. API-key routes have no plan window.
    """
    lane_key = lane.strip().lower()
    if lane_key == "codex":
        from lu_machinery.agent_runtime.codex_accounts import CODEX_SUBSCRIPTION_ACCOUNTS, resolve_codex_account_label

        account = resolve_codex_account_label(codex_account)
        if account not in CODEX_SUBSCRIPTION_ACCOUNTS:
            return None
        if account == "b-sub":
            lane_key = "codex:b"
    if policy is None:
        try:
            policy = load_policy()
        except ValueError as exc:
            return _policy_error_refusal(lane_key, model, str(exc))
    if model_allowed(policy, lane_key, model):
        return None
    from lu_machinery.fleet.claude_account_lane import bind_snapshot, no_claude_account, other_lane_record

    raw = read_routing_budget()
    # A credit lane is one no Claude account owns: the snapshot is bound to no account where it
    # enters, and the claude lane is never read without its account (#10630, #10770).
    if raw is not None:
        budget = bind_snapshot(raw, account=no_claude_account())
        now = datetime.now(UTC)
        if lane_key == "codex:b":
            accounts = budget.get("codex_accounts")
            row = accounts.get(lane_key) if isinstance(accounts, Mapping) else None
            record = dict(row) if isinstance(row, Mapping) else {}
            remaining = plan_remaining_pct(record)
            # Aggregate diagnostics describe other lanes too. Account b needs its own fresh
            # provider reading because the account row can also carry a ledger estimate.
            freshness = (
                FRESH
                if record.get("codex_account") == "b-sub"
                and record.get("status_source") == "weekly_pace"
                and probe_freshness(record, policy.credit_max_age_s)[0] == FRESH
                and _fresh_at(_field(record, "fetched_at"), now, policy.credit_max_age_s) is not None
                and remaining is not None
                and 0 <= remaining <= 100
                else UNKNOWN
            )
        else:
            record = dict(other_lane_record(budget, lane_key))
            freshness, _ = snapshot_freshness(budget.get("diagnostics"))
        if isinstance(record.get("status"), str) and freshness != UNKNOWN:
            state = lane_credit_state(lane_key, record, policy, now=now, snapshot_stale=freshness == STALE)
            return refusal_text(lane_key, model, state, policy) if allowlist_applies(state) else None
    return (
        f"{REFUSAL_CODE}: lane {lane_key} is routing_snapshot_unavailable "
        "(Monitor routing-budget snapshot unreadable for this lane). "
        f"Dispatch an allowlisted model [{', '.join(policy.lane_models(lane_key) or ())}] "
        "or retry when Monitor can serve the routing-budget snapshot."
    )
