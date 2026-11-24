"""Runtime observability API router."""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import inspect
import ipaddress
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from lu_machinery.agent_identity import RETIRED_AGENT_ALIASES
from lu_machinery.agent_runtime import registry as agent_registry
from lu_machinery.agent_runtime.accounting import recorded_model_accounting_fields
from lu_machinery.agent_runtime.adapters.acpx import ACPX_SUPPORTED_PARTICIPANTS
from lu_machinery.agent_runtime.adapters.gemini import has_gemini_oauth_credentials, resolve_gemini_auth_mode
from lu_machinery.agent_runtime.attempt_boundary import CLAUDE_ACCOUNT_REFUSAL_CODES
from lu_machinery.agent_runtime.codex_accounts import (
    DEFAULT_CODEX_ACCOUNT,
    CodexAccountRefused,
    resolve_codex_account_label,
)
from lu_machinery.agent_runtime.failure_codes import RUNTIME_FAILURE_CODES
from lu_machinery.agent_runtime.usage import _iter_usage_records as _iter_usage_file_records
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.common.task_store_paths import read_cached_task_record
from lu_machinery.dispatcher.admission import review_typed
from lu_machinery.dispatcher.types import StartRefusal
from lu_machinery.fleet import claude_account_lane
from lu_machinery.fleet_comms import message_plane
from lu_machinery.lib.readonly_sqlite import SQLiteConnection, import_named_module, open_readonly
from lu_machinery.orchestration import codex_transport_health, dispatch_admission
from lu_machinery.orchestration.dead_worker_state import worker_liveness
from lu_machinery.orchestration.task_record_store import iter_task_records
from lu_machinery.review import bench_health
from lu_machinery.review.model_catalog import canonical_model_id, load_model_catalog
from lu_machinery.review.reviewer_resolver import (
    REVIEW_CANDIDATES,
    REVIEW_LADDERS,
    ResolverInputs,
    codex_account_key,
    resolve_reviewer,
)

from . import account_logins, panel_values, subscriptions
from . import delegate_router as delegate_api
from .model_stats_rows import current_catalog_model_ids
from .monitor_context import MonitorContext, get_ctx, resolve_context
from .monitor_context import production_context as production_context  # re-export: test monkeypatches
from .state_helpers import cache_get_or_refresh, cache_read_swr, ctx_scoped_ttl_key, swr_route

router = APIRouter(tags=["runtime"])




_KNOWN_OUTCOMES = ("ok", "error", "timeout", "rate_limited")
_RUNTIME_ATTRIBUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}$")
_RUNTIME_ATTRIBUTION_SOURCES = frozenset({"explicit", "session_env", "unknown"})
_RUNTIME_FAILURE_CODES = RUNTIME_FAILURE_CODES
_ACP_LEGACY_PARTICIPANTS = ("codex", "grok")
_ACP_ENABLED_PARTICIPANTS = frozenset(ACPX_SUPPORTED_PARTICIPANTS)
_ACP_STATES = frozenset({
    "CREATED",
    "INITIAL_FANOUT",
    "INITIAL_COMPLETE",
    "PARTIAL",
    "CROSS_EXCHANGE",
    "CROSS_EXCHANGE_COMPLETE",
    "SYNTHESIS",
    "COMPLETE",
    "PARTIAL_COMPLETE",
    "FAILED",
    "CANCELLED",
})
_ACP_EVENT_TYPES = frozenset({
    "CONVERSATION_CREATED",
    "CREATED",
    "STATE",
    "CALL_RESERVED",
    "CALL_TERMINAL",
    "SYNTHESIS_TERMINAL",
    "ORPHAN_RESERVATION",
    "INITIAL_FANOUT",
    "PARTICIPANT_MESSAGE",
    "PARTICIPANT_COMPLETE",
    "CROSS_EXCHANGE",
    "CROSS_EXCHANGE_MESSAGE",
    "CROSS_EXCHANGE_COMPLETE",
    "SYNTHESIS",
    "SYNTHESIS_COMPLETE",
    "DUPLICATE_SUPPRESSED",
    "BUDGET_EXHAUSTED",
    "DEADLINE_EXCEEDED",
    "FAILED",
    "CANCELLED",
})
_ACP_OUTCOMES = frozenset({
    "queued",
    "running",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
    "duplicate_suppressed",
    "budget_exhausted",
    "deadline_exceeded",
})
_ACP_MESSAGE_EVENTS = frozenset({
    "PARTICIPANT_MESSAGE",
    "PARTICIPANT_COMPLETE",
    "CROSS_EXCHANGE_MESSAGE",
    "CROSS_EXCHANGE_COMPLETE",
    "CALL_TERMINAL",
    "SYNTHESIS_TERMINAL",
})
_ACP_TRANSCRIPT_KINDS = frozenset({"request", "reply", "synthesis"})
_ACP_TRANSCRIPT_MAX_MESSAGES = 32
_ACP_TRANSCRIPT_MAX_BODY_BYTES = 256 * 1024
_ACP_TRANSCRIPT_MAX_RESPONSE_BYTES = 512 * 1024
_ACP_EVENT_TYPE_ALIASES = {
    "created": "CONVERSATION_CREATED",
    "conversation_created": "CONVERSATION_CREATED",
    "state": "STATE",
    "call_reserved": "CALL_RESERVED",
    "call_terminal": "CALL_TERMINAL",
    "synthesis_terminal": "SYNTHESIS_TERMINAL",
    "orphan_reservation": "ORPHAN_RESERVATION",
    "initial_fanout": "INITIAL_FANOUT",
    "fanout": "INITIAL_FANOUT",
    "participant_message": "PARTICIPANT_MESSAGE",
    "message": "PARTICIPANT_MESSAGE",
    "participant_complete": "PARTICIPANT_COMPLETE",
    "cross_exchange": "CROSS_EXCHANGE",
    "cross_exchange_message": "CROSS_EXCHANGE_MESSAGE",
    "cross_exchange_complete": "CROSS_EXCHANGE_COMPLETE",
    "synthesis": "SYNTHESIS",
    "synthesis_complete": "SYNTHESIS_COMPLETE",
    "duplicate_suppressed": "DUPLICATE_SUPPRESSED",
    "budget_exhausted": "BUDGET_EXHAUSTED",
    "deadline_exceeded": "DEADLINE_EXCEEDED",
    "budget_terminal": "BUDGET_EXHAUSTED",
    "deadline_terminal": "DEADLINE_EXCEEDED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
}


def _registry_path() -> Path | None:
    """Resolve the on-disk source of the live agent registry, fresh each call.

    Deliberately not a module-level constant: routing this through the
    imported ``lu_machinery.agent_runtime.registry`` module's own ``__file__`` keeps the
    on-disk source a single live indirection a test can repoint directly
    (``monkeypatch.setattr(agent_registry, "__file__", ...)``), instead of a
    second cached ``Path`` this router would need its own OPSEC fixture seam
    for.
    """
    source = getattr(agent_registry, "__file__", None)
    return Path(source).resolve() if source else None


def _registry_source_signature() -> tuple[int, int] | None:
    """Return a cheap change token for the runtime agent registry source."""
    registry_path = _registry_path()
    if registry_path is None:
        return None
    try:
        stat = registry_path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _registry_default_models(registry: Any) -> dict[str, str | None]:
    """Snapshot the registry defaults used by runtime observability."""
    entries = getattr(registry, "AGENTS", None)
    if not isinstance(entries, dict):
        raise ValueError("agent registry must define AGENTS as a dictionary")

    defaults: dict[str, str | None] = {}
    for name, entry in entries.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError("agent registry entries must be string-keyed dictionaries")
        model = entry.get("default_model")
        if model is not None and not isinstance(model, str):
            raise ValueError("agent registry default_model values must be strings or null")
        defaults[name] = model
    return defaults


def _load_registry_default_models() -> dict[str, str | None]:
    """Execute the registry source in isolation and return a validated snapshot."""
    registry_path = _registry_path()
    if registry_path is None:
        raise RuntimeError("agent registry source path is unavailable")
    spec = importlib.util.spec_from_file_location("_lu_runtime_registry_snapshot", registry_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load agent registry from {registry_path}")
    registry_snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry_snapshot)
    return _registry_default_models(registry_snapshot)


_registry_refresh_lock = Lock()
_registry_signature = _registry_source_signature()
_registry_models = _registry_default_models(agent_registry)


def _refresh_agent_registry_if_changed() -> None:
    """Refresh registry-backed defaults after an on-disk registry update.

    The live ``lu_machinery.agent_runtime.registry`` module is never reloaded in place: a
    bad or incomplete write cannot corrupt concurrent runtime consumers. Its
    last known-good snapshot stays live and the next request retries the load.
    """
    global _registry_models, _registry_signature

    signature = _registry_source_signature()
    if signature is None or signature == _registry_signature:
        return

    with _registry_refresh_lock:
        signature = _registry_source_signature()
        if signature is None or signature == _registry_signature:
            return
        try:
            defaults = _load_registry_default_models()
        except Exception:
            return
        _registry_models = defaults
        _registry_signature = signature


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _usage_day_from_name(path: Path) -> date | None:
    stem = path.stem
    if not stem.startswith("usage_"):
        return None
    try:
        day_str = stem.rsplit("_", 1)[1]
        return date.fromisoformat(day_str)
    except (IndexError, ValueError):
        return None


def _usage_dir(ctx: MonitorContext | None = None) -> Path:
    return resolve_context(ctx).roots.batch_state_dir / "api_usage"


def _usage_files(
    *, days: int, usage_dir: Path | None = None, ctx: MonitorContext | None = None
) -> list[Path]:
    root = usage_dir if usage_dir is not None else _usage_dir(ctx)
    if not root.exists():
        return []
    today = datetime.now(UTC).date()
    earliest = today - timedelta(days=max(1, days) - 1)
    files: list[Path] = []
    # Hyphens are valid in both agent and entrypoint names, so the filename's
    # ``<agent>-<entrypoint>`` segment is intentionally not parsed here.
    # Callers apply exact filters to the JSONL record fields instead.
    for path in sorted(root.glob("usage_*.jsonl")):
        day = _usage_day_from_name(path)
        if day is None or day < earliest or day > today:
            continue
        files.append(path)
    return files


