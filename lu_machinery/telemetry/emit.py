"""Best-effort local JSONL telemetry emitters.

File-only: no network calls, no API posts. ``emit_event`` writes the legacy v1
stream (``batch_state/telemetry/events/YYYY-MM-DD.jsonl``); ``emit_settlement``
writes the bounded, visibility-partitioned ``telemetry-event.v2`` log under
``batch_state/telemetry/events/{public,private}/`` (plan of record TRACETEL-1128).
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lu_machinery import state_paths
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import state_root
from lu_machinery.secret_redactor import redact_text, redact_value

SCHEMA_VERSION = 1
_DISABLED_ENV = "LU_TELEMETRY_DISABLED"
_EVENTS_DIR_ENV = "LU_TELEMETRY_EVENTS_DIR"
_RUN_ID_ENV = "LU_RUN_ID"
_SESSION_ID_ENV = "LU_SESSION_ID"
_SOURCE_ENV = "LU_TELEMETRY_SOURCE"
_RECURSION_GUARD_ENV = "LU_TELEMETRY_EMITTING"
_logger = logging.getLogger(__name__)
_emitting = False


def current_run_id() -> str:
    """Return the current run id, minting and exporting one if absent."""
    return _current_or_new_env_id(_RUN_ID_ENV, "run")


def current_session_id() -> str:
    """Return the current telemetry session id, minting and exporting one if absent."""
    return _current_or_new_env_id(_SESSION_ID_ENV, "session")


def emit_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: str | None = None,
) -> None:
    """Append one central telemetry event.

    Telemetry is best-effort observability. This function must never fail the
    caller, even on serialization errors, missing directories, permission
    errors, or accidental recursive emission.
    """
    global _emitting
    try:
        if os.environ.get(_DISABLED_ENV) == "1":
            return None
        if _emitting or os.environ.get(_RECURSION_GUARD_ENV) == "1":
            return None

        _emitting = True
        previous_guard = os.environ.get(_RECURSION_GUARD_ENV)
        os.environ[_RECURSION_GUARD_ENV] = "1"
        try:
            event = {
                "schema_version": SCHEMA_VERSION,
                "ts": datetime.now(UTC).isoformat(),
                "event_type": str(event_type),
                "run_id": str(run_id or current_run_id()),
                "session_id": current_session_id(),
                "source": os.environ.get(_SOURCE_ENV) or "local",
            }
            event.update(payload)
            event = redact_value(event)

            path = _event_file()
            line = (
                json.dumps(event, ensure_ascii=False, default=str) + "\n"
            ).encode("utf-8")
            _write_line(path, line)
        finally:
            if previous_guard is None:
                os.environ.pop(_RECURSION_GUARD_ENV, None)
            else:
                os.environ[_RECURSION_GUARD_ENV] = previous_guard
            _emitting = False
    except Exception as exc:  # pragma: no cover - degraded mode only
        safe_exc = redact_text(str(exc)) or ""
        _logger.debug(
            "failed to emit telemetry event: %s: %s",
            type(exc).__name__,
            safe_exc,
        )
    return None


def _current_or_new_env_id(env_name: str, prefix: str) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    value = f"{prefix}_{uuid.uuid4().hex}"
    os.environ[env_name] = value
    return value


def _event_dir() -> Path:
    override = os.environ.get(_EVENTS_DIR_ENV)
    path = Path(override) if override else state_paths.telemetry_events_dir(state_root())
    path.mkdir(parents=True, exist_ok=True)
    return path


def _event_file(day: datetime | None = None) -> Path:
    if day is None:
        day = datetime.now(UTC)
    return _event_dir() / f"{day:%Y-%m-%d}.jsonl"


def _write_line(path: Path, line: bytes) -> None:
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


# --- telemetry-event.v2: bounded, visibility-partitioned settlement events -------------------------------------
#
# A local JSONL format (not OTLP). Events live beside the task store that settled them, in
# ``<batch_state>/telemetry/events/{public,private}/YYYY-MM-DD.jsonl``; the v1 files directly under
# ``events/`` stay unread by v2 readers (they carry free fields such as ``cwd``).
#
# Isolation: each scope has its own segments, sub-cap, eviction and drop counters, so private writes never
# evict public segments or move public counters. Both scopes share the host free-space reserve; a private
# append additionally keeps back the space the public scope may still grow into, so a private append never
# turns a public append that would have fitted into a ``reserve`` drop.

EVENT_SCHEMA_V2 = "telemetry-event.v2"
COUNTERS_SCHEMA = "telemetry-counters.v1"
SETTLEMENT_EVENT = "task.settled"
REVIEW_EVENT = "review.round"
CI_EVENT = "ci.result"
MERGE_GROUP_EVENT = "merge_group.result"
EVENT_TYPES = frozenset({SETTLEMENT_EVENT, REVIEW_EVENT, CI_EVENT, MERGE_GROUP_EVENT})
PUBLIC = "public"
PRIVATE = "private"
SCOPES = (PUBLIC, PRIVATE)
DROP_REASONS = ("oversize", "invalid", "segment_full", "cap", "reserve", "write_error")
COUNTERS_FILE = "counters.json"
SEGMENT_NAME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\.jsonl\Z")

_KIB = 1024
_MIB = 1024 * _KIB
_GIB = 1024 * _MIB
MAX_COUNTERS_BYTES = 4 * _KIB
_COUNTERS_MAX_DEPTH = 3

_LABEL_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_RUN_NONCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_UTC_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00\Z")


@dataclass(frozen=True)
class EventLogLimits:
    """Bounds of the v2 event log (plan of record §3). Callers and tests pass their own; production uses defaults."""

    record_bytes: int = 4 * _KIB
    segment_bytes: int = 16 * _MIB
    scope_bytes: int = 64 * _MIB  # two scopes: a 128 MiB unified ceiling
    age_days: int = 14
    reserve_fraction: float = 0.10
    reserve_floor_bytes: int = 5 * _GIB
    block_bytes: int = 4 * _KIB  # allocation granularity assumed when estimating what a write consumes
    slack_bytes: int = 64 * _KIB  # public growth not counted in segment bytes (counters, directory blocks)


DEFAULT_LIMITS = EventLogLimits()
_os_write = os.write  # the one append call; a test seam for short writes


@dataclass(frozen=True)
class DiskSpace:
    available: int
    total: int


DiskProbe = Callable[[Path], DiskSpace]


def disk_space(path: Path) -> DiskSpace:
    """Free space an unprivileged writer can use, and the filesystem size."""
    vfs = os.statvfs(path)
    return DiskSpace(available=vfs.f_bavail * vfs.f_frsize, total=vfs.f_blocks * vfs.f_frsize)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def events_root(batch_state: Path) -> Path:
    """``<batch_state>/telemetry/events``: the v2 scopes are its ``public`` and ``private`` children."""
    return state_paths.telemetry_events_in(batch_state)


def _int_in(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _hex_id(value: object, pattern: re.Pattern[str]) -> bool:
    """A W3C trace or span id: lowercase hex of the exact width, never all zeros (an invalid id per the spec)."""
    return isinstance(value, str) and bool(pattern.fullmatch(value)) and value.strip("0") != ""


def _link_ok(value: object, keys: frozenset[str]) -> bool:
    return (isinstance(value, dict) and set(value) == keys
            and all(_field_ok(name, value[name]) and value[name] is not None for name in keys))


def _field_ok(name: str, value: object) -> bool:
    if name == "schema":
        return value == EVENT_SCHEMA_V2
    if name == "event_type":
        return value in EVENT_TYPES
    if name == "ts":
        return isinstance(value, str) and bool(_UTC_TS.match(value)) and _parses(value)
    if name == "visibility":
        return value in SCOPES
    if name in {"task_id", "parent_task_id"} and value is not None:
        return isinstance(value, str) and bool(_TASK_ID.match(value))
    if name in {"run_nonce", "parent_run_nonce"} and value is not None:
        return isinstance(value, str) and bool(_RUN_NONCE.match(value))
    if name == "status":
        return isinstance(value, str) and value in _terminal_statuses()
    if value is None:
        return name in _NULLABLE_FIELDS
    if name == "never_started_reason":
        return isinstance(value, str) and value in _start_refusals()
    if name in {"agent", "model", "effort", "mode", "repo"}:
        return isinstance(value, str) and bool(_LABEL_VALUE.match(value))
    if name == "duration_s":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    if name in {"pr", "blamed_pr"}:
        return _int_in(value, 1, 2**31 - 1)
    if name in {"pinned_head", "reviewed_sha", "pr_head_sha", "tested_sha", "head_sha"}:
        return isinstance(value, str) and bool(_SHA.match(value))
    if name == "trace_id":
        return _hex_id(value, TRACE_ID)
    if name in {"span_id", "parent_span_id"}:
        return _hex_id(value, SPAN_ID)
    if name == "review_round":
        return _int_in(value, 1, MAX_REVIEW_ROUND)
    if name == "verdict":
        return isinstance(value, str) and value in REVIEW_VERDICTS
    if name == "run_id":
        return _int_in(value, 1, 2**63 - 1)
    if name == "run_attempt":
        return _int_in(value, 1, MAX_RUN_ATTEMPT)
    if name == "failed_jobs":
        return _int_in(value, 0, MAX_JOBS)
    if name == "conclusion":
        return isinstance(value, str) and value in CI_CONCLUSIONS
    if name == "ci_binding":
        return isinstance(value, str) and value in CI_BINDINGS
    if name == "ownership":
        return isinstance(value, str) and value in OWNERSHIP_STATES
    if name == "attribution":
        return isinstance(value, str) and value in ATTRIBUTIONS
    if name == "receipt_state":
        return isinstance(value, str) and value in RECEIPT_STATES
    if name == "receipts":
        return (isinstance(value, list) and len(value) <= MAX_GROUP_LINKS
                and all(_link_ok(item, _RECEIPT_KEYS) for item in value))
    if name == "span_links":
        return (isinstance(value, list) and len(value) <= MAX_GROUP_LINKS
                and all(_link_ok(item, _SPAN_LINK_KEYS) for item in value))
    if name == "invalid_fields":
        return (isinstance(value, list) and len(value) <= len(_SETTLEMENT_OPTIONAL)
                and all(item in _SETTLEMENT_OPTIONAL for item in value))
    return False


def _parses(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


# W3C Trace Context: version-00 ``traceparent`` is ``00-<32 hex trace id>-<16 hex parent id>-<2 hex flags>``.
TRACEPARENT_ENV = "TRACEPARENT"
TRACE_ID = re.compile(r"[0-9a-f]{32}")
SPAN_ID = re.compile(r"[0-9a-f]{16}")
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
MAX_REVIEW_ROUND = 99
MAX_RUN_ATTEMPT = 1000
MAX_JOBS = 1000
MAX_GROUP_LINKS = 12  # with both lists full, one merge-group event still fits the 4 KiB record bound
REVIEW_VERDICTS = frozenset({"APPROVED", "CHANGES_REQUESTED", "BLOCKED"})  # record_cf_verdict.NORMALIZED
CI_CONCLUSIONS = frozenset({"success", "failure", "cancelled", "timed_out", "skipped", "neutral", "action_required",
                            "stale", "startup_failure", "pending", "missing", "unknown"})
# exact: the run tested the PR head and its jobs are that attempt's; stale_head: it tested another head (a rebase or
# push since); ambiguous: its jobs are not provably that attempt's, or several runs match; missing: no run found.
CI_BINDINGS = frozenset({"exact", "stale_head", "ambiguous", "missing"})
OWNERSHIP_STATES = frozenset({"single", "transferred", "unknown"})
ATTRIBUTIONS = frozenset({"pr", "candidate_only"})
RECEIPT_STATES = frozenset({"recorded", "missing", "mismatch", "oversize"})
_RECEIPT_KEYS = frozenset({"pr", "head_sha"})
_SPAN_LINK_KEYS = frozenset({"trace_id", "span_id", "pr", "head_sha"})

_COMMON_REQUIRED = ("schema", "event_type", "ts", "visibility")
_SETTLEMENT_OPTIONAL = ("never_started_reason", "agent", "model", "effort", "mode", "repo", "duration_s", "pr",
                        "pinned_head", "trace_id", "span_id", "invalid_fields")
# Required and optional members of each event type. A required member is present and never null; an optional
# one may be absent or null.
_SCHEMAS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    SETTLEMENT_EVENT: (("task_id", "run_nonce", "status"), _SETTLEMENT_OPTIONAL),
    REVIEW_EVENT: (("task_id", "run_nonce", "repo", "pr", "reviewed_sha", "review_round", "verdict"),
                   ("parent_task_id", "parent_run_nonce", "trace_id", "span_id", "parent_span_id")),
    CI_EVENT: (("repo", "pr", "pr_head_sha", "ci_binding", "conclusion", "ownership"),
               ("tested_sha", "run_id", "run_attempt", "failed_jobs", "trace_id", "span_id", "span_links")),
    MERGE_GROUP_EVENT: (("repo", "ci_binding", "conclusion", "attribution", "receipt_state"),
                        ("tested_sha", "run_id", "run_attempt", "blamed_pr", "failed_jobs", "receipts", "span_links")),
}
_NULLABLE_FIELDS = frozenset(name for _required, optional in _SCHEMAS.values() for name in optional)
# Settlement callers and tests read these names (Slice 2).
_REQUIRED_FIELDS = (*_COMMON_REQUIRED, *_SCHEMAS[SETTLEMENT_EVENT][0])
_OPTIONAL_FIELDS = _SETTLEMENT_OPTIONAL
ALLOWED_FIELDS = frozenset(name for required, optional in _SCHEMAS.values() for name in (*_COMMON_REQUIRED, *required,
                                                                                          *optional))


def _terminal_statuses() -> frozenset[str]:
    from lu_machinery.dispatcher.store import TERMINAL  # lazy-ok: the dispatcher store imports this module lazily

    return TERMINAL


def _start_refusals() -> frozenset[str]:
    from lu_machinery.dispatcher.types import StartRefusal  # lazy-ok: keeps emit import-light

    return frozenset(StartRefusal)


def validate_event(event: object) -> str | None:
    """None for a well-formed v2 event, else a typed refusal: only allowlisted keys with bounded values pass."""
    if not isinstance(event, dict):
        return "not_object"
    if any(not isinstance(key, str) for key in event):
        return "unallowlisted_key"
    schema = _SCHEMAS.get(event["event_type"]) if isinstance(event.get("event_type"), str) else None
    if schema is None:
        if any(key not in ALLOWED_FIELDS for key in event):
            return "unallowlisted_key"
        return "missing_field" if "event_type" not in event else "bad_event_type"
    required = (*_COMMON_REQUIRED, *schema[0])
    if any(key not in required and key not in schema[1] for key in event):
        return "unallowlisted_key"
    if any(name not in event for name in required):
        return "missing_field"
    for name, value in event.items():
        if not _field_ok(name, value) or (value is None and name in required):
            return f"bad_{name}"
    return None


def record_visibility(record: Mapping[str, Any], repos: Mapping[str, Any] | None = None) -> str:
    """``public`` only for a record bound to one registered public repository with no declared data class.

    Every other record is ``private``: no repository, an unknown or private one, conflicting declarations, a
    declared data class, or an unreadable registry (fail closed). ``repos`` defaults to the fleet registry.
    """
    if repos is None:
        try:
            repos = load_fleet_repos()
        except Exception:
            return PRIVATE
    raw_options = record.get("options")
    options: Mapping[str, Any] = raw_options if isinstance(raw_options, Mapping) else {}
    sources: tuple[Mapping[str, Any], ...] = (record, options)
    if any(source.get("data_class") is not None for source in sources):
        return PRIVATE
    by_slug = {slug.lower(): key for key, repo in repos.items() for slug in repo.slugs if slug.isascii()}
    keys: set[str] = set()
    for source in sources:
        value = source.get("repo")
        if value is not None:
            if not isinstance(value, str) or value not in repos:
                return PRIVATE
            keys.add(value)
        for field_name in ("repository", "repository_id"):
            slug = source.get(field_name)
            if slug is None:
                continue
            if not isinstance(slug, str) or not slug.isascii() or slug.strip().lower() not in by_slug:
                return PRIVATE
            keys.add(by_slug[slug.strip().lower()])
    if len(keys) != 1:
        return PRIVATE
    (key,) = keys
    return PUBLIC if repos[key].authorized_visibility == PUBLIC else PRIVATE


def load_fleet_repos() -> Mapping[str, Any]:
    from lu_machinery.orchestration.fleet_repos import load_fleet_repos as load  # lazy-ok

    return load()


def settlement_event(record: Mapping[str, Any], *, now: datetime | None = None,
                     repos: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The allowlisted settlement event of one terminal task record, keyed by ``(task_id, run_nonce)``.

    Optional fields that fail validation are written as null and named in ``invalid_fields``.
    """
    when = now or _utcnow()
    event: dict[str, Any] = {
        "schema": EVENT_SCHEMA_V2,
        "event_type": SETTLEMENT_EVENT,
        "ts": when.astimezone(UTC).isoformat(timespec="microseconds"),
        "visibility": record_visibility(record, repos),
        "task_id": record.get("task_id"),
        "run_nonce": record.get("run_nonce"),
        "status": record.get("status"),
    }
    invalid: list[str] = []
    for name in _OPTIONAL_FIELDS[:-1]:
        value = record.get(name)
        if name == "pr" and isinstance(value, str) and value.isascii() and value.isdigit():
            value = int(value)
        if value is not None and not _field_ok(name, value):
            invalid.append(name)
            value = None
        event[name] = value
    if invalid:
        event["invalid_fields"] = invalid
    if event["visibility"] == PUBLIC and inherited_linkage(record):
        event["trace_id"] = None  # an inherited trace may be a private parent's: never in the public scope
    return event


