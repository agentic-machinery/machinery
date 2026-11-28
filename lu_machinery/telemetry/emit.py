"""Best-effort local JSONL telemetry emitters.

File-only: no network calls, no API posts. ``emit_event`` writes the legacy v1
stream (``batch_state/telemetry/events/YYYY-MM-DD.jsonl``); ``emit_settlement``
writes the bounded, visibility-partitioned ``telemetry-event.v2`` log under
``batch_state/telemetry/events/{public,private}/`` (plan of record TRACETEL-1128).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping
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
EVENT_TYPES = frozenset({SETTLEMENT_EVENT})
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


def _field_ok(name: str, value: object) -> bool:
    if name == "schema":
        return value == EVENT_SCHEMA_V2
    if name == "event_type":
        return value in EVENT_TYPES
    if name == "ts":
        return isinstance(value, str) and bool(_UTC_TS.match(value)) and _parses(value)
    if name == "visibility":
        return value in SCOPES
    if name == "task_id":
        return isinstance(value, str) and bool(_TASK_ID.match(value))
    if name == "run_nonce":
        return isinstance(value, str) and bool(_RUN_NONCE.match(value))
    if name == "status":
        return isinstance(value, str) and value in _terminal_statuses()
    if value is None:
        return name in _OPTIONAL_FIELDS
    if name == "never_started_reason":
        return isinstance(value, str) and value in _start_refusals()
    if name in {"agent", "model", "effort", "mode", "repo"}:
        return isinstance(value, str) and bool(_LABEL_VALUE.match(value))
    if name == "duration_s":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    if name == "pr":
        return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**31
    if name == "pinned_head":
        return isinstance(value, str) and bool(_SHA.match(value))
    if name == "invalid_fields":
        return (isinstance(value, list) and len(value) <= len(_OPTIONAL_FIELDS)
                and all(item in _OPTIONAL_FIELDS for item in value))
    return False


def _parses(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


_REQUIRED_FIELDS = ("schema", "event_type", "ts", "visibility", "task_id", "run_nonce", "status")
_OPTIONAL_FIELDS = ("never_started_reason", "agent", "model", "effort", "mode", "repo", "duration_s", "pr",
                    "pinned_head", "invalid_fields")
ALLOWED_FIELDS = frozenset((*_REQUIRED_FIELDS, *_OPTIONAL_FIELDS))


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
    if any(not isinstance(key, str) or key not in ALLOWED_FIELDS for key in event):
        return "unallowlisted_key"
    if any(name not in event for name in _REQUIRED_FIELDS):
        return "missing_field"
    for name, value in event.items():
        if not _field_ok(name, value):
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
    space: DiskSpace = disk(root.parent.parent)
    need = _reserve(space, limits) + cost
    if scope == PRIVATE:
        need += max(0, limits.scope_bytes - scope_bytes(root / PUBLIC)) + limits.slack_bytes
    return space.available >= need


def _open_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _ensure_dir(scope_dir: Path) -> None:
    """Create ``telemetry/events/<scope>`` below an existing batch-state directory; refuse any symlinked level."""
    scope_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for level in (scope_dir.parent.parent, scope_dir.parent, scope_dir):
        if level.is_symlink() or not level.is_dir():
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
