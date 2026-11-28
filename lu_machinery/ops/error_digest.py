"""Summarize completed UTC days and publish redacted error digests."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
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
_EGRESS_TIMEOUT_S = 60
_MICROSECOND = timedelta(microseconds=1)
_VERSION = "v1"
_LEDGER = "error-digest-kinds.v1.json"
_PUBLISH_STATE = ".error-digest-publish.v2.json"
_REPORT_PREFIX = "error-digest-"
_TOP_KINDS = 10
_JOURNAL_HEADLINE_LIMIT = 140
_JOURNAL_BODY_LIMIT = 2000
_SIGNATURE = re.compile(r"[0-9a-f]{12}")
_PROPOSAL_KEY = re.compile(r"error-digest:([0-9]{4}-[0-9]{2}-[0-9]{2})")
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


class PartialJournalError(DigestError):
    """A journal window that can no longer be reconstructed."""


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

    def collect_journal(self, start: datetime, end: datetime, deadline: float) -> int:
        first, stop = _micros(start), _micros(end)
        if _journal_head(deadline) > first:
            raise PartialJournalError("journal: partial (head_after_window_start)")

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
            raise PartialJournalError("journal: partial (head_moved_into_window)")
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


def build_report(day: date, *, deadline: float | None = None) -> dict[str, Any]:
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
    if deadline is None:
        deadline = time.monotonic() + _JOURNAL_TIMEOUT_S
    journal_entries = digest.collect_journal(start, end, deadline)
    tasks_settled = None if task_store_error else digest.collect_tasks(records, start, end)
    return digest.report(day, journal_entries, tasks_settled)


def _report_path(directory: Path, day: date) -> Path:
    return directory / f"{_REPORT_PREFIX}{day.isoformat()}.json"


def persisted_payload(day: date, directory: Path, *, deadline: float | None = None) -> bytes:
    path = _report_path(directory, day)
    require_not_direct_task_store_path(path, label="error digest")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    payload = _json_bytes(build_report(day, deadline=deadline))
    if scan_text(payload.decode()):
        raise DigestError("report: refused (opsec_scan)")
    directory.mkdir(parents=True, exist_ok=True)
    _write_state(path, payload, replace=False)
    return path.read_bytes()


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"


def _write_state(path: Path, payload: bytes, *, replace: bool) -> None:
    require_not_direct_task_store_path(path, label="error digest")
    descriptor, temporary_path = mkstemp(dir=path.parent, prefix=".error-digest-")
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        if replace:
            os.replace(temporary_path, path)
        else:
            with suppress(FileExistsError):
                os.link(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary_path)


def _date_text(value: object) -> bool:
    return isinstance(value, str) and date.fromisoformat(value).isoformat() == value


def _ledger_row(row: object, *, proposed: bool) -> bool:
    return (
        isinstance(row, dict) and _date_text(row["first_seen"])
        and isinstance(row["template"], str) and isinstance(row["source"], str)
        and row["severity"] in _SEVERITIES
        and (not proposed or (
            _date_text(row["proposal_day"]) and store.parse_utc_timestamp(row["proposed_at"]) is not None
        ))
    )


def _load_ledger(path: Path) -> dict[str, Any]:
    """An absent ledger is empty. An invalid ledger blocks proposals."""
    try:
        records = json_depth.loads(path.read_text(), max_depth=3)
        if not isinstance(records, dict):
            raise ValueError
        for sig, row in records.items():
            if not _SIGNATURE.fullmatch(sig) or not _ledger_row(row, proposed=True):
                raise ValueError
    except FileNotFoundError:
        return {}
    except (KeyError, TypeError, ValueError, RecursionError):
        raise DigestError("ledger: unavailable (invalid_state)") from None
    return records


def _attempt_day(key: object) -> str:
    match = _PROPOSAL_KEY.fullmatch(key) if isinstance(key, str) else None
    if match is None or not _date_text(match[1]):
        raise ValueError("invalid proposal key")
    return match[1]


def _utf8_text(value: object) -> bool:
    """A non-empty string that encodes as UTF-8; a JSON string can hold a lone surrogate."""
    try:
        return isinstance(value, str) and bool(value.encode())
    except UnicodeEncodeError:
        return False


def _load_publish_state(path: Path) -> dict[str, Any]:
    """The pending proposal, kept until Fleet returns its receipt and the ledger records it."""
    try:
        state = json_depth.loads(path.read_text(), max_depth=4)
        if not isinstance(state, dict) or set(state) != {"attempt"}:
            raise ValueError
        attempt = state["attempt"]
        if attempt is not None and (
            not isinstance(attempt, dict) or set(attempt) != {"key", "sender", "body", "proposed"}
            or not _attempt_day(attempt["key"])
            or not _utf8_text(attempt["sender"]) or not _utf8_text(attempt["body"])
            or not isinstance(attempt["proposed"], dict) or not attempt["proposed"]
            or not all(
                _SIGNATURE.fullmatch(sig) and _ledger_row(row, proposed=False)
                for sig, row in attempt["proposed"].items()
            )
        ):
            raise ValueError
    except FileNotFoundError:
        return {"attempt": None}
    except (KeyError, TypeError, ValueError, RecursionError):
        raise DigestError("publish state: unavailable (invalid_state)") from None
    return state


def _deliver(state: dict, state_path: Path, ledger: dict, ledger_path: Path, deadline: float) -> DigestError | None:
    """Send the pending proposal, record its kinds, then clear it.

    The proposal keeps its key and body across runs. When an earlier send
    committed, Fleet returns that receipt and creates no second message.
    """
    attempt = state["attempt"]
    try:
        _publish_proposal(attempt["body"], attempt["sender"], attempt["key"], deadline - _EGRESS_TIMEOUT_S)
    except DigestError as exc:
        return exc
    proposed_at = datetime.now(UTC).isoformat()
    day = _attempt_day(attempt["key"])
    for sig, row in attempt["proposed"].items():
        ledger[sig] = {**row, "proposed_at": proposed_at, "proposal_day": day}
    _write_state(ledger_path, _json_bytes(ledger), replace=True)
    state["attempt"] = None
    _write_state(state_path, _json_bytes(state), replace=True)
    return None


def _pending_note(state: dict) -> str:
    if not (attempt := state.get("attempt")):
        return ""
    body_sha = hashlib.sha256(attempt["body"].encode()).hexdigest()
    return (
        f"Fleet proposal for {_attempt_day(attempt['key'])} not confirmed, body SHA-256 {body_sha}. "
        "The next run resends it under the same key."
    )


def _safe_output(text: str) -> str:
    clean = redact_text(text) or ""
    if scan_text(clean):
        raise DigestError("egress: refused (opsec_scan)")
    return clean


def _command(setting: str) -> str:
    value = os.environ.get(setting, "")
    if (
        value and value.strip() == value
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
        and (resolved := shutil.which(value))
    ):
        return resolved
    raise DigestError(f"egress: unavailable ({setting}_missing_or_invalid)")


def _send(command: list[str], target: str, deadline: float, **kwargs: Any) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DigestError(f"{target}: unavailable (timeout)")
    try:
        # Only the configured executables receive fixed argv and scanned data.
        subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            timeout=min(_EGRESS_TIMEOUT_S, remaining), **kwargs,
        )
    except subprocess.TimeoutExpired:
        raise DigestError(f"{target}: unavailable (timeout)") from None
    except (OSError, ValueError, subprocess.CalledProcessError):
        raise DigestError(f"{target}: unavailable (write_refused)") from None


def _post_journal(cli: str, day: date, headline: str, body: str, sender: str, deadline: float) -> None:
    _send(
        [cli, "journal", "add", "--project", "fleet", "--type", "note",
         "--headline", _safe_output(headline)[:_JOURNAL_HEADLINE_LIMIT],
         "--body", _safe_output(body)[:_JOURNAL_BODY_LIMIT],
         "--source-key", f"error-digest:{day}"],
        "journal write", deadline, env=dict(os.environ, LU_INBOX_WORKER=sender), stdin=subprocess.DEVNULL,
    )


def _cell(value: str) -> str:
    return " ".join(value.replace("\0", " ").split()).replace("|", r"\|")


def _rank(kinds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(kinds, key=lambda kind: (-kind["count"], kind["sig"]))


def _kind_line(kind: dict[str, Any]) -> str:
    group = " / ".join(kind["group"]) or kind["source"]
    return f'- {kind["sig"]} ({kind["count"]}): {_cell(group)} {_cell(kind["severity"])} - {_cell(kind["template"])}'


def _journal_summary(report: dict, new_count: int | None, banner: str) -> tuple[str, str]:
    day = report["day"]
    units = report["journal"]["units"]
    unproven = sum(unit.get("coverage") == "severity unproven" for unit in units)
    totals = report["totals"]
    kinds = report["kinds"]
    new = f"{new_count} new" if new_count is not None else "new kind count unknown"
    headline = (
        f'Error digest {day}: {totals["error"]} errors, {totals["warning"]} warnings, '
        f'{len(kinds)} kinds ({new}); {unproven} units unproven'
    )
    lines = [f"Full table: error-digest-{day}.json", f'Withheld by OPSEC scan: {report["withheld"]}.']
    if banner:
        lines.insert(0, banner)
    if report["tasks"]["status"] == "unavailable":
        lines.append("Task counts unavailable.")
    lines.extend(("", "Top kinds"))
    for kind in _rank(kinds)[:_TOP_KINDS]:
        line = _kind_line(kind)
        # Reserve space for the counts table even when templates are long.
        if len("\n".join([*lines, line])) > _JOURNAL_BODY_LIMIT * 3 // 4:
            break
        lines.append(line)
    lines.extend(("", "| Unit or worker | Errors | Warnings | Unparsed | Coverage |", "| --- | ---: | ---: | ---: | --- |"))
    rows = [(unit["family"], unit) for unit in units]
    rows.extend((f'{worker["agent"]} / {worker["model"]}', worker) for worker in report["tasks"].get("workers", []))
    for label, row in rows:
        columns = [str(row.get(severity, "-")) for severity in _SEVERITIES]
        line = f'| {_cell(label)} | {" | ".join(columns)} | {row.get("coverage", "-")} |'
        if len("\n".join([*lines, line])) > _JOURNAL_BODY_LIMIT:
            break
        lines.append(line)
    return headline, "\n".join(lines)


def _read_report(payload: bytes, day: date) -> dict:
    try:
        report = json_depth.loads(payload.decode(), max_depth=8)
        if report["schema"] != f"error-digest.{_VERSION}" or report["day"] != day.isoformat():
            raise ValueError
        if not isinstance(report["kinds"], list):
            raise ValueError
        for kind in report["kinds"]:
            if (
                not _SIGNATURE.fullmatch(kind["sig"]) or kind["severity"] not in _SEVERITIES
                or not isinstance(kind["group"], list) or not all(isinstance(part, str) for part in kind["group"])
                or not isinstance(kind["source"], str) or not isinstance(kind["template"], str)
                or type(kind["count"]) is not int or kind["count"] < 1
            ):
                raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError):
        raise DigestError("report: refused (invalid_state)") from None
    assert isinstance(report, dict)
    return report


def _saved_days(directory: Path, day: date) -> set[date]:
    days = set()
    for path in directory.glob(f"{_REPORT_PREFIX}????-??-??.json"):
        with suppress(ValueError):
            value = date.fromisoformat(path.stem.removeprefix(_REPORT_PREFIX))
            if date.min < value <= day and path == _report_path(directory, value):
                days.add(value)
    return days


def _backfill_start(day: date, saved: set[date], deadline: float) -> date:
    previous = max(saved - {day}, default=None)
    if day in saved and previous is None:
        return day
    if previous == day - timedelta(days=1):
        return previous
    try:
        start = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=_journal_head(deadline))
        first_full_day = start.date() + timedelta(days=int(start.time() != datetime.min.time()))
    except OverflowError:
        raise DigestError("journal: unavailable (invalid_realtime_timestamp)") from None
    return min(day, max(first_full_day, previous or first_full_day))


def _collect_for_publish(
    directory: Path, day: date, *, backfill: bool, ledger: dict, ledger_error: str,
    state: dict, recovered_day: date | None, cli: str, sender: str, deadline: float,
) -> tuple[list[dict[str, Any]], dict, int | None]:
    saved = _saved_days(directory, day)
    # Leave time for the last day's Fleet attempt and journal note.
    report_deadline = deadline - 2 * _EGRESS_TIMEOUT_S
    start = _backfill_start(day, saved, report_deadline) if backfill else day
    due = {start + timedelta(days=offset) for offset in range((day - start).days + 1)}
    if backfill and saved:
        # The last persisted report may have been followed by a failed journal write.
        due.add(max(saved - {day}, default=day))
    if recovered_day is not None and recovered_day in saved:
        due.add(recovered_day)
    candidates: dict[str, dict[str, Any]] = {}
    for current in sorted(saved | due):
        try:
            report = _read_report(persisted_payload(current, directory, deadline=report_deadline), current)
        except PartialJournalError as exc:
            if current in due:
                _post_journal(cli, current, f"Error digest {current}: journal partial", str(exc), sender, deadline)
            raise
        report_day = current.isoformat()
        new_sigs = set()
        for kind in report["kinds"]:
            sig = kind["sig"]
            if known := ledger.get(sig):
                # A Fleet success can precede a failed journal write. The
                # ledger preserves the original day for the retried note.
                if known["first_seen"] == report_day:
                    new_sigs.add(sig)
                continue
            if sig in candidates:
                candidates[sig]["count"] += kind["count"]
            else:
                candidates[sig] = {**kind, "first_seen": report_day}
                new_sigs.add(sig)
        count = None if ledger_error else len(new_sigs)
        if current in due and current != day:
            banner = "\n".join(filter(None, (ledger_error, _pending_note(state))))
            headline, body = _journal_summary(report, count, banner)
            _post_journal(cli, current, headline, body, sender, deadline)
    # The target is the last day in the collection. Its note includes the
    # outcome of this invocation's Fleet attempt.
    return _rank(list(candidates.values())), report, count


def _prepare_proposal(day: date, kinds: list[dict[str, Any]], max_new: int) -> tuple[str, dict[str, Any]]:
    lines = [f"Error digest {day}. Suggested issues for {len(kinds)} new kinds."]
    if len(kinds) > max_new:
        groups: dict[str, list[int]] = {}
        for kind in kinds:
            group = " / ".join(kind["group"]) or kind["source"]
            counts = groups.setdefault(group, [0, 0])
            counts[0] += 1
            counts[1] += kind["count"]
        lines = [f"Error digest {day}. Normaliser miss: {len(kinds)} new kinds. Top {_TOP_KINDS} suggested issues.", ""]
        lines.extend(
            f"- {_cell(group)}: {distinct} new kinds, {occurrences} occurrences"
            for group, (distinct, occurrences) in sorted(groups.items())
        )
        kinds = kinds[:_TOP_KINDS]
        lines.append("")
    lines.extend(_kind_line(kind) for kind in kinds)
    return _safe_output("\n".join(lines)), {
        kind["sig"]: {key: kind[key] for key in ("first_seen", "source", "severity", "template")}
        for kind in kinds
    }


def _publish_proposal(body: str, sender: str, key: str, deadline: float) -> None:
    command = (
        [_command("LU_ERROR_DIGEST_FLEET_CLI")]
        if "LU_ERROR_DIGEST_FLEET_CLI" in os.environ
        else [sys.executable, "-I", "-m", "lu_machinery.fleet_comms"]
    )
    _send(
        [*command, "channel", "publish", "cto", "-", "--sender", sender,
         "--recipient", "claude-cto", "--kind", "proposal", "--idempotency-key", key],
        "Fleet publish", deadline, input=body.encode(),
    )


def _publish_run(day: date, directory: Path, sender: str, *, backfill: bool, max_new: int) -> None:
    cli = _command("LU_DRIVER_STATE_CLI")
    deadline = time.monotonic() + _JOURNAL_TIMEOUT_S
    ledger_path = directory / _LEDGER
    state_path = directory / _PUBLISH_STATE
    require_not_direct_task_store_path(ledger_path, label="error digest ledger")
    directory.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DigestError("publish: unavailable (another_digest_running)") from None

        ledger, state, ledger_error = {}, {}, ""
        try:
            ledger = _load_ledger(ledger_path)
            state = _load_publish_state(state_path)
        except DigestError as exc:
            ledger_error = str(exc)
        failure = DigestError(ledger_error) if ledger_error else None
        recovered_day = None
        if attempt := state.get("attempt"):
            matched_day = _attempt_day(attempt["key"])
            recovered_day = date.fromisoformat(matched_day)
            if all(ledger.get(sig, {}).get("proposal_day") == matched_day for sig in attempt["proposed"]):
                # The ledger replacement is atomic and committed: only the clear is left.
                state["attempt"] = None
                _write_state(state_path, _json_bytes(state), replace=True)
            else:
                failure = _deliver(state, state_path, ledger, ledger_path, deadline)
        kinds, report, count = _collect_for_publish(
            directory, day, backfill=backfill, ledger=ledger, ledger_error=ledger_error,
            state=state, recovered_day=recovered_day, cli=cli, sender=sender, deadline=deadline,
        )
        if not failure and kinds and not any(row["proposal_day"] == day.isoformat() for row in ledger.values()):
            body, proposed = _prepare_proposal(day, kinds, max_new)
            state["attempt"] = {"key": f"error-digest:{day}", "sender": sender, "body": body, "proposed": proposed}
            _write_state(state_path, _json_bytes(state), replace=True)
            failure = _deliver(state, state_path, ledger, ledger_path, deadline)
        banner = "\n".join(filter(None, (ledger_error, _pending_note(state))))
        headline, journal_body = _journal_summary(report, count, banner)
        _post_journal(cli, day, headline, journal_body, sender, deadline)
        if failure:
            raise failure
    finally:
        os.close(directory_fd)


def main(argv: list[str] | None = None) -> int:
    today = datetime.now(UTC).date()
    parser = argparse.ArgumentParser(
        description="Persist redacted journal and dispatcher counts for a completed UTC day.\n"
                    "Use --publish from the daily service to write journal notes and propose new kinds.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  .venv/bin/python -m lu_machinery.ops.error_digest\n"
               "  .venv/bin/python -m lu_machinery.ops.error_digest --publish --sender devops\n"
               "Outputs: error-digest-<UTC day>.json in the report directory. Publication uses "
               "LU_DRIVER_STATE_CLI, sends at most one Fleet proposal and updates the ledger after delivery.\n"
               "ERROR_DIGEST_MAX_NEW sets the overflow threshold (default: 20). "
               "LU_ERROR_DIGEST_FLEET_CLI can select an alternate Fleet CLI executable.\n"
               "Exit codes: 0 = report ready, 1 = preparation or publication failed, 2 = invalid usage.\n"
               "Related: lu_machinery.orchestration.issue_quality_report.",
    )
    parser.add_argument("--day", type=date.fromisoformat,
                        help="UTC day, e.g. 2025-02-01 (default: yesterday, with backfill when publishing)")
    parser.add_argument("--report-dir", type=Path,
                        help="directory path, for example ./reports (default: configured state reports)")
    parser.add_argument("--publish", action="store_true",
                        help="post digest notes and propose new kinds (default: persist only)")
    parser.add_argument("--sender", default="devops",
                        help="journal author and Fleet sender seat (default: devops)")
    args = parser.parse_args(argv)
    day = args.day or today - timedelta(days=1)
    if not date.min < day < today:
        parser.error("--day must be a completed, representable UTC day")
    try:
        directory = args.report_dir or state_paths.reports_dir()
        if args.publish:
            try:
                max_new = int(os.environ.get("ERROR_DIGEST_MAX_NEW", "20"))
                if max_new < 1:
                    raise ValueError
            except ValueError:
                raise DigestError("publish: refused (invalid_ERROR_DIGEST_MAX_NEW)") from None
            _publish_run(day, directory, args.sender, backfill=args.day is None, max_new=max_new)
        else:
            persisted_payload(day, directory)
    except DigestError as exc:
        print(f"error digest: {exc}", file=sys.stderr)
        return 1
    except (KeyError, OSError, TypeError, ValueError):
        print("error digest: report unavailable (state_or_io_error)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