def _today_usage_files(ctx: MonitorContext | None = None) -> list[Path]:
    return _usage_files(days=1, ctx=ctx)


def _unreadable_summary(unreadable: dict[str, int]) -> dict[str, int]:
    """The #9868 unreadable-evidence shape: ``files``/``lines``/``records`` plus their total."""
    return {**unreadable, "total": sum(unreadable.values())}


class UsageRecords(list[dict[str, Any]]):
    """Usage JSONL records plus the strict reader's fault counts for that read (#9924).

    ``unreadable`` carries the #9868 summary shape so every caller can surface
    the counts in its response, including callers whose
    ``_iter_usage_records(paths)`` call shape stays frozen at one argument.
    """

    unreadable: dict[str, int]

    def __init__(self, records: list[dict[str, Any]], unreadable: dict[str, int]) -> None:
        super().__init__(records)
        self.unreadable = _unreadable_summary(unreadable)


def _iter_usage_records(paths: list[Path], unreadable: dict[str, int] | None = None) -> UsageRecords:
    """Parse usage JSONL records through the shared strict line reader.

    Every usage-file reader shares ``lu_machinery.agent_runtime.usage._iter_usage_records``
    (#9924): a line that is not strict UTF-8, not JSON, or not a JSON object is
    skipped and counted on ``unreadable`` — the ``{"files", "lines", "records"}``
    counter the usage summaries expose — never raised, never silently dropped.
    Valid rows from the same file and from later files still count. The returned
    list always carries the read's counts on its ``unreadable`` attribute in the
    #9868 summary shape, even when the caller passes no counter, so production
    callers surface them in API responses instead of discarding them.
    """
    sink: dict[str, int] = unreadable if unreadable is not None else {"files": 0, "lines": 0, "records": 0}
    records: list[dict[str, Any]] = []
    for path in paths:
        records.extend(_iter_usage_file_records(path, sink))
    return UsageRecords(records, sink)


def _new_outcome_bucket() -> dict[str, Any]:
    bucket: dict[str, Any] = {"total": 0, "total_duration_s": 0.0}
    for key in _KNOWN_OUTCOMES:
        bucket[key] = 0
    return bucket


