"""Runtime observability API router."""

from __future__ import annotations

import asyncio
import errno
import functools
import hashlib
import importlib.util
import inspect
import ipaddress
import json
import os
import re
import sqlite3
import stat
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
from lu_machinery.control_plane.storage import Authority, ControlPlaneError, StoreId, resolve_authority
from lu_machinery.dispatcher.admission import review_typed
from lu_machinery.dispatcher.types import StartRefusal
from lu_machinery.fleet_comms import message_plane
from lu_machinery.fleet_comms.artifacts import RETENTION_EXPIRED_PREFIX
from lu_machinery.fleet_comms.paths import PlaneRootAnchorError
from lu_machinery.lib.readonly_sqlite import SQLiteConnection, import_named_module, open_readonly
from lu_machinery.orchestration import codex_transport_health, dispatch_admission
from lu_machinery.orchestration.dead_worker_state import worker_liveness
from lu_machinery.review.model_catalog import VALID_CODEX_EFFORTS, canonical_model_id, load_model_catalog

from . import account_logins, panel_values, subscriptions
from . import delegate_router as delegate_api
from .model_stats_rows import current_catalog_model_ids
from .monitor_context import MonitorContext, get_ctx, resolve_context
from .monitor_context import production_context as production_context  # re-export: test monkeypatches
from .state_helpers import cache_get_stored, cache_read_swr, ctx_cache_scope, swr_route

router = APIRouter(tags=["runtime"])




_KNOWN_OUTCOMES = ("ok", "error", "timeout", "rate_limited")
_RUNTIME_ATTRIBUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}$")
_RUNTIME_ATTRIBUTION_SOURCES = frozenset({"explicit", "session_env", "unknown"})
_RUNTIME_FAILURE_CODES = RUNTIME_FAILURE_CODES
_ACP_LEGACY_PARTICIPANTS = ("codex", "grok")
_ACP_MIN_PARTICIPANTS = 2
_ACP_MAX_PARTICIPANTS = 4
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
_ACP_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACP_TRANSCRIPT_STATE_OK = "ok"
_ACP_TRANSCRIPT_STATE_BODY_EXPIRED = "body_expired"
_ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT = "missing_or_corrupt"
_ACP_TRANSCRIPT_STATE_BACKEND_UNAVAILABLE = "backend_unavailable"
_ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL = "oversized_or_partial"
_ACP_TRANSCRIPT_STATE_LABELS: dict[str, str] = {
    _ACP_TRANSCRIPT_STATE_OK: "ok",
    _ACP_TRANSCRIPT_STATE_BODY_EXPIRED: "body expired",
    _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT: "missing or corrupt",
    _ACP_TRANSCRIPT_STATE_BACKEND_UNAVAILABLE: "backend unavailable",
    _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL: "oversized or partial",
}
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
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


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
# resolver seats each review. This view reads the evidence each dispatch already
# records on its task record (with the admission snapshot it took) and keeps no store.

# The scan is served stale-while-revalidate; the page's freshness bound allows two refreshes.
ROUTING_TTL_S = 30.0
ROUTING_STALE_AFTER_S = 90.0
ROUTING_WINDOW_S = 24 * 3600.0
# A start later than this past the snapshot time is a bad clock or a bad record, not a decision.
ROUTING_FUTURE_SKEW_S = 300.0
# A scan takes up to about 1 s on a full store: a read waits this long (routes answer within 1 s) and
# otherwise answers "pending"; the page reads again after 3 s.
ROUTING_WAIT_S = 0.5
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
# Closed public vocabularies: a value outside them is published as None ("not recorded"), never verbatim.
_ROUTING_RISKS = frozenset({"low", "medium", "high", "critical"})
_ROUTING_PROFILES = frozenset({"code", "infra", "ukrainian"})
# Statuses dispatch records; any other value (a typo, trailing space, non-string) is an unreadable record.
_ROUTING_STATUSES = dispatch_admission.ACTIVE_STATUSES | dispatch_admission._KNOWN_TERMINAL_STATUSES | {
    "settling", "blocked", "dry-run", "dry_run",
}
_ROUTING_ROLES = frozenset({
    "implementation", "implementer", "review", "reviewer", "advisory", "advisor", "evaluation", "evaluator",
    "analyst", "quality", "test", "recon", "integrator", "verification", "investigation", "investigator", "design",
    "writer", "architect", "operations", "critic", "research", "researcher", "planner", "orchestrator", "author",
    "driver",
})
_ROUTING_ACTIVE = dispatch_admission.ACTIVE_STATUSES
# A recorded worker identity, as dispatch writes it: the boot id and the process start time in clock ticks.
_ROUTING_PROCESS_START = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9]+", re.ASCII)
# Every mode dispatch records; any other value leaves the kind of work, and so its admission, unknown.
_ROUTING_MODES = dispatch_admission.WRITE_CAPABLE_MODES | {"read-only"}
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
# A refusal marker that proves nothing: malformed, contradicting the record's lifecycle, or a code dispatch never writes.
_ROUTING_REFUSAL_UNKNOWN = "unknown"
# Codes only the dispatch CLI writes, before any launch (``dispatcher/cli.py`` ``record_refusal``): launch evidence
# contradicts them. Admission's own deferral (capacity) also records the denial it was.
_ROUTING_PRE_LAUNCH_CODES = frozenset({StartRefusal.CAPACITY.value, StartRefusal.ELIGIBILITY.value})
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


def _routing_number(value: object) -> float | None:
    number = panel_values.number(value)  # NaN, infinities, bools and huge magnitudes are unmeasured
    return None if number is None else float(number)


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


def _routing_account_by(record: Mapping[str, Any], pool: str | None) -> str | None:
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
    # AGY and Grok+Cursor each have one subscription: nothing chooses an account there (if the pool is known).
    return "single_account" if pool is not None else None


def _routing_same_model(left: str, right: str) -> bool:
    return (canonical_model_id(left) or left.casefold()) == (canonical_model_id(right) or right.casefold())


def _routing_substitution(record: Mapping[str, Any], model: str | None) -> str | None:
    """Plain text when the record shows the run used another model than the one asked for."""
    substitution = record.get("substitution")
    if isinstance(substitution, Mapping) and substitution.get("substituted") is True:
        actual = panel_values.model(substitution.get("actual_model"))
        requested = panel_values.model(substitution.get("requested_model")) or model
        if actual and requested and not _routing_same_model(requested, actual):
            return f"asked for {requested}; the runtime ran {actual}"
    observed = panel_values.model(recorded_model_accounting_fields(record).get("observed_model"))
    if observed and model and not _routing_same_model(model, observed):
        return f"asked for {model}; the provider reported {observed}"
    return None


