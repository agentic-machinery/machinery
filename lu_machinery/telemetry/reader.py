"""Bounded readers of the ``telemetry-event.v2`` log.

Two surfaces with different trust:

* :func:`public_snapshot` reads only ``events/public/`` and confirmed-public model-stats rows; it feeds the
  shared, unauthenticated ``/metrics`` scrape. It re-validates every line and re-classifies every record, so a
  private, unclassified, malformed or legacy v1 record never contributes.
* :func:`settlements` and :func:`trace` read either scope. They are internal-only: no route serves them until
  the caller-authorization boundary of plan Slice 4 exists.

Every read is bounded: only date-named regular segment files of the retained window, at most the scope cap in
bytes, and at most ``record_bytes`` per line. Settlement events are keyed by ``(task_id, run_nonce)`` and
deduplicated at read time (the store re-emits on every terminal write); the last line for a key wins.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from lu_machinery.common import json_depth
from lu_machinery.telemetry import emit

_log = logging.getLogger(__name__)
_EVENT_MAX_DEPTH = 3
_ROW_MAX_DEPTH = 32
_MAX_ROW_BYTES = 64 * 1024
_TAIL_CHUNK = 64 * 1024


def _decode(line: bytes, limits: emit.EventLogLimits) -> dict[str, Any] | None:
    if len(line) > limits.record_bytes or not line.endswith(b"\n"):
        return None
    try:
        event = json_depth.loads(line.decode("ascii"), max_depth=_EVENT_MAX_DEPTH,
                                 object_pairs_hook=emit.unique_members)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return event if emit.validate_event(event) is None else None


def _scope_lines(path: Path, limits: emit.EventLogLimits) -> Iterator[bytes]:
    """Lines of one segment, never more than the segment cap and never a line longer than one record."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return
        budget = limits.segment_bytes
        while budget > 0:
            line = stream.readline(min(limits.record_bytes + 1, budget))
            if not line:
                return
            budget -= len(line)
            if not line.endswith(b"\n"):  # longer than a record, or a torn tail: skip to the next newline
                while line and not line.endswith(b"\n") and budget > 0:
                    line = stream.readline(min(limits.record_bytes + 1, budget))
                    budget -= len(line)
                continue
            yield line


def _window(scope_dir: Path, limits: emit.EventLogLimits, today: datetime | None) -> list[tuple[str, Path, int]]:
    """The retained segments, oldest first, newest ``age_days`` and at most the scope cap of bytes."""
    day = (today or emit._utcnow()).astimezone(UTC)
    oldest = f"{day - timedelta(days=limits.age_days - 1):%Y-%m-%d}"
    kept = [item for item in emit.segments(scope_dir) if oldest <= item[0] <= f"{day:%Y-%m-%d}"]
    total, window = 0, []
    for item in reversed(kept):
        total += item[2]
        if total > limits.scope_bytes:
            break
        window.append(item)
    return window[::-1]


def _events(batch_state: Path, scope: str, limits: emit.EventLogLimits, today: datetime | None,
            repos: Mapping[str, Any] | None) -> Iterator[dict[str, Any]]:
    scope_dir = emit.checked_scope_dir(batch_state, scope)
    for _day, path, _size in _window(scope_dir, limits, today):
        for line in _scope_lines(path, limits):
            event = _decode(line, limits)
            if event is None or event["visibility"] != scope:
                continue
            if scope == emit.PUBLIC and emit.record_visibility(event, repos) != emit.PUBLIC:
                continue  # classified public when written, not now: fail closed
            yield event