def _update_outcome_bucket(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    bucket["total"] += 1
    outcome = str(record.get("outcome") or "")
    if outcome in _KNOWN_OUTCOMES:
        bucket[outcome] += 1
    duration = record.get("duration_s")
    if isinstance(duration, (int, float)):
        bucket["total_duration_s"] = round(bucket["total_duration_s"] + float(duration), 3)


def _last_used_agent_models(
    *, days: int = 7, ctx: MonitorContext | None = None, unreadable: dict[str, int] | None = None
) -> dict[str, str]:
    """Extract the most recently used model per agent from runtime usage records.

    ``unreadable`` receives the strict reader's fault counts (#9924) the same
    way ``lu_machinery.agent_runtime.usage.has_headroom`` exposes them; omitted means the
    counts are computed but not surfaced to the caller.
    """
    latest_by_agent: dict[str, tuple[datetime, str]] = {}
    for record in _iter_usage_records(_usage_files(days=days, ctx=ctx), unreadable):
        agent = record.get("agent")
        model = record.get("model")
        if not isinstance(agent, str) or not isinstance(model, str):
            continue
        model_str = model.strip()
        if not model_str:
            continue
        ts = _parse_iso_datetime(record.get("ts")) or datetime.min.replace(tzinfo=UTC)
        current = latest_by_agent.get(agent)
        if current is None or ts >= current[0]:
            latest_by_agent[agent] = (ts, model_str)
    return {agent: model for agent, (_, model) in latest_by_agent.items()}


def _adapters_dir(ctx: MonitorContext) -> Path:
    """Resolve the installed adapters package from the explicit context root.

    A fixture context (``ctx.root is not None``) must never enumerate — and so
    never echo — the real installed adapter modules into a scanned response; it
    gets a definitely empty directory under its own disposable root instead.
    """
    if ctx.root is not None:
        return ctx.root / "runtime_adapters_fixture_stub"
    return machinery_path("agent_runtime/adapters")


def list_runtime_agents(ctx: MonitorContext, unreadable: dict[str, int] | None = None) -> list[dict[str, Any]]:
    _refresh_agent_registry_if_changed()
    last_used = _last_used_agent_models(ctx=ctx, unreadable=unreadable)
    adapters_dir = _adapters_dir(ctx)
    agents: list[dict[str, Any]] = []
    for path in sorted(adapters_dir.glob("*.py")):
        # Skip non-agent helper modules.
        if path.stem in {"__init__", "acpx", "base"} or path.stem.startswith("_"):
            continue
        try:
            module = import_named_module(f"lu_machinery.agent_runtime.adapters.{path.stem}")
        except Exception:
            continue

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            if not hasattr(obj, "name") or not all(hasattr(obj, attr) for attr in ("default_model", "supported_modes")):
                continue
            build_invocation = getattr(obj, "build_invocation", None)
            if not callable(build_invocation):
                continue
            if not callable(getattr(obj, "parse_response", None)):
                continue
            try:
                source = inspect.getsource(build_invocation)
            except (OSError, TypeError):
                source = ""
            if 'shutil.which("claude")' in source:
                binary = "claude"
            elif "@anthropic-ai/claude-code@latest" in source:
                binary = "npx @anthropic-ai/claude-code@latest"
            elif 'shutil.which("gemini")' in source:
                binary = "gemini"
            elif 'shutil.which("codex")' in source:
                binary = "codex"
            else:
                binary = str(obj.name)

            agent_name = str(obj.name)
            # Retired CLI seats (gemini→agy, glm→cursor) stay off the live roster.
            # Usage history may still mention them; inventory must not.
            if agent_name in RETIRED_AGENT_ALIASES:
                break
            registry_default = _registry_models.get(agent_name, getattr(obj, "default_model", None))
            last_model = last_used.get(agent_name)
            headroom_model = last_model or registry_default
            agents.append({
                "name": agent_name,
                "binary": binary,
                "default_model": registry_default,
                "last_used_model": last_model,
                "headroom_model": headroom_model,
                "supported_modes": sorted(str(mode) for mode in getattr(obj, "supported_modes", [])),
            })
            break
    return agents


def summarize_runtime_usage(
    *,
    days: int = 7,
    agent: str | None = None,
    entrypoint: str | None = None,
    usage_dir: Path | None = None,
    ctx: MonitorContext | None = None,
    unreadable: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Aggregate usage records over the window.

    ``unreadable`` reports the strict reader's fault counts in the #9868 shape:
    lines that were not strict UTF-8, not JSON, or not JSON objects never count
    toward ``records_total`` and are never silently dropped (#9924). A caller
    that passes its own counter (the orient collector aggregating several
    reads) receives this read's faults on that counter; the response field
    then reflects the shared counter, not this read alone.
    """
    window_days = min(max(1, int(days)), 30)
    by_agent: dict[str, dict[str, Any]] = defaultdict(_new_outcome_bucket)
    by_entrypoint: dict[str, dict[str, Any]] = defaultdict(_new_outcome_bucket)
    total = 0

    records = _iter_usage_records(_usage_files(days=window_days, usage_dir=usage_dir, ctx=ctx), unreadable)
    for record in records:
        record_agent = record.get("agent")
        record_entrypoint = record.get("entrypoint")
        if agent and record_agent != agent:
            continue
        if entrypoint and record_entrypoint != entrypoint:
            continue
        total += 1
        if record_agent:
            _update_outcome_bucket(by_agent[str(record_agent)], record)
        if record_entrypoint:
            _update_outcome_bucket(by_entrypoint[str(record_entrypoint)], record)

    return {
        "window_days": window_days,
        "records_total": total,
        "by_agent": dict(by_agent),
        "by_entrypoint": dict(by_entrypoint),
        "unreadable": records.unreadable,
    }


def recent_runtime_records(*, limit: int = 50, ctx: MonitorContext | None = None) -> dict[str, Any]:
    record_limit = min(max(1, int(limit)), 500)
    summaries: list[dict[str, Any]] = []
    records = _iter_usage_records(_today_usage_files(ctx))
    for record in records:
        ts = _parse_iso_datetime(record.get("ts"))
        initiator = record.get("initiator")
        if not isinstance(initiator, str) or not _RUNTIME_ATTRIBUTION_ID.fullmatch(initiator):
            initiator = "unknown"
        source_provenance = record.get("attribution_source")
        if source_provenance not in _RUNTIME_ATTRIBUTION_SOURCES:
            source_provenance = "unknown"
        source_task_id = record.get("attribution_task_id")
        if not isinstance(source_task_id, str) or not _RUNTIME_ATTRIBUTION_ID.fullmatch(source_task_id):
            source_task_id = None
        failure_code = record.get("failure_code")
        if failure_code not in _RUNTIME_FAILURE_CODES:
            outcome = record.get("outcome")
            if outcome in {"timeout", "hard_timeout", "stalled"}:
                failure_code = "timeout"
            elif outcome == "rate_limited":
                failure_code = "rate_limited"
            else:
                failure_code = "unknown" if outcome not in {"ok", None} else None
        summaries.append({
            "ts": _isoformat_z(ts) if ts else record.get("ts"),
            "agent": record.get("agent"),
            "entrypoint": record.get("entrypoint"),
            "via": record.get("entrypoint"),
            "source": initiator,
            "source_provenance": source_provenance,
            "source_task_id": source_task_id,
            "model": record.get("model"),
            **recorded_model_accounting_fields(record),
            "outcome": record.get("outcome"),
            "failure_code": failure_code,
            "duration_s": record.get("duration_s"),
        })
    summaries.sort(key=lambda item: _parse_iso_datetime(item.get("ts")) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return {"records": summaries[:record_limit], "unreadable": records.unreadable}


ACP_CONVERSATIONS_TTL_S = 15.0


# ---------------------------------------------------------------------
# Routing page (/api/runtime/routing)
# ---------------------------------------------------------------------
#
# Work is routed in four places: the brief names the worker's model, the dispatch
# picks the account, dispatch admission admits or defers a write, and the reviewer
# resolver seats each review. This view reads each stage's own evidence where it
# already lives and keeps no store: task records (with the admission snapshot each
# dispatch records), the routing budget admission reads, admission's own report-only
# evaluation per pool, and the resolver's own evaluation and pick.

# Both snapshots (recent decisions; pools now) are served stale-while-revalidate; the
# page's freshness bound allows two refreshes.
ROUTING_TTL_S = 30.0
ROUTING_STALE_AFTER_S = 90.0
ROUTING_WINDOW_S = 24 * 3600.0
# A start later than this past the snapshot time is a bad clock or a bad record, not a decision.
ROUTING_FUTURE_SKEW_S = 300.0
# Evaluating admission per pool and the resolver takes several seconds; a read waits at most this
# long for it and otherwise answers "pending" (the page retries) or the last evaluation with its age.
ROUTING_NOW_WAIT_S = 1.0
# The review the "excluded" column and the resolver's pick describe.
ROUTING_REVIEW_PROFILE = "code"
ROUTING_REVIEW_RISK = "medium"
# Admission meters in page order, with the subscription account each one bills.
ROUTING_POOLS: tuple[tuple[str, str], ...] = (
    ("claude:a", "Claude a"),
    ("claude:b", "Claude b"),
    ("codex:a", "Codex a"),
    ("codex:b", "Codex b"),
    ("agy:gemini", "AGY Gemini"),
    ("agy:claude_gpt", "AGY Claude/GPT"),
    ("grok+cursor", "Grok+Cursor"),
)
_ROUTING_POOL_LABELS = dict(ROUTING_POOLS)
# Author families the resolver's pick is shown for, with the page's name for each.
_ROUTING_AUTHOR_FAMILIES = (("anthropic", "Claude"), ("openai", "GPT"), ("google", "Gemini"), ("xai", "Grok"))
_ROUTING_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+\[\]-]{0,99}$")
_ROUTING_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ROUTING_WORD = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_ROUTING_RISKS = frozenset({"low", "medium", "high", "critical"})
_ROUTING_ACTIVE = frozenset({"running", "spawning"})
# Fixed public text for each typed start refusal; nothing a record carries as free text is shown.
_ROUTING_START_REFUSALS = {
    StartRefusal.CAPACITY.value: ("deferred", "admission deferred it"),
    StartRefusal.HEADROOM.value: ("refused", "refused: the pool had no allowance headroom"),
    StartRefusal.ACCOUNT.value: ("refused", "refused: the account check failed"),
    StartRefusal.ELIGIBILITY.value: ("refused", "refused: the route is not eligible"),
    StartRefusal.MODEL_UNAVAILABLE.value: ("refused", "refused: the model is unavailable"),
    StartRefusal.PLACEMENT.value: ("refused", "refused: no worker placement"),
    StartRefusal.BOUNDARY.value: ("refused", "refused by the write boundary"),
    StartRefusal.SETUP.value: ("refused", "refused during setup"),
    StartRefusal.REVIEW_SETUP.value: ("refused", "refused during review setup (round budget or target)"),
}
_ROUTING_METER_DEFERS = {
    "exhausted": "the pool's allowance is used up",
    "near_cap": "the pool's allowance is nearly used up",
    "rate_limited": "the pool was rate limited in the last few minutes",
    "cap": "the pool's write-worker cap was reached",
    "cadence": "another write had just started on this pool (launches are spaced out)",
}
_ROUTING_CLAUDE_ACCOUNT_SOURCES = {"explicit": "helper", "default-file": "saved_default", "builtin": "builtin_default"}


class _RoutingMalformed(ValueError):
    """A task record in the window whose identity or start time cannot be read."""


def _routing_iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _routing_text(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _routing_number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _routing_started(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _routing_meter(agent: object, model: object, claude_account: object, codex_account: object) -> str | None:
    """Admission's meter for these fields, given only string inputs; anything else has no pool."""
    texts = [value if isinstance(value, str) else None for value in (agent, model, claude_account, codex_account)]
    meter = dispatch_admission.subscription_meter(
        texts[0], model=texts[1], claude_account=texts[2], codex_account=texts[3],
    )
    return meter if meter in _ROUTING_POOL_LABELS else None


def _routing_admission_check(record: Mapping[str, Any]) -> dict[str, list[str]]:
    """Plain reasons for an admission snapshot (``AdmissionDecision.to_record`` shape), from its typed fields.

    ``failing`` lists the checks that refused; ``unmeasured`` the checks with no
    reading. Admission's free-text ``failures`` are never copied: a failure no typed
    field explains is reported as "another admission check".
    """
    failing: list[str] = []
    unmeasured: list[str] = []

    def above(value: object, limit: object) -> bool | None:
        number, bound = _routing_number(value), _routing_number(limit)
        return None if number is None or bound is None else number > bound

    defer = record.get("meter_defer")
    meter = record.get("meter")
    if isinstance(defer, str) and defer in _ROUTING_METER_DEFERS:
        pool = _ROUTING_POOL_LABELS.get(meter) if isinstance(meter, str) else None
        failing.append(f"{pool + ': ' if pool else ''}{_ROUTING_METER_DEFERS[defer]}")
    live, cap = _routing_number(record.get("live_write_workers")), _routing_number(record.get("max_live_write_workers"))
    if live is not None and cap is not None and live >= cap:
        failing.append("the host's write-worker cap was reached")
    if record.get("orchestrator_cap_refused") is True:
        failing.append("the host's orchestrator cap was reached")
    memory = above(record.get("min_mem_available_gib"), record.get("mem_available_gib"))
    if memory is None:
        unmeasured.append("memory")
    elif memory:
        failing.append("host memory is below the floor")
    load = above(record.get("load_per_cpu"), record.get("max_load_per_cpu"))
    if load is None:
        unmeasured.append("CPU load")
    elif load:
        failing.append("host CPU load is above the limit")
    if record.get("pool_check_skipped") is not None:
        unmeasured.append("shared worker memory pool")
    else:
        used = [_routing_number(record.get(key)) for key in ("pool_nonreclaimable_gib", "worker_mem_reserve_gib")]
        limit = _routing_number(record.get("pool_limit_gib"))
        if None in used or limit is None:
            unmeasured.append("shared worker memory pool")
        elif sum(used) > limit:  # type: ignore[arg-type]
            failing.append("the shared worker memory pool is full")
    queue = above(record.get("test_slot_queue_depth"), record.get("max_test_slot_queue"))
    if record.get("test_slot_queue_skipped") is not None or queue is None:
        unmeasured.append("test-slot queue")
    elif queue:
        failing.append("too many runs are waiting for test slots")
    if record.get("disk_refused") is True:
        failing.append("the disk is too full")
    elif record.get("disk_check_skipped") is not None or _routing_number(record.get("disk_used_percent")) is None:
        unmeasured.append("disk")
    if record.get("admitted") is False and not failing:
        failing.append("another admission check")
    return {"failing": failing, "unmeasured": unmeasured}


def _routing_account_by(record: Mapping[str, Any]) -> str | None:
    """Who chose the account: the dispatch's explicit account (the account helper), a default, or the only account."""
    agent = record.get("agent")
    if agent == "claude":
        source = record.get("claude_account_source")
        return _ROUTING_CLAUDE_ACCOUNT_SOURCES.get(source) if isinstance(source, str) else None
    if agent == "codex":
        # Dispatch resolves an omitted account to the default before it records its options, so
        # the default label cannot tell an explicit choice from no choice: that is "not recorded".
        try:
            label = resolve_codex_account_label(record.get("codex_account"))
        except CodexAccountRefused:
            return None
        return "not_recorded" if label == DEFAULT_CODEX_ACCOUNT else "helper"
    # AGY and Grok+Cursor each have one subscription: nothing chooses an account there.
    pool = _routing_meter(agent, record.get("model"), None, None)
    return "single_account" if pool is not None else None


def _routing_same_model(left: str, right: str) -> bool:
    return (canonical_model_id(left) or left.casefold()) == (canonical_model_id(right) or right.casefold())


def _routing_substitution(record: Mapping[str, Any], model: str | None) -> str | None:
    """Plain text when the record shows the run used another model than the one asked for."""
    substitution = record.get("substitution")
    if isinstance(substitution, Mapping) and substitution.get("substituted") is True:
        actual = _routing_text(substitution.get("actual_model"), _ROUTING_MODEL_ID)
        requested = _routing_text(substitution.get("requested_model"), _ROUTING_MODEL_ID) or model
        if actual and requested and not _routing_same_model(requested, actual):
            return f"asked for {requested}; the runtime ran {actual}"
    observed = _routing_text(recorded_model_accounting_fields(record).get("observed_model"), _ROUTING_MODEL_ID)
    if observed and model and not _routing_same_model(model, observed):
        return f"asked for {model}; the provider reported {observed}"
    return None


def _routing_admission(record: Mapping[str, Any], *, write: bool) -> dict[str, Any]:
    """The admission outcome a record proves, in fixed words; missing evidence is unknown."""
    snapshot = record.get("admission")
    check = _routing_admission_check(snapshot) if isinstance(snapshot, Mapping) else None
    never = record.get("never_started_reason")
    if never is not None:
        state, reason = _ROUTING_START_REFUSALS.get(never, ("refused", "refused before start")) if isinstance(
            never, str) else ("refused", "refused before start")
        if check and check["failing"] and state == "deferred":
            reason = f"{reason}: {'; '.join(check['failing'])}"
        return {"state": state, "reason": reason}
    if not write:
        return {"state": "not_checked", "reason": "read-only work is not admission-checked"}
    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("admitted"), bool):
        return {"state": "unknown", "reason": "no admission record"}
    if snapshot["admitted"]:
        return {"state": "admitted", "reason": None}
    if snapshot.get("forced") is True:
        failing = "; ".join(check["failing"]) if check else ""
        return {"state": "forced", "reason": f"started by an admission override{': ' + failing if failing else ''}"}
    return {"state": "unknown", "reason": "started although its admission record did not admit it"}


def _routing_decision(record: Mapping[str, Any], *, now: datetime) -> dict[str, Any] | None:
    """One allowlisted row, or None outside the window; raises ``_RoutingMalformed`` for an unreadable record.

    Rows carry ids, labels, model names and fixed words only, never paths, prompts or free text.
    """
    # A refused start is recorded under its own id; the id the dispatch asked for is the one people know.
    task_id = _routing_text(record.get("requested_task_id"), _ROUTING_TASK_ID) or _routing_text(
        record.get("task_id"), _ROUTING_TASK_ID,
    )
    started = _routing_started(record.get("started_at"))
    if task_id is None or started is None:
        raise _RoutingMalformed("task id or start time unreadable")
    age_s = (now - started).total_seconds()
    if age_s > ROUTING_WINDOW_S:
        return None
    if age_s < -ROUTING_FUTURE_SKEW_S:
        raise _RoutingMalformed("start time in the future")
    mode = record.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise _RoutingMalformed("mode unreadable")  # the kind of work, and so its admission, is unknown
    review = review_typed(dict(record))
    write = mode in dispatch_admission.WRITE_CAPABLE_MODES
    model = _routing_text(record.get("model"), _ROUTING_MODEL_ID)
    snapshot = record.get("admission")
    recorded_meter = snapshot.get("meter") if isinstance(snapshot, Mapping) else None
    # Admission names the meter it judged; the record's own fields are the fallback.
    pool = recorded_meter if isinstance(recorded_meter, str) and recorded_meter in _ROUTING_POOL_LABELS else (
        _routing_meter(record.get("agent"), record.get("model"), record.get("claude_account"),
                       record.get("codex_account"))
    )
    status, alive = delegate_api._derived_task_status(dict(record))
    risk = record.get("review_risk")
    return {
        "task_id": task_id,
        "started_at": _routing_iso_z(started),
        "kind": "review" if review else "write" if write else "read_only",
        "role": _routing_text(record.get("research_role"), _ROUTING_WORD),
        "profile": _routing_text(record.get("review_profile"), _ROUTING_WORD) if review else None,
        "risk": risk if review and isinstance(risk, str) and risk in _ROUTING_RISKS else None,
        "author_model": _routing_text(record.get("review_author_model"), _ROUTING_MODEL_ID) if review else None,
        "model": model,
        "effort": _routing_text(record.get("effort"), _ROUTING_WORD),
        "pool": pool,
        "pool_label": _ROUTING_POOL_LABELS.get(pool or ""),
        "model_by": "resolver" if review else "brief",
        "account_by": _routing_account_by(record),
        "admission": _routing_admission(record, write=write),
        "status": status if _ROUTING_WORD.fullmatch(status) else "unknown",
        "live": status in _ROUTING_ACTIVE and (alive or status == "spawning"),
        "substitution": _routing_substitution(record, model),
        "parent_task_id": _routing_text(record.get("parent_task_id"), _ROUTING_TASK_ID),
    }


def _routing_lane_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    admission = Counter(row["admission"]["state"] for row in rows)
    return {
        "total": len(rows),
        "by_model": dict(Counter(row["model"] or "unknown" for row in rows)),
        "by_pool": dict(Counter(row["pool_label"] or "no subscription pool" for row in rows)),
        "by_account_chooser": dict(Counter(row["account_by"] or "not_recorded" for row in rows if row["pool"])),
        "admission": {
            state: admission.get(state, 0)
            for state in ("admitted", "forced", "deferred", "refused", "not_checked", "unknown")
        },
        "substitutions": sum(1 for row in rows if row["substitution"]),
        "live": sum(1 for row in rows if row["live"]),
    }


def routing_decisions(ctx: MonitorContext | None = None) -> dict[str, Any]:
    """Every dispatch and review decision started in the last day, from the hot task records.

    Records last written before the window are skipped by mtime before they are
    read: a record is written when its task starts, so its mtime is never older
    than its start. A store that cannot be listed is ``unknown``; records that
    cannot be read or parsed are counted, and make the result ``partial``.
    """
    now = datetime.now(UTC)
    tasks_dir = delegate_api._tasks_dir(resolve_context(ctx))
    try:
        with os.scandir(tasks_dir):
            pass
    except FileNotFoundError:
        return {"status": "unknown", "reason": "task_records_missing", "as_of": _routing_iso_z(now)}
    except OSError:
        return {"status": "unknown", "reason": "task_records_unreadable", "as_of": _routing_iso_z(now)}
    cutoff = now.timestamp() - ROUTING_WINDOW_S
    rows: list[dict[str, Any]] = []
    unreadable = 0
    for path in iter_task_records(tasks_dir, include_archive=False):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            record = read_cached_task_record(path, tasks_dir=tasks_dir, display_unknown_schema=True)
            if record is None:
                continue  # a foreign file in the store, not a task record
            row = _routing_decision(record, now=now)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            unreadable += 1
            continue
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: row["started_at"], reverse=True)
    reviews = [row for row in rows if row["kind"] == "review"]
    return {
        "status": "partial" if unreadable else "ok",
        "reason": "task_records_unreadable" if unreadable else None,
        "as_of": _routing_iso_z(now),
        "window_s": ROUTING_WINDOW_S,
        "total": len(rows),
        "unreadable": unreadable,
        "counts": {
            "brief": _routing_lane_counts([row for row in rows if row["kind"] != "review"]),
            "resolver": _routing_lane_counts(reviews),
        },
        "decisions": rows,
    }


def _routing_candidate_pool(candidate: Any, claude_label: str | None) -> str | None:
    if candidate.route == "codex":
        return codex_account_key(candidate)
    if candidate.route == "claude":
        return f"claude:{claude_label}" if claude_label else None
    return _routing_meter(candidate.route, candidate.concrete_model, None, None)


def _routing_resolver_pick(budget: Mapping[str, Any], family: str, account: Any) -> dict[str, Any]:
    """The seat ``resolve_reviewer`` itself picks now for a review authored in ``family``."""
    resolution = resolve_reviewer(ResolverInputs(
        author_model=family, author_family=family, review_profile=ROUTING_REVIEW_PROFILE,
        risk=ROUTING_REVIEW_RISK, data_egress_policy="local_interactive",
        routing_snapshot=budget, claude_account=account,
    ))
    seats = [resolution.selected] if resolution.selected is not None else list(resolution.quorum)
    picks = []
    for seat in seats:
        candidate = REVIEW_CANDIDATES.get(seat.name)
        meter = _routing_candidate_pool(candidate, account.label) if candidate is not None else None
        picks.append({"name": seat.name, "model": seat.concrete_model, "pool": meter,
                      "pool_label": _ROUTING_POOL_LABELS.get(meter or "")})
    return {"picks": picks, "reason": None if picks else resolution.fail_closed_reason or "no eligible reviewer"}


def _routing_review_seats(budget: Mapping[str, Any]) -> dict[str, Any]:
    """The resolver's own evaluation of each automatic seat on the medium code ladder, and its pick.

    A seat is eligible when the resolver would admit it for at least one author
    family; otherwise it is excluded with the resolver's reason for another family
    (same-family exclusion is the cross-family rule, not a pool state). Among
    eligible seats the resolver ranks by fit, tier and allowance, so the pick per
    author family comes from ``resolve_reviewer`` itself, never from ladder order.
    """
    eligible, excluded = bench_health._bench_inventory(
        budget, data_egress_policy="local_interactive",
        review_profile=ROUTING_REVIEW_PROFILE, risk=ROUTING_REVIEW_RISK,
    )
    account = claude_account_lane.resolve_route_account(None, strict=False)
    first_choice = []
    picked_for: dict[str, list[str]] = {}
    for family, name in _ROUTING_AUTHOR_FAMILIES:
        pick = _routing_resolver_pick(budget, family, account)
        first_choice.append({"author": name, "seats": [
            {key: picked[key] for key in ("model", "pool", "pool_label")} for picked in pick["picks"]
        ], "reason": pick["reason"]})
        for picked in pick["picks"]:
            picked_for.setdefault(picked["name"], []).append(name)
    seats: dict[str, list[dict[str, Any]]] = {meter: [] for meter, _ in ROUTING_POOLS}
    unpooled = []
    for candidate in (candidate for rung in REVIEW_LADDERS[ROUTING_REVIEW_RISK] for candidate in rung):
        families = [family for family in eligible if family != candidate.family]
        seat: dict[str, Any] = {"model": candidate.concrete_model, "picked_for": picked_for.get(candidate.name, [])}
        if any(candidate.name in eligible[family] for family in families):
            seat.update(state="eligible", reason=None)
        else:
            reason = next((excluded[family].get(candidate.name) for family in families
                           if excluded[family].get(candidate.name)), None)
            seat.update(state="excluded", reason=reason or "not eligible")
        meter = _routing_candidate_pool(candidate, account.label)
        if meter in seats:
            seats[meter].append(seat)
        else:
            unpooled.append(seat)
    return {"by_pool": seats, "unpooled": unpooled, "first_choice": first_choice}


def _routing_admission_now(tasks_dir: Path, budget: Mapping[str, Any]) -> dict[str, Any]:
    """Admission's own report-only evaluation of a new write on each pool, and the host checks.

    ``dispatch_admission.evaluate`` runs exactly as dispatch runs it, except that it
    sweeps no dead record, writes no pressure journal, and leaves the disk to one
    ``check_disk`` here, which measures and never alerts.
    """
    limits = dispatch_admission.load_thresholds()
    disk, _ = dispatch_admission.check_disk(limits)
    pools: dict[str, Any] = {}
    host: dict[str, Any] | None = None
    for meter, _label in ROUTING_POOLS:
        decision = dispatch_admission.evaluate(
            "workspace-write", tasks_dir, pid_alive=dispatch_admission.process_alive,
            record_liveness=lambda record: worker_liveness(record, pid_alive=dispatch_admission.process_alive),
            on_dead=None, thresholds=limits, disk_guard=False, record_shadow=False,
            meter=meter, meter_budget=budget,
        )
        snapshot = decision.to_record()
        snapshot.update(
            disk_refused=disk.refused, disk_check_skipped=disk.probe.skipped,
            disk_used_percent=disk.probe.used_percent,
        )
        check = _routing_admission_check(snapshot)
        host_failing = [reason for reason in check["failing"] if not reason.startswith(f"{_ROUTING_POOL_LABELS[meter]}:")]
        if host is None:
            host = {"admitting": not host_failing, "failing": host_failing, "unmeasured": check["unmeasured"],
                    "live_writes": len(decision.live_task_ids), "write_cap": limits.max_live_write_workers}
        telemetry = decision.meter_telemetry
        note = None if telemetry == "fresh" else (
            f"admission cannot see this pool's allowance ({telemetry.replace('_', ' ').replace(':', ': ')}); "
            f"its cap is lowered to {decision.meter_effective_cap}"
        )
        admitted = decision.admitted and not disk.refused
        pools[meter] = {
            "state": "admitted" if admitted else "deferred",
            "reason": None if admitted else "; ".join(check["failing"]) or "another admission check",
            "note": note,
            "live_writes": decision.meter.live if decision.meter is not None else None,
            "cap": decision.meter_effective_cap,
        }
    return {"host": host, "pools": pools}


def routing_now(ctx: MonitorContext, budget: Mapping[str, Any], budget_generated_at: str | None) -> dict[str, Any]:
    """Pools now, evaluated against one usable routing budget: admission, resolver and live workers.

    A part that cannot be evaluated is ``unknown`` with its reason; it never falls back to a guess.
    """
    resolved = resolve_context(ctx)
    tasks_dir = delegate_api._tasks_dir(resolved)
    now: dict[str, Any] = {"budget_generated_at": budget_generated_at, "evaluated_at": _routing_iso_z(datetime.now(UTC))}
    try:
        now["admission"] = {"state": "ok", **_routing_admission_now(tasks_dir, budget)}
    except (OSError, ValueError):
        # Unreadable admission settings or product policy: admission itself would refuse, so the verdict is unknown.
        now["admission"] = {"state": "unknown", "reason": "admission settings or policy unreadable"}
    try:
        now["resolver"] = {"state": "ok", **_routing_review_seats(budget)}
    except ValueError:
        # The resolver refuses a malformed health reading; its seats are then unknown, never eligible.
        now["resolver"] = {"state": "unknown", "reason": "the resolver could not read the routing budget's health fields"}
    now["live"] = routing_live(resolved)
    return now


def routing_live(ctx: MonitorContext) -> dict[str, Any]:
    """Live workers per pool, from the same active-record read the Fleet page uses."""
    try:
        active = delegate_api._active_snapshot_tasks(ctx)["tasks"]
    except OSError:
        return {"state": "unknown", "reason": "active task records unreadable"}
    counts = Counter(
        _routing_meter(row.get("agent"), row.get("model"), row.get("claude_account"), row.get("codex_account"))
        for row in active
    )
    return {"state": "ok", "by_pool": {meter: counts.get(meter, 0) for meter, _ in ROUTING_POOLS}}


def _routing_allowance(account: Mapping[str, Any] | None) -> dict[str, Any]:
    """One pool's weekly allowance; only a fresh reading carries a figure."""
    if account is None:
        return {"state": "unknown", "reason": "no subscription reading for this pool", "read_at": None}
    raw_reading, raw_weekly = account.get("reading"), account.get("weekly")
    reading: Mapping[str, Any] = raw_reading if isinstance(raw_reading, Mapping) else {}
    weekly: Mapping[str, Any] = raw_weekly if isinstance(raw_weekly, Mapping) else {}
    left = panel_values.number(weekly.get("left_pct"))
    state = reading.get("status") if reading.get("status") in {"fresh", "stale", "unknown"} else "unknown"
    if weekly.get("status") != "ok" or left is None:
        state = "unknown"
    return {
        "state": state,
        "reason": reading.get("reason_code") if state != "fresh" else None,
        "read_at": reading.get("read_at"),
        "left_pct": left if state == "fresh" else None,
        "resets_at": weekly.get("resets_at") if state == "fresh" else None,
    }


def routing_allowances() -> dict[str, Any]:
    subs = _public_subscriptions(subscriptions.subscriptions_snapshot())
    accounts = {row["account"]: row for provider in subs["providers"] for row in provider["accounts"]}
    return {meter: _routing_allowance(accounts.get(label)) for meter, label in ROUTING_POOLS}


async def _routing_budget(ctx: MonitorContext) -> tuple[dict[str, Any] | None, str | None]:
    """The routing budget exactly as dispatch admission reads it (``GET /api/state/routing-budget``).

    Anything but a budget with its ``generated_at`` and an ``agents`` mapping is unusable.
    """
    from . import state_router  # noqa: PLC0415  # lazy-ok: import cycle (state_router imports this module)

    try:
        response = await state_router.routing_budget(
            fresh_codexbar=False, transport="dispatch", ctx=ctx, include_subscriptions=False,
        )
    except HTTPException as exc:
        return None, "warming" if exc.status_code == 503 else f"http_{exc.status_code}"
    try:
        body = json_depth.loads(bytes(response.body), max_depth=512)
    except (ValueError, RecursionError):
        return None, "invalid_budget"
    usable = (
        isinstance(body, dict) and isinstance(body.get("agents"), Mapping)
        and _routing_started(body.get("generated_at")) is not None
    )
    return (body, None) if usable else (None, "invalid_budget")


def runtime_recent_outcomes_today(ctx: MonitorContext | None = None, *, unreadable: dict[str, int] | None = None) -> dict[str, int]:
    """Today's per-outcome counts.

    ``unreadable`` receives the strict reader's fault counts (#9924) the same
    way ``lu_machinery.agent_runtime.usage.has_headroom`` exposes them; omitted means the
    counts are computed but not surfaced to the caller.
    """
    counts = {key: 0 for key in _KNOWN_OUTCOMES}
    for record in _iter_usage_records(_today_usage_files(ctx), unreadable):
        outcome = str(record.get("outcome") or "")
        if outcome in counts:
            counts[outcome] += 1
    return counts


def _nested(data: dict[str, Any] | None, *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _rounded(value: Any, digits: int = 2) -> float | None:
    number = _number(value)
    return round(float(number), digits) if number is not None else None


# ---------------------------------------------------------------------
# ACP conversation timeline (#6078)
# ---------------------------------------------------------------------


def _acp_db_path(ctx: MonitorContext) -> Path:
    """Return the configured fleet-comms database without creating it."""
    return message_plane.default_plane_root(repo_root=ctx.roots.state_root) / "comms.sqlite3"


def _open_acp_db_readonly(ctx: MonitorContext) -> SQLiteConnection | None:
    """Open fleet-comms storage read-only, returning ``None`` when unavailable."""
    db_path = _acp_db_path(ctx)
    if not db_path.is_file():
        return None
    try:
        connection = open_readonly(db_path.resolve())
    except (OSError, sqlite3.Error, ValueError):
        return None
    connection.row_factory = sqlite3.Row
    return connection


def _acp_table_exists(connection: SQLiteConnection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _acp_text(value: Any, *, allowed: frozenset[str] | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or (allowed is not None and normalized not in allowed):
        return None
    return normalized


def _acp_event_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    normalized = _ACP_EVENT_TYPE_ALIASES.get(normalized, normalized.upper())
    return normalized if normalized in _ACP_EVENT_TYPES else None


def _acp_outcome(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in _ACP_OUTCOMES:
        return normalized
    if normalized == "ok":
        return "succeeded"
    if normalized in {"busy", "orphan"}:
        return "partial"
    if normalized in {"error", "timeout", "rate_limited"}:
        return "failed"
    return None


def _acp_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _acp_identifier(value: Any) -> str | None:
    """Accept only compact opaque IDs; malformed IDs are never reflected."""
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    if not all(character.isascii() and (character.isalnum() or character in "-_") for character in value):
        return None
    return value


def _acp_discussion_participants(value: Any) -> tuple[str, str]:
    """Return a privacy-safe enabled pair, tolerating legacy stored shapes."""
    try:
        decoded = json_depth.loads(value, max_depth=512) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
    enabled: list[str] = []
    if isinstance(decoded, list):
        for item in decoded:
            if item in _ACP_ENABLED_PARTICIPANTS and item not in enabled:
                enabled.append(item)
    if len(enabled) == 2:
        return enabled[0], enabled[1]
    return _ACP_LEGACY_PARTICIPANTS


def _acp_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = _parse_iso_datetime(value)
    return _isoformat_z(parsed) if parsed else None


def _acp_classification(state: str | None) -> str:
    if state == "COMPLETE":
        return "complete"
    if state in {"PARTIAL", "PARTIAL_COMPLETE"}:
        return "partial"
    if state == "CANCELLED":
        return "cancelled"
    if state == "FAILED":
        return "failed"
    if state == "CREATED":
        return "queued"
    return "running"


def _acp_synthesis_state(events: list[dict[str, Any]], current_state: str | None) -> str:
    states = {event["state"] for event in events if event.get("state")}
    event_types = {event["event_type"] for event in events if event.get("event_type")}
    if current_state == "FAILED":
        return "failed"
    if current_state in {"PARTIAL", "PARTIAL_COMPLETE", "CANCELLED"}:
        return "partial"
    if current_state == "COMPLETE" and (
        "SYNTHESIS" in states
        or "SYNTHESIS_COMPLETE" in event_types
        or "SYNTHESIS_TERMINAL" in event_types
    ):
        return "complete"
    if current_state == "SYNTHESIS" or "SYNTHESIS" in states:
        return "running"
    return "not_started"


def _acp_termination(events: list[dict[str, Any]], current_state: str | None) -> str | None:
    terminal_events = {
        "DUPLICATE_SUPPRESSED": "duplicate_suppressed",
        "BUDGET_EXHAUSTED": "budget_exhausted",
        "DEADLINE_EXCEEDED": "deadline_exceeded",
    }
    for event in reversed(events):
        reason = terminal_events.get(event.get("event_type"))
        if reason:
            return reason
    if current_state == "CANCELLED":
        return "cancelled"
    if current_state == "FAILED":
        return "failed"
    return None


def _sanitize_acp_event(
    row: sqlite3.Row, *, allowed_lanes: frozenset[str]
) -> dict[str, Any] | None:
    sequence = _acp_int(row["sequence"])
    state = _acp_text(row["state"], allowed=_ACP_STATES)
    event_type = _acp_event_type(row["event_type"])
    created_at = _acp_timestamp(row["created_at"])
    if sequence is None or state is None or event_type is None or created_at is None:
        return None
    event: dict[str, Any] = {
        "sequence": sequence,
        "event_type": event_type,
        "state": state,
        "created_at": created_at,
    }
    sender = _acp_text(row["sender"], allowed=allowed_lanes)
    recipient = _acp_text(row["recipient"], allowed=allowed_lanes)
    if sender:
        event["sender"] = sender
    if recipient:
        event["recipient"] = recipient
    round_number = _acp_int(row["round"], minimum=1)
    if round_number is not None:
        event["round"] = round_number
    outcome = _acp_outcome(row["outcome"])
    if outcome:
        event["outcome"] = outcome
    duration_ms = _acp_int(row["duration_ms"])
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    token_count = _acp_int(row["token_count"])
    if token_count is not None:
        event["token_count"] = token_count
    return event


def _acp_events(
    connection: SQLiteConnection,
    conversation_id: str,
    participants: tuple[str, str],
) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            """
            SELECT sequence, event_type, state, sender, recipient, round, outcome,
                   duration_ms, token_count, created_at
            FROM acp_conversation_events
            WHERE conversation_id = ?
            ORDER BY sequence ASC
            LIMIT 500
            """,
            (conversation_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    allowed_lanes = frozenset({"root", "codex", *participants})
    events = [
        event
        for row in rows
        if (event := _sanitize_acp_event(row, allowed_lanes=allowed_lanes)) is not None
    ]
    return events


def _acp_rounds_completed(
    events: list[dict[str, Any]], participants: tuple[str, str]
) -> int:
    lanes_by_round: dict[int, set[str]] = defaultdict(set)
    for event in events:
        if (
            event.get("event_type") not in _ACP_MESSAGE_EVENTS
            or event.get("outcome") != "succeeded"
        ):
            continue
        round_number = event.get("round")
        if not isinstance(round_number, int):
            continue
        for lane in (event.get("sender"), event.get("recipient")):
            if lane in participants:
                lanes_by_round[round_number].add(lane)
    completed = 0
    for round_number in sorted(lanes_by_round):
        if round_number != completed + 1 or lanes_by_round[round_number] != set(participants):
            break
        completed = round_number
    return completed


def _acp_summary(
    row: sqlite3.Row,
    events: list[dict[str, Any]],
    participants: tuple[str, str],
) -> dict[str, Any] | None:
    conversation_id = _acp_identifier(row["conversation_id"])
    created_at = _acp_timestamp(row["created_at"])
    rounds_requested = _acp_int(row["rounds_requested"], minimum=1)
    if conversation_id is None or created_at is None or rounds_requested is None:
        return None
    current_state = events[-1]["state"] if events else "CREATED"
    deadline_at = _acp_timestamp(row["deadline_at"])
    terminal = current_state in {"COMPLETE", "PARTIAL_COMPLETE", "FAILED", "CANCELLED"}
    deadline = _parse_iso_datetime(deadline_at)
    expired = not terminal and (deadline is None or datetime.now(UTC) > deadline)
    stale_or_unhealthy = expired or current_state in {
        "PARTIAL", "PARTIAL_COMPLETE", "FAILED", "CANCELLED",
    }
    updated_at = events[-1]["created_at"] if events else created_at
    terminal_event = next(
        (
            event
            for event in reversed(events)
            if event.get("event_type") == "STATE"
            and event.get("state") in {"COMPLETE", "PARTIAL_COMPLETE", "FAILED", "CANCELLED"}
        ),
        None,
    )
    terminal_duration = terminal_event.get("duration_ms") if terminal_event else None
    if isinstance(terminal_duration, int):
        duration_ms = terminal_duration
    elif terminal_event:
        start = _parse_iso_datetime(created_at)
        finish = _parse_iso_datetime(terminal_event.get("created_at"))
        duration_ms = max(0, round((finish - start).total_seconds() * 1000)) if start and finish else 0
    else:
        duration_ms = sum(event.get("duration_ms", 0) for event in events)
    terminal_tokens = terminal_event.get("token_count") if terminal_event else None
    total_tokens = (
        terminal_tokens
        if isinstance(terminal_tokens, int)
        else sum(event.get("token_count", 0) for event in events)
    )
    termination = _acp_termination(events, current_state)
    return {
        "conversation_id": conversation_id,
        "current_state": current_state,
        "classification": _acp_classification(current_state),
        "participants": list(participants),
        "rounds_requested": rounds_requested,
        "rounds_completed": _acp_rounds_completed(events, participants),
        "created_at": created_at,
        "deadline_at": deadline_at,
        "expired": expired,
        "stale_or_unhealthy": stale_or_unhealthy,
        "updated_at": updated_at,
        "total_duration_ms": duration_ms,
        "total_tokens": total_tokens,
        "synthesis_state": _acp_synthesis_state(events, current_state),
        "duplicate_suppressed": termination == "duplicate_suppressed",
        "termination_reason": termination,
    }


def _acp_available_connection(ctx: MonitorContext) -> SQLiteConnection | None:
    connection = _open_acp_db_readonly(ctx)
    if connection is None:
        return None
    try:
        if not (_acp_table_exists(connection, "acp_conversations") and _acp_table_exists(connection, "acp_conversation_events")):
            connection.close()
            return None
    except sqlite3.Error:
        connection.close()
        return None
    return connection


def list_acp_conversations(*, limit: int = 50, ctx: MonitorContext) -> dict[str, Any]:
    """Return sanitized active-conversation summaries without touching storage."""
    connection = _acp_available_connection(ctx)
    if connection is None:
        return {"availability": "unavailable", "conversations": []}
    try:
        rows = connection.execute(
            """
            SELECT conversation_id, rounds_requested, participants_json, created_at, deadline_at
            FROM acp_conversations
            ORDER BY created_at DESC, conversation_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conversations = []
        for row in rows:
            participants = _acp_discussion_participants(row["participants_json"])
            events = _acp_events(connection, row["conversation_id"], participants)
            summary = _acp_summary(row, events, participants)
            if summary is not None:
                conversations.append(summary)
    except sqlite3.Error:
        return {"availability": "unavailable", "conversations": []}
    finally:
        connection.close()
    return {"availability": "available" if conversations else "empty", "conversations": conversations}


def get_acp_conversation(conversation_id: str, *, ctx: MonitorContext) -> dict[str, Any] | None:
    """Return one sanitized conversation timeline, or ``None`` without details."""
    safe_id = _acp_identifier(conversation_id)
    if safe_id is None:
        return None
    connection = _acp_available_connection(ctx)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT conversation_id, rounds_requested, participants_json, created_at, deadline_at FROM acp_conversations WHERE conversation_id = ?",
            (safe_id,),
        ).fetchone()
        if row is None:
            return None
        participants = _acp_discussion_participants(row["participants_json"])
        events = _acp_events(connection, safe_id, participants)
        summary = _acp_summary(row, events, participants)
        return {**summary, "events": events} if summary is not None else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _acp_transcript_client_is_loopback(request: Request) -> bool:
    """Accept only direct loopback peers addressed through a loopback host.

    ``request.client`` is supplied by the accepted connection, rather than a
    forwarding header. Requiring a loopback URL host as well closes the usual
    DNS-rebinding path from a non-local browser origin.
    """
    client = request.client
    client_host = client.host if client else None
    if not isinstance(client_host, str):
        return False
    try:
        client_address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    client_mapped = getattr(client_address, "ipv4_mapped", None)
    if not client_address.is_loopback and not (client_mapped and client_mapped.is_loopback):
        return False

    url_host = request.url.hostname
    if url_host == "localhost":
        return True
    if not isinstance(url_host, str):
        return False
    try:
        url_address = ipaddress.ip_address(url_host)
    except ValueError:
        return False
    url_mapped = getattr(url_address, "ipv4_mapped", None)
    return url_address.is_loopback or bool(url_mapped and url_mapped.is_loopback)


def _acp_transcript_body(value: Any, *, remaining_bytes: int) -> str | None:
    """Return a bounded JSON-safe body or reject the whole malformed row."""
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8")
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeError:
        return None
    if len(encoded) > _ACP_TRANSCRIPT_MAX_BODY_BYTES or len(serialized) > remaining_bytes:
        return None
    return value


def _acp_transcript_entries(
    connection: SQLiteConnection,
    conversation_id: str,
    participants: tuple[str, str],
) -> list[dict[str, Any]] | None:
    """Read a bounded, allowlisted ACP transcript without exposing store IDs."""
    try:
        rows = connection.execute(
            """
            SELECT messages.kind, messages.sender, messages.recipient,
                   messages.body_inline, messages.created_at, event_round.round
            FROM comms_messages AS messages
            JOIN (
                SELECT message_id, MIN(sequence) AS message_sequence,
                       CASE
                           WHEN MIN(round) = MAX(round) AND MIN(round) >= 1
                           THEN MIN(round)
                       END AS round
                FROM acp_conversation_events
                WHERE conversation_id = ? AND message_id IS NOT NULL
                GROUP BY message_id
            ) AS event_round ON event_round.message_id = messages.message_id
            WHERE messages.conversation_id = ?
            ORDER BY event_round.message_sequence ASC
            LIMIT ?
            """,
            (conversation_id, conversation_id, _ACP_TRANSCRIPT_MAX_MESSAGES + 1),
        ).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) > _ACP_TRANSCRIPT_MAX_MESSAGES:
        return None

    entries: list[dict[str, Any]] = []
    remaining_bytes = _ACP_TRANSCRIPT_MAX_RESPONSE_BYTES
    for row in rows:
        kind = _acp_text(row["kind"], allowed=_ACP_TRANSCRIPT_KINDS)
        allowed_lanes = frozenset({"root", "codex", *participants})
        sender = _acp_text(row["sender"], allowed=allowed_lanes)
        recipient = _acp_text(row["recipient"], allowed=allowed_lanes)
        created_at = _acp_timestamp(row["created_at"])
        body = _acp_transcript_body(row["body_inline"], remaining_bytes=remaining_bytes)
        if None in (kind, sender, recipient, created_at, body):
            continue
        entry: dict[str, Any] = {
            "ordinal": len(entries) + 1,
            "kind": kind,
            "sender": sender,
            "recipient": recipient,
            "created_at": created_at,
            "body": body,
        }
        round_number = _acp_int(row["round"], minimum=1)
        if round_number is not None:
            entry["round"] = round_number
        entries.append(entry)
        remaining_bytes -= len(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    return entries


def get_acp_conversation_transcript(conversation_id: str, *, ctx: MonitorContext) -> dict[str, Any] | None:
    """Return a single bounded ACP transcript, or ``None`` without diagnostics."""
    safe_id = _acp_identifier(conversation_id)
    if safe_id is None:
        return None
    connection = _open_acp_db_readonly(ctx)
    if connection is None:
        return None
    try:
        if not all(
            _acp_table_exists(connection, table)
            for table in ("acp_conversations", "acp_conversation_events", "comms_messages")
        ):
            return None
        conversation = connection.execute(
            "SELECT participants_json FROM acp_conversations WHERE conversation_id = ?", (safe_id,)
        ).fetchone()
        if conversation is None:
            return None
        participants = _acp_discussion_participants(conversation["participants_json"])
        entries = _acp_transcript_entries(connection, safe_id, participants)
        return {"conversation_id": safe_id, "messages": entries} if entries is not None else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


@router.get("/agents")
async def runtime_agents(ctx: MonitorContext = Depends(get_ctx)):
    unreadable: dict[str, int] = {"files": 0, "lines": 0, "records": 0}
    agents = await asyncio.to_thread(list_runtime_agents, ctx, unreadable)
    return {"agents": agents, "unreadable": _unreadable_summary(unreadable)}



@router.get("/acp/conversations")
@swr_route("runtime-acp-conversations", ACP_CONVERSATIONS_TTL_S, vary=("limit",))
def runtime_acp_conversations(
    limit: int = Query(50, ge=1, le=100),
    ctx: MonitorContext = Depends(get_ctx),
):
    """Read only, allowlisted summaries of persisted ACP conversations, with their true age."""
    return list_acp_conversations(limit=limit, ctx=ctx)


@router.get("/acp/conversations/{conversation_id}")
async def runtime_acp_conversation(
    conversation_id: str,
    ctx: MonitorContext = Depends(get_ctx),
):
    """Read one allowlisted ACP conversation timeline without message content."""
    conversation = await asyncio.to_thread(get_acp_conversation, conversation_id, ctx=ctx)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("/acp/conversations/{conversation_id}/transcript")
async def runtime_acp_conversation_transcript(
    conversation_id: str,
    request: Request,
    ctx: MonitorContext = Depends(get_ctx),
):
    """Read body-inline ACP content for the local UI only; never mutate storage."""
    no_store = {"Cache-Control": "no-store"}
    if not _acp_transcript_client_is_loopback(request):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"}, headers=no_store)
    transcript = await asyncio.to_thread(get_acp_conversation_transcript, conversation_id, ctx=ctx)
    if transcript is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Conversation not found"},
            headers=no_store,
        )
    return JSONResponse(content=transcript, headers=no_store)




@router.get("/routing")
async def runtime_routing(ctx: MonitorContext = Depends(get_ctx)) -> JSONResponse:
    """How work is routed: each pool now, and the last day's dispatch and review decisions.

    ``now`` evaluates admission (report-only, per pool), the reviewer resolver and
    live workers against the routing budget dispatch admission reads, and names the
    budget it used; it is served stale-while-revalidate, and an unusable budget
    makes those parts ``unknown`` rather than serving an older evaluation. Allowance
    comes from the subscription readings on every request. ``recent`` is one scan
    of the last day's task records, also stale-while-revalidate. Ages are in the
    payload and the oldest is in ``X-Snapshot-Age-S``. Rows carry task ids, model
    and pool labels and fixed words only: no paths, prompts or credentials.
    """
    budget, budget_reason = await _routing_budget(ctx)
    decisions_read = cache_read_swr(
        ctx_scoped_ttl_key(ctx, "runtime-routing-decisions"), ROUTING_TTL_S,
        functools.partial(routing_decisions, ctx), storable=lambda value: value.get("status") in {"ok", "partial"},
    )
    if budget is None:
        (decisions, age, stored), live, allowance = await asyncio.gather(
            decisions_read, asyncio.to_thread(routing_live, resolve_context(ctx)), asyncio.to_thread(routing_allowances),
        )
        evaluated: dict[str, Any] = {
            "state": "unknown", "age_s": None, "budget_generated_at": None,
            "admission": {"state": "unknown", "reason": f"routing budget unavailable ({budget_reason})"},
            "resolver": {"state": "unknown", "reason": f"routing budget unavailable ({budget_reason})"},
            "live": live,
        }
    else:
        (decisions, age, stored), now_read, allowance = await asyncio.gather(
            decisions_read,
            asyncio.to_thread(
                cache_get_or_refresh, ctx_scoped_ttl_key(ctx, "runtime-routing-now"), ROUTING_TTL_S,
                functools.partial(routing_now, ctx, budget, budget.get("generated_at")), wait_s=ROUTING_NOW_WAIT_S,
            ),
            asyncio.to_thread(routing_allowances),
        )
        if now_read is None:
            pending = {"state": "pending", "reason": "being evaluated now"}
            evaluated = {
                "state": "pending", "age_s": None, "budget_generated_at": None, "admission": pending,
                "resolver": pending, "live": await asyncio.to_thread(routing_live, resolve_context(ctx)),
            }
        else:
            now_value, now_age = cast(tuple[dict[str, Any], float], now_read)
            evaluated = {"state": "ok", "age_s": round(now_age, 3), **now_value}
    diagnostics = budget.get("diagnostics") if budget is not None else None
    payload = {
        "now": {
            "pools": [{"pool": meter, "label": label} for meter, label in ROUTING_POOLS],
            "profile": ROUTING_REVIEW_PROFILE,
            "risk": ROUTING_REVIEW_RISK,
            "budget": {
                "state": "ok" if budget is not None else "unknown",
                "reason": budget_reason,
                "generated_at": budget.get("generated_at") if budget is not None else None,
                "age_s": panel_values.number(diagnostics.get("data_age_s")) if isinstance(diagnostics, Mapping) else None,
            },
            "allowance": allowance,
            **evaluated,
        },
        "recent": {**decisions, "age_s": round(age, 3) if stored else None},
    }
    ages = [value for value in (age if stored else None, evaluated["age_s"]) if value is not None]
    return JSONResponse(content=payload, headers=panel_values.snapshot_headers(
        max(ages) if stored else None, ROUTING_STALE_AFTER_S, unavailable=not stored,
    ))


@router.get("/transport-health")
async def runtime_transport_health():
    """Return the cached Codex fresh-process probe; never launch a model.

    No ``MonitorContext`` root applies here: the receipt path and the
    product's Codex config are resolved through ``codex_transport_health``'s
    own resolvers at call time (the config through ``product_root``,
    C-4746; never re-exported as a local ``Path`` global), and the response
    is fully sanitized status fields with no filesystem paths.
    """
    return await asyncio.to_thread(
        lambda: codex_transport_health.current_transport_health(
            config_path=codex_transport_health.default_config_path(),
        ),
    )


# ---------------------------------------------------------------------
# Auth mode snapshot (#1313 / Codex-7)
# ---------------------------------------------------------------------


@router.get("/auth")
def runtime_auth():
    """Per-agent auth mode snapshot.

    Tells an operator — without grepping env vars — whether Gemini is
    running in ``subscription`` (logged-in OAuth / Google account) or
    ``api`` (GEMINI_API_KEY-based) or ``auto`` mode, and whether a
    key is currently inherited from the environment.

    Why it matters (Codex-7 / #1313): "would have saved us time
    immediately" — the session lost hours debugging which auth path
    Gemini actually used. This endpoint makes it deterministic.

    Shape::
        {
          "gemini": {
            "auth_mode":          "subscription" | "api",
            "auth_mode_raw_valid": true,
            "auth_mode_raw_length": 12,
            "api_key_present":    false,
            "google_key_present": false,
            "google_oauth_cred":  true,   # ~/.gemini/oauth_creds.json readable
          },
          "claude":  {"api_key_present": true,  "source": "ANTHROPIC_API_KEY"},
          "codex":   {"api_key_present": true,  "source": "OPENAI_API_KEY"},
          "checked_at": "2026-04-17T..."
        }

    All information is derived from env + filesystem — no subprocess,
    no network call. Cheap and safe to poll.
    """
    env = os.environ
    home = Path.home()

    # Sanitize GEMINI_AUTH_MODE rather than echo it raw. Reviewer
    # Codex BLOCKER on #1312 pre-merge: this endpoint's contract says
    # "never echoes key values". If an operator accidentally pastes a
    # key fragment into GEMINI_AUTH_MODE (typo in their shell
    # profile), echoing the raw value leaks it. We still surface
    # WHETHER the raw value was recognized via a ``raw_valid`` flag +
    # its LENGTH — enough to debug an invalid config without exposing
    # content.
    _AUTH_MODE_VALID = {"auto", "subscription", "api"}
    raw_mode = (env.get("GEMINI_AUTH_MODE") or "").strip()
    raw_valid = raw_mode.lower() in _AUTH_MODE_VALID
    gemini = {
        "auth_mode": resolve_gemini_auth_mode(),
        "auth_mode_raw_valid": raw_valid,
        "auth_mode_raw_length": len(raw_mode),
        "api_key_present": bool(env.get("GEMINI_API_KEY")),
        "google_key_present": bool(env.get("GOOGLE_API_KEY")),
        "google_oauth_cred": has_gemini_oauth_credentials(home),
    }

    # Claude is subscription-only via the CLI; we still surface whether a
    # key happens to be present in case someone's running the SDK too.
    claude = {
        "api_key_present": bool(env.get("ANTHROPIC_API_KEY")),
        "source": (
            "ANTHROPIC_API_KEY" if env.get("ANTHROPIC_API_KEY") else None
        ),
    }

    # Codex uses its own key env var. Same logic.
    codex = {
        "api_key_present": bool(env.get("OPENAI_API_KEY") or env.get("CODEX_API_KEY")),
        "source": (
            "OPENAI_API_KEY" if env.get("OPENAI_API_KEY")
            else "CODEX_API_KEY" if env.get("CODEX_API_KEY")
            else None
        ),
    }

    return {
        "gemini": gemini,
        "claude": claude,
        "codex": codex,
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }



# These HTTP views deliberately do not publish provider-authored prose. The
# scheduler/CLI readers retain their private diagnostics for local operators.
_SUBSCRIPTION_REASONS = frozenset({
    "not_configured", "unreadable", "invalid", "not_in_plan_facts", "no_reading",
    "not_reported", "no_weekly_window", "balance_not_read", "no_monitor_reading",
    "unreadable_reading", "read_time_in_future", "usage_cache_missing",
    "usage_cache_invalid", "usage_cache_unreadable", "usage_cache_not_configured",
    "provider_error", "expired", "not_probed", "too_early_in_window",
})
_LOGIN_REASON_CODES = CLAUDE_ACCOUNT_REFUSAL_CODES | {
    code.replace("claude_account", "codex_account") for code in CLAUDE_ACCOUNT_REFUSAL_CODES
} | {
    "probe_failed", "probe_timeout", "unreadable_status", "claude_cli_not_found", "codex_cli_not_found",
    "codex_account_binding_invalid", "codex_account_unbound", "codex_account_login_unverified",
    "codex_account_not_logged_in", "codex_account_home_not_private", "codex_account_binding_shared",
    "codex_account_home_unavailable",
}
_WINDOW_LABELS = frozenset({
    "Weekly", "Session", "Primary", "Secondary", "Tertiary", "primary", "secondary", "tertiary",
    "Code review", "Gemini weekly", "Gemini 5-hour", "Claude/GPT weekly", "Claude/GPT 5-hour",
    "Claude", "Codex", "Grok", "Cursor", "Gemini", "Claude/GPT", "Auto", "API", "Total",
})


def _subscription_section(raw: object) -> dict[str, Any]:
    row = panel_values.mapping(raw)
    out = panel_values.scalars(
        row,
        numbers=("age_s", "window_minutes", "used_pct", "left_pct", "resets_in_s", "headroom_pct",
                 "expected_used_pct", "runs_out_in_s", "balance", "count", "days_to_end", "days_to_renewal"),
        flags=("reset_passed", "lasts_to_reset", "expired", "cancelled", "ended"),
        times=("read_at", "resets_at", "runs_out_at", "renews_on", "ends_on", "next_renewal_on", "expires_on"),
    )
    for key, allowed in {
        "status": {"ok", "none", "unknown", "fresh", "stale", "missing", "invalid", "unreadable", "not_configured"},
        "state": {"on_pace", "deficit", "reserve"},
        "source": {"provider", "computed", "plan_facts", "usage_cache", "monitor"},
        "label": _WINDOW_LABELS,
        "unit": {"credits", "USD"},
        "kind": {"weekly reset", "session reset", "weekly", "session", "reset", "5-hour"},
    }.items():
        if key in row:
            out[key] = panel_values.choice(row[key], allowed)
    if "reason" in row:
        out["reason_code"] = panel_values.reason(row.get("reason"), _SUBSCRIPTION_REASONS)
    if "items" in row:
        out["items"] = [_subscription_section(item) for item in panel_values.rows(row["items"], 10)]
    # name, summary and reset_description are arbitrary provider or plan-facts text.
    return out


def _public_subscriptions(raw: object) -> dict[str, Any]:
    snapshot = panel_values.mapping(raw)
    providers = []
    identities = {provider: (label, {account: key for account, key, *_ in accounts})
                  for provider, label, accounts in subscriptions.PROVIDERS}
    for provider in panel_values.rows(snapshot.get("providers"), 16):
        provider_id = provider.get("provider")
        identity = identities.get(provider_id) if isinstance(provider_id, str) else None
        if identity is None:
            continue
        label, accounts = identity
        rows = []
        for account in panel_values.rows(provider.get("accounts"), 16):
            name = panel_values.choice(account.get("account"), accounts)
            if name is None:
                continue
            row: dict[str, Any] = {"account": name, "plan_key": accounts[name],
                   "source": panel_values.choice(account.get("source"), {"usage_cache", "monitor"})}
            for section in ("reading", "weekly", "session", "pace", "credits", "banked_resets", "plan"):
                row[section] = _subscription_section(account.get(section))
            row["extra_windows"] = [_subscription_section(window)
                                    for window in panel_values.rows(account.get("extra_windows"), 20)]
            # Percent breakdown labels have their own closed vocabulary in the reader.
            row["breakdown"] = [
                {"label": item["label"], "value": item["value"]}
                for item in panel_values.rows(account.get("breakdown"), 10)
                if panel_values.choice(item.get("label"), {"Cursor", "Grok", "Gemini", "Claude/GPT"})
                and isinstance(item.get("value"), str)
                and re.fullmatch(r"(?:100|[0-9]{1,2})(?:\.[0-9]{1,2})?%", item["value"])
            ]
            rows.append(row)
        providers.append({"provider": provider["provider"], "label": label, "accounts": rows})
    sources = panel_values.mapping(snapshot.get("sources"))
    try:
        catalog_model_ids = current_catalog_model_ids(load_model_catalog())
    except (OSError, ValueError):
        catalog_model_ids = None
    return {**panel_values.scalars(snapshot, numbers=("stale_after_s",), times=("generated_at",)),
            "sources": {key: _subscription_section(sources.get(key)) for key in ("usage_cache", "plan_facts")},
            "catalog_models": sorted(catalog_model_ids) if catalog_model_ids is not None else None,
            "providers": providers}


def _public_logins(raw: object, ends: object) -> dict[str, Any]:
    snapshot = panel_values.mapping(raw)
    rows = []
    names = {"Claude a": "claude", "Claude b": "claude", "Codex a": "codex", "Codex b": "codex"}
    for row in panel_values.rows(snapshot.get("accounts"), 8):
        name = panel_values.choice(row.get("account"), names)
        if name is None:
            continue
        rows.append({"account": name, "provider": names[name],
                     **panel_values.scalars(row, flags=("logged_in", "expiry_reported"), times=("expires_at",)),
                     "reason_code": panel_values.reason(row.get("error"), _LOGIN_REASON_CODES)})
    subscriptions_ends = panel_values.mapping(ends)
    return {"state": panel_values.choice(snapshot.get("state"), {"pending", "ok"}, "unknown"),
            **panel_values.scalars(snapshot, times=("read_at",)), "accounts": rows,
            "subscriptions": {"ends": {key: panel_values.scalars(panel_values.mapping(value),
                                                                          flags=("ended",), times=("ends_on",))
                                        for key, value in panel_values.mapping(subscriptions_ends.get("ends")).items()
                                        if key in {"claude_a", "claude_b", "codex_a", "codex_b", "agy", "grok"}},
                              "reason_code": panel_values.reason(subscriptions_ends.get("reason"),
                                                                 {"not_configured", "unreadable", "invalid"})}}


@router.get("/logins")
def runtime_logins():
    """Sign-in health of the subscription accounts and the cancelled subscriptions' end dates.

    ``accounts`` comes from a background snapshot (``account_logins``): signed in
    or not, and the sign-in end date where the CLI reports one. No email, organization, directory or credential. ``state`` is
    ``pending`` until the first snapshot exists. ``subscriptions`` holds the
    operator's end dates for cancelled subscriptions, read from the host config.
    """
    payload = _public_logins(account_logins.logins_snapshot(), account_logins.subscription_ends())
    return JSONResponse(content=payload, headers=panel_values.snapshot_headers(
        # 600 s between probes, up to four 30 s probes, two page cycles and
        # collection margin. This is display freshness, never login admission.
        panel_values.observation_age_s(payload.get("read_at")), account_logins.TTL_S + 300.0,
        unavailable=payload.get("state") != "ok",
    ))


@router.get("/subscriptions")
def runtime_subscriptions():
    """Every subscription account's usage, pace, windows, credits, banked resets and plan facts.

    One row per account, grouped by provider (``providers[].accounts[]``), for
    the home page and for dispatch planning. Built by
    ``subscriptions.subscriptions_snapshot`` from the host's usage cache, the
    Monitor's Claude readings and the operator's plan-facts file; it
    never calls a provider. ``catalog_models`` lists the fleet catalog's
    non-retired model IDs, or is ``null`` if the catalog cannot be read.
    A missing usage value is ``null`` in a section
    whose ``status`` is ``unknown`` with a ``reason_code``; a reading older than
    15 minutes is ``stale``. The heading is ``unavailable`` only when no
    non-ended account has a fresh or stale reading. Fresh readings beside an
    unknown or stale one stay visible as ``stale``, at the age of the readings
    that exist. Provider prose and plan names are omitted from HTTP; the local
    console reader retains its private diagnostic view.
    """
    payload = _public_subscriptions(subscriptions.subscriptions_snapshot())
    readings = [account.get("reading", {}) for provider in payload["providers"] for account in provider["accounts"]
                if not account.get("plan", {}).get("ended")]
    # A fresh or stale row is a reading the panel can show. Unknown is a gap in
    # that row, not an empty panel, so it must not discard the other ages.
    usable = [reading for reading in readings if reading.get("status") in {"fresh", "stale"}]
    ages = [reading["age_s"] for reading in usable if panel_values.number(reading.get("age_s")) is not None]
    return JSONResponse(content=payload, headers=panel_values.snapshot_headers(
        max(ages) if ages else None, payload.get("stale_after_s", subscriptions.STALE_AFTER_S),
        unavailable=not usable,
        stale=bool(usable) and any(reading.get("status") != "fresh" for reading in readings),
    ))