def _routing_refusal(record: Mapping[str, Any]) -> tuple[str | None, bool]:
    """The start refusal a record proves, and whether the record is the CLI's refusal record.

    The code is None when the record has no marker, and ``_ROUTING_REFUSAL_UNKNOWN``
    when the marker proves nothing: admission's own classification
    (``classify_never_started_refusal``) finds it inconsistent with the record's
    lifecycle, dispatch never writes the code, a code only the CLI writes before
    launch is on any other record, or an admitting snapshot contradicts admission's
    deferral. The CLI's refusal record (``store.record_admission_refusal``) is a
    pre-launch record under a fresh id that names the id asked for
    (``requested_task_id``) and copies no account; any other refused record is the
    task's own, with its own id and account fields, launched or not.
    """
    code = record.get("never_started_reason")
    if code is None:
        return None, False
    pre_launch, contradiction = dispatch_admission.classify_never_started_refusal(record)
    if contradiction is not None or code not in _ROUTING_START_REFUSALS:
        return _ROUTING_REFUSAL_UNKNOWN, False
    cli_record = pre_launch and "requested_task_id" in record
    snapshot = record.get("admission")
    admitted = isinstance(snapshot, Mapping) and snapshot.get("admitted") is True
    if code in _ROUTING_PRE_LAUNCH_CODES and (not cli_record or (code == StartRefusal.CAPACITY.value and admitted)):
        return _ROUTING_REFUSAL_UNKNOWN, False
    return cast(str, code), cli_record


def _routing_admission(record: Mapping[str, Any], *, write: bool, refusal: str | None) -> dict[str, Any]:
    """The admission outcome a record proves, in fixed words; missing or contradictory evidence is unknown."""
    snapshot = record.get("admission")
    check = _routing_admission_check(snapshot) if isinstance(snapshot, Mapping) else None
    if refusal == _ROUTING_REFUSAL_UNKNOWN:
        return {"state": "unknown", "reason": "its refusal marker contradicts the record"}
    if refusal is not None:
        state, reason = _ROUTING_START_REFUSALS[refusal]
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


def _routing_pool(record: Mapping[str, Any], refusal: str | None, cli_record: bool) -> str | None:
    if refusal == _ROUTING_REFUSAL_UNKNOWN:
        return None  # the record contradicts itself: which pool it meant is unknown
    snapshot = record.get("admission")
    recorded_meter = snapshot.get("meter") if isinstance(snapshot, Mapping) else None
    if isinstance(recorded_meter, str) and recorded_meter in _ROUTING_POOL_LABELS:
        return recorded_meter  # the meter admission judged
    if cli_record:
        # The CLI's refusal record copies no account, so without a recorded meter the pool is unknown.
        return None
    agent = record.get("agent")
    if not isinstance(agent, str):
        return None  # no agent, no meter; the row and its outcome stay readable
    model = panel_values.model(record.get("model"))  # the catalog id, so admission's meter reads its canonical name
    # Admission splits these agents' meters by model or account (``gemini`` is AGY's older route name) and
    # defaults a missing one: here the pool is unknown instead.
    split = {"agy": model, "gemini": model, "claude": record.get("claude_account"), "codex": record.get("codex_account")}
    if agent in split and not isinstance(split[agent], str):
        return None
    return _routing_meter(agent, model, record.get("claude_account"), record.get("codex_account"))


def _routing_status(record: Mapping[str, Any], *, now: datetime) -> tuple[str, bool]:
    """The record's status word, and whether its worker is live; raises ``_RoutingMalformed`` when unknowable.

    A starting dispatch is live by admission's own slot rule: without a worker PID,
    when it is not orphaned and ``pidless_spawning_holds_slot`` holds (owner, grace
    and clock-skew bounds); with one, unless its worker is proven dead. A running one is live only on the
    worker-identity check (PID and start time); a dead one is a zombie, and one
    whose liveness cannot be proven, or whose recorded identity is not one
    dispatch writes, is unreadable.
    """
    status = record.get("status")
    if not isinstance(status, str) or status not in _ROUTING_STATUSES:
        raise _RoutingMalformed("status unreadable or unsupported")
    if status not in _ROUTING_ACTIVE:
        return status, False
    identity = record.get("schema_version") == 2  # the records whose liveness compares a recorded start time
    start = record.get("process_start")
    if identity and start is not None and not (isinstance(start, str) and _ROUTING_PROCESS_START.fullmatch(start)):
        raise _RoutingMalformed("worker identity unreadable")  # compared as recorded, it would prove a death
    if status == "spawning":
        if dispatch_admission._parse_pid(record.get("pid")) is None:  # admission's own reading of the pid
            state = dict(record)
            return status, not dispatch_admission.is_orphaned_pidless_record(state) and (
                dispatch_admission.pidless_spawning_holds_slot(
                    state, now=now, task_id=str(record.get("task_id")), pid_alive=dispatch_admission.process_alive,
                )
            )
        return status, worker_liveness(dict(record), pid_alive=dispatch_admission.process_alive) is not False
    if identity and start is None:
        raise _RoutingMalformed("worker identity not recorded")  # liveness would rest on the PID alone
    alive = worker_liveness(dict(record), pid_alive=dispatch_admission.process_alive)
    if alive is None:
        raise _RoutingMalformed("worker liveness unknown")
    return ("running", True) if alive else ("zombie", False)


