"""Count journal and dispatcher errors for a completed UTC day, locally."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryFile, mkstemp
from typing import Any

from lu_machinery import state_paths
from lu_machinery.api.opsec_scan import scan_text
from lu_machinery.common import json_depth
from lu_machinery.common.task_store_paths import require_not_direct_task_store_path
from lu_machinery.dispatcher import store
from lu_machinery.dispatcher.pool_selector import provider_error_events
from lu_machinery.secret_redactor import iter_url_matches, redact_text

_UNIT_FIELDS = "_SYSTEMD_USER_UNIT,_SYSTEMD_UNIT,_COMM,_UID,USER_UNIT,SYSLOG_IDENTIFIER"
_DAY_FIELDS = f"PRIORITY,_TRANSPORT,{_UNIT_FIELDS},MESSAGE,MESSAGE_ID"
# SD_MESSAGE_UNIT_FAILURE_RESULT, emitted when a unit enters the failed state.
# SD_MESSAGE_UNIT_FAILED describes a failed start job and can accompany this record.
_UNIT_FAILURE = "d9b373ed55a64feb8242e02dbe79a49c"
_JOURNAL_TIMEOUT_S = 300
_MICROSECOND = timedelta(microseconds=1)
_VERSION = "v1"
_SEVERITIES = ("error", "warning", "unparsed")
_TIMESTAMP = re.compile(
    r"(?<!\w)\d{4}-\d\d-\d\d(?:[Tt ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:[Zz]|[+-]\d\d:?\d\d| UTC)?)?(?!\w)"
    r"|(?<!\w)\d\d:\d\d:\d\d(?:[.,]\d+)?(?!\w)"
)
_UUID = re.compile(r"(?i)[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_UNIT_ID = re.compile(_UUID.pattern + r"$")
_ID_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_ISSUE = re.compile(r"#\d+\b")
_HEX = re.compile(r"(?i)\b(?:0x)?[0-9a-f]{7,64}\b")
_EMAIL_HOST = re.compile(r"@[A-Za-z0-9\ue000-\ue009.-]+")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w:]|\.\d)")
_IPV6 = re.compile(r"(?<![\w:])(?:\[[0-9a-fA-F:.]+(?:%[\w.-]+)?\](?::\d+)?"
                   r"|[0-9a-fA-F]*:[0-9a-fA-F:.]*:[0-9a-fA-F:.]*(?:%[\w.-]+)?)(?![\w:])")
_PATH = re.compile(r"""(?<![\w/])(?:~[\w\ue000-\ue009.-]*)?/[^\s<>"'`]+""")
_NUMBER = re.compile(r"(?<!\w)[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_REASON = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
# During normalization a replacement can be inside a URL or path that a
# later step removes. Keep markers out of those token delimiters until done.
_MARKERS = {name: chr(0xE000 + index) for index, name in enumerate(
    ("ts", "uuid", "task", "agent", "n", "hex", "url", "email", "addr", "path")
)}
_EXPAND_MARKERS = str.maketrans({value: f"<{name}>" for name, value in _MARKERS.items()})
_CLEAR_MARKERS = str.maketrans({value: "\ufffd" for value in _MARKERS.values()})
_EMAIL_NAME_PUNCTUATION = frozenset("_.!#$%&'*+/=?^`{|}~-" + "".join(_MARKERS.values()))


class DigestError(ValueError):
    """A refusal safe to display without source paths or payload text."""


def _address(match: re.Match[str]) -> str:
    value = match.group()
    if value.startswith("["):
        address = value[1:value.index("]")]
    elif match.re is _IPV4:
        address = value.partition(":")[0]
    else:
        address = value.rstrip(".")
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return value
    return _MARKERS["addr"] + (value[len(address):] if match.re is _IPV6 and not value.startswith("[") else "")


def _replace_emails(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    # Start at @ so a long run without a hostname is never retried at each
    # character. Backward scans stop at separators or already emitted text.
    for match in _EMAIL_HOST.finditer(text):
        host = match.group()[1:].rstrip(".")
        if "." not in host or not all(host.split(".")):
            continue
        start = match.start()
        while start > cursor:
            char = text[start - 1]
            if not char.isalnum() and char not in _EMAIL_NAME_PUNCTUATION:
                break
            start -= 1
        if start < match.start():
            parts.extend((text[cursor:start], _MARKERS["email"]))
            cursor = match.start() + 1 + len(host)
    parts.append(text[cursor:])
    return "".join(parts)


def normalise(message: str, task_ids: set[str], agent_names: set[str]) -> str:
    """Return the first-line v1 template with exact dispatcher identity replacement."""
    text = message.partition("\n")[0].partition("\r")[0].translate(_CLEAR_MARKERS)
    text = _TIMESTAMP.sub(_MARKERS["ts"], text)

    def replace_id(match: re.Match[str]) -> str:
        token = match.group()
        # Match against the original token so a UUID inside a known task ID
        # doesn't split that ID into two tokens.
        stem = token.rstrip(".")
        for candidate in (token, stem + "." if len(stem) < len(token) else token, stem):
            if candidate in task_ids:
                return _MARKERS["task"] + token[len(candidate):]
            if candidate in agent_names:
                return _MARKERS["agent"] + token[len(candidate):]
        return _UUID.sub(_MARKERS["uuid"], token)

    text = _ID_TOKEN.sub(replace_id, text)
    text = _ISSUE.sub("#" + _MARKERS["n"], text)
    text = _HEX.sub(_MARKERS["hex"], text)
    parts: list[str] = []
    cursor = 0
    for match in iter_url_matches(text):
        parts.extend((text[cursor:match.start()], _MARKERS["url"]))
        cursor = match.end()
    parts.append(text[cursor:])
    text = _replace_emails("".join(parts))
    text = _IPV4.sub(_address, _IPV6.sub(_address, text))
    text = _PATH.sub(_MARKERS["path"], text)
    text = _NUMBER.sub(_MARKERS["n"], text)
    return " ".join(text.translate(_EXPAND_MARKERS).split())[:200]


def signature(source: str, family: str, severity: str, template: str) -> str:
    return hashlib.sha256(f"{_VERSION}|{source}|{family}|{severity}|{template}".encode()).hexdigest()[:12]


def _safe_text(text: str) -> tuple[str, bool]:
    redacted = redact_text(text) or ""
    if findings := scan_text(redacted):
        return f"<withheld:opsec:{findings[0].kind}>", True
    return redacted, False


def _journal_rows(deadline: float, *args: str) -> Iterator[dict[str, Any]]:
    """Stream journalctl JSON and reject output from a failed or truncated read."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DigestError("journal: unavailable (timeout)")
    try:
        with TemporaryFile() as diagnostics, subprocess.Popen(
            ["journalctl", "--user", "--no-pager", "--output=json", *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=diagnostics,
        ) as process:
            assert process.stdout is not None
            expired = threading.Event()

            def stop() -> None:
                expired.set()
                process.kill()

            timer = threading.Timer(remaining, stop)
            timer.start()
            try:
                for line in process.stdout:
                    if not line.endswith(b"\n"):
                        raise DigestError("journal: unavailable (truncated_json)")
                    row = json_depth.loads(line.decode(), max_depth=8)
                    if not isinstance(row, dict):
                        raise DigestError("journal: unavailable (invalid_entry)")
                    yield row
                exit_code = process.wait()
            finally:
                timer.cancel()
                timer.join()
                if process.poll() is None:
                    process.kill()
            if expired.is_set():
                raise DigestError("journal: unavailable (timeout)")
            if exit_code or diagnostics.tell():
                raise DigestError("journal: unavailable (journalctl_refused)")
    except (OSError, RecursionError, UnicodeError, json.JSONDecodeError):
        raise DigestError("journal: unavailable (journalctl_or_json_error)") from None


def _realtime(row: dict) -> int:
    value = row.get("__REALTIME_TIMESTAMP")
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise DigestError("journal: unavailable (invalid_realtime_timestamp)")
    return int(value)


def _journal_head(deadline: float) -> int:
    oldest = list(_journal_rows(deadline, "--lines=+1", "--output-fields=PRIORITY"))
    if len(oldest) != 1:
        raise DigestError("journal: unavailable (no_readable_head)")
    return _realtime(oldest[0])


def _journal_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f UTC")


def _micros(value: datetime) -> int:
    return (value - datetime(1970, 1, 1, tzinfo=UTC)) // _MICROSECOND


def _priority(row: dict) -> int | None:
    value = row.get("PRIORITY")
    if isinstance(value, str) and value in "01234567" and len(value) == 1:
        return int(value)
    return None


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _unit_family(unit: str) -> str:
    unit = redact_text(unit) or ""
    base, dot, suffix = unit.rpartition(".")
    if dot and "@" in base:
        unit = f"{base.partition('@')[0]}@.{suffix}"
    elif dot:
        unit = f"{_UNIT_ID.sub('<id>', base)}.{suffix}"
    return _safe_text(unit)[0]


class Digest:
    def __init__(self, records: list[dict]) -> None:
        self.task_ids = {_text(record, "task_id") for record in records} - {""}
        self.agent_names = {_text(record, "agent") for record in records} - {""}
        self.kinds: Counter[tuple[str, tuple[str, ...], str, str]] = Counter()
        self.journal: dict[tuple[str, bool], Counter[str]] = {}
        self.workers: dict[tuple[str, str], Counter[str]] = {}
        self.withheld = 0

    def add(self, source: str, group: tuple[str, ...], severity: str, message: str, *, literal: bool = False) -> None:
        template = message
        if not literal:
            first_line = message.partition("\n")[0].partition("\r")[0]
            # The redactor needs intact credentials before normalise replaces
            # numeric fields and clips the rendered template.
            template = normalise(redact_text(first_line) or "", self.task_ids, self.agent_names)
        template, withheld = _safe_text(template)
        self.withheld += int(withheld)
        self.kinds[source, group, severity, template] += 1

    def collect_journal(self, start: datetime, end: datetime) -> int:
        deadline = time.monotonic() + _JOURNAL_TIMEOUT_S
        first, stop = _micros(start), _micros(end)
        if _journal_head(deadline) > first:
            raise DigestError("journal: partial (head_after_window_start)")

        tagged = set()
        for row in _journal_rows(
            deadline, f"--until={_journal_time(start - _MICROSECOND)}",
            "--output-fields=PRIORITY,_TRANSPORT,_SYSTEMD_USER_UNIT",
            "_TRANSPORT=stdout", *(f"PRIORITY={n}" for n in range(8) if n != 6),
        ):
            if row.get("_TRANSPORT") == "stdout" and _priority(row) not in (None, 6):
                tagged.add(_text(row, "_SYSTEMD_USER_UNIT"))

        units_seen: dict[str, set[str]] = {}
        unit_families: dict[str, str] = {}
        identifiers: dict[str, str] = {}
        uid = str(os.getuid())
        manager = f"user@{uid}.service"
        entries = 0
        for row in _journal_rows(
            deadline, f"--since={_journal_time(start)}", f"--until={_journal_time(end - _MICROSECOND)}",
            f"--output-fields={_DAY_FIELDS}",
        ):
            if not first <= _realtime(row) < stop:
                raise DigestError("journal: unavailable (entry_outside_window)")
            entries += 1
            priority = _priority(row)
            emitter = _text(row, "_SYSTEMD_USER_UNIT")
            if emitter and row.get("_TRANSPORT") == "stdout" and priority not in (None, 6):
                tagged.add(emitter)
            # USER_UNIT and MESSAGE_ID are supplied by the sender. Only the
            # user manager in its reserved scope can attribute other units.
            unit_failed = (
                row.get("MESSAGE_ID") == _UNIT_FAILURE
                and row.get("_TRANSPORT") == "journal"
                and emitter == "init.scope"
                and row.get("_COMM") == "systemd"
                and row.get("_SYSTEMD_UNIT") == manager
                and row.get("_UID") == uid
            )
            unit = (_text(row, "USER_UNIT") if unit_failed else "") or emitter
            if unit:
                if unit not in unit_families:
                    unit_families[unit] = _unit_family(unit)
                family = unit_families[unit]
                units_seen.setdefault(family, set()).add(unit)
            else:
                identifier = _text(row, "SYSLOG_IDENTIFIER") or "unknown"
                if identifier not in identifiers:
                    identifiers[identifier] = _safe_text("ident:" + identifier)[0]
                family = identifiers[identifier]
            key = (family, bool(unit))
            counts = self.journal.setdefault(key, Counter())
            if unit_failed:
                severity = "error"
            elif priority is None:
                severity = "unparsed"
            elif priority <= 4:
                severity = "warning" if priority == 4 else "error"
            else:
                continue
            counts[severity] += 1
            message = "unit-failed" if unit_failed else (_text(row, "MESSAGE") or "<unparsed-message>")
            self.add("journal", (family,), severity, message, literal=unit_failed)

        if _journal_head(deadline) > first:
            raise DigestError("journal: partial (head_moved_into_window)")
        self.unproven = {family for family, units in units_seen.items() if units - tagged}
        return entries

    def collect_tasks(self, records: list[dict], start: datetime, end: datetime) -> int:
        settled = 0
        for record in records:
            status = _text(record, "status")
            if status in store.ACTIVE:
                continue
            finished = store.parse_utc_timestamp(record.get("finished_at"))
            if status not in store.TERMINAL or finished is None:
                self.add("digest", (), "error", "digest/unreadable-record/invalid-lifecycle", literal=True)
                continue
            if not start <= finished < end:
                continue
            settled += 1
            group = tuple(_safe_text(_text(record, key) or "unknown")[0] for key in ("agent", "model"))
            agent, model = group
            counts = self.workers.setdefault((agent, model), Counter())
            if status != "done":
                reason = _text(record, "error").partition(":")[0]
                reason = reason if _REASON.fullmatch(reason) else "untyped"
                counts["error"] += 1
                self.add("task", group, "error", f"task/{status}/{reason}", literal=True)
            try:
                for _ in provider_error_events(record):
                    severity = "warning" if status == "done" else "error"
                    counts[severity] += 1
                    self.add("provider", group, severity, "provider-error", literal=True)
            except (OSError, ValueError):
                counts["error"] += 1
                self.add("digest", group, "error", "digest/unreadable-log/io-or-path", literal=True)
        return settled

    def report(self, day: date, journal_entries: int, tasks_settled: int | None) -> dict[str, Any]:
        kinds = []
        totals: Counter[str] = Counter()
        for (source, group, severity, template), count in sorted(self.kinds.items()):
            family = "/".join(group)
            kinds.append(dict(source=source, group=list(group), severity=severity, template=template, count=count,
                              sig=signature(source, family, severity, template)))
            totals[severity] += count
        units = []
        for (family, is_unit), counts in sorted(self.journal.items()):
            item: dict[str, Any] = {"family": family}
            unproven = is_unit and family in self.unproven
            if is_unit:
                item["coverage"] = "severity unproven" if unproven else "tagged"
            for severity in _SEVERITIES:
                if not unproven or counts[severity]:
                    item[severity] = counts[severity]
            units.append(item)
        return {
            "schema": f"error-digest.{_VERSION}",
            "day": day.isoformat(),
            "journal": {"entries": journal_entries, "units": units},
            "tasks": {"status": "unavailable"} if tasks_settled is None else {
                "status": "available",
                "settled": tasks_settled,
                "workers": [
                    dict(agent=agent, model=model, **{severity: counts[severity] for severity in ("error", "warning")})
                    for (agent, model), counts in sorted(self.workers.items())
                ],
            },
            "totals": {severity: totals[severity] for severity in _SEVERITIES},
            "withheld": self.withheld,
            "kinds": kinds,
        }


def build_report(day: date) -> dict[str, Any]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    task_store_error = None
    try:
        records = store.list_tasks(strict=True)
    except (OSError, ValueError) as exc:
        records = []
        task_store_error = type(exc).__name__
    digest = Digest(records)
    if task_store_error:
        digest.add("digest", (), "error", f"digest/task-store-unreadable/{task_store_error}", literal=True)
    journal_entries = digest.collect_journal(start, end)
    tasks_settled = None if task_store_error else digest.collect_tasks(records, start, end)
    return digest.report(day, journal_entries, tasks_settled)


def persisted_payload(day: date, directory: Path) -> bytes:
    path = directory / f"error-digest-{day.isoformat()}.json"
    require_not_direct_task_store_path(path, label="error digest")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    payload = json.dumps(build_report(day), sort_keys=True, indent=2).encode() + b"\n"
    if scan_text(payload.decode()):
        raise DigestError("report: refused (opsec_scan)")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = mkstemp(dir=directory, prefix=".error-digest-")
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        with suppress(FileExistsError):
            os.link(temporary_path, path)
    finally:
        os.unlink(temporary_path)
    return path.read_bytes()


def main(argv: list[str] | None = None) -> int:
    today = datetime.now(UTC).date()
    parser = argparse.ArgumentParser(
        description="Persist redacted journal and dispatcher counts for a completed UTC day.\n"
                    "Use this command to prepare or replay a local daily report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  .venv/bin/python -m lu_machinery.ops.error_digest\n"
               "  .venv/bin/python -m lu_machinery.ops.error_digest --day 2025-02-01 --report-dir ./reports\n"
               "Outputs: error-digest-<UTC day>.json in the report directory.\n"
               "Exit codes: 0 = report ready, 1 = journal or report unavailable, 2 = invalid usage.\n"
               "Related: lu_machinery.orchestration.issue_quality_report.",
    )
    parser.add_argument("--day", type=date.fromisoformat, default=today - timedelta(days=1),
                        help="UTC day, YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--report-dir", type=Path,
                        help="directory path, for example ./reports (default: configured state reports)")
    args = parser.parse_args(argv)
    if not date.min < args.day < today:
        parser.error("--day must be a completed, representable UTC day")
    try:
        persisted_payload(args.day, args.report_dir or state_paths.reports_dir())
    except DigestError as exc:
        print(f"error digest: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("error digest: report unavailable (state_or_io_error)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