def _blocks(size: int, limits: EventLogLimits) -> int:
    return -(-size // limits.block_bytes) * limits.block_bytes


def _reserve(space: DiskSpace, limits: EventLogLimits) -> int:
    return max(int(space.total * limits.reserve_fraction), limits.reserve_floor_bytes)


def _regular_size(path: Path) -> int | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def segments(scope_dir: Path) -> list[tuple[str, Path, int]]:
    """``(day, path, size)`` of a scope's regular segment files, oldest first; anything else is ignored."""
    found = []
    try:
        entries = list(os.scandir(scope_dir))
    except FileNotFoundError:
        return []
    for entry in entries:
        match = SEGMENT_NAME.match(entry.name)
        if match and entry.is_file(follow_symlinks=False):
            found.append((match.group(1), Path(entry.path), entry.stat(follow_symlinks=False).st_size))
    return sorted(found)


def scope_bytes(scope_dir: Path) -> int:
    """Bytes of a scope's regular files (segments and counters): its footprint."""
    total = 0
    try:
        entries = list(os.scandir(scope_dir))
    except FileNotFoundError:
        return 0
    for entry in entries:
        if entry.is_file(follow_symlinks=False):
            total += entry.stat(follow_symlinks=False).st_size
    return total


def _admitted(root: Path, scope: str, cost: int, limits: EventLogLimits, disk: DiskProbe) -> bool:
    """Whether a write of ``cost`` bytes keeps the shared reserve; private writes also keep public headroom."""
    batch_state = root.parent.parent
    space: DiskSpace = disk(batch_state)
    need = _reserve(space, limits) + cost
    if scope == PRIVATE:
        public_used = scope_bytes(checked_scope_dir(batch_state, PUBLIC))
        need += max(0, limits.scope_bytes - public_used) + limits.slack_bytes
    return space.available >= need


def _open_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _levels(scope_dir: Path) -> tuple[Path, Path, Path]:
    """``telemetry``, ``telemetry/events`` and the scope directory, top down below the batch state."""
    return scope_dir.parent.parent, scope_dir.parent, scope_dir


def checked_scope_dir(batch_state: Path, scope: str) -> Path:
    """The scope directory, refusing (``OSError``) a symlink or non-directory at any existing level below the batch
    state. Levels are checked top down, so no check resolves through an unchecked ancestor; a missing level ends
    the walk (nothing exists below it)."""
    scope_dir = state_paths.telemetry_scope_dir(batch_state, scope)
    for level in _levels(scope_dir):
        try:
            info = level.lstat()
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"telemetry directory is not a plain directory: {level.name}")
    return scope_dir