def _routing_decision(record: Mapping[str, Any], *, now: datetime) -> dict[str, Any] | None:
    """One allowlisted row, or None outside the window; raises ``_RoutingMalformed`` for an unreadable record.

    Rows carry ids, labels, model names and fixed words only, never paths, prompts or free text.
    """
    # A refused start is recorded under its own id and names the asked-for id; any other record is its task id.
    refusal, cli_record = _routing_refusal(record)
    raw_id = (record.get("requested_task_id") or record.get("task_id")) if cli_record else record.get("task_id")
    started = _routing_started(record.get("started_at"))
    if not isinstance(raw_id, str) or not raw_id or started is None:
        raise _RoutingMalformed("task id or start time unreadable")
    age_s = (now - started).total_seconds()
    if age_s > ROUTING_WINDOW_S:
        return None
    if age_s < -ROUTING_FUTURE_SKEW_S:
        raise _RoutingMalformed("start time in the future")
    mode = record.get("mode")
    if not isinstance(mode, str) or mode not in _ROUTING_MODES:
        raise _RoutingMalformed("mode unreadable")  # the kind of work, and so its admission, is unknown
    review = review_typed(dict(record))
    write = mode in dispatch_admission.WRITE_CAPABLE_MODES
    model = panel_values.model(record.get("model"))
    pool = _routing_pool(record, refusal, cli_record)
    status, live = _routing_status(record, now=now)
    risk = record.get("review_risk")
    return {
        "task_id": panel_values.task_reference(raw_id),  # None: withheld by the public task-reference policy
        "started_at": _routing_iso_z(started),
        "kind": "review" if review else "write" if write else "read_only",
        "role": panel_values.choice(record.get("research_role"), _ROUTING_ROLES),
        "profile": panel_values.choice(record.get("review_profile"), _ROUTING_PROFILES) if review else None,
        "risk": risk if review and isinstance(risk, str) and risk in _ROUTING_RISKS else None,
        "author_model": panel_values.model(record.get("review_author_model")) if review else None,
        "model": model,
        "effort": panel_values.choice(record.get("effort"), VALID_CODEX_EFFORTS),
        "pool": pool,
        "pool_label": _ROUTING_POOL_LABELS.get(pool or ""),
        # The dispatch caller names the model; a review record does not prove the resolver picked it.
        "model_by": "not_recorded" if review else "brief",
        "account_by": _routing_account_by(record, pool),
        "admission": _routing_admission(record, write=write, refusal=refusal),
        "status": status,
        "live": live,
        "substitution": _routing_substitution(record, model),
        "parent_task_id": panel_values.task_reference(record.get("parent_task_id")),
    }


def _routing_lane_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    admission = Counter(row["admission"]["state"] for row in rows)
    return {
        "total": len(rows),
        "by_model": dict(Counter(row["model"] or "unknown" for row in rows)),
        "by_pool": dict(Counter(row["pool_label"] or "pool not recorded" for row in rows)),
        "by_account_chooser": dict(Counter(row["account_by"] or "not_recorded" for row in rows if row["pool"])),
        "admission": {
            state: admission.get(state, 0)
            for state in ("admitted", "forced", "deferred", "refused", "not_checked", "unknown")
        },
        "substitutions": sum(1 for row in rows if row["substitution"]),
        "live": sum(1 for row in rows if row["live"]),
    }


def _routing_listing(tasks_dir: Path) -> tuple[list[Path], str | None]:
    """Admission's own strict enumeration of the store: a listing fault is a typed reason, never an empty store."""
    try:
        return dispatch_admission.scan_record_candidates(tasks_dir), None
    except FileNotFoundError:
        return [], "task_records_missing"
    except OSError:
        return [], "task_records_unreadable"


def routing_decisions(ctx: MonitorContext | None = None) -> dict[str, Any]:
    """Every dispatch and review decision started in the last day.

    A record is written when its task starts, so a file last written before the
    window holds no decision in it and is not read. A store that cannot be listed
    is ``unknown``; records that cannot be read or parsed are counted, and make
    the result ``partial``.
    """
    now = datetime.now(UTC)
    tasks_dir = delegate_api._tasks_dir(resolve_context(ctx))
    candidates, fault = _routing_listing(tasks_dir)
    if fault is not None:
        return {"status": "unknown", "reason": fault, "as_of": _routing_iso_z(now)}
    cutoff = now.timestamp() - ROUTING_WINDOW_S
    rows: list[dict[str, Any]] = []
    unreadable = 0
    for path in candidates:
        try:
            try:
                written = path.stat().st_mtime
            except FileNotFoundError:
                continue  # archived or removed since the listing: no longer a hot record
            if written < cutoff:
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
    try:
        db_path = _acp_db_path(ctx)
        if not db_path.is_file():
            return None
        connection = open_readonly(db_path.resolve())
    except (PlaneRootAnchorError, OSError, sqlite3.Error, RuntimeError, ValueError):
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


def _acp_discussion_participants(value: Any) -> tuple[str, ...]:
    """Return a privacy-safe enabled participant tuple (2-4 seats), tolerating legacy stored shapes."""
    try:
        decoded = json_depth.loads(value, max_depth=512) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        decoded = None
    enabled: list[str] = []
    if isinstance(decoded, list):
        for item in decoded:
            if isinstance(item, str) and item in _ACP_ENABLED_PARTICIPANTS and item not in enabled:
                enabled.append(item)
    if _ACP_MIN_PARTICIPANTS <= len(enabled) <= _ACP_MAX_PARTICIPANTS:
        return tuple(enabled)
    return _ACP_LEGACY_PARTICIPANTS


def _acp_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return None
    try:
        return _isoformat_z(parsed)
    except (ValueError, OverflowError):
        return None


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
    participants: tuple[str, ...],
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
    events: list[dict[str, Any]], participants: tuple[str, ...]
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
    participants: tuple[str, ...],
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


_ACP_TRANSCRIPT_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def _is_loopback_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    # ipaddress.{IPv4,IPv6}Address.is_loopback is a boolean property, not a method.
    return address.is_loopback or bool(mapped and mapped.is_loopback)  # nosemgrep: python.lang.maintainability.is-function-without-parentheses


def _whatwg_ipv6_serialize(address: ipaddress.IPv6Address) -> str:
    """Serialize an IPv6 address using the WHATWG URL Standard IPv6 serializer.

    Browsers serialize IPv4-mapped IPv6 addresses as hex hextets
    (``::ffff:7f00:1``) rather than Python's dotted-quad form
    (``::ffff:127.0.0.1``).
    """
    value = int(address)
    pieces = [(value >> (16 * (7 - index))) & 0xFFFF for index in range(8)]
    best_start = -1
    best_len = 1
    cur_start = -1
    cur_len = 0
    for index, piece in enumerate(pieces):
        if piece == 0:
            if cur_start == -1:
                cur_start = index
                cur_len = 1
            else:
                cur_len += 1
            if cur_len > best_len:
                best_start = cur_start
                best_len = cur_len
        else:
            cur_start = -1
            cur_len = 0
    compress = best_start if best_len > 1 else None
    out: list[str] = []
    ignore0 = False
    for index, piece in enumerate(pieces):
        if ignore0 and piece == 0:
            continue
        if ignore0:
            ignore0 = False
        if compress == index:
            out.append("::" if index == 0 else ":")
            ignore0 = True
            continue
        out.append(f"{piece:x}")
        if index != 7:
            out.append(":")
    return "".join(out)