def settlements(batch_state: Path, scope: str, *, limits: emit.EventLogLimits | None = None,
                today: datetime | None = None, repos: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """One settlement event per ``(task_id, run_nonce)`` of one scope, in first-seen order; last line wins.

    Internal-only for the private scope (plan Slice 4 owns caller authorization).
    """
    limits = limits or emit.DEFAULT_LIMITS
    if repos is None and scope == emit.PUBLIC:
        repos = _repos_or_empty()
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for event in _events(Path(batch_state), scope, limits, today, repos):
        if event["event_type"] == emit.SETTLEMENT_EVENT:
            by_key[(event["task_id"], event["run_nonce"])] = event
    return list(by_key.values())


def trace(batch_state: Path, task_id: str, *, limits: emit.EventLogLimits | None = None,
          today: datetime | None = None, repos: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Internal-only: every deduplicated settlement of ``task_id`` across both scopes, oldest event first."""
    found = [
        event
        for scope in emit.SCOPES
        for event in settlements(batch_state, scope, limits=limits, today=today, repos=repos)
        if event["task_id"] == task_id
    ]
    return sorted(found, key=lambda event: event["ts"])


def _repos_or_empty() -> Mapping[str, Any]:
    try:
        return emit.load_fleet_repos()
    except Exception:
        return {}  # an unreadable registry classifies nothing as public


@dataclass(frozen=True)
class PublicTelemetry:
    """The public-scope numbers the shared scrape may show. ``None`` means unknown, never zero."""

    events_last_ts: float | None
    independent_settlement_last_ts: float | None
    footprint_bytes: int
    emit_errors_total: int | None
    dropped_events_total: Mapping[str, int] | None = field(default=None)


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        return None
    return parsed.timestamp()


def _last_public_event_ts(batch_state: Path, limits: emit.EventLogLimits, today: datetime | None,
                          repos: Mapping[str, Any]) -> float | None:
    """The newest valid public event's time, reading the retained segments backwards in bounded chunks."""
    scope_dir = emit.checked_scope_dir(batch_state, emit.PUBLIC)
    for _day, path, size in reversed(_window(scope_dir, limits, today)):
        for line in _reversed_lines(path, size, limits):
            event = _decode(line, limits)
            if event is not None and event["visibility"] == emit.PUBLIC and \
                    emit.record_visibility(event, repos) == emit.PUBLIC:
                return _epoch(event["ts"])
    return None


def _reversed_lines(path: Path, size: int, limits: emit.EventLogLimits) -> Iterator[bytes]:
    """Newline-terminated lines of a segment, last first, from its newest ``segment_bytes`` at most.

    Bytes after the last newline (a torn append) and lines longer than one record are skipped, never joined.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return
        end = min(size, info.st_size)
        lower = max(0, end - limits.segment_bytes)
        pos, buf, discarding = end, b"", True
        while pos > lower:
            start = max(lower, pos - _TAIL_CHUNK)
            buf = os.pread(fd, pos - start, start) + buf
            pos = start
            if discarding:
                cut = buf.rfind(b"\n")
                if cut < 0:
                    buf = b"" if len(buf) > limits.record_bytes else buf
                    continue
                buf, discarding = buf[: cut + 1], False
            while (cut := buf.rfind(b"\n", 0, len(buf) - 1)) >= 0:
                yield buf[cut + 1:]
                buf = buf[: cut + 1]
            if len(buf) > limits.record_bytes:
                buf, discarding = b"", True  # over-long: resume at the end of the line before it
        if pos == 0 and buf and not discarding:
            yield buf
    finally:
        os.close(fd)


class _RowsCursor:
    """Incremental scan of ``model-stats/rows.jsonl`` for the newest confirmed-public settlement time.

    Each refresh reads only bytes appended since the last one, so the value depends on public rows alone (a
    private row is parsed, classified private and ignored); a replaced or truncated file or a changed registry
    restarts the scan.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._state: dict[str, tuple[tuple[int, int], int, float | None, tuple[Any, ...]]] = {}

    def last_public(self, path: Path, repos: Mapping[str, Any]) -> float | None:
        signature = tuple(sorted((key, repo.authorized_visibility, repo.slugs) for key, repo in repos.items()))
        try:
            rows_dir = path.parent.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(rows_dir.st_mode):  # a symlinked rows directory is never followed: unknown
            _log.warning("model-stats rows directory is not a plain directory; freshness reference unknown")
            return None
        with self._lock:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                self._state.pop(str(path), None)
                return None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    return None
                identity = (info.st_dev, info.st_ino)
                saved = self._state.get(str(path))
                offset, newest = 0, None
                if saved is not None and saved[0] == identity and saved[1] <= info.st_size and saved[3] == signature:
                    offset, newest = saved[1], saved[2]
                with os.fdopen(os.dup(fd), "rb") as stream:
                    stream.seek(offset)
                    while True:
                        line = stream.readline(_MAX_ROW_BYTES + 1)
                        if not line.endswith(b"\n"):
                            if len(line) > _MAX_ROW_BYTES:  # over-long: skip it whole
                                offset += len(line)
                                while line and not line.endswith(b"\n"):
                                    line = stream.readline(_MAX_ROW_BYTES + 1)
                                    offset += len(line)
                                continue
                            break  # end of file, or a row still being written: read it next time
                        offset += len(line)
                        when = _public_row_time(line, repos)
                        if when is not None and (newest is None or when > newest):
                            newest = when
                self._state[str(path)] = (identity, offset, newest, signature)
                return newest
            finally:
                os.close(fd)


def _public_row_time(line: bytes, repos: Mapping[str, Any]) -> float | None:
    try:
        row = json_depth.loads(line.decode("utf-8"), max_depth=_ROW_MAX_DEPTH, object_pairs_hook=emit.unique_members)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    from lu_machinery.api.model_stats_rows import ROW_SCHEMA  # lazy-ok: the API package is heavy

    if not isinstance(row, dict) or row.get("schema") != ROW_SCHEMA:  # a row is written only at settlement
        return None
    if row.get("visibility") != emit.PUBLIC:  # classified by the writer from the raw record; absent is private
        return None
    if emit.record_visibility(row, repos) != emit.PUBLIC:
        return None
    return _epoch(row.get("finished_at"))


_ROWS = _RowsCursor()


def model_stats_rows_path(batch_state: Path) -> Path:
    from lu_machinery.api.model_stats_rows import ROWS_DIR, ROWS_FILE  # lazy-ok: the API package is heavy

    return Path(batch_state) / ROWS_DIR / ROWS_FILE


def public_snapshot(batch_state: Path, *, limits: emit.EventLogLimits | None = None,
                    today: datetime | None = None, repos: Mapping[str, Any] | None = None) -> PublicTelemetry:
    """Public-scope freshness, footprint, error and drop numbers; nothing private, unclassified or legacy."""
    limits = limits or emit.DEFAULT_LIMITS
    batch_state = Path(batch_state)
    if repos is None:
        repos = _repos_or_empty()
    scope_dir = emit.checked_scope_dir(batch_state, emit.PUBLIC)
    counters = _public_counters(scope_dir)
    return PublicTelemetry(
        events_last_ts=_last_public_event_ts(batch_state, limits, today, repos),
        independent_settlement_last_ts=_ROWS.last_public(model_stats_rows_path(batch_state), repos) if repos else None,
        footprint_bytes=emit.scope_bytes(scope_dir),
        emit_errors_total=None if counters is None else counters["emit_errors"],
        dropped_events_total=None if counters is None else dict(counters["dropped"]),
    )


def _public_counters(scope_dir: Path) -> dict[str, Any] | None:
    path = scope_dir / emit.COUNTERS_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"dropped": dict.fromkeys(emit.DROP_REASONS, 0), "emit_errors": 0}
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return emit.parse_counters(os.pread(fd, emit.MAX_COUNTERS_BYTES + 1, 0))
    finally:
        os.close(fd)