def _ensure_dir(scope_dir: Path) -> None:
    """Create the telemetry levels one at a time, each only below a level already proven a plain directory."""
    for level in _levels(scope_dir):
        with contextlib.suppress(FileExistsError):
            level.mkdir(mode=0o700)
        if not stat.S_ISDIR(level.lstat().st_mode):
            raise OSError(f"telemetry directory is not a plain directory: {level.name}")


def _zero_counters() -> dict[str, Any]:
    return {"schema": COUNTERS_SCHEMA, "dropped": dict.fromkeys(DROP_REASONS, 0), "emit_errors": 0}


def _write_counters(path: Path, counters: dict[str, Any], fd: int | None = None) -> None:
    """Rewrite the counters in place (same file, same blocks): counting never allocates past its first write."""
    own = fd is None
    if fd is None:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("telemetry counters are not a regular file")
        data = json.dumps(counters, sort_keys=True, separators=(",", ":")).encode("ascii")
        os.pwrite(fd, data, 0)
        os.ftruncate(fd, len(data))
    finally:
        if own:
            os.close(fd)


def _read_counters(fd: int) -> dict[str, Any]:
    raw = os.pread(fd, MAX_COUNTERS_BYTES + 1, 0)
    if not raw:
        return _zero_counters()
    counters = parse_counters(raw)
    if counters is None:
        raise ValueError("telemetry counters damaged")
    return counters