def _bound_acp_listener(request: Request) -> tuple[str, str] | None:
    """Return ``(expected_host, exact_origin)`` for the local loopback HTTP listener.

    Reads the accepted connection's local interface and effective port from ASGI
    ``scope["server"]`` (which Uvicorn populates from ``socket.getsockname()``,
    independent of HTTP headers), so alternate loopback ports (such as
    ``launchd_supervisor start --port``) and IPv6 / IPv4-mapped loopback listeners
    authorize their own exact authority without extra launcher plumbing. Fails
    closed (returns ``None``) if ``scope["server"]`` is absent, malformed, or
    non-loopback.
    """
    server = request.scope.get("server")
    if not isinstance(server, (tuple, list)) or len(server) != 2:
        return None
    raw_host, raw_port = server
    if (
        not isinstance(raw_host, str)
        or not isinstance(raw_port, int)
        or isinstance(raw_port, bool)
        or not (1 <= raw_port <= 65535)
    ):
        return None
    if not raw_host or any(ord(ch) <= 0x20 or ord(ch) >= 0x7F or ch in "/?#@\\%" for ch in raw_host):
        return None
    if raw_host.startswith("[") or raw_host.endswith("]"):
        if not (raw_host.startswith("[") and raw_host.endswith("]")):
            return None
        try:
            address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.IPv6Address(raw_host[1:-1])
        except ValueError:
            return None
    else:
        try:
            address = ipaddress.ip_address(raw_host)
        except ValueError:
            return None
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        return None
    if not _is_loopback_ip(address):
        return None
    if isinstance(address, ipaddress.IPv6Address):
        authority_host = f"[{_whatwg_ipv6_serialize(address)}]"
    else:
        authority_host = address.compressed
    if raw_port == 80:
        return authority_host, f"http://{authority_host}"
    authority = f"{authority_host}:{raw_port}"
    return authority, f"http://{authority}"


def _acp_transcript_access_refusal(request: Request) -> str | None:
    """Return a typed refusal reason when the request violates the local-listener boundary.

    Returns ``None`` only when:
    - the bound ASGI listener in ``scope["server"]`` is a valid loopback HTTP listener,
    - no ``Forwarded`` or ``X-Forwarded-*`` header is present,
    - ``request.client`` is a direct loopback IP,
    - the request scheme is ``http`` and exactly one raw ``Host`` matches the bound listener,
    - any ``Origin`` header is singular and equals that exact origin (scheme, host, port), and
    - any ``Sec-Fetch-Site`` header is singular and in ``{"same-origin", "none"}``.
    """
    listener = _bound_acp_listener(request)
    if listener is None:
        return "invalid_listener"
    expected_host, expected_origin = listener

    hosts: list[str] = []
    origins: list[str] = []
    fetch_sites: list[str] = []
    for raw_name, raw_value in request.headers.raw:
        name = raw_name.decode("latin-1").strip().lower()
        if name == "forwarded" or name.startswith("x-forwarded-"):
            return "forwarding_header_present"
        if name == "host":
            hosts.append(raw_value.decode("latin-1"))
        elif name == "origin":
            origins.append(raw_value.decode("latin-1"))
        elif name == "sec-fetch-site":
            fetch_sites.append(raw_value.decode("latin-1"))

    client = request.client
    client_host = client.host if client else None
    if not isinstance(client_host, str):
        return "non_loopback_peer"
    try:
        client_address = ipaddress.ip_address(client_host)
    except ValueError:
        return "non_loopback_peer"
    if isinstance(client_address, ipaddress.IPv6Address) and client_address.scope_id is not None:
        return "non_loopback_peer"
    if not _is_loopback_ip(client_address):
        return "non_loopback_peer"

    if request.scope.get("scheme") != "http":
        return "invalid_scheme"
    if len(hosts) != 1 or hosts[0] != expected_host:
        return "invalid_host"
    if len(origins) > 1 or (len(origins) == 1 and origins[0] != expected_origin):
        return "invalid_origin"
    if len(fetch_sites) > 1 or (len(fetch_sites) == 1 and fetch_sites[0] not in _ACP_TRANSCRIPT_ALLOWED_FETCH_SITES):
        return "cross_site_request"
    return None


class _AcpBackendUnavailable(Exception):
    """Internal typed backend failure; reason is a closed static code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _acp_json_bytes(value: Any) -> bytes:
    """Serialize identically to Starlette's ``JSONResponse.render()``."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _acp_transcript_participants(value: Any) -> tuple[tuple[str, ...] | None, str | None]:
    """Validate stored ACP participants strictly (2-4 unique enabled seats) for transcript reads."""
    if not isinstance(value, str):
        return None, "malformed_participants"
    try:
        decoded = json_depth.loads(value, max_depth=512)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return None, "malformed_participants"
    if not isinstance(decoded, list):
        return None, "malformed_participants"
    enabled: list[str] = []
    for item in decoded:
        if not isinstance(item, str) or item not in _ACP_ENABLED_PARTICIPANTS or item in enabled:
            return None, "malformed_participants"
        enabled.append(item)
    if not (_ACP_MIN_PARTICIPANTS <= len(enabled) <= _ACP_MAX_PARTICIPANTS):
        return None, "malformed_participants"
    return tuple(enabled), None


def _open_acp_transcript_store(ctx: MonitorContext) -> tuple[Path, SQLiteConnection]:
    """Open the Fleet Comms SQLite store read-only for transcript reads, or raise a typed backend failure."""
    try:
        authority = resolve_authority(StoreId.FLEET_COMMS)
    except (ControlPlaneError, OSError, RuntimeError, ValueError):
        raise _AcpBackendUnavailable("store_root_unavailable") from None
    if authority is Authority.PG:
        raise _AcpBackendUnavailable("authority_unsupported_component")
    try:
        plane_root = message_plane.default_plane_root(repo_root=ctx.roots.state_root).resolve()
    except (PlaneRootAnchorError, OSError, RuntimeError, ValueError):
        raise _AcpBackendUnavailable("store_root_unavailable") from None
    db_path = plane_root / "comms.sqlite3"
    try:
        if not db_path.is_file():
            raise _AcpBackendUnavailable("store_unavailable")
        connection = cast(SQLiteConnection, open_readonly(db_path))
    except _AcpBackendUnavailable:
        raise
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        raise _AcpBackendUnavailable("store_unavailable") from None
    connection.row_factory = sqlite3.Row
    try:
        has_tables = all(
            _acp_table_exists(connection, table)
            for table in (
                "acp_conversations",
                "acp_conversation_events",
                "comms_messages",
                "artifacts",
                "message_artifacts",
            )
        )
    except sqlite3.Error:
        connection.close()
        raise _AcpBackendUnavailable("store_unavailable") from None
    if not has_tables:
        connection.close()
        raise _AcpBackendUnavailable("schema_unavailable")
    return plane_root, connection