def parse_counters(raw: bytes) -> dict[str, Any] | None:
    """The counters document, or None when it is damaged (never a silent zero)."""
    if len(raw) > MAX_COUNTERS_BYTES:
        return None
    try:
        data = json_depth.loads(raw.decode("ascii"), max_depth=_COUNTERS_MAX_DEPTH,
                                object_pairs_hook=unique_members)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("schema") != COUNTERS_SCHEMA or set(data) != {
            "schema", "dropped", "emit_errors"}:
        return None
    dropped, errors = data["dropped"], data["emit_errors"]
    if not isinstance(dropped, dict) or set(dropped) != set(DROP_REASONS):
        return None
    if any(not _count_ok(value) for value in (*dropped.values(), errors)):
        return None
    return data


def unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook refusing repeated member names: the decoder would silently keep only the last value."""
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("duplicate JSON object member")
    return members


def _count_ok(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bump(scope_dir: Path, scope: str, *, reason: str | None, limits: EventLogLimits, disk: DiskProbe) -> None:
    """Count one drop (``reason``) or one emit error (``None``) in the scope's own counters, in place.

    A missing counters file is created only when the reserve admits it, so counting never spends reserve space;
    an uncountable drop is logged instead.
    """
    path = scope_dir / COUNTERS_FILE
    root = scope_dir.parent
    checked_scope_dir(root.parent.parent, scope)
    if _regular_size(path) is None:
        missing = sum(1 for level in (root.parent, root, scope_dir, path) if not level.exists())
        if not _admitted(root, scope, limits.block_bytes * missing, limits, disk):
            _logger.warning("telemetry %s drop not counted (reserve): %s", scope, reason or "emit_error")
            return
        _ensure_dir(scope_dir)
    with _ScopeLock(scope_dir):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("telemetry counters are not a regular file")
            counters = _read_counters(fd)
            if reason is None:
                counters["emit_errors"] += 1
            else:
                counters["dropped"][reason] += 1
            _write_counters(path, counters, fd)
        finally:
            os.close(fd)


class _ScopeLock:
    """An exclusive ``flock`` on the scope directory itself (no lock file to allocate). Not reentrant."""

    def __init__(self, scope_dir: Path) -> None:
        self._dir = scope_dir
        self._fd: int | None = None

    def __enter__(self) -> None:
        import fcntl  # lazy-ok: POSIX only

        self._fd = _open_dir(self._dir)
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing releases the flock


def append_event(batch_state: Path, event: dict[str, Any], *, limits: EventLogLimits | None = None,
                 disk: DiskProbe | None = None, now: datetime | None = None) -> str:
    """Append one v2 event to its visibility scope; return ``written`` or the typed drop reason.

    Never raises: telemetry must not fail the settlement that emits it. An invalid event is dropped and counted
    in the private scope (it cannot be trusted to name its own scope).
    """
    limits = limits or DEFAULT_LIMITS
    disk = disk or disk_space
    root = events_root(batch_state)
    invalid = validate_event(event)
    scope = PRIVATE if invalid else event["visibility"]
    scope_dir = state_paths.telemetry_scope_dir(batch_state, scope)
    try:
        checked_scope_dir(batch_state, scope)
        if invalid:
            _bump(scope_dir, scope, reason="invalid", limits=limits, disk=disk)
            return "invalid"
        line = (json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
        if len(line) > limits.record_bytes:
            _bump(scope_dir, scope, reason="oversize", limits=limits, disk=disk)
            return "oversize"
        day = (now or _utcnow()).astimezone(UTC)
        segment = scope_dir / f"{day:%Y-%m-%d}.jsonl"
        counters = scope_dir / COUNTERS_FILE
        new_files = sum(1 for path in (root.parent, root, scope_dir, segment, counters) if not path.exists())
        cost = _blocks(len(line) + 1, limits) + limits.block_bytes * new_files
        if not _admitted(root, scope, cost, limits, disk):
            _bump(scope_dir, scope, reason="reserve", limits=limits, disk=disk)
            return "reserve"
        _ensure_dir(scope_dir)
        with _ScopeLock(scope_dir):
            result = _append_locked(scope_dir, segment, day, line, limits)
            if result == "written" and _regular_size(counters) is None:
                _write_counters(counters, _zero_counters())  # admitted with this append: later drops count in place
        if result != "written":
            _bump(scope_dir, scope, reason=result, limits=limits, disk=disk)
        return result
    except Exception as exc:
        _logger.warning("telemetry %s event not written: %s", scope, type(exc).__name__)
        try:
            _bump(scope_dir, scope, reason=None, limits=limits, disk=disk)
        except Exception as count_exc:  # pragma: no cover - degraded mode only
            _logger.warning("telemetry %s emit error not counted: %s", scope, type(count_exc).__name__)
        return "error"


def _append_locked(scope_dir: Path, segment: Path, day: datetime, line: bytes, limits: EventLogLimits) -> str:
    oldest_kept = f"{day - timedelta(days=limits.age_days - 1):%Y-%m-%d}"
    present = []
    for name, path, size in segments(scope_dir):
        if name < oldest_kept:
            path.unlink(missing_ok=True)  # aged out
        else:
            present.append((name, path, size))
    today = segment.name[: -len(".jsonl")]
    current = next((size for name, _path, size in present if name == today), 0)
    if current + len(line) + 1 > limits.segment_bytes:
        return "segment_full"
    used = sum(size for _name, _path, size in present)
    for name, path, size in present:  # oldest first; never today's segment
        if used + len(line) + 1 <= limits.scope_bytes or name >= today:
            break
        path.unlink(missing_ok=True)
        used -= size
    if used + len(line) + 1 > limits.scope_bytes:
        return "cap"
    fd = os.open(segment, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("telemetry segment is not a regular file")
        size = info.st_size
        # A torn earlier append left a fragment: start this record on its own line so readers skip only that.
        data = line if size == 0 or os.pread(fd, 1, size - 1) == b"\n" else b"\n" + line
        try:
            written = _os_write(fd, data)
        except OSError:
            written = -1
        if written != len(data):
            os.ftruncate(fd, size)  # never leave this append's own fragment behind
            return "write_error"
    finally:
        os.close(fd)
    return "written"


def emit_settlement(record: Mapping[str, Any], batch_state: Path) -> str:
    """Write the settlement event of a terminal task record into ``batch_state``'s event log. Never raises."""
    try:
        event = settlement_event(record)
    except Exception as exc:
        _logger.warning("telemetry settlement event not built: %s", type(exc).__name__)
        return "error"
    return append_event(batch_state, event)