def _open_acp_blob_no_follow(plane_root: Path, sha256: str) -> int:
    """Open a CAS blob file under ``plane_root/blobs/sha256/<aa>/<sha256>`` without following symlinks.

    Any error (including missing) on shared CAS root directories (``plane_root``, ``blobs``,
    ``blobs/sha256``) and any non-missing error on the shard directory raises
    ``_AcpBackendUnavailable``; a missing shard or leaf component raises ``FileNotFoundError``;
    leaf-file ``OSError`` propagates.
    """
    cloexec = getattr(os, "O_CLOEXEC", 0)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | cloexec
    try:
        parent_fd = os.open(plane_root, os.O_RDONLY | os.O_DIRECTORY | cloexec)
    except OSError:
        raise _AcpBackendUnavailable("blob_store_unavailable") from None
    try:
        for part in ("blobs", "sha256"):
            try:
                next_fd = os.open(part, dir_flags, dir_fd=parent_fd)
            except OSError:
                raise _AcpBackendUnavailable("blob_store_unavailable") from None
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            shard_fd = os.open(sha256[:2], dir_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            raise
        except OSError:
            raise _AcpBackendUnavailable("blob_store_unavailable") from None
        os.close(parent_fd)
        parent_fd = shard_fd
        return os.open(sha256, file_flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _acp_artifact_is_expired(connection: SQLiteConnection, artifact_id: str) -> bool:
    """Re-read ``artifacts.retention_class`` to detect retention expiry including the mark/unlink race."""
    row = connection.execute(
        "SELECT retention_class FROM artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        return False
    retention_class = row["retention_class"]
    return isinstance(retention_class, str) and retention_class.startswith(RETENTION_EXPIRED_PREFIX)


def _acp_digest_is_expired(connection: SQLiteConnection, sha256_lower: str) -> bool:
    """Check whether any ``artifacts`` row matching ``sha256_lower`` carries the retention expiry marker."""
    rows = connection.execute(
        "SELECT retention_class FROM artifacts WHERE LOWER(sha256) = ?",
        (sha256_lower,),
    ).fetchall()
    return any(
        isinstance(row["retention_class"], str) and row["retention_class"].startswith(RETENTION_EXPIRED_PREFIX)
        for row in rows
    )


def _acp_transcript_inline_body(
    value: Any,
    *,
    content_sha256: Any,
) -> tuple[str, str | None, str | None, str | None]:
    """Validate a legacy inline-only ACP body and return ``(state, reason, body, sha256)``."""
    if not isinstance(value, str):
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "body_missing", None, None
    try:
        encoded = value.encode("utf-8")
        _acp_json_bytes(value)
    except (UnicodeError, ValueError):
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "body_not_utf8", None, None
    computed_sha256 = hashlib.sha256(encoded).hexdigest()
    if content_sha256 is not None:
        if not isinstance(content_sha256, str) or not _ACP_SHA256_RE.fullmatch(content_sha256.lower()):
            return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "content_digest_mismatch", None, None
        if computed_sha256 != content_sha256.lower():
            return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "content_digest_mismatch", None, None
    if len(encoded) > _ACP_TRANSCRIPT_MAX_BODY_BYTES:
        return _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL, "body_too_large", None, computed_sha256
    return _ACP_TRANSCRIPT_STATE_OK, None, value, computed_sha256


def _read_acp_artifact_body(
    connection: SQLiteConnection,
    plane_root: Path,
    artifact_id: str,
    *,
    content_sha256: Any,
) -> tuple[str, str | None, str | None, str | None]:
    """Read an integrity-checked CAS body, withholding expired blobs and handling the mark/unlink race."""
    artifact_row = connection.execute(
        "SELECT sha256, bytes, retention_class FROM artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    if artifact_row is None:
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "artifact_row_missing", None, None

    retention_class = artifact_row["retention_class"]
    if not isinstance(retention_class, str):
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "artifact_metadata_corrupt", None, None
    if retention_class.startswith(RETENTION_EXPIRED_PREFIX):
        return _ACP_TRANSCRIPT_STATE_BODY_EXPIRED, "body_expired", None, None

    raw_sha = artifact_row["sha256"]
    if not isinstance(raw_sha, str) or not _ACP_SHA256_RE.fullmatch(raw_sha.lower()):
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "artifact_metadata_corrupt", None, None
    sha256 = raw_sha.lower()

    if content_sha256 is not None and (not isinstance(content_sha256, str) or content_sha256.lower() != sha256):
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "content_digest_mismatch", None, None

    declared_bytes = artifact_row["bytes"]
    if isinstance(declared_bytes, bool) or not isinstance(declared_bytes, int) or declared_bytes < 0:
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "artifact_metadata_corrupt", None, None

    try:
        blob_fd = _open_acp_blob_no_follow(plane_root, sha256)
    except FileNotFoundError:
        if _acp_artifact_is_expired(connection, artifact_id):
            return _ACP_TRANSCRIPT_STATE_BODY_EXPIRED, "body_expired", None, sha256
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "blob_missing", None, sha256
    except _AcpBackendUnavailable:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
            return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "blob_not_regular_file", None, sha256
        raise _AcpBackendUnavailable("blob_store_unavailable") from None

    try:
        try:
            st = os.fstat(blob_fd)
        except OSError:
            raise _AcpBackendUnavailable("blob_store_unavailable") from None
        if not stat.S_ISREG(st.st_mode):
            return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "blob_not_regular_file", None, sha256
        if st.st_size != declared_bytes:
            return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "blob_digest_mismatch", None, sha256
        if declared_bytes > _ACP_TRANSCRIPT_MAX_BODY_BYTES:
            if _acp_artifact_is_expired(connection, artifact_id):
                return _ACP_TRANSCRIPT_STATE_BODY_EXPIRED, "body_expired", None, sha256
            return _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL, "body_too_large", None, sha256
        try:
            with os.fdopen(blob_fd, "rb") as handle:
                blob_fd = -1
                raw = handle.read(_ACP_TRANSCRIPT_MAX_BODY_BYTES + 1)
        except OSError:
            raise _AcpBackendUnavailable("blob_store_unavailable") from None
    finally:
        if blob_fd >= 0:
            os.close(blob_fd)

    if _acp_artifact_is_expired(connection, artifact_id):
        return _ACP_TRANSCRIPT_STATE_BODY_EXPIRED, "body_expired", None, sha256
    if len(raw) != declared_bytes or hashlib.sha256(raw).hexdigest() != sha256:
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "blob_digest_mismatch", None, sha256
    try:
        text = raw.decode("utf-8")
        _acp_json_bytes(text)
    except (UnicodeError, ValueError):
        return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, "body_not_utf8", None, sha256
    return _ACP_TRANSCRIPT_STATE_OK, None, text, sha256


def _acp_transcript_payload(
    conversation_id: str,
    entries: list[dict[str, Any]],
    *,
    truncated: bool,
) -> dict[str, Any]:
    """Assemble one HTTP 200 ACP transcript payload with its derived top-level state."""
    transcript_state, transcript_reason = _acp_transcript_summary_state(entries, truncated=truncated)
    return {
        "conversation_id": conversation_id,
        "state": transcript_state,
        "label": _ACP_TRANSCRIPT_STATE_LABELS[transcript_state],
        "reason": transcript_reason,
        "truncated": truncated,
        "messages": entries,
    }


def _acp_transcript_entries(
    connection: SQLiteConnection,
    plane_root: Path,
    conversation_id: str,
    participants: tuple[str, ...],
) -> tuple[list[dict[str, Any]], bool]:
    """Read a bounded, allowlisted ACP transcript with an explicit state for every turn.

    Response-cap contract (``_ACP_TRANSCRIPT_MAX_RESPONSE_BYTES``, 512 KiB): after finalizing
    retention over all candidate turns in one snapshot, if the all-``ok`` serialized HTTP
    response exceeds the cap, reserve for every not-yet-decided surviving turn the size of its
    smaller serialized representation (``ok`` when its serialized ``ok`` entry fits within its
    ``oversized_or_partial`` entry, else ``oversized_or_partial``), then walk surviving turns
    in transcript order and admit each turn as ``ok`` if the exact serialized HTTP response
    with that choice plus the reserved minimum of all remaining turns stays within the cap;
    otherwise mark it ``oversized_or_partial`` (``reason="response_budget_exceeded"``,
    ``body=None``). No turn is dropped and the serialized response stays within the cap.
    """
    rows = connection.execute(
        """
        SELECT messages.message_id, messages.kind, messages.sender, messages.recipient,
               messages.body_inline, messages.body_artifact_id, messages.content_sha256,
               messages.created_at, event_round.round, event_round.round_invalid
        FROM (
            SELECT message_id, MIN(sequence) AS message_sequence,
                   CASE
                       WHEN COUNT(round) = 0 THEN NULL
                       WHEN COUNT(round) = COUNT(*)
                            AND SUM(CASE WHEN typeof(round) != 'integer' OR round < 1 THEN 1 ELSE 0 END) = 0
                            AND MIN(round) = MAX(round)
                       THEN MIN(round)
                   END AS round,
                   CASE
                       WHEN COUNT(round) = 0 THEN 0
                       WHEN COUNT(round) = COUNT(*)
                            AND SUM(CASE WHEN typeof(round) != 'integer' OR round < 1 THEN 1 ELSE 0 END) = 0
                            AND MIN(round) = MAX(round)
                       THEN 0
                       ELSE 1
                   END AS round_invalid
            FROM acp_conversation_events
            WHERE conversation_id = ? AND message_id IS NOT NULL
            GROUP BY message_id
        ) AS event_round
        LEFT JOIN comms_messages AS messages
               ON messages.message_id = event_round.message_id
              AND messages.conversation_id = ?
        ORDER BY event_round.message_sequence ASC
        LIMIT ?
        """,
        (conversation_id, conversation_id, _ACP_TRANSCRIPT_MAX_MESSAGES + 1),
    ).fetchall()

    truncated = len(rows) > _ACP_TRANSCRIPT_MAX_MESSAGES
    rows = rows[:_ACP_TRANSCRIPT_MAX_MESSAGES]

    allowed_lanes = frozenset({"root", "codex", *participants})

    entries: list[dict[str, Any]] = []
    candidates: list[tuple[int, str, str | None, str | None]] = []

    for index, row in enumerate(rows, start=1):
        round_invalid = bool(row["round_invalid"])
        round_number = None if round_invalid else _acp_int(row["round"], minimum=1)
        if not round_invalid and row["round"] is not None and round_number is None:
            round_invalid = True
        artifact_id: str | None = None
        digest_lower: str | None = None
        if row["message_id"] is None:
            kind = sender = recipient = created_at = None
            state = _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT
            reason: str | None = "message_row_missing"
            body: str | None = None
        else:
            kind = _acp_text(row["kind"], allowed=_ACP_TRANSCRIPT_KINDS)
            sender = _acp_text(row["sender"], allowed=allowed_lanes)
            recipient = _acp_text(row["recipient"], allowed=allowed_lanes)
            created_at = _acp_timestamp(row["created_at"])
            if round_invalid or None in (kind, sender, recipient, created_at):
                state = _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT
                reason = "malformed_turn_metadata"
                body = None
            else:
                raw_artifact_id = row["body_artifact_id"]
                artifact_ref_error: str | None = None
                if raw_artifact_id is not None:
                    if isinstance(raw_artifact_id, str) and raw_artifact_id.strip():
                        artifact_id = raw_artifact_id
                    else:
                        artifact_ref_error = "malformed_artifact_reference"
                else:
                    linked = connection.execute(
                        """SELECT artifact_id FROM message_artifacts
                           WHERE message_id = ? AND relation = 'body'
                           ORDER BY artifact_id ASC LIMIT 2""",
                        (row["message_id"],),
                    ).fetchall()
                    if len(linked) == 1:
                        linked_id = linked[0]["artifact_id"]
                        if isinstance(linked_id, str) and linked_id.strip():
                            artifact_id = linked_id
                        else:
                            artifact_ref_error = "malformed_artifact_reference"
                    elif len(linked) > 1:
                        artifact_ref_error = "malformed_artifact_reference"

                raw_digest = row["content_sha256"]
                if isinstance(raw_digest, str) and _ACP_SHA256_RE.fullmatch(raw_digest.lower()):
                    digest_lower = raw_digest.lower()

                if artifact_ref_error is not None:
                    state = _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT
                    reason = artifact_ref_error
                    body = None
                elif artifact_id is not None:
                    state, reason, body, artifact_sha256 = _read_acp_artifact_body(
                        connection,
                        plane_root,
                        artifact_id,
                        content_sha256=row["content_sha256"],
                    )
                    if artifact_sha256 is not None:
                        digest_lower = artifact_sha256
                elif digest_lower is not None and _acp_digest_is_expired(connection, digest_lower):
                    state = _ACP_TRANSCRIPT_STATE_BODY_EXPIRED
                    reason = "body_expired"
                    body = None
                else:
                    state, reason, body, inline_sha256 = _acp_transcript_inline_body(
                        row["body_inline"],
                        content_sha256=row["content_sha256"],
                    )
                    if inline_sha256 is not None:
                        digest_lower = inline_sha256
                        if _acp_digest_is_expired(connection, inline_sha256):
                            state = _ACP_TRANSCRIPT_STATE_BODY_EXPIRED
                            reason = "body_expired"
                            body = None

        entry: dict[str, Any] = {
            "ordinal": index,
            "state": state,
            "label": _ACP_TRANSCRIPT_STATE_LABELS[state],
            "reason": reason,
            "kind": kind,
            "sender": sender,
            "recipient": recipient,
            "created_at": created_at,
            "body": body,
        }
        if round_number is not None:
            entry["round"] = round_number
        entries.append(entry)
        if state == _ACP_TRANSCRIPT_STATE_OK and body is not None:
            candidates.append((len(entries) - 1, body, artifact_id, digest_lower))

    # Recheck retention for ALL candidate turns (both CAS artifact IDs and inline digests,
    # including legacy inline rows whose digest was derived from validated UTF-8 bytes) in ONE
    # single SQLite SELECT snapshot after the last blob read finishes, BEFORE response budgeting.
    surviving: list[tuple[int, str]] = []
    if candidates:
        art_ids = sorted({art_id for _, _, art_id, _ in candidates if art_id is not None})
        digests = sorted({digest for _, _, _, digest in candidates if digest is not None})
        expired_art_ids: set[str] = set()
        expired_digests: set[str] = set()
        where_parts: list[str] = []
        params: list[str] = []
        if art_ids:
            where_parts.append(f"artifact_id IN ({','.join('?' for _ in art_ids)})")
            params.extend(art_ids)
        if digests:
            where_parts.append(f"LOWER(sha256) IN ({','.join('?' for _ in digests)})")
            params.extend(digests)
        if where_parts:
            for art_row in connection.execute(
                f"SELECT artifact_id, sha256, retention_class FROM artifacts WHERE {' OR '.join(where_parts)}",
                params,
            ).fetchall():
                klass = art_row["retention_class"]
                if isinstance(klass, str) and klass.startswith(RETENTION_EXPIRED_PREFIX):
                    expired_art_ids.add(str(art_row["artifact_id"]))
                    raw_sha = art_row["sha256"]
                    if isinstance(raw_sha, str):
                        expired_digests.add(raw_sha.lower())
        for entry_idx, text, art_id, digest_lower in candidates:
            if (art_id is not None and art_id in expired_art_ids) or (
                digest_lower is not None and digest_lower in expired_digests
            ):
                entries[entry_idx]["state"] = _ACP_TRANSCRIPT_STATE_BODY_EXPIRED
                entries[entry_idx]["label"] = _ACP_TRANSCRIPT_STATE_LABELS[_ACP_TRANSCRIPT_STATE_BODY_EXPIRED]
                entries[entry_idx]["reason"] = "body_expired"
                entries[entry_idx]["body"] = None
            else:
                surviving.append((entry_idx, text))

    # Budget surviving turns per the response-cap contract: reserve each undecided turn's smaller
    # serialized representation (ok when ok <= oversized_or_partial, else oversized_or_partial),
    # then walk in transcript order admitting ok whenever the exact serialized payload fits <= cap.
    if surviving and len(
        _acp_json_bytes(_acp_transcript_payload(conversation_id, entries, truncated=truncated))
    ) > _ACP_TRANSCRIPT_MAX_RESPONSE_BYTES:
        for entry_idx, text in surviving:
            entry = entries[entry_idx]
            ok_size = len(_acp_json_bytes(entry))
            entry["state"] = _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL
            entry["label"] = _ACP_TRANSCRIPT_STATE_LABELS[_ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL]
            entry["reason"] = "response_budget_exceeded"
            entry["body"] = None
            if ok_size <= len(_acp_json_bytes(entry)):
                entry["state"] = _ACP_TRANSCRIPT_STATE_OK
                entry["label"] = _ACP_TRANSCRIPT_STATE_LABELS[_ACP_TRANSCRIPT_STATE_OK]
                entry["reason"] = None
                entry["body"] = text
        for entry_idx, text in surviving:
            entry = entries[entry_idx]
            if entry["state"] == _ACP_TRANSCRIPT_STATE_OK:
                continue
            entry["state"] = _ACP_TRANSCRIPT_STATE_OK
            entry["label"] = _ACP_TRANSCRIPT_STATE_LABELS[_ACP_TRANSCRIPT_STATE_OK]
            entry["reason"] = None
            entry["body"] = text
            if (
                len(_acp_json_bytes(_acp_transcript_payload(conversation_id, entries, truncated=truncated)))
                > _ACP_TRANSCRIPT_MAX_RESPONSE_BYTES
            ):
                entry["state"] = _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL
                entry["label"] = _ACP_TRANSCRIPT_STATE_LABELS[
                    _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL
                ]
                entry["reason"] = "response_budget_exceeded"
                entry["body"] = None

    return entries, truncated


def _acp_transcript_summary_state(
    entries: list[dict[str, Any]],
    *,
    truncated: bool,
) -> tuple[str, str | None]:
    """Derive the transcript-level state and typed reason from its turns and truncation flag."""
    for entry in entries:
        if entry["state"] == _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT:
            return _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT, cast(str, entry["reason"])
    if truncated:
        return _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL, "message_limit_exceeded"
    for entry in entries:
        if entry["state"] == _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL:
            return _ACP_TRANSCRIPT_STATE_OVERSIZED_OR_PARTIAL, cast(str, entry["reason"])
    for entry in entries:
        if entry["state"] == _ACP_TRANSCRIPT_STATE_BODY_EXPIRED:
            return _ACP_TRANSCRIPT_STATE_BODY_EXPIRED, "body_expired"
    return _ACP_TRANSCRIPT_STATE_OK, None


def _acp_backend_unavailable_response(conversation_id: str | None, reason: str) -> tuple[int, dict[str, Any]]:
    return (
        503,
        {
            "conversation_id": conversation_id,
            "state": _ACP_TRANSCRIPT_STATE_BACKEND_UNAVAILABLE,
            "label": _ACP_TRANSCRIPT_STATE_LABELS[_ACP_TRANSCRIPT_STATE_BACKEND_UNAVAILABLE],
            "reason": reason,
            "detail": "ACP transcript store unavailable",
        },
    )


def get_acp_conversation_transcript(
    conversation_id: str, *, ctx: MonitorContext
) -> tuple[int, dict[str, Any]]:
    """Return ``(http_status, payload)`` for one bounded ACP transcript with explicit typed states."""
    safe_id = _acp_identifier(conversation_id)
    try:
        plane_root, connection = _open_acp_transcript_store(ctx)
    except _AcpBackendUnavailable as exc:
        return _acp_backend_unavailable_response(safe_id, exc.reason)
    try:
        if safe_id is None:
            return 404, {"detail": "Conversation not found", "reason": "invalid_conversation_id"}
        conversation = connection.execute(
            "SELECT participants_json FROM acp_conversations WHERE conversation_id = ?", (safe_id,)
        ).fetchone()
        if conversation is None:
            return 404, {"detail": "Conversation not found", "reason": "conversation_not_found"}
        participants, participant_error = _acp_transcript_participants(conversation["participants_json"])
        if participants is None:
            return (
                200,
                {
                    "conversation_id": safe_id,
                    "state": _ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT,
                    "label": _ACP_TRANSCRIPT_STATE_LABELS[_ACP_TRANSCRIPT_STATE_MISSING_OR_CORRUPT],
                    "reason": participant_error or "malformed_participants",
                    "truncated": False,
                    "messages": [],
                },
            )
        entries, truncated = _acp_transcript_entries(connection, plane_root, safe_id, participants)
    except _AcpBackendUnavailable as exc:
        return _acp_backend_unavailable_response(safe_id, exc.reason)
    except sqlite3.Error:
        return _acp_backend_unavailable_response(safe_id, "store_query_failed")
    finally:
        connection.close()

    return 200, _acp_transcript_payload(safe_id, entries, truncated=truncated)


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
    """Read ACP transcript content for the local UI with explicit states; never mutate storage."""
    no_store = {"Cache-Control": "no-store"}
    refusal = _acp_transcript_access_refusal(request)
    if refusal is not None:
        return JSONResponse(
            status_code=403,
            content={"detail": "Forbidden", "reason": refusal},
            headers=no_store,
        )
    status_code, payload = await asyncio.to_thread(
        get_acp_conversation_transcript, conversation_id, ctx=ctx
    )
    return JSONResponse(status_code=status_code, content=payload, headers=no_store)




@router.get("/routing")
async def runtime_routing(ctx: MonitorContext = Depends(get_ctx)) -> JSONResponse:
    """How work was routed: the last day's dispatch and review decisions, with per-lane counts.

    ``recent`` is one scan of the task records, served stale-while-revalidate.
    The store is listed on every request, and a listing fault makes ``recent``
    unknown even when a scan is cached. Before the deadline the request does no
    I/O: the store's path and the cache key come from the context alone. Listing
    the store and the scan run in one task under one deadline (routes answer
    within 1 s): past it, ``recent`` is ``pending`` and that task runs on, so a
    late listing still stores its scan. A result that is not a reading (unknown
    or pending) carries no counts, and its ``as_of`` is the time of the last
    stored reading (None when there is none). A reading's ``age_s`` and ``X-Snapshot-Age-S`` count from the
    scan's own ``as_of``. Rows carry task references, catalog models, pool labels
    and fixed words only: no paths, prompts or credentials.
    """
    # Path arithmetic only (no resolve): the deadline bounds every filesystem step, and a pending
    # answer can always find the last stored reading.
    tasks_dir = delegate_api._tasks_dir(ctx)
    key = f"runtime-routing-decisions{ctx_cache_scope(ctx)}"

    async def read() -> tuple[dict[str, Any], bool]:
        # The listing and the scan run in this one task. Past the deadline it runs on: a listing
        # that finishes late still starts the shared scan, which stores its value.
        _, store_fault = await asyncio.to_thread(_routing_listing, tasks_dir)
        if store_fault is not None:
            return {"status": "unknown", "reason": store_fault}, False
        value, _, stored = await cache_read_swr(
            key, ROUTING_TTL_S, functools.partial(routing_decisions, ctx),
            storable=lambda value: value.get("status") in {"ok", "partial"},
        )
        return value, stored

    task = asyncio.ensure_future(read())
    task.add_done_callback(lambda done: done.cancelled() or done.exception())
    try:
        decisions, stored = await asyncio.wait_for(asyncio.shield(task), ROUTING_WAIT_S)
    except TimeoutError:
        decisions, stored = {"status": "pending", "reason": "task_records_being_read"}, False
    if not stored:
        # Not a reading: "unknown since" the last stored reading, never since this attempt.
        last = cache_get_stored(key)
        last_as_of = last[0].get("as_of") if last is not None and isinstance(last[0], dict) else None
        decisions = {"status": decisions.get("status"), "reason": decisions.get("reason"), "as_of": last_as_of}
    scanned = _routing_started(decisions.get("as_of")) if stored else None
    # The scan's age counts from when it read the store, not from when its result was cached.
    age = round(max((datetime.now(UTC) - scanned).total_seconds(), 0.0), 3) if scanned is not None else None
    payload = {
        "pools": [{"pool": meter, "label": label} for meter, label in ROUTING_POOLS],
        "recent": {**decisions, "age_s": age},
    }
    return JSONResponse(content=payload, headers=panel_values.snapshot_headers(
        age, ROUTING_STALE_AFTER_S, unavailable=age is None,
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