# --- Trace identity (Slice 3) --------------------------------------------------------------------------------------
#
# A dispatch mints one W3C trace identity before anything of it exists; the hold, the record, both refusal paths and
# the supervisor carry it, and so does a provider launched straight from ``environment.child_env`` (a wrapper that
# rebuilds its own environment, such as the read-only AGY sandbox or a bound review's runtime, carries none:
# absent, never wrong; plan row 3 keeps provider spans local). A dispatch started inside a traced run joins that
# run's trace through the TRACEPARENT it inherited, which must be a valid version-00 traceparent: anything else
# refuses (fail closed).


def _new_hex(width: int) -> str:
    import secrets  # lazy-ok: only minting needs it

    while True:
        value = secrets.token_hex(width // 2)
        if value.strip("0"):
            return value


def mint_trace() -> dict[str, str]:
    """A fresh ``{trace_id, span_id}``: a new root trace with this dispatch as its span."""
    return {"trace_id": _new_hex(32), "span_id": _new_hex(16)}


def parse_traceparent(value: object) -> tuple[str, str]:
    """``(trace_id, parent span id)`` of a valid version-00 W3C traceparent; ``ValueError`` for anything else.

    Only version 00 with exactly its four lowercase-hex fields and non-zero ids passes: a future version, extra
    fields, uppercase hex, surrounding space and the all-zero ids the specification calls invalid all refuse.
    """
    match = _TRACEPARENT.fullmatch(value) if isinstance(value, str) else None
    if match is None or not _hex_id(match[1], TRACE_ID) or not _hex_id(match[2], SPAN_ID):
        raise ValueError("trace_context_invalid: TRACEPARENT is not a valid W3C version-00 traceparent")
    return match[1], match[2]


def join_inbound_trace(trace: dict[str, str], environ: Mapping[str, str]) -> None:
    """Join the trace an inherited ``TRACEPARENT`` names, in place; an invalid one raises ``ValueError``.

    The dispatch keeps its own new span id; the inherited span becomes its ``parent_span_id``. Without a
    ``TRACEPARENT`` the minted root trace stays. On refusal ``trace`` is unchanged, so the refusal carries it.
    """
    if TRACEPARENT_ENV not in environ:
        return
    trace_id, parent = parse_traceparent(environ[TRACEPARENT_ENV])
    trace.update(trace_id=trace_id, parent_span_id=parent)


def trace_of(record: Mapping[str, Any]) -> dict[str, str] | None:
    """The record's own valid ``{trace_id, span_id}``, or None when it has none or they are malformed."""
    trace_id, span_id = record.get("trace_id"), record.get("span_id")
    if _hex_id(trace_id, TRACE_ID) and _hex_id(span_id, SPAN_ID):
        return {"trace_id": str(trace_id), "span_id": str(span_id)}
    return None


def valid_span_id(value: object) -> bool:
    """True for a W3C span id: 16 lowercase hex digits, not all zero."""
    return _hex_id(value, SPAN_ID)


def traceparent(trace: Mapping[str, Any]) -> str:
    """The version-00 ``traceparent`` naming ``trace``'s span; ``ValueError`` when its ids are not valid."""
    valid = trace_of(trace)
    if valid is None:
        raise ValueError("trace_context_invalid: the task's trace identity is missing or malformed")
    return f"00-{valid['trace_id']}-{valid['span_id']}-01"


def check_traceparent(environ: Mapping[str, str], trace: Mapping[str, Any]) -> None:
    """Refuse (``ValueError``) unless ``environ``'s TRACEPARENT is valid and names exactly ``trace``'s span."""
    if parse_traceparent(environ.get(TRACEPARENT_ENV)) != (trace.get("trace_id"), trace.get("span_id")):
        raise ValueError("trace_context_invalid: TRACEPARENT does not name this task's trace span")


# --- Review-round, CI and merge-group events -----------------------------------------------------------------------
#
# Scope rule: an event in the public scope never carries cross-task linkage it cannot prove public. A record that
# names a parent (it joined an inherited trace) keeps its parent ids and trace id out of public events; they stay on
# the record and in private events. Join rule: every identity is matched exactly, never by PR number alone.


def inherited_linkage(source: Mapping[str, Any]) -> bool:
    """True when ``source`` names a parent task or span: its trace id may be another (possibly private) task's."""
    return any(source.get(name) is not None for name in ("parent_task_id", "parent_run_nonce", "parent_span_id"))


def _now_ts(now: datetime | None) -> str:
    return (now or _utcnow()).astimezone(UTC).isoformat(timespec="microseconds")


def _valid_or_none(name: str, value: object) -> object:
    return value if value is not None and _field_ok(name, value) else None


def review_round_event(record: Mapping[str, Any], *, pr: int, reviewed_sha: str, review_round: int, verdict: str,
                       now: datetime | None = None, repos: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The ``review.round`` event of one recorded verdict, bound to its review run and that run's trace.

    It names ``(parent_task_id, parent_run_nonce, reviewed_sha, review_round)``: the task whose change was reviewed,
    when the review record names it, the exact head and the round. In the public scope the parent ids and an
    inherited trace id are null (scope rule above). Optional members that fail their contract are null; required
    ones that fail make the event invalid, which the log drops and counts.
    """
    visibility = record_visibility(record, repos)
    event: dict[str, Any] = {
        "schema": EVENT_SCHEMA_V2, "event_type": REVIEW_EVENT, "ts": _now_ts(now), "visibility": visibility,
        "task_id": record.get("task_id"), "run_nonce": record.get("run_nonce"), "repo": record.get("repo"), "pr": pr,
        "reviewed_sha": reviewed_sha, "review_round": review_round, "verdict": verdict,
    }
    private = visibility != PUBLIC
    for name in ("parent_task_id", "parent_run_nonce", "parent_span_id"):
        event[name] = _valid_or_none(name, record.get(name)) if private else None
    trace = trace_of(record)
    event["span_id"] = trace["span_id"] if trace else None
    event["trace_id"] = trace["trace_id"] if trace and (private or not inherited_linkage(record)) else None
    return event


def _completed_conclusion(run: Mapping[str, Any]) -> str:
    """The run's own conclusion for its current attempt: ``pending`` until it completes, ``unknown`` if malformed."""
    if run.get("status") != "completed":
        return "pending"
    value = run.get("conclusion")
    return value if isinstance(value, str) and value in CI_CONCLUSIONS - {"pending", "missing", "unknown"} else "unknown"


def attempt_jobs_bound(run: Mapping[str, Any], jobs: object) -> bool:
    """True when every job is provably the run's own current attempt's (GitHub's attempt-specific jobs read).

    Each job names the run's id, its attempt and its head commit. The run-level jobs read lists the latest
    attempt's jobs without saying so; a rerun's earlier attempt, another run's jobs, or a job of another head
    never bind. An empty, oversized or unreadable list does not either.
    """
    run_id, attempt, head = run.get("id"), run.get("run_attempt"), run.get("head_sha")
    if not (_int_in(run_id, 1, 2**63 - 1) and _int_in(attempt, 1, MAX_RUN_ATTEMPT) and _field_ok("head_sha", head)):
        return False
    return (isinstance(jobs, list) and bool(jobs) and len(jobs) <= MAX_JOBS
            and all(isinstance(job, Mapping) and type(job.get("run_id")) is int and job["run_id"] == run_id
                    and type(job.get("run_attempt")) is int and job["run_attempt"] == attempt
                    and isinstance(job.get("head_sha"), str) and job["head_sha"] == head for job in jobs))


def _failed_jobs(jobs: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for job in jobs if isinstance(job.get("conclusion"), str)
               and job["conclusion"] in {"failure", "timed_out", "startup_failure"})


def _owner_links(owners: Sequence[Mapping[str, Any]], pr: int, head_sha: str, visibility: str) -> list[dict[str, Any]]:
    """Span links of the owners recorded for exactly ``(pr, head_sha)``; in the public scope, uninherited ones only.

    An owner is one task's ``{pr, head_sha, trace_id, span_id}`` (plus its parent fields, if any). An owner of
    another PR or another head is not this head's owner and is never relabelled as one.
    """
    links = []
    for owner in owners:
        if not isinstance(owner, Mapping) or owner.get("pr") != pr or owner.get("head_sha") != head_sha:
            continue
        valid = trace_of(owner)
        if valid is not None and (visibility != PUBLIC or not inherited_linkage(owner)):
            links.append({**valid, "pr": pr, "head_sha": head_sha})
    return links


def ci_result_event(*, repo: str, visibility: str, pr: int, pr_head_sha: str, runs: Sequence[Mapping[str, Any]],
                    tested_sha: str | None, jobs: object, owners: Sequence[Mapping[str, Any]],
                    now: datetime | None = None) -> dict[str, Any]:
    """The ``ci.result`` event joining a PR head to its CI run by ``(repo, pr_head_sha, tested_sha, run_id,
    run_attempt)``.

    ``runs`` are the PR workflow's runs found for the PR; a run's ``head_sha`` is the PR head it ran for.
    ``tested_sha`` is the commit that attempt checked out (a pull-request run tests GitHub's merge commit, not the
    head), from attempt-bound checkout evidence, or None when the caller has none. ``jobs`` are that attempt's own
    jobs; ``owners`` the recorded owners (``{pr, head_sha, trace_id, span_id}``) of PR heads.

    Binding: no run is ``missing``; several runs or a malformed run id are ``ambiguous``; a run for another head (a
    rebase or push since) is ``stale_head``; unbound jobs or no tested commit are ``ambiguous``; only a run for this
    head with its attempt's jobs and a tested commit is ``exact``. The conclusion is that one run's own for its
    current attempt when it ran for this head, so a cancellation stays visible; otherwise ``missing`` or
    ``unknown``, never a success. One owner of this exact head binds the event to its span; several (an ownership
    transfer) are span links instead; none is ``unknown``.
    """
    event: dict[str, Any] = {
        "schema": EVENT_SCHEMA_V2, "event_type": CI_EVENT, "ts": _now_ts(now), "visibility": visibility,
        "repo": repo, "pr": pr, "pr_head_sha": pr_head_sha,
    }
    run = runs[0] if len(runs) == 1 and isinstance(runs[0], Mapping) else None
    if not runs:
        event.update(ci_binding="missing", conclusion="missing")
    elif run is None or not _int_in(run.get("id"), 1, 2**63 - 1):
        event.update(ci_binding="ambiguous", conclusion="unknown")
    else:
        event.update(run_id=run["id"], run_attempt=_valid_or_none("run_attempt", run.get("run_attempt")),
                     tested_sha=_valid_or_none("tested_sha", tested_sha))
        if run.get("head_sha") != pr_head_sha:
            event.update(ci_binding="stale_head", conclusion="unknown")
        else:
            bound = attempt_jobs_bound(run, jobs) and event["tested_sha"] is not None
            event.update(ci_binding="exact" if bound else "ambiguous", conclusion=_completed_conclusion(run))
            if bound and isinstance(jobs, list):
                event["failed_jobs"] = _failed_jobs(jobs)
    owned = [owner for owner in owners
             if isinstance(owner, Mapping) and owner.get("pr") == pr and owner.get("head_sha") == pr_head_sha]
    links = _owner_links(owned, pr, pr_head_sha, visibility)
    if len(owned) == 1 and len(links) == 1:
        event.update(ownership="single", trace_id=links[0]["trace_id"], span_id=links[0]["span_id"])
    elif len(owned) > 1:
        event.update(ownership="transferred", span_links=links[:MAX_GROUP_LINKS])
    else:
        event["ownership"] = "unknown"
    return event


def merge_group_event(*, repo: str, visibility: str, run: Mapping[str, Any], jobs: object,
                      receipt: Mapping[str, Any] | None, owners: Sequence[Mapping[str, Any]] = (),
                      now: datetime | None = None) -> dict[str, Any]:
    """The ``merge_group.result`` event of one merge-group run attempt.

    ``receipt`` is the explicit record ``{tested_sha, entries: [{pr, head_sha}, ...]}`` binding each constituent PR
    head to the candidate commit; it counts only when its ``tested_sha`` is the run's own head commit (else
    ``mismatch``). Span links come only from it, for owners of exactly a receipt entry's ``(pr, head_sha)``. A
    failure is pinned on a PR (``attribution: "pr"``) only when that receipt names exactly one constituent and the
    attempt's own jobs read it; any other failure stays ``candidate_only`` and blames no PR. The conclusion is the
    run's own for its current attempt.
    """
    event: dict[str, Any] = {
        "schema": EVENT_SCHEMA_V2, "event_type": MERGE_GROUP_EVENT, "ts": _now_ts(now), "visibility": visibility,
        "repo": repo, "tested_sha": _valid_or_none("tested_sha", run.get("head_sha")),
        "run_id": _valid_or_none("run_id", run.get("id")), "run_attempt": _valid_or_none("run_attempt", run.get("run_attempt")),
    }
    bound = attempt_jobs_bound(run, jobs)  # False for a malformed run id, attempt or head: typed ambiguous
    event.update(ci_binding="exact" if bound else "ambiguous", conclusion=_completed_conclusion(run))
    if bound and isinstance(jobs, list):
        event["failed_jobs"] = _failed_jobs(jobs)
    entries: list[dict[str, Any]] | None = None
    state = "missing"
    raw = receipt.get("entries") if isinstance(receipt, Mapping) else None
    if isinstance(receipt, Mapping) and (event["tested_sha"] is None or receipt.get("tested_sha") != event["tested_sha"]):
        state = "mismatch"
    elif isinstance(raw, list) and raw:
        entries = [{"pr": item.get("pr"), "head_sha": item.get("head_sha")} for item in raw
                   if isinstance(item, Mapping) and set(item) == _RECEIPT_KEYS]
        pairs = {(item["pr"], item["head_sha"]) for item in entries if _link_ok(item, _RECEIPT_KEYS)}
        if len(entries) != len(raw) or len(pairs) != len(entries) or len({pr for pr, _ in pairs}) != len(pairs):
            entries = None  # malformed, repeated or conflicting entries prove no membership
        elif len(entries) > MAX_GROUP_LINKS:
            entries, state = None, "oversize"
        else:
            state = "recorded"
    if entries is None:
        event.update(receipt_state=state, receipts=None, span_links=None)
    else:
        links = [link for item in entries for link in _owner_links(owners, item["pr"], item["head_sha"], visibility)]
        event.update(receipt_state=state, receipts=entries, span_links=links[:MAX_GROUP_LINKS])
    if bound and entries is not None and len(entries) == 1 and event["conclusion"] == "failure":
        event.update(attribution="pr", blamed_pr=entries[0]["pr"])
    else:
        event.update(attribution="candidate_only", blamed_pr=None)
    return event


def task_trace_refusal(record: Mapping[str, Any], options: object, environ: Mapping[str, str]) -> str | None:
    """Why a supervisor must not launch this task's provider, or None (one check before every launch path).

    The record's trace and the options' copy (which ``child_env`` turns into the provider's TRACEPARENT) must agree:
    both absent (a task dispatched before traces existed) or both the same valid pair. Partial, malformed or
    conflicting identity refuses. A TRACEPARENT in ``environ`` must name exactly that span, and a task with no
    identity carries none; an absent one passes on no context (absent, never wrong).
    """
    copies = (record, options if isinstance(options, Mapping) else {})
    present = [any(name in copy for name in ("trace_id", "span_id")) for copy in copies]
    pairs = [trace_of(copy) for copy in copies]
    if any(present) and (not all(present) or None in pairs or pairs[0] != pairs[1]):
        return "trace_context_invalid: the task's trace identity is partial, malformed or inconsistent"
    if TRACEPARENT_ENV not in environ:
        return None
    if not any(present):
        return "trace_context_invalid: TRACEPARENT set for a task with no trace identity"
    try:
        check_traceparent(environ, record)
    except ValueError as exc:
        return str(exc)
    return None


def fleet_repo_of(slug: object, repos: Mapping[str, Any] | None = None) -> tuple[str | None, str]:
    """``(fleet key, visibility)`` of a GitHub ``owner/name`` slug; ``(None, "private")`` when it is unregistered."""
    if repos is None:
        try:
            repos = load_fleet_repos()
        except Exception:
            return None, PRIVATE
    if not isinstance(slug, str) or not slug.isascii():
        return None, PRIVATE
    keys = {key for key, repo in repos.items() for name in repo.slugs
            if isinstance(name, str) and name.isascii() and name.lower() == slug.strip().lower()}
    if len(keys) != 1:
        return None, PRIVATE
    (key,) = keys
    return key, record_visibility({"repo": key}, repos)


def emit_v2(event: Mapping[str, Any], batch_state: Path | None = None) -> str:
    """Append one built v2 event beside the task store (default: the configured one); never raises."""
    try:
        if batch_state is None:
            from lu_machinery.common.task_store_paths import tasks_dir  # lazy-ok: keeps emit import-light

            batch_state = tasks_dir().parent
        return append_event(batch_state, dict(event))
    except Exception as exc:
        _logger.warning("telemetry %s event not written: %s", event.get("event_type"), type(exc).__name__)
        return "error"
